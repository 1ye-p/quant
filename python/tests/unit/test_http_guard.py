"""Unit tests for the outbound guard layer (P3-2) — ZERO real network.

DNS is stubbed by monkeypatching ``socket.getaddrinfo``; HTTP is stubbed via
``httpx.MockTransport`` injected through ``guarded_fetch(_transport=...)``.
"""

from __future__ import annotations

import json
import socket
from datetime import date, timedelta

import httpx
import pytest

from cquant.datahub.pipelines.indicator_sources.http_config import (
    CustomHTTPConfig,
    ExtractionSpec,
)
from cquant.datahub.pipelines.indicator_sources.http_guard import (
    GuardError,
    guarded_fetch,
    validate_url,
)

# ---------------------------------------------------------------------------
# DNS stub
# ---------------------------------------------------------------------------

DNS: dict[str, list[str]] = {
    # public
    "public.example.com": ["93.184.216.34"],
    "public2.example.com": ["93.184.216.35"],
    "public6.example.com": ["2606:2800:220:1:248:1893:25c8:1946"],
    # private / special (by hostname)
    "private10.example.com": ["10.0.0.5"],
    "private192.example.com": ["192.168.1.7"],
    "private17216.example.com": ["172.16.0.1"],
    "boundary17232.example.com": ["172.32.0.1"],  # outside 172.16/12 → allowed
    "loopback.example.com": ["127.0.0.1"],
    "metadata.example.com": ["169.254.169.254"],
    "v6loop.example.com": ["::1"],
    "v6link.example.com": ["fe80::1"],
    "mapped.example.com": ["::ffff:10.1.2.3"],
    # multiple records, one private → must reject
    "multi-evil.example.com": ["93.184.216.34", "192.168.1.1"],
    # direct IP literals in URLs
    "127.0.0.1": ["127.0.0.1"],
    "10.1.2.3": ["10.1.2.3"],
    "172.16.1.1": ["172.16.1.1"],
    "172.31.255.255": ["172.31.255.255"],
    "172.32.0.1": ["172.32.0.1"],
    "192.168.0.1": ["192.168.0.1"],
    "169.254.169.254": ["169.254.169.254"],
    "8.8.8.8": ["8.8.8.8"],
    # IP-literal encodings (getaddrinfo resolves these forms to loopback)
    "2130706433": ["127.0.0.1"],
    "0x7f000001": ["127.0.0.1"],
    "::1": ["::1"],
}


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        ips = DNS.get(host)
        if ips is None:
            raise socket.gaierror(8, f"Name or service not known: {host}")
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, 0),
            )
            for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cfg(transport_url: str | None = None, **kw) -> CustomHTTPConfig:
    base = {
        "url_template": transport_url or "https://public.example.com/api?date={date}",
        "extraction": ExtractionSpec(
            type="jsonpath",
            records_path="$.data.records[*]",
            field_map={"trade_date": "date", "value": "close"},
        ),
    }
    base.update(kw)
    return CustomHTTPConfig(**base)


def _json_response(records: list[dict], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json={"data": {"records": records}}
    )


def _dates(n: int) -> list[date]:
    start = date(2026, 1, 1)
    return [start + timedelta(days=i) for i in range(n)]


# ---------------------------------------------------------------------------
# validate_url — SSRF
# ---------------------------------------------------------------------------


class TestValidateUrlSSRF:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.1.1",
            "172.31.255.255",  # last address inside 172.16/12
            "192.168.0.1",
            "169.254.169.254",
        ],
    )
    def test_private_ipv4_rejected(self, host):
        with pytest.raises(GuardError) as ei:
            validate_url(f"https://{host}/x")
        assert ei.value.stage == "ssrf_blocked"

    @pytest.mark.parametrize(
        "host",
        [
            "loopback.example.com",
            "private10.example.com",
            "private192.example.com",
            "private17216.example.com",
            "metadata.example.com",
            "v6loop.example.com",
            "v6link.example.com",
            "mapped.example.com",  # ::ffff:10.1.2.3
        ],
    )
    def test_private_hostnames_rejected(self, host):
        with pytest.raises(GuardError) as ei:
            validate_url(f"https://{host}/x")
        assert ei.value.stage == "ssrf_blocked"

    @pytest.mark.parametrize("host", ["172.32.0.1", "boundary17232.example.com", "8.8.8.8", "public6.example.com"])
    def test_public_addresses_allowed(self, host):
        validate_url(f"https://{host}/x")  # must not raise

    def test_http_scheme_rejected_by_default(self):
        with pytest.raises(GuardError) as ei:
            validate_url("http://public.example.com/x")
        assert ei.value.stage == "ssrf_blocked"

    def test_http_scheme_allowed_with_allow_insecure(self):
        validate_url("http://public.example.com/x", allow_insecure=True)

    def test_non_http_scheme_rejected(self):
        with pytest.raises(GuardError) as ei:
            validate_url("ftp://public.example.com/x", allow_insecure=True)
        assert ei.value.stage == "ssrf_blocked"

    def test_dns_failure_labeled(self):
        with pytest.raises(GuardError) as ei:
            validate_url("https://nonexistent.example.com/x")
        assert ei.value.stage == "dns"

    def test_multi_record_one_private_rejected(self):
        # DNS rebinding mitigation: ANY private record → reject
        with pytest.raises(GuardError) as ei:
            validate_url("https://multi-evil.example.com/x")
        assert ei.value.stage == "ssrf_blocked"

    @pytest.mark.parametrize(
        "url",
        [
            "http://2130706433/",  # decimal IPv4 encoding of 127.0.0.1
            "http://0x7f000001/",  # hex IPv4 encoding of 127.0.0.1
            "https://[::1]/",  # IPv6 loopback literal
        ],
    )
    def test_ip_encoding_variants_rejected(self, url):
        with pytest.raises(GuardError) as ei:
            validate_url(url, allow_insecure=True)
        assert ei.value.stage == "ssrf_blocked"


# ---------------------------------------------------------------------------
# guarded_fetch — fetch pipeline
# ---------------------------------------------------------------------------


class TestGuardedFetchHappyPath:
    def test_jsonpath_extraction_maps_fields(self):
        seen_params = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.append(dict(request.url.params))
            return _json_response([{"date": "20260101", "close": 123.5}])

        cfg = _cfg()
        rows = guarded_fetch(cfg, _dates(1), _transport=httpx.MockTransport(handler))
        assert rows == [{"trade_date": "20260101", "value": 123.5}]
        assert len(seen_params) == 1

    def test_yyyymmdd_render_format(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return _json_response([{"date": "x", "close": 1}])

        guarded_fetch(_cfg(), [date(2026, 1, 2)], _transport=httpx.MockTransport(handler))
        assert "date=20260102" in seen[0]

    def test_yyyy_mm_dd_render_format(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return _json_response([{"date": "x", "close": 1}])

        cfg = _cfg(date_param_style="yyyy-mm-dd")
        guarded_fetch(cfg, [date(2026, 1, 2)], _transport=httpx.MockTransport(handler))
        assert "date=2026-01-02" in seen[0]

    def test_date_rendered_in_params(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return _json_response([{"date": "x", "close": 1}])

        cfg = _cfg(
            url_template="https://public.example.com/api",
            params={"d": "{date}"},
        )
        guarded_fetch(cfg, [date(2026, 3, 5)], _transport=httpx.MockTransport(handler))
        assert seen[0]["d"] == "20260305"

    def test_one_request_per_date(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response([{"date": "x", "close": 1}])

        guarded_fetch(_cfg(), _dates(3), _transport=httpx.MockTransport(handler))
        assert len(calls) == 3

    def test_none_style_single_request(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response(
                [{"date": "20260101", "close": 1.0}, {"date": "20260102", "close": 2.0}]
            )

        cfg = _cfg(
            url_template="https://public.example.com/api/bulk", date_param_style="none"
        )
        rows = guarded_fetch(cfg, _dates(5), _transport=httpx.MockTransport(handler))
        assert len(calls) == 1
        assert [r["value"] for r in rows] == [1.0, 2.0]


class TestGuardedFetchRedirects:
    def test_redirect_each_hop_revalidated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "public.example.com"
            return httpx.Response(
                302, headers={"Location": "https://private10.example.com/steal"}
            )

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "redirect_blocked"
        assert "private10.example.com" in str(ei.value)

    def test_redirect_to_metadata_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"Location": "https://metadata.example.com/latest/meta-data"}
            )

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "redirect_blocked"

    def test_safe_redirect_chain_followed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api":
                return httpx.Response(
                    302, headers={"Location": "https://public2.example.com/final"}
                )
            assert request.url.host == "public2.example.com"
            return _json_response([{"date": "20260101", "close": 9.9}])

        rows = guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert rows[0]["value"] == 9.9

    def test_too_many_redirects_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"Location": "https://public2.example.com/loop"}
            )

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "redirect_blocked"

    def test_cross_host_redirect_strips_credential_headers(self):
        requests_by_host: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            requests_by_host[request.url.host] = request
            if request.url.host == "public.example.com":
                return httpx.Response(
                    302, headers={"Location": "https://public2.example.com/final"}
                )
            return _json_response([{"date": "20260101", "close": 1}])

        cfg = _cfg(
            headers={
                "Authorization": "Bearer ${TEST_TOKEN}",
                "Cookie": "session=abc",
                "X-Api-Key": "secret-key",
                "X-Custom": "keep-me",
            }
        )
        rows = guarded_fetch(cfg, _dates(1), _transport=httpx.MockTransport(handler))
        assert rows[0]["value"] == 1

        second = requests_by_host["public2.example.com"]
        assert "authorization" not in second.headers
        assert "cookie" not in second.headers
        assert "x-api-key" not in second.headers
        assert second.headers.get("x-custom") == "keep-me"

    def test_same_host_redirect_keeps_credential_headers(self):
        requests_by_path: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            requests_by_path[request.url.path] = request
            if request.url.path != "/final":
                return httpx.Response(
                    302, headers={"Location": "https://public.example.com/final"}
                )
            return _json_response([{"date": "20260101", "close": 2}])

        cfg = _cfg(headers={"Authorization": "Bearer ${TEST_TOKEN}"})
        rows = guarded_fetch(cfg, _dates(1), _transport=httpx.MockTransport(handler))
        assert rows[0]["value"] == 2
        assert requests_by_path["/final"].headers.get("authorization") is not None


class TestGuardedFetchSizeAndTimeout:
    def test_oversized_body_interrupted(self):
        chunk_size, n_chunks, consumed = 1024, 64, []

        def body_stream():
            for i in range(n_chunks):
                consumed.append(i)
                yield b"x" * chunk_size

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body_stream())

        cfg = _cfg(max_bytes=4 * 1024)  # body is 64 KiB
        with pytest.raises(GuardError) as ei:
            guarded_fetch(cfg, _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "oversized"
        # the stream was aborted early, not fully consumed
        assert len(consumed) < n_chunks

    def test_body_within_limit_passes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=json.dumps({"data": {"records": [{"date": "d", "close": 1}]}})
            )

        cfg = _cfg(max_bytes=1024 * 1024)
        rows = guarded_fetch(cfg, _dates(1), _transport=httpx.MockTransport(handler))
        assert rows[0]["value"] == 1

    def test_timeout_enforced(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("simulated connect timeout")

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "timeout"

    def test_read_timeout_labeled(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated read timeout")

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "timeout"

    def test_connection_error_labeled(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "request_failed"


class TestGuardedFetchParseAndPath:
    def test_non_json_body_labeled(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "json_parse"

    def test_records_path_miss_raises_labeled_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"other": []}})

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "path_miss"

    def test_missing_field_raises_labeled_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response([{"date": "20260101"}])  # no 'close'

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "path_miss"
        assert "close" in str(ei.value)

    def test_http_error_status_labeled(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"})

        with pytest.raises(GuardError) as ei:
            guarded_fetch(_cfg(), _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "http_error"


class TestGuardedFetchLimits:
    def test_too_many_dates_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response([{"date": "x", "close": 1}])

        with pytest.raises(GuardError) as ei:
            guarded_fetch(
                _cfg(), _dates(401), _transport=httpx.MockTransport(handler)
            )
        assert ei.value.stage == "too_many_requests"

    def test_400_dates_still_allowed(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _json_response([{"date": "x", "close": 1}])

        rows = guarded_fetch(
            _cfg(), _dates(400), _transport=httpx.MockTransport(handler)
        )
        assert len(rows) == 400

    def test_first_hop_ssrf_blocks_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("request must not be sent")

        cfg = _cfg(url_template="https://private10.example.com/api?date={date}")
        with pytest.raises(GuardError) as ei:
            guarded_fetch(cfg, _dates(1), _transport=httpx.MockTransport(handler))
        assert ei.value.stage == "ssrf_blocked"
