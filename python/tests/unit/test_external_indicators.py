"""Tests for external indicators: DDL, CSV importer, PIT loader (T1/T2/T4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from cquant.datahub.catalog import Catalog
from cquant.datahub.external_loader import load_external_series
from cquant.datahub.pipelines.external_indicator_importer import (
    ExternalIndicatorImporter,
    ImportConfig,
    normalize_asset_id,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


@pytest.fixture()
def importer(catalog):
    return ExternalIndicatorImporter(catalog)


def _write_csv(tmp_path, rows: list[tuple], header="date,code,val") -> Path:
    p = tmp_path / "ext.csv"
    lines = [header] + [",".join(str(x) for x in r) for r in rows]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ── T1: DDL ──────────────────────────────────────────────────────────────────


class TestSilverExternalIndicatorsTable:
    def test_table_exists_after_initialize(self, catalog):
        n = catalog.query("SELECT COUNT(*) AS n FROM silver_external_indicators")["n"][0]
        assert n == 0

    def test_market_sentinel_and_stock_rows_same_table_upsert(self, catalog):
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES ('test', 'margin_balance', '__MARKET__', '2025-01-06', 100.0, '2025-01-07')"
        )
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES ('test', 'margin_balance', 'SSE:600000', '2025-01-06', 1.0, '2025-01-07')"
        )
        # UPSERT (ON CONFLICT) on same PK updates, does not duplicate
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES ('test', 'margin_balance', '__MARKET__', '2025-01-06', 200.0, '2025-01-07') "
            "ON CONFLICT (source, indicator_key, asset_id, trade_date) "
            "DO UPDATE SET value = excluded.value"
        )
        df = catalog.query("SELECT asset_id, value FROM silver_external_indicators ORDER BY asset_id")
        assert df.height == 2
        by_asset = dict(zip(df["asset_id"].to_list(), df["value"].to_list()))
        assert by_asset == {"SSE:600000": 1.0, "__MARKET__": 200.0}

    def test_default_asset_id_is_market_sentinel(self, catalog):
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, trade_date, available_date) "
            "VALUES ('t', 'k', '2025-01-06', '2025-01-07')"
        )
        aid = catalog.query("SELECT asset_id FROM silver_external_indicators")["asset_id"][0]
        assert aid == "__MARKET__"


# ── T2: normalization + importer ─────────────────────────────────────────────


class TestNormalizeAssetId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("600000.SH", "SSE:600000"),
            ("000001.SZ", "SZSE:000001"),
            ("sh600000", "SSE:600000"),
            ("sz000001", "SZSE:000001"),
            ("600000", "SSE:600000"),
            ("000001", "SZSE:000001"),
            ("300750", "SZSE:300750"),
            ("SSE:600000", "SSE:600000"),  # pass-through
        ],
    )
    def test_three_forms(self, raw, expected):
        assert normalize_asset_id(raw) == expected

    def test_unknown_format_passthrough_with_warning_path(self):
        assert normalize_asset_id("AAPL") == "AAPL"


class TestImporter:
    def test_column_mapping_and_market_sentinel(self, importer, catalog, tmp_path):
        p = _write_csv(tmp_path, [("2025-01-06", "", "123.5")], header="日期,代码,融资余额")
        report = importer.import_csv(
            p,
            ImportConfig(
                source="test",
                indicator_key="margin_balance",
                column_map={"日期": "trade_date", "融资余额": "value"},
            ),
        )
        assert report.skipped == 0
        df = catalog.query("SELECT asset_id, trade_date, value, available_date FROM silver_external_indicators")
        assert df.height == 1
        assert df["asset_id"][0] == "__MARKET__"
        assert df["value"][0] == 123.5

    def test_code_normalization_three_forms_in_csv(self, importer, catalog, tmp_path):
        p = _write_csv(
            tmp_path,
            [("2025-01-06", "600000.SH", 1.0), ("2025-01-06", "sh600000", 2.0), ("2025-01-07", "600000", 3.0)],
            header="date,code,val",
        )
        report = importer.import_csv(
            p,
            ImportConfig(
                source="test",
                indicator_key="margin_by_stock",
                column_map={"date": "trade_date", "val": "value", "code": "asset_id"},
                available_date_rule="A",
            ),
        )
        assert report.total == 3
        df = catalog.query("SELECT asset_id, trade_date FROM silver_external_indicators ORDER BY trade_date, asset_id")
        # 600000.SH and sh600000 same (asset, date) → deduped within file
        assert df.height == 2
        assert report.deduped == 1
        assert all(a == "SSE:600000" for a in df["asset_id"].to_list())

    def test_available_date_rule_a_vs_b(self, importer, catalog, tmp_path):
        p = _write_csv(tmp_path, [("2025-01-06", 1.0)], header="date,val")
        importer.import_csv(
            p,
            ImportConfig(
                source="t1",
                indicator_key="rule_a",
                column_map={"date": "trade_date", "val": "value"},
                available_date_rule="A",
            ),
        )
        importer.import_csv(
            p,
            ImportConfig(
                source="t1",
                indicator_key="rule_b",
                column_map={"date": "trade_date", "val": "value"},
                available_date_rule="B",
            ),
        )
        df = catalog.query(
            "SELECT indicator_key, available_date FROM silver_external_indicators ORDER BY indicator_key"
        )
        av = dict(zip(df["indicator_key"].to_list(), df["available_date"].to_list()))
        assert av["rule_a"] == date(2025, 1, 6)
        # 2025-01-06 is Monday; next trading day is 2025-01-07
        assert av["rule_b"] == date(2025, 1, 7)

    def test_non_trading_day_warning_non_blocking(self, importer, catalog, tmp_path):
        p = _write_csv(tmp_path, [("2025-01-04", 1.0)], header="date,val")  # Saturday
        report = importer.import_csv(
            p,
            ImportConfig(
                source="test",
                indicator_key="weekend_series",
                column_map={"date": "trade_date", "val": "value"},
            ),
        )
        assert report.skipped == 0
        assert any("非" in w and "交易日" in w for w in report.warnings)
        assert catalog.query("SELECT COUNT(*) AS n FROM silver_external_indicators")["n"][0] == 1

    def test_bad_rows_skipped_with_chinese_reasons_and_line_numbers(self, importer, catalog, tmp_path):
        p = _write_csv(
            tmp_path,
            [("2025-01-06", "1.5"), ("not-a-date", "bad1"), ("2025-01-07", "bad2")],
            header="date,val",
        )
        report = importer.import_csv(
            p,
            ImportConfig(
                source="test",
                indicator_key="bad_rows",
                column_map={"date": "trade_date", "val": "value"},
            ),
        )
        assert report.skipped == 2
        reasons = " | ".join(report.skipped_reasons)
        assert "第 3 行" in reasons and "trade_date" in reasons
        assert "第 4 行" in reasons and "value" in reasons
        assert catalog.query("SELECT COUNT(*) AS n FROM silver_external_indicators")["n"][0] == 1

    def test_invalid_indicator_key_rejected(self, importer, tmp_path):
        p = _write_csv(tmp_path, [("2025-01-06", 1.0)])
        with pytest.raises(ValueError, match="indicator_key"):
            importer.import_csv(
                p,
                ImportConfig(
                    source="t",
                    indicator_key="Bad-Key!",
                    column_map={"date": "trade_date", "val": "value"},
                ),
            )

    def test_missing_column_in_map_rejected(self, importer, tmp_path):
        p = _write_csv(tmp_path, [("2025-01-06", 1.0)], header="date,val")
        with pytest.raises(ValueError, match="不存在列"):
            importer.import_csv(
                p,
                ImportConfig(
                    source="t",
                    indicator_key="k",
                    column_map={"nope": "trade_date", "val": "value"},
                ),
            )

    def test_reimport_dedup_counts_existing_rows(self, importer, catalog, tmp_path):
        p = _write_csv(tmp_path, [("2025-01-06", 1.0), ("2025-01-07", 2.0)], header="date,val")
        cfg = ImportConfig(
            source="test",
            indicator_key="dup_series",
            column_map={"date": "trade_date", "val": "value"},
        )
        r1 = importer.import_csv(p, cfg)
        assert r1.inserted == 2
        r2 = importer.import_csv(p, cfg)
        assert r2.inserted == 0
        assert r2.deduped == 2
        assert catalog.query("SELECT COUNT(*) AS n FROM silver_external_indicators")["n"][0] == 2


# ── T4: PIT loader ───────────────────────────────────────────────────────────


class TestLoadExternalSeries:
    def _seed(self, catalog):
        catalog.executemany(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("t", "margin_balance", "__MARKET__", date(2025, 1, 6), 100.0, date(2025, 1, 7)),
                ("t", "margin_balance", "__MARKET__", date(2025, 1, 7), 101.0, date(2025, 1, 8)),
                ("t", "margin_balance", "__MARKET__", date(2025, 1, 8), 102.0, date(2025, 1, 9)),
            ],
        )

    def test_pit_rows_after_as_of_not_returned(self, catalog):
        self._seed(catalog)
        df = load_external_series(catalog, "margin_balance", as_of_date=date(2025, 1, 8))
        assert df["trade_date"].to_list() == [date(2025, 1, 6), date(2025, 1, 7)]
        assert df["value"].to_list() == [100.0, 101.0]

    def test_all_rows_hidden_before_first_available(self, catalog):
        self._seed(catalog)
        df = load_external_series(catalog, "margin_balance", as_of_date=date(2025, 1, 6))
        assert df.height == 0

    def test_boundary_equal_available_date_returned(self, catalog):
        self._seed(catalog)
        df = load_external_series(catalog, "margin_balance", as_of_date=date(2025, 1, 9))
        assert df.height == 3

    def test_unknown_indicator_empty(self, catalog):
        self._seed(catalog)
        df = load_external_series(catalog, "nope", as_of_date=date(2026, 1, 1))
        assert df.height == 0

    def test_cross_source_dedup_latest_updated_at_wins(self, catalog):
        """同 key 不同 source 同 trade_date → 1 行，取 updated_at 最新的值。"""
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date, updated_at) "
            "VALUES ('src_old', 'margin_balance', '__MARKET__', '2025-01-06', 111.0, '2025-01-07', "
            "TIMESTAMPTZ '2025-01-07 08:00:00')"
        )
        catalog.execute(
            "INSERT INTO silver_external_indicators "
            "(source, indicator_key, asset_id, trade_date, value, available_date, updated_at) "
            "VALUES ('src_new', 'margin_balance', '__MARKET__', '2025-01-06', 999.0, '2025-01-07', "
            "TIMESTAMPTZ '2025-06-01 08:00:00')"
        )
        df = load_external_series(catalog, "margin_balance", as_of_date=date(2025, 6, 2))
        assert df.height == 1
        assert df["value"].to_list() == [999.0]


# ── T5: empty cells never 500; bare 8x code warning ──────────────────────────


class TestEmptyCellsAndSectorWarning:
    def test_empty_date_cell_skipped_not_raised(self, importer, catalog, tmp_path):
        p = _write_csv(tmp_path, [("", "1.5"), ("2025-01-07", "2.5")], header="date,val")
        report = importer.import_csv(
            p,
            ImportConfig(
                source="test",
                indicator_key="empty_date",
                column_map={"date": "trade_date", "val": "value"},
            ),
        )
        assert report.skipped == 1
        assert any("第 2 行" in r and "为空" in r for r in report.skipped_reasons)
        n = catalog.query("SELECT COUNT(*) AS n FROM silver_external_indicators")["n"][0]
        assert n == 1

    def test_empty_value_cell_skipped(self, importer, catalog, tmp_path):
        p = _write_csv(tmp_path, [("2025-01-06", ""), ("2025-01-07", "2.5")], header="date,val")
        report = importer.import_csv(
            p,
            ImportConfig(
                source="test",
                indicator_key="empty_val",
                column_map={"date": "trade_date", "val": "value"},
            ),
        )
        assert report.skipped == 1
        assert any("第 2 行" in r and "value" in r for r in report.skipped_reasons)

    def test_bare_8x_code_warns_sector_index_but_normalizes_bse(
        self, importer, catalog, tmp_path
    ):
        p = _write_csv(tmp_path, [("2025-01-06", "881101", "1.0")], header="date,code,val")
        report = importer.import_csv(
            p,
            ImportConfig(
                source="test",
                indicator_key="sector_idx",
                column_map={"date": "trade_date", "val": "value", "code": "asset_id"},
            ),
        )
        assert report.skipped == 0  # 不阻断
        assert any("行业指数" in w and "SSE:881101" in w for w in report.warnings)
        aid = catalog.query("SELECT asset_id FROM silver_external_indicators")["asset_id"][0]
        assert aid == "BSE:881101"  # 仍按 BSE 归一


# ── T6: upload hardening (suffix whitelist, size cap, temp cleanup) ──────────


class TestUploadHardening:
    def _make_file(self, content: bytes, filename: str = "ext.csv"):
        import io

        from fastapi import UploadFile

        return UploadFile(file=io.BytesIO(content), filename=filename)

    def test_suffix_whitelist_rejects_non_csv(self):
        import asyncio

        from cquant.api_server.routes.datasets import _save_upload

        with pytest.raises(Exception) as ei:
            asyncio.run(_save_upload(self._make_file(b"a,b\n1,2", "evil.xlsx")))
        assert "不支持的文件类型" in str(ei.value)

    def test_size_cap_rejects_oversized(self, monkeypatch):
        import asyncio

        import cquant.api_server.routes.datasets as routes
        from fastapi import HTTPException

        monkeypatch.setattr(routes, "_MAX_UPLOAD_BYTES", 10)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes._save_upload(self._make_file(b"x" * 100)))
        assert ei.value.status_code == 413

    def test_import_cleans_temp_file(self, catalog, tmp_path):
        import asyncio
        import glob

        from cquant.api_server.routes import datasets as routes

        before = set(glob.glob("/tmp/ext_ind_*"))
        cfg = '{"source": "t", "indicator_key": "k", "column_map": {"date": "trade_date", "val": "value"}}'
        csv_bytes = b"date,val\n2025-01-06,1.5\n"
        report = asyncio.run(
            routes.import_external_indicators(
                catalog, file=self._make_file(csv_bytes), config=cfg
            )
        )
        assert report["inserted"] == 1
        after = set(glob.glob("/tmp/ext_ind_*"))
        assert after == before  # 无新增残留

    def test_preview_cleans_temp_file(self):
        import asyncio
        import glob

        from cquant.api_server.routes import datasets as routes

        before = set(glob.glob("/tmp/ext_ind_*"))
        out = asyncio.run(
            routes.preview_external_indicators_csv(file=self._make_file(b"date,val\n2025-01-06,1.5\n"))
        )
        assert out["total_rows"] == 1
        after = set(glob.glob("/tmp/ext_ind_*"))
        assert after == before
