"""HTTPIndicatorAdapter + custom_http 刷新分支测试（P3-3）。

零真网：guarded_fetch 全程 stub（monkeypatch http_adapter.guarded_fetch）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.external_indicator_importer import ImportConfig
from cquant.datahub.pipelines.indicator_sources import refresh as refresh_mod
from cquant.datahub.pipelines.indicator_sources.adapters import IndicatorFetchError
from cquant.datahub.pipelines.indicator_sources.http_adapter import HTTPIndicatorAdapter
from cquant.datahub.pipelines.indicator_sources.http_config import CustomHTTPConfig
from cquant.datahub.pipelines.indicator_sources.http_guard import GuardError
from cquant.datahub.pipelines.indicator_sources.refresh import (
    run_external_indicator_refresh,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
ANCHOR = date(2025, 6, 6)  # 2025-06-06 周五

NONE_CFG = {
    "method": "GET",
    "url_template": "https://api.example.com/v1/history",
    "date_param_style": "none",
    "extraction": {
        "type": "jsonpath",
        "records_path": "$.data[*]",
        "field_map": {"trade_date": "d", "value": "v"},
    },
}

DAY_CFG = {
    "method": "GET",
    "url_template": "https://api.example.com/v1/day?date={date}",
    "date_param_style": "yyyymmdd",
    "extraction": {
        "type": "jsonpath",
        "records_path": "$.data[*]",
        "field_map": {"trade_date": "d", "value": "v"},
    },
}


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _seed_anchor(cat: Catalog, anchor: date = ANCHOR) -> None:
    cat.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, source) "
        "VALUES (?, ?, 10, 11, 9, 10, 1000, 'test')",
        ["SZSE:TEST", anchor],
    )


def _seed_custom_http_row(
    cat: Catalog, key: str, source_name: str, cfg: dict | None = NONE_CFG,
    backfill_start: str = "2025-05-01",
) -> None:
    cat.execute(
        "INSERT INTO silver_external_indicator_catalog "
        "(indicator_key, display_name, source_type, source_name, source_config, "
        " frequency, backfill_start, enabled) "
        "VALUES (?, ?, 'custom_http', ?, ?, 'daily', ?, TRUE)",
        [key, key, source_name, json.dumps(cfg) if cfg is not None else None,
         backfill_start],
    )


# ── 适配器单元（none 模式窗口过滤 / 逐日序列） ────────────────────────────────


class TestAdapterUnit:
    def test_none_mode_wide_return_filtered_to_window(self, monkeypatch):
        """宽返回（跨 60 天）→ 只留 [start, end] 窗口内行，升序输出。"""
        cfg = CustomHTTPConfig.model_validate(NONE_CFG)
        rows = [
            {"d": "20250501", "v": 1.0},
            {"d": "2025-05-20", "v": 2.0},
            {"d": "20250525", "v": 3.0},
            {"d": "2025-07-01", "v": 4.0},  # 窗口外（右）
        ]
        seen: list[list] = []

        def fake_guarded_fetch(c, dates):
            seen.append(list(dates))
            # 模拟 guard 的 field_map 产物：trade_date/value 列名
            return [{"trade_date": r["d"], "value": r["v"]} for r in rows]

        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.http_adapter.guarded_fetch",
            fake_guarded_fetch,
        )
        ad = HTTPIndicatorAdapter("custom_http:t", cfg)
        df = ad.fetch("k", date(2025, 5, 10), date(2025, 5, 31))
        assert seen == [[]]  # none 模式单请求、空日期序列
        assert df["trade_date"].to_list() == [date(2025, 5, 20), date(2025, 5, 25)]
        assert df.schema == {"trade_date": pl.Date, "value": pl.Float64}
        assert df["trade_date"].is_sorted()

    def test_none_mode_empty_window_raises_with_explanation(self, monkeypatch):
        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.http_adapter.guarded_fetch",
            lambda c, dates: [{"trade_date": "20250101", "value": 1.0}],
        )
        ad = HTTPIndicatorAdapter("custom_http:t", CustomHTTPConfig.model_validate(NONE_CFG))
        with pytest.raises(IndicatorFetchError, match="none fall inside"):
            ad.fetch("k", date(2025, 5, 1), date(2025, 5, 31))

    def test_day_mode_weekday_date_sequence(self, monkeypatch):
        """逐日模式：guarded_fetch 收到窗口内工作日序列（周末剔除）。"""
        cfg = CustomHTTPConfig.model_validate(DAY_CFG)
        seen: list[list] = []

        def fake_guarded_fetch(c, dates):
            seen.append(list(dates))
            return [{"trade_date": d.isoformat(), "value": 1.0} for d in dates]

        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.http_adapter.guarded_fetch",
            fake_guarded_fetch,
        )
        ad = HTTPIndicatorAdapter("custom_http:t", cfg)
        # 2025-06-02(一) .. 2025-06-08(日)：工作日 = 一~五
        ad.fetch("k", date(2025, 6, 2), date(2025, 6, 8))
        assert seen[0] == [
            date(2025, 6, 2), date(2025, 6, 3), date(2025, 6, 4),
            date(2025, 6, 5), date(2025, 6, 6),
        ]

    def test_guard_error_propagates_unchanged(self, monkeypatch):
        """GuardError 不吞不改写 stage —— 原样上抛。"""
        def boom(c, dates):
            raise GuardError("too_many_requests", "401 dates requested exceeds cap")

        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.http_adapter.guarded_fetch",
            boom,
        )
        ad = HTTPIndicatorAdapter("custom_http:t", CustomHTTPConfig.model_validate(DAY_CFG))
        with pytest.raises(GuardError) as ei:
            ad.fetch("k", date(2024, 1, 1), date(2025, 12, 31))
        assert ei.value.stage == "too_many_requests"


# ── 刷新全流程（custom_http 目录行） ─────────────────────────────────────────


class TestCustomHttpRefreshFlow:
    def test_full_flow_ok(self, catalog, monkeypatch):
        """none 模式：窗口正确 → ImportConfig 正确 → refresh_log/catalog 状态 ok。"""
        _seed_anchor(catalog)
        _seed_custom_http_row(catalog, "my_custom", "mysrc")
        captured_cfgs: list[ImportConfig] = []

        def fake_import_frame(self, df, config):
            captured_cfgs.append(config)
            from cquant.datahub.pipelines.external_indicator_importer import (
                ImportReport,
            )
            return ImportReport(total=df.height, inserted=df.height)

        monkeypatch.setattr(
            "cquant.datahub.pipelines.external_indicator_importer."
            "ExternalIndicatorImporter.import_frame",
            fake_import_frame,
        )

        def fake_guarded_fetch(c, dates):
            assert dates == []
            return [
                {"trade_date": "2025-05-15", "value": 1.5},
                {"trade_date": "20250601", "value": 2.5},
                {"trade_date": "2025-06-06", "value": 3.5},
            ]

        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.http_adapter.guarded_fetch",
            fake_guarded_fetch,
        )

        summary = run_external_indicator_refresh(
            catalog, keys=["my_custom"], inter_source_delay=0
        )
        r = summary.results[0]
        assert r.status == "ok"
        assert r.source == "custom_http:mysrc"
        assert r.range_start == date(2025, 5, 1) and r.range_end == ANCHOR
        assert r.rows_fetched == 3  # backfill_start 无既有数据 → 回填窗口，3 行全在窗内
        # ImportConfig：source = custom_http:mysrc，column_map 标准帧
        assert captured_cfgs[0].source == "custom_http:mysrc"
        assert captured_cfgs[0].indicator_key == "my_custom"
        assert captured_cfgs[0].column_map == {"trade_date": "trade_date", "value": "value"}
        # refresh_log + 目录状态
        log = catalog.query(
            "SELECT source_name, status, range_start, range_end FROM "
            "silver_external_indicator_refresh_log WHERE indicator_key = ?",
            ["my_custom"],
        ).row(0, named=True)
        assert log["source_name"] == "custom_http:mysrc" and log["status"] == "ok"
        assert (log["range_start"], log["range_end"]) == (
            date(2025, 5, 1), ANCHOR,
        )
        cat_row = catalog.query(
            "SELECT source_type, source_name, last_status FROM "
            "silver_external_indicator_catalog WHERE indicator_key = ?",
            ["my_custom"],
        ).row(0, named=True)
        assert cat_row["source_type"] == "custom_http"  # 不被写穿改类型
        assert cat_row["source_name"] == "custom_http:mysrc"
        assert cat_row["last_status"] == "ok"

    def test_incremental_window_for_custom_http(self, catalog, monkeypatch):
        """有既有数据 → 增量窗口 max−4..anchor（同 builtin 路径）。"""
        _seed_anchor(catalog)
        _seed_custom_http_row(catalog, "my_custom", "mysrc")
        last = date(2025, 6, 2)
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES (?, ?, '__MARKET__', ?, 1.0, ?)",
            ["custom_http:mysrc", "my_custom", last, last],
        )
        seen: list[list] = []

        def fake_guarded_fetch(c, dates):
            seen.append(list(dates))
            return [{"trade_date": "2025-06-06", "value": 9.0}]

        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.http_adapter.guarded_fetch",
            fake_guarded_fetch,
        )
        summary = run_external_indicator_refresh(
            catalog, keys=["my_custom"], inter_source_delay=0
        )
        assert summary.results[0].status == "ok"
        # none 模式单请求；窗口断言来自 result（start = max−4）
        assert summary.results[0].range_start == last - timedelta(days=4)

    def test_guard_error_becomes_indicator_error_not_fatal(self, catalog, monkeypatch):
        """GuardError(too_many_requests) 透传 → 该指标 error，其余继续。"""
        _seed_anchor(catalog)
        _seed_custom_http_row(catalog, "bad_src", "big")
        _seed_custom_http_row(catalog, "good_src", "small")

        state = {"n": 0}

        def fake_guarded_fetch(c, dates):
            state["n"] += 1
            if state["n"] == 1:
                raise GuardError(
                    "too_many_requests", "900 dates requested exceeds the cap"
                )
            return [{"trade_date": "2025-06-06", "value": 1.0}]

        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.http_adapter.guarded_fetch",
            fake_guarded_fetch,
        )
        summary = run_external_indicator_refresh(
            catalog, keys=["bad_src", "good_src"], inter_source_delay=0
        )
        by_key = {r.indicator_key: r for r in summary.results}
        assert by_key["bad_src"].status == "error"
        assert "too_many_requests" in by_key["bad_src"].error
        assert by_key["good_src"].status == "ok"
        assert catalog.query(
            "SELECT last_status FROM silver_external_indicator_catalog "
            "WHERE indicator_key = 'bad_src'"
        ).item(0, "last_status") == "error"

    def test_invalid_source_config_error_isolated(self, catalog):
        """source_config 非法（缺 extraction / 坏 jsonpath）→ config_invalid
        error 条目，不阻断其他指标。"""
        _seed_anchor(catalog)
        _seed_custom_http_row(catalog, "bad_cfg", "b", cfg={
            "url_template": "https://api.example.com/x",  # 缺 extraction
        })
        _seed_custom_http_row(catalog, "bad_path", "p", cfg={
            "url_template": "https://api.example.com/x",
            "extraction": {
                "type": "jsonpath", "records_path": "$[",  # 坏 jsonpath
                "field_map": {"trade_date": "d", "value": "v"},
            },
        })
        # 一个正常的 builtin 行证明流程未被阻断（fake akshare adapter）
        class _FakeAk:
            name = "akshare"

            def fetch(self, indicator_key, start, end):
                return pl.DataFrame(
                    {"trade_date": [start, end], "value": [1.0, 2.0]},
                    schema={"trade_date": pl.Date, "value": pl.Float64},
                )

        catalog.execute(
            "INSERT INTO silver_external_indicator_catalog "
            "(indicator_key, display_name, source_type, source_name, frequency, "
            " backfill_start, enabled) "
            "VALUES ('north_net_buy', 'x', 'builtin', 'builtin', 'daily', "
            " '2025-05-01', TRUE)"
        )
        summary = run_external_indicator_refresh(
            catalog, keys=["bad_cfg", "bad_path", "north_net_buy"],
            adapters={"akshare": _FakeAk()}, inter_source_delay=0,
        )
        by_key = {r.indicator_key: r for r in summary.results}
        assert by_key["bad_cfg"].status == "error"
        assert "config_invalid" in by_key["bad_cfg"].error
        assert by_key["bad_path"].status == "error"
        assert "config_invalid" in by_key["bad_path"].error
        assert by_key["north_net_buy"].status == "ok"  # 阻断隔离成立

    def test_missing_source_config_error(self, catalog):
        _seed_anchor(catalog)
        _seed_custom_http_row(catalog, "no_cfg", "n", cfg=None)
        summary = run_external_indicator_refresh(
            catalog, keys=["no_cfg"], inter_source_delay=0
        )
        assert summary.results[0].status == "error"
        assert "config_invalid" in summary.results[0].error
