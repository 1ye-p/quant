"""Outbound guard layer for custom HTTP indicator sources (P3-2, security-critical).

This module is the **single egress gate** for user-configured HTTP indicator
sources. Every request goes through :func:`guarded_fetch`, which enforces:

- **SSRF protection** (:func:`validate_url`): scheme whitelist (https always;
  http only with ``allow_insecure_http``), DNS resolution of *all* A/AAAA
  records, and per-address rejection of private / loopback / link-local /
  reserved / unspecified / multicast ranges (this covers the cloud metadata
  endpoint 169.254.169.254 via link-local, and IPv4-mapped IPv6 bypasses via
  ``::ffff:10.x`` style addresses). Rejection raises
  ``GuardError(stage='ssrf_blocked')``; DNS failures raise
  ``GuardError(stage='dns')``.
- **Manual redirect handling**: httpx ``follow_redirects=False`` + at most 3
  hops, with the **redirect target re-validated per hop** (DNS included) —
  a public first hop cannot bounce us onto the internal network.
- **Size cap**: streaming read with a running byte counter; exceeding
  ``max_bytes`` aborts the transfer (``stage='oversized'``).
- **Timeouts**: connect + total deadlines via ``httpx.Timeout``
  (``stage='timeout'``).
- **Labeled failures** everywhere via :class:`GuardError` ``stage`` tags so
  the API layer can map errors to actionable user messages.

All tests for this module run with **zero real network**: DNS is stubbed via
``socket.getaddrinfo`` monkeypatching and HTTP via ``httpx.MockTransport``
(passed through the private ``_transport`` argument).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from datetime import date
from typing import Sequence
from urllib.parse import urljoin, urlsplit

import httpx
from jsonpath_ng import parse as jsonpath_parse

from .http_config import CustomHTTPConfig, ExtractionSpec

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 3
MAX_RENDERED_DATES = 400

# Credential headers stripped when a redirect crosses to a different host
# (mirrors httpx auto-follow behavior, which the manual redirect loop would
# otherwise bypass). Whitelist-style: everything else is forwarded.
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})

_STAGES = (
    "ssrf_blocked",
    "timeout",
    "oversized",
    "redirect_blocked",
    "json_parse",
    "path_miss",
    "dns",
    "date_render",
    "too_many_requests",
    "http_error",
    "request_failed",
)


class GuardError(Exception):
    """Every guard failure carries a machine-readable ``stage`` tag."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the address must never be fetched (non-public / special use).

    IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``) are unwrapped first so the
    mapped IPv4 form cannot sneak past the private-range checks.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254/16 incl. cloud metadata 169.254.169.254
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url(url: str, *, allow_insecure: bool = False) -> None:
    """Validate one URL end-to-end, or raise ``GuardError`` with a stage tag.

    Steps: scheme whitelist → hostname present → DNS resolve *all* A/AAAA
    records → reject if any resolved address is non-public. A hostname with
    multiple addresses where *any single* record is private is rejected
    (DNS-rebinding mitigation: we do not trust that the httpx connection will
    pick the public record).
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme == "https":
        pass
    elif scheme == "http":
        if not allow_insecure:
            raise GuardError(
                "ssrf_blocked",
                "http:// URLs are blocked by default; set allow_insecure_http "
                "(prefer https://)",
            )
    else:
        raise GuardError(
            "ssrf_blocked", f"unsupported URL scheme {scheme!r}: only http/https"
        )

    host = parts.hostname
    if not host:
        raise GuardError("ssrf_blocked", f"URL has no hostname: {url!r}")

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise GuardError("dns", f"DNS resolution failed for {host!r}: {exc}") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise GuardError("dns", f"no A/AAAA records returned for {host!r}")

    for addr in sorted(addresses):
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise GuardError(
                "dns", f"unparseable address {addr!r} for host {host!r}"
            ) from exc
        if _ip_is_blocked(ip):
            raise GuardError(
                "ssrf_blocked",
                f"host {host!r} resolves to non-public address {ip} — blocked",
            )


def _render_date(d: date, style: str) -> str:
    if style == "yyyymmdd":
        return d.strftime("%Y%m%d")
    if style == "yyyy-mm-dd":
        return d.isoformat()
    raise GuardError("date_render", f"unsupported date_param_style {style!r}")


def _render_template(text: str, d: date, style: str) -> str:
    return text.replace("{date}", _render_date(d, style))


def _extract_records(body: object, spec: ExtractionSpec) -> list[dict]:
    """Apply the ExtractionSpec JSONPath to a parsed body → list of row dicts."""
    try:
        matches = jsonpath_parse(spec.records_path).find(body)
    except Exception as exc:  # defensive: parse() validated at config time
        raise GuardError("json_parse", f"records_path failed on body: {exc}") from exc

    if not matches:
        raise GuardError(
            "path_miss", f"records_path {spec.records_path!r} matched no records"
        )

    rows: list[dict] = []
    for match in matches:
        record = match.value
        if not isinstance(record, dict):
            raise GuardError(
                "path_miss",
                f"records_path {spec.records_path!r} matched a non-object "
                f"({type(record).__name__}); expected objects to map fields from",
            )
        row: dict = {}
        for out_key, src_key in spec.field_map.items():
            if src_key not in record:
                raise GuardError(
                    "path_miss",
                    f"field {src_key!r} (→ {out_key!r}) missing in record "
                    f"with keys {sorted(record)}",
                )
            row[out_key] = record[src_key]
        rows.append(row)
    return rows


def _fetch_and_read(
    client: httpx.Client, cfg: CustomHTTPConfig, url: str, params: dict[str, str] | None
) -> object:
    """Single request → manual redirects (re-validated per hop) → size-capped
    streamed body → parsed JSON. Returns the parsed JSON object."""
    current_url = url
    current_params = params
    prev_netloc: str | None = None
    for hop in range(MAX_REDIRECTS + 1):
        validate_url(current_url, allow_insecure=cfg.allow_insecure_http)

        netloc = urlsplit(current_url).netloc
        if prev_netloc is not None and netloc != prev_netloc:
            # Cross-host redirect: never re-send credential headers to the
            # new origin (Authorization may carry a ${ENV_VAR}-rendered token).
            headers = {
                k: v
                for k, v in cfg.headers.items()
                if k.lower() not in SENSITIVE_HEADERS
            }
            logger.debug(
                "guarded_fetch cross-host redirect %s -> %s: stripped "
                "credential headers",
                prev_netloc,
                netloc,
            )
        else:
            headers = cfg.headers

        request = client.build_request(
            cfg.method,
            current_url,
            headers=headers,
            params=current_params,
        )
        try:
            response = client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise GuardError(
                "timeout", f"request to {current_url} timed out: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise GuardError(
                "request_failed", f"request to {current_url} failed: {exc}"
            ) from exc

        try:
            if response.has_redirect_location:
                if hop == MAX_REDIRECTS:
                    raise GuardError(
                        "redirect_blocked",
                        f"more than {MAX_REDIRECTS} redirects (last: {current_url})",
                    )
                location = response.headers.get("location", "")
                next_url = urljoin(str(response.request.url), location)
                try:
                    validate_url(
                        next_url, allow_insecure=cfg.allow_insecure_http
                    )
                except GuardError as exc:
                    raise GuardError(
                        "redirect_blocked",
                        f"redirect target {next_url} rejected: {exc}",
                    ) from exc
                logger.debug("guarded_fetch redirect hop %d: %s", hop + 1, next_url)
                prev_netloc = netloc
                current_url = next_url
                current_params = None  # query is carried in the Location URL
                continue

            if response.status_code >= 400:
                raise GuardError(
                    "http_error",
                    f"HTTP {response.status_code} from {current_url}",
                )

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > cfg.max_bytes:
                    raise GuardError(
                        "oversized",
                        f"response from {current_url} exceeded max_bytes="
                        f"{cfg.max_bytes} (>{total} bytes read), transfer aborted",
                    )
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            response.close()

        try:
            return json.loads(raw)
        except ValueError as exc:
            snippet = raw[:120].decode("utf-8", errors="replace")
            raise GuardError(
                "json_parse",
                f"response from {current_url} is not valid JSON "
                f"(starts with {snippet!r}): {exc}",
            ) from exc

    raise GuardError("redirect_blocked", "redirect budget exhausted")  # unreachable


def guarded_fetch(
    cfg: CustomHTTPConfig,
    dates: Sequence[date],
    *,
    _transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    """Fetch indicator rows through the outbound guard. Returns
    ``[{trade_date, value, ...}]`` rows extracted per :attr:`cfg.extraction`.

    ``date_param_style != 'none'``: one request per date ({date} rendered
    into url/params); more than ``MAX_RENDERED_DATES`` dates raises
    ``too_many_requests`` as an upper-bound protection. ``'none'``: a single
    request, records carry their own dates via ``field_map``.
    """
    if cfg.date_param_style != "none" and len(dates) > MAX_RENDERED_DATES:
        raise GuardError(
            "too_many_requests",
            f"{len(dates)} dates requested exceeds the per-fetch cap of "
            f"{MAX_RENDERED_DATES}",
        )

    timeout = httpx.Timeout(cfg.timeout_total_sec, connect=cfg.timeout_connect_sec)
    with httpx.Client(
        transport=_transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        if cfg.date_param_style == "none":
            body = _fetch_and_read(client, cfg, cfg.url_template, cfg.params or None)
            return _extract_records(body, cfg.extraction)

        rows: list[dict] = []
        for d in dates:
            rendered_url = _render_template(cfg.url_template, d, cfg.date_param_style)
            rendered_params = {
                k: _render_template(v, d, cfg.date_param_style)
                for k, v in cfg.params.items()
            }
            body = _fetch_and_read(
                client, cfg, rendered_url, rendered_params or None
            )
            rows.extend(_extract_records(body, cfg.extraction))
        return rows
