"""Unit tests for custom HTTP indicator source config model + redaction (P3-1)."""

import pytest
from pydantic import ValidationError

from cquant.datahub.pipelines.indicator_sources.http_config import (
    CustomHTTPConfig,
    ExtractionSpec,
    redact_config,
)


def _extraction(**kw):
    base = {
        "type": "jsonpath",
        "records_path": "$.data.records[*]",
        "field_map": {"trade_date": "date", "value": "close"},
    }
    base.update(kw)
    return base


def _cfg(**kw):
    base = {
        "url_template": "https://api.example.com/indicator?date={date}",
        "extraction": _extraction(),
    }
    base.update(kw)
    return CustomHTTPConfig(**base)


class TestSchemeValidation:
    def test_https_passes(self):
        assert _cfg().url_template.startswith("https://")

    def test_http_rejected_by_default(self):
        with pytest.raises(ValidationError):
            _cfg(url_template="http://api.example.com/indicator?date={date}")

    def test_http_with_allow_insecure_passes(self):
        cfg = _cfg(
            url_template="http://api.example.com/indicator?date={date}",
            allow_insecure_http=True,
        )
        assert cfg.allow_insecure_http is True

    def test_ftp_rejected(self):
        with pytest.raises(ValidationError):
            _cfg(
                url_template="ftp://example.com/file?date={date}",
                allow_insecure_http=True,
            )

    def test_missing_scheme_rejected(self):
        with pytest.raises(ValidationError):
            _cfg(url_template="api.example.com/indicator?date={date}")


class TestDatePlaceholder:
    def test_yyyymmdd_without_placeholder_rejected(self):
        with pytest.raises(ValidationError):
            _cfg(url_template="https://api.example.com/indicator")

    def test_placeholder_in_params_counts(self):
        cfg = _cfg(
            url_template="https://api.example.com/indicator",
            params={"d": "{date}"},
        )
        assert cfg.params["d"] == "{date}"

    def test_none_style_without_placeholder_passes(self):
        cfg = _cfg(
            url_template="https://api.example.com/indicator",
            date_param_style="none",
        )
        assert cfg.date_param_style == "none"

    def test_none_style_with_placeholder_rejected(self):
        with pytest.raises(ValidationError):
            _cfg(date_param_style="none")


class TestExtraction:
    def test_bad_jsonpath_rejected(self):
        with pytest.raises(ValidationError):
            _cfg(extraction=_extraction(records_path="$.data[["))

    def test_field_map_missing_trade_date_rejected(self):
        with pytest.raises(ValidationError):
            _cfg(extraction=_extraction(field_map={"value": "close"}))

    def test_field_map_missing_value_rejected(self):
        with pytest.raises(ValidationError):
            _cfg(extraction=_extraction(field_map={"trade_date": "date"}))


class TestClamp:
    def test_timeout_connect_clamped_to_60(self):
        assert _cfg(timeout_connect_sec=999).timeout_connect_sec == 60

    def test_timeout_total_clamped_to_120(self):
        assert _cfg(timeout_total_sec=9999).timeout_total_sec == 120

    def test_max_bytes_clamped_to_50mb(self):
        assert _cfg(max_bytes=999 * 1024 * 1024).max_bytes == 50 * 1024 * 1024

    def test_values_within_limits_untouched(self):
        cfg = _cfg(timeout_connect_sec=5, timeout_total_sec=20, max_bytes=1024)
        assert (cfg.timeout_connect_sec, cfg.timeout_total_sec, cfg.max_bytes) == (
            5,
            20,
            1024,
        )

    def test_zero_or_negative_clamped_to_floor_of_1(self):
        cfg = _cfg(timeout_connect_sec=0, timeout_total_sec=-5, max_bytes=0)
        assert (cfg.timeout_connect_sec, cfg.timeout_total_sec, cfg.max_bytes) == (
            1,
            1,
            1,
        )


class TestRedact:
    def test_env_var_reference_preserved(self):
        cfg = _cfg(headers={"Authorization": "${MY_TOKEN}"})
        out = redact_config(cfg)
        assert out["headers"]["Authorization"] == "${MY_TOKEN}"

    def test_bearer_plaintext_redacted(self):
        cfg = _cfg(headers={"Authorization": "Bearer abc123def456ghi789jkl012"})
        out = redact_config(cfg)
        assert out["headers"]["Authorization"] == "***redacted***"

    def test_sk_prefixed_secret_redacted(self):
        cfg = _cfg(headers={"X-Api-Key": "sk-a1b2c3d4e5f6g7h8i9j0k1l2m3"})
        out = redact_config(cfg)
        assert out["headers"]["X-Api-Key"] == "***redacted***"

    def test_long_random_string_redacted(self):
        cfg = _cfg(headers={"Token": "a8f3k2l9q4w7e5r1t0y6u3i8o5p2a7s4"})
        out = redact_config(cfg)
        assert out["headers"]["Token"] == "***redacted***"

    def test_plain_content_type_preserved(self):
        cfg = _cfg(headers={"Content-Type": "application/json"})
        out = redact_config(cfg)
        assert out["headers"]["Content-Type"] == "application/json"

    def test_non_header_fields_untouched(self):
        cfg = _cfg()
        out = redact_config(cfg)
        assert out["url_template"] == cfg.url_template
        assert out["extraction"]["records_path"] == cfg.extraction.records_path


class TestExtractionSpecDirect:
    def test_valid(self):
        spec = ExtractionSpec(**_extraction())
        assert spec.type == "jsonpath"

    def test_wrong_type_rejected(self):
        with pytest.raises(ValidationError):
            ExtractionSpec(**_extraction(type="xpath"))
