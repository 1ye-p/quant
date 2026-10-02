"""Custom HTTP indicator source: pydantic config model + echo redaction (P3-1).

Field semantics follow design §6.1. Validator policy decisions (§6.2):

- URL scheme whitelist: ``https`` always allowed; ``http`` only when
  ``allow_insecure_http=True``; every other scheme (ftp/file/...) rejected.
- ``{date}`` placeholder: when ``date_param_style != "none"``, at least one
  ``{date}`` placeholder must appear in ``url_template`` or ``params`` values
  (otherwise the request cannot be dated → reject). When
  ``date_param_style == "none"``, a leftover ``{date}`` placeholder is
  **rejected** — it would be sent literally and signals a config mistake.
- Header values that look like plaintext secrets are *not* rejected at
  validation time (this is a research tool; users may legitimately embed
  tokens). They are masked by :func:`redact_config` on echo.
- Timeout / size fields are **clamped** to their upper bounds (lenient),
  not rejected: ``timeout_connect_sec`` ≤ 60, ``timeout_total_sec`` ≤ 120,
  ``max_bytes`` ≤ 50 MiB.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

logger = logging.getLogger(__name__)

_MAX_TIMEOUT_CONNECT_SEC = 60
_MAX_TIMEOUT_TOTAL_SEC = 120
_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB

_REDACTED = "***redacted***"

# Heuristics for "this plaintext header value looks like a secret".
_SECRET_PATTERNS = (
    re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^sk-[\w-]{8,}", re.IGNORECASE),
    re.compile(r"key-[0-9a-f]{16,}", re.IGNORECASE),
    re.compile(r"^[0-9a-zA-Z_\-]{24,}$"),  # long random-looking token
)
_SECRET_KEY_HINTS = re.compile(
    r"(authorization|api[-_]?key|secret|token|password|passwd)", re.IGNORECASE
)


class ExtractionSpec(BaseModel):
    """How to extract records from the HTTP response body.

    ``records_path`` is a JSONPath expression validated at save time via
    ``jsonpath_ng.parse`` — bad syntax raises ``ValidationError`` immediately
    rather than at first fetch.

    ``field_map`` maps output columns to record keys and must contain both
    ``trade_date`` and ``value``.
    """

    type: Literal["jsonpath"]
    records_path: str
    field_map: dict[str, str]

    @field_validator("records_path")
    @classmethod
    def _validate_jsonpath(cls, v: str) -> str:
        from jsonpath_ng import parse

        try:
            parse(v)
        except Exception as exc:  # jsonpath_ng raises LeiSyntaxError etc.
            raise ValueError(f"invalid JSONPath records_path: {v!r} ({exc})") from exc
        return v

    @field_validator("field_map")
    @classmethod
    def _validate_field_map(cls, v: dict[str, str]) -> dict[str, str]:
        missing = {"trade_date", "value"} - set(v)
        if missing:
            raise ValueError(
                f"field_map must contain 'trade_date' and 'value'; missing: {sorted(missing)}"
            )
        return v


class CustomHTTPConfig(BaseModel):
    """User-defined HTTP indicator source configuration.

    Clamp policy (lenient, see module docstring): numeric limits beyond the
    caps are silently clamped to the maximum, not rejected.
    """

    method: Literal["GET", "POST"] = "GET"
    url_template: str
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    date_param_style: Literal["yyyymmdd", "yyyy-mm-dd", "none"] = "yyyymmdd"
    extraction: ExtractionSpec
    allow_insecure_http: bool = False
    timeout_connect_sec: int = 10
    timeout_total_sec: int = 30
    max_bytes: int = 10485760  # 10 MiB default, hard cap 50 MiB

    @model_validator(mode="after")
    def _validate(self) -> "CustomHTTPConfig":
        self._validate_scheme()
        self._validate_date_placeholder()
        self._clamp_limits()
        return self

    def _validate_scheme(self) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(self.url_template)
        scheme = (parsed.scheme or "").lower()
        if scheme == "https":
            return
        if scheme == "http":
            if not self.allow_insecure_http:
                raise ValueError(
                    "http:// URLs require allow_insecure_http=True "
                    "(prefer https:// where possible)"
                )
            return
        raise ValueError(
            f"unsupported URL scheme {scheme!r}: only http/https are allowed"
        )

    def _validate_date_placeholder(self) -> None:
        has_placeholder = "{date}" in self.url_template or any(
            "{date}" in str(v) for v in self.params.values()
        )
        if self.date_param_style == "none":
            if has_placeholder:
                raise ValueError(
                    "date_param_style='none' but a '{date}' placeholder is present "
                    "in url_template/params — remove the placeholder or choose a "
                    "date_param_style"
                )
            return
        if not has_placeholder:
            raise ValueError(
                f"date_param_style={self.date_param_style!r} requires at least one "
                "'{date}' placeholder in url_template or params"
            )

    def _clamp_limits(self) -> None:
        # Lenient clamp policy (design §6.2): values above the cap are clamped
        # down; values below 1 (zero/negative) are clamped up to a sane floor
        # — a 0-second timeout or 0-byte budget would break every request.
        object.__setattr__(
            self,
            "timeout_connect_sec",
            max(1, min(self.timeout_connect_sec, _MAX_TIMEOUT_CONNECT_SEC)),
        )
        object.__setattr__(
            self,
            "timeout_total_sec",
            max(1, min(self.timeout_total_sec, _MAX_TIMEOUT_TOTAL_SEC)),
        )
        object.__setattr__(self, "max_bytes", max(1, min(self.max_bytes, _MAX_BYTES)))

    @field_validator("headers")
    @classmethod
    def _warn_on_plaintext_secrets(cls, v: dict[str, str]) -> dict[str, str]:
        # Validation layer only warns — research tool, users may deliberately
        # embed plaintext tokens. Redaction happens in redact_config().
        risky_keys = [
            key
            for key, value in v.items()
            if not (value.startswith("${") and value.endswith("}"))
            and (
                _SECRET_KEY_HINTS.search(key)
                or any(p.search(value) for p in _SECRET_PATTERNS)
            )
        ]
        if risky_keys:
            logger.warning(
                "headers %s look like plaintext secrets; consider using "
                "${ENV_VAR} references instead",
                risky_keys,
            )
        return v


def redact_config(cfg: CustomHTTPConfig) -> dict:
    """Serialize ``cfg`` for frontend echo with sensitive values masked.

    Policy:
    - ``${VAR}`` env references are kept verbatim (no secret material stored).
    - Plaintext header values matching secret heuristics (Bearer prefix,
      ``sk-`` prefix, key-like, or long random tokens) → ``***redacted***``.
    - Everything else passes through unchanged.

    All frontend GET echo of custom HTTP source configs must go through this.
    """
    data = cfg.model_dump()

    redacted_headers: dict[str, str] = {}
    for key, value in cfg.headers.items():
        if value.startswith("${") and value.endswith("}"):
            redacted_headers[key] = value
        elif _SECRET_KEY_HINTS.search(key) or any(
            p.search(value) for p in _SECRET_PATTERNS
        ):
            redacted_headers[key] = _REDACTED
        else:
            redacted_headers[key] = value
    data["headers"] = redacted_headers
    return data
