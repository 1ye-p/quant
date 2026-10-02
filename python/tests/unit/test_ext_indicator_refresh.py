"""统一外部指标刷新任务测试（P2-3）。

fake adapter 注入 —— 零真网、零真睡（inter_source_delay=0）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.external_indicator_importer import ImportConfig
from cquant.datahub.pipelines.indicator_sources.adapters import IndicatorFetchError
from cquant.datahub.pipelines.indicator_sources import refresh as refresh_mod
from cquant.datahub.pipelines.indicator_sources.refresh import (
    NoSourceReadyError,
    RefreshSummary,
    resolve_source,
    run_external_indicator_refresh,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

ANCHOR = date(2025, 6, 6)


class FakeAdapter:
    """记录 fetch 调用；按 key 返回预设帧或抛错。"""

    name = "fake"

    def __init__(self, name: str, frames: dict | None = None, error: Exception | None = None):
        self.name = name
        self.frames = frames or {}
        self.error = error
        self.calls: list[dict] = []

    def fetch(self, indicator_key: str, start: date, end: date) -> pl.DataFrame:
        self.calls.append({"key": indicator_key, "start": start, "end": end})
        if self.error is not None:
            raise self.error
        return self.frames.get(indicator_key, pl.DataFrame(
            {"trade_date": [start, end], "value": [1.0, 2.0]},
            schema={"trade_date": pl.Date, "value": pl.Float64},
        ))


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _seed_anchor(cat: Catalog, anchor: date = ANCHOR) -> None:
    cat.execute(
        "DELETE FROM silver_prices_1d WHERE asset_id = 'SZSE:000001' "
        f"AND trade_date = '{anchor}'"
    )
    cat.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, source) "
        f"VALUES ('SZSE:000001', '{anchor}', 10, 11, 9, 10, 1000, 'test')"
    )


def _seed_catalog_row(
    cat: Catalog, key: str, *, source_type: str = "builtin",
    pinned: str | None = None, enabled: bool = True,
    frequency: str = "daily", backfill_start: str | None = None,
    last_refresh_at: datetime | str | None = None,
) -> None:
    lr = f", '{last_refresh_at}'" if last_refresh_at else ", NULL"
    cat.execute(
        "INSERT INTO silver_external_indicator_catalog "
        "(indicator_key, display_name, source_type, source_name, pinned_source, "
        " frequency, backfill_start, enabled, last_refresh_at) "
        f"VALUES ('{key}', '{key}', '{source_type}', 'builtin', "
        f"{'NULL' if pinned is None else repr(pinned)}, '{frequency}', "
        f"{'NULL' if backfill_start is None else repr(backfill_start)}, "
        f"{enabled}{lr})"
    )


def _seed_indicator_data(cat: Catalog, key: str, source: str, dates: list[date]) -> None:
    for d in dates:
        cat.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            f"VALUES ('{source}', '{key}', '__MARKET__', '{d}', 1.0, '{d}')"
        )


# ── resolve_source 三档 ──────────────────────────────────────────────────────


def _entry(key: str, **kw) -> dict:
    base = {"indicator_key": key, "source_type": "builtin", "pinned_source": None}
    base.update(kw)
    return base


class TestResolveSource:
    def test_pinned_tushare_used_when_ready(self, monkeypatch):
        """pinned tushare + tushare_ready=True → tushare 优先（candidates 前）。"""
        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.builtin_registry.tushare_ready",
            lambda: True,
        )
        ak, ts = FakeAdapter("akshare"), FakeAdapter("tushare")
        ad = resolve_source(
            _entry("shibor_overnight", pinned_source="tushare"),
            {"akshare": ak, "tushare": ts},
        )
        assert ad is ts

    def test_pinned_tushare_not_ready_falls_back(self, monkeypatch):
        """pinned tushare 未就绪 → 回落 candidates（akshare）。"""
        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.builtin_registry.tushare_ready",
            lambda: False,
        )
        ak, ts = FakeAdapter("akshare"), FakeAdapter("tushare")
        ad = resolve_source(
            _entry("shibor_overnight", pinned_source="tushare"),
            {"akshare": ak, "tushare": ts},
        )
        assert ad is ak

    def test_tushare_not_ready_falls_back_akshare(self, monkeypatch):
        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.builtin_registry.tushare_ready",
            lambda: False,
        )
        # candidates 首选 akshare（恒 ready）→ akshare 被解析
        ak, ts = FakeAdapter("akshare"), FakeAdapter("tushare")
        ad = resolve_source(_entry("shibor_overnight"), {"akshare": ak, "tushare": ts})
        assert ad is ak
        # tushare-only candidates + not ready → NoSourceReadyError
        fake_reg = refresh_mod.BUILTIN_BY_KEY["shibor_overnight"]
        monkeypatch.setitem(
            refresh_mod.BUILTIN_BY_KEY, "shibor_overnight",
            type(fake_reg)(indicator_key="shibor_overnight", display_name="x",
                           unit=None, description="", candidates=("tushare",),
                           available_date_rule="B", frequency="daily"),
        )
        with pytest.raises(NoSourceReadyError):
            resolve_source(_entry("shibor_overnight"), {"akshare": ak, "tushare": ts})

    def test_no_source_ready_raises(self):
        with pytest.raises(NoSourceReadyError):
            resolve_source(_entry("north_net_buy"), {"tushare": FakeAdapter("tushare")})

    def test_pinned_only_for_builtin_rows(self):
        """csv 行带 pinned 仍走 candidates（carried finding）。"""
        ak = FakeAdapter("akshare")
        ad = resolve_source(
            _entry("north_net_buy", source_type="csv", pinned_source="tushare"),
            {"akshare": ak},
        )
        assert ad is ak  # pinned tushare 被忽略（且 adapters 里也没有），candidates 兜底


# ── 窗口 ─────────────────────────────────────────────────────────────────────


class TestWindow:
    def test_incremental_window_five_day_overlap(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy")
        last = date(2025, 6, 2)
        _seed_indicator_data(catalog, "north_net_buy", "akshare", [date(2025, 5, 30), last])
        ak = FakeAdapter("akshare")
        run_external_indicator_refresh(
            catalog, keys=["north_net_buy"], adapters={"akshare": ak},
            inter_source_delay=0,
        )
        assert ak.calls[0]["start"] == last - timedelta(days=4)  # max−4 → 5 日重叠
        assert ak.calls[0]["end"] == ANCHOR

    def test_backfill_window_from_backfill_start(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy", backfill_start="2024-01-01")
        _seed_indicator_data(catalog, "north_net_buy", "akshare", [date(2025, 6, 2)])
        ak = FakeAdapter("akshare")
        run_external_indicator_refresh(
            catalog, keys=["north_net_buy"], backfill=True,
            adapters={"akshare": ak}, inter_source_delay=0,
        )
        assert (ak.calls[0]["start"], ak.calls[0]["end"]) == (date(2024, 1, 1), ANCHOR)

    def test_no_data_incremental_becomes_backfill(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy", backfill_start="2024-06-01")
        ak = FakeAdapter("akshare")
        run_external_indicator_refresh(
            catalog, keys=["north_net_buy"],  # backfill=False 但无数据 → 回填
            adapters={"akshare": ak}, inter_source_delay=0,
        )
        assert ak.calls[0]["start"] == date(2024, 6, 1)


# ── 主流程 ───────────────────────────────────────────────────────────────────


class TestRunRefresh:
    def test_import_config_fields(self, catalog, monkeypatch):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy")
        captured: list[ImportConfig] = []

        def fake_import_frame(self, df, config):
            captured.append(config)
            from cquant.datahub.pipelines.external_indicator_importer import ImportReport
            return ImportReport(total=df.height, inserted=df.height)

        monkeypatch.setattr(
            "cquant.datahub.pipelines.external_indicator_importer."
            "ExternalIndicatorImporter.import_frame",
            fake_import_frame,
        )
        ak = FakeAdapter("akshare")
        summary = run_external_indicator_refresh(
            catalog, keys=["north_net_buy"], adapters={"akshare": ak},
            inter_source_delay=0,
        )
        assert summary.results[0].status == "ok"
        cfg = captured[0]
        assert cfg.source == "akshare"
        assert cfg.indicator_key == "north_net_buy"
        assert cfg.available_date_rule == "B"  # 目录行规则

    def test_rerun_idempotent_upsert(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy")
        fixed = pl.DataFrame(
            {"trade_date": [date(2025, 6, 1), date(2025, 6, 2)],
             "value": [1.0, 2.0]},
            schema={"trade_date": pl.Date, "value": pl.Float64},
        )
        ak = FakeAdapter("akshare", frames={"north_net_buy": fixed})
        for _ in range(2):
            run_external_indicator_refresh(
                catalog, keys=["north_net_buy"], adapters={"akshare": ak},
                inter_source_delay=0,
            )
        n = catalog.query(
            "SELECT COUNT(*) AS n FROM silver_external_indicators "
            "WHERE indicator_key = 'north_net_buy'"
        ).item(0, "n")
        assert n == 2  # 两次同帧，UPSERT 不重复

    def test_failure_isolated_per_indicator(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy")
        _seed_catalog_row(catalog, "cn_gov_yield_10y")
        # 同名双实例：bad 只对第一个 key 抛错不易表达 → 用 frames 按区分发
        class Mixed(FakeAdapter):
            def fetch(self, indicator_key, start, end):
                self.calls.append({"key": indicator_key, "start": start, "end": end})
                if indicator_key == "north_net_buy":
                    raise IndicatorFetchError("boom")
                return super().fetch(indicator_key, start, end)

        mixed = Mixed("akshare")
        summary = run_external_indicator_refresh(
            catalog, keys=["north_net_buy", "cn_gov_yield_10y"],
            adapters={"akshare": mixed}, inter_source_delay=0,
        )
        by_key = {r.indicator_key: r for r in summary.results}
        assert by_key["north_net_buy"].status == "error"
        assert "boom" in by_key["north_net_buy"].error
        assert by_key["cn_gov_yield_10y"].status == "ok"  # 其余继续

        row = catalog.query(
            "SELECT last_status, last_error FROM silver_external_indicator_catalog "
            "WHERE indicator_key = 'north_net_buy'"
        ).row(0, named=True)
        assert row["last_status"] == "error"
        assert row["last_error"]
        log = catalog.query(
            "SELECT status, error FROM silver_external_indicator_refresh_log "
            "WHERE indicator_key = 'north_net_buy'"
        ).row(0, named=True)
        assert log["status"] == "error" and "boom" in log["error"]
        ok_row = catalog.query(
            "SELECT last_status FROM silver_external_indicator_catalog "
            "WHERE indicator_key = 'cn_gov_yield_10y'"
        ).item(0, "last_status")
        assert ok_row == "ok"

    def test_refresh_log_source_name_is_resolved_source(self, catalog, monkeypatch):
        """akshare 回落（tushare 未就绪）时 refresh_log 留痕 akshare。"""
        monkeypatch.setattr(
            "cquant.datahub.pipelines.indicator_sources.builtin_registry.tushare_ready",
            lambda: False,
        )
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "shibor_overnight", pinned="tushare")
        ak = FakeAdapter("akshare")
        summary = run_external_indicator_refresh(
            catalog, keys=["shibor_overnight"],
            adapters={"akshare": ak, "tushare": FakeAdapter("tushare")},
            inter_source_delay=0,
        )
        # tushare 未就绪（无 token）→ pinned 失效回落 akshare
        assert summary.results[0].source == "akshare"
        log = catalog.query(
            "SELECT source_name, status, rows_fetched, rows_upserted, "
            "range_start, range_end, trigger FROM "
            "silver_external_indicator_refresh_log WHERE indicator_key = ?",
            ["shibor_overnight"],
        ).row(0, named=True)
        assert log["source_name"] == "akshare"
        assert log["status"] == "ok"
        assert log["rows_fetched"] == 2 and log["rows_upserted"] == 2
        assert log["range_end"] == ANCHOR
        assert log["trigger"] == "scheduled"
        cat_row = catalog.query(
            "SELECT source_name, last_status FROM silver_external_indicator_catalog "
            "WHERE indicator_key = 'shibor_overnight'"
        ).row(0, named=True)
        assert cat_row["source_name"] == "akshare" and cat_row["last_status"] == "ok"

    def test_non_builtin_rows_skipped(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "k_csv", source_type="csv")
        _seed_catalog_row(catalog, "k_http", source_type="custom_http")
        summary = run_external_indicator_refresh(
            catalog, keys=["k_csv", "k_http"],
            adapters={"akshare": FakeAdapter("akshare")}, inter_source_delay=0,
        )
        assert all(r.status == "skipped" for r in summary.results)

    def test_builtin_source_type_preserved(self, catalog):
        """import 写穿不得把 builtin 目录行改成 csv。"""
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy")
        run_external_indicator_refresh(
            catalog, keys=["north_net_buy"],
            adapters={"akshare": FakeAdapter("akshare")}, inter_source_delay=0,
        )
        st = catalog.query(
            "SELECT source_type FROM silver_external_indicator_catalog "
            "WHERE indicator_key = 'north_net_buy'"
        ).item(0, "source_type")
        assert st == "builtin"


# ── due 判定 ─────────────────────────────────────────────────────────────────


class TestDue:
    KEY = "north_net_buy"  # 真实 builtin key（registry 里有 candidates）

    def _due_for(self, catalog, frequency, last_refresh_at):
        _seed_anchor(catalog)
        _seed_catalog_row(
            catalog, self.KEY, frequency=frequency,
            last_refresh_at=last_refresh_at,
        )
        ak = FakeAdapter("akshare")
        run_external_indicator_refresh(
            catalog, adapters={"akshare": ak}, inter_source_delay=0
        )
        return [c["key"] for c in ak.calls]

    def test_daily_always_due(self, catalog):
        now = datetime.now(timezone.utc) - timedelta(days=1)
        assert self.KEY in self._due_for(catalog, "daily", now)

    def test_weekly_5d_not_due_7d_due(self, catalog):
        now = datetime.now(timezone.utc)
        assert self.KEY not in self._due_for(catalog, "weekly", now - timedelta(days=5))
        catalog.execute("DELETE FROM silver_external_indicator_catalog")
        assert self.KEY in self._due_for(catalog, "weekly", now - timedelta(days=7))

    def test_monthly_25d_due(self, catalog):
        now = datetime.now(timezone.utc)
        assert self.KEY not in self._due_for(catalog, "monthly", now - timedelta(days=24))
        catalog.execute("DELETE FROM silver_external_indicator_catalog")
        assert self.KEY in self._due_for(catalog, "monthly", now - timedelta(days=25))

    def test_disabled_not_enumerated(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy", enabled=False)
        ak = FakeAdapter("akshare")
        summary = run_external_indicator_refresh(
            catalog, adapters={"akshare": ak}, inter_source_delay=0
        )
        assert ak.calls == []
        assert summary.results == []

    def test_never_run_is_due(self, catalog):
        _seed_anchor(catalog)
        _seed_catalog_row(catalog, "north_net_buy", frequency="weekly")
        ak = FakeAdapter("akshare")
        run_external_indicator_refresh(
            catalog, adapters={"akshare": ak}, inter_source_delay=0
        )
        assert [c["key"] for c in ak.calls] == ["north_net_buy"]


# ── summary 形状 ─────────────────────────────────────────────────────────────


def test_summary_shape(catalog):
    _seed_anchor(catalog)
    _seed_catalog_row(catalog, "north_net_buy")
    summary = run_external_indicator_refresh(
        catalog, keys=["north_net_buy"],
        adapters={"akshare": FakeAdapter("akshare")}, inter_source_delay=0,
    )
    assert isinstance(summary, RefreshSummary)
    assert summary.trigger == "scheduled"
    assert summary.finished_at >= summary.started_at
    d = summary.as_dict()
    assert d["ok"] == 1 and d["error"] == 0
    r = d["results"][0]
    assert r["indicator_key"] == "north_net_buy"
    assert r["source"] == "akshare" and r["status"] == "ok"
    assert r["rows_fetched"] == 2 and r["rows_upserted"] == 2
