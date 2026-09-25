"""IC 汇总落表（gold_factor_ic_summary）+ DSL preview 修复测试。

- _compute_ic / _compute_ic_matrix 计算完成后 upsert gold_factor_ic_summary
- ic-leaderboard / ic-status 读真实表；空表返回显式 message（非静默空）
- DSL preview 样本锚 = max(trade_date) 回溯 30d（stale 数据有真实样本）
- Qlib 语法（$close / Ref(...)）→ 错误信息附带 DSL 小写提示
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import duckdb
import polars as pl
import pytest

from cquant.api_server.routes import factors as factors_routes


class StubCatalog:
    """最小 Catalog stub：duckdb 内存库，仅实现 execute/query。"""

    def __init__(self) -> None:
        self.con = duckdb.connect(":memory:")

    def execute(self, sql: str, params: list | None = None) -> None:
        self.con.execute(sql, params or [])

    def query(self, sql: str, params: list | None = None) -> pl.DataFrame:
        return self.con.execute(sql, params or []).pl()


@pytest.fixture()
def catalog() -> StubCatalog:
    stub = StubCatalog()
    stub.execute(
        "CREATE TABLE gold_factor_values ("
        "feature_set_version VARCHAR, factor_name VARCHAR, trade_date DATE,"
        "asset_id VARCHAR, value DOUBLE)"
    )
    stub.execute(
        "CREATE TABLE silver_prices_1d ("
        "asset_id VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE,"
        "close DOUBLE, volume DOUBLE, amount DOUBLE)"
    )
    stub.execute(
        "CREATE TABLE meta_factor_analytics ("
        "job_id VARCHAR, factor_name VARCHAR, feature_set_version VARCHAR,"
        "horizon_days INTEGER, status VARCHAR, submitted_at VARCHAR,"
        "series_json VARCHAR, summary_json VARCHAR, completed_at VARCHAR,"
        "error_text VARCHAR)"
    )
    yield stub
    factors_routes._ic_summary_table_ensured = False


def _seed_ic_data(catalog: StubCatalog) -> None:
    """6 资产 × 8 交易日：因子值与下期收益正相关（IC > 0）。"""
    assets = [f"SSE:60000{i}" for i in range(6)]
    dates = [f"2024-01-{d:02d}" for d in range(1, 9)]
    for i, a in enumerate(assets):
        base_close = 10.0 + i
        for j, d in enumerate(dates):
            close = base_close + j * 0.1 * (i + 1)
            catalog.execute(
                "INSERT INTO silver_prices_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [a, d, close * 0.99, close * 1.01, close * 0.98, close, 1000.0, 10000.0],
            )
            catalog.execute(
                "INSERT INTO gold_factor_values VALUES ('fsv_test', 'test_factor', ?, ?, ?)",
                [d, a, float(i)],
            )


def test_compute_ic_upserts_summary_and_leaderboard_nonempty(catalog: StubCatalog) -> None:
    _seed_ic_data(catalog)
    catalog.execute(
        "INSERT INTO meta_factor_analytics "
        "(job_id, factor_name, feature_set_version, horizon_days, status, submitted_at) "
        "VALUES ('job1', 'test_factor', 'fsv_test', 1, 'pending', '2024-01-01')"
    )

    body = factors_routes.ICComputeBody(
        factor_name="test_factor", feature_set_version="fsv_test", horizon_days=1
    )
    factors_routes._compute_ic("job1", body, catalog)

    # 汇总表写入
    rows = catalog.query("SELECT * FROM gold_factor_ic_summary").to_dicts()
    assert len(rows) == 1
    row = rows[0]
    assert row["factor_name"] == "test_factor"
    assert row["ic_mean"] is not None
    assert row["n"] > 0
    assert row["window_start"] is not None and row["window_end"] is not None

    # 排行榜非空
    lb = asyncio.run(factors_routes.ic_leaderboard(catalog=catalog, limit=5))
    assert lb["items"], "ic-leaderboard 应返回刚计算的因子"
    assert lb["items"][0]["factor_name"] == "test_factor"


def test_leaderboard_empty_returns_explicit_message(catalog: StubCatalog) -> None:
    lb = asyncio.run(factors_routes.ic_leaderboard(catalog=catalog, limit=5))
    assert lb["items"] == []
    assert "尚无 IC 计算" in lb.get("message", "")

    st = asyncio.run(factors_routes.factor_ic_status(catalog=catalog))
    assert st["items"] == []
    assert "尚无 IC 计算" in st.get("message", "")


def test_ic_matrix_upserts_per_factor(catalog: StubCatalog) -> None:
    _seed_ic_data(catalog)
    catalog.execute(
        "INSERT INTO meta_factor_analytics "
        "(job_id, factor_name, feature_set_version, horizon_days, status, submitted_at) "
        "VALUES ('job2', 'test_factor', 'fsv_test', 1, 'pending', '2024-01-01')"
    )
    body = factors_routes.ICMatrixBody(
        factor_names=["test_factor"], feature_set_version="fsv_test", horizon_days=1
    )
    factors_routes._compute_ic_matrix("job2", body, catalog)

    rows = catalog.query("SELECT factor_name FROM gold_factor_ic_summary").to_dicts()
    assert [r["factor_name"] for r in rows] == ["test_factor"]


def test_qlib_syntax_hint() -> None:
    assert "$" in factors_routes._qlib_syntax_hint("$close / Ref($close, 20)")
    assert "Ref" in factors_routes._qlib_syntax_hint("Ref(close, 20) / close")
    assert factors_routes._qlib_syntax_hint("ref(close, 20) / close") == ""


def test_preview_anchor_uses_max_trade_date_not_current_date(catalog: StubCatalog) -> None:
    """数据停在 2024（相对当前为 stale），preview 仍应有真实样本。"""
    _seed_ic_data(catalog)  # trade_date 全部为 2024-01
    body = factors_routes.CustomFactorPreviewBody(expression="close / open")
    resp = asyncio.run(factors_routes.preview_custom_factor(body=body, catalog=catalog))
    assert resp["valid"] is True
    assert resp["preview"], "stale 数据应基于 max(trade_date) 回溯 30 天取样，而非 CURRENT_DATE"


def test_preview_qlib_expression_gets_hint(catalog: StubCatalog) -> None:
    _seed_ic_data(catalog)
    body = factors_routes.CustomFactorPreviewBody(expression="$close / Ref($close, 20)")
    resp = asyncio.run(factors_routes.preview_custom_factor(body=body, catalog=catalog))
    assert resp["valid"] is False
    assert "Qlib" in (resp["error"] or "")


# ── Backlog #7: DDL moved to sql/duckdb/factors.sql ─────────────────────────

def test_ic_summary_ddl_loaded_by_catalog_initialize(tmp_path) -> None:
    """Catalog.initialize() (via _DDL_FILES) creates gold_factor_ic_summary."""
    from cquant.datahub.catalog import Catalog

    repo_root = Path(__file__).resolve().parents[3]
    cat = Catalog(db_path=tmp_path / "ddl.duckdb", repo_root=repo_root)
    cat.initialize()
    cols = cat.query(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE table_name = 'gold_factor_ic_summary' ORDER BY column_index"
    )["column_name"].to_list()
    assert cols == [
        "factor_name", "ic_mean", "icir", "ic_positive_pct", "n",
        "window_start", "window_end", "updated_at",
    ]
    cat.close()


def test_ensure_ic_summary_table_loads_from_sql_file(tmp_path) -> None:
    """Route-level ensure path reads sql/duckdb/factors.sql (equivalent DDL)."""
    class _PathCatalog(StubCatalog):
        """Stub + repo_root pointing at the real repo (like Catalog)."""

        def __init__(self, repo_root: Path) -> None:
            super().__init__()
            self.repo_root = repo_root

    repo_root = Path(__file__).resolve().parents[3]
    cat = _PathCatalog(repo_root)
    factors_routes._ensure_ic_summary_table(cat)
    cols = cat.query(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE table_name = 'gold_factor_ic_summary' ORDER BY column_index"
    )["column_name"].to_list()
    assert cols, "ensure path must create the table from sql/duckdb/factors.sql"
    assert "factor_name" in cols and "updated_at" in cols
    factors_routes._ic_summary_table_ensured = False
