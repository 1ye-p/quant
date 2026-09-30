"""外部指标目录两张表（catalog + refresh_log）DDL 建表测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

CATALOG_COLUMNS = {
    "indicator_key", "display_name", "unit", "description", "source_type",
    "source_name", "pinned_source", "source_config", "available_date_rule",
    "frequency", "backfill_start", "enabled", "last_refresh_at",
    "last_status", "last_error", "updated_at",
}

REFRESH_LOG_COLUMNS = {
    "run_id", "indicator_key", "source_name", "started_at", "finished_at",
    "status", "trigger", "range_start", "range_end", "rows_fetched",
    "rows_upserted", "error",
}


def _make_catalog(tmp_path: Path) -> Catalog:
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _table_columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        [table],
    ).fetchall()
    return {r[0] for r in rows}


def test_catalog_tables_created_on_initialize(tmp_path):
    cat = _make_catalog(tmp_path)
    conn = cat._get_conn()

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "silver_external_indicator_catalog" in tables
    assert "silver_external_indicator_refresh_log" in tables

    assert _table_columns(conn, "silver_external_indicator_catalog") == CATALOG_COLUMNS
    assert _table_columns(conn, "silver_external_indicator_refresh_log") == REFRESH_LOG_COLUMNS


def test_ddl_idempotent(tmp_path):
    cat = _make_catalog(tmp_path)
    # 再跑一遍 initialize()，IF NOT EXISTS 语义下不应报错
    cat.initialize()
    conn = cat._get_conn()
    assert _table_columns(conn, "silver_external_indicator_catalog") == CATALOG_COLUMNS
