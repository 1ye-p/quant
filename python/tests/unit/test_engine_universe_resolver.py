"""B2: engine-side universe resolver anchoring + index exclusion, IC filter.

- resolver 锚定数据 max(trade_date)（非 CURRENT_DATE）：stale 数据下 sse/cyb
  预设仍解析出非空列表
- 默认 universe（"all"）不含 880xxx/881xxx 指数
- IC 计算样本无指数（factors 路由 _compute_ic）
"""

from __future__ import annotations

import duckdb
import polars as pl
import pytest

from cquant.api_server.routes import factors as factors_routes
from cquant.backtest_vector.universe import resolve_universe


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
        "CREATE TABLE silver_prices_1d ("
        "asset_id VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE,"
        "close DOUBLE, volume DOUBLE, amount DOUBLE)"
    )
    return stub


def _seed_stale_prices(cat: StubCatalog, trade_date: str) -> None:
    """6 只股票 + 2 个板块指数，数据停在 trade_date（相对当前为 stale）。"""
    assets = [
        "SSE:600000", "SSE:600001", "SSE:688001",   # SSE stocks
        "SZSE:000001", "SZSE:300001", "SZSE:300002",  # SZSE / CYB stocks
        "SSE:880004", "SSE:881193",                  # sector indices
    ]
    for i, a in enumerate(assets):
        cat.execute(
            "INSERT INTO silver_prices_1d VALUES (?, ?, 10, 11, 9, 10, 100, 1000)",
            [a, trade_date],
        )


class TestResolverAnchoring:
    def test_resolver_no_current_date_anchor(self, catalog) -> None:
        """数据 max_date = 90 天前；sse/cyb 预设仍解析出非空列表。"""
        from datetime import date, timedelta

        stale = (date.today() - timedelta(days=90)).isoformat()
        _seed_stale_prices(catalog, stale)

        sse = resolve_universe(catalog, "sse")
        cyb = resolve_universe(catalog, "cyb")
        assert sse, "stale 数据下 sse 预设应锚定 max(trade_date) 解析出非空列表"
        assert set(sse) == {"SSE:600000", "SSE:600001", "SSE:688001"}
        assert set(cyb) == {"SZSE:300001", "SZSE:300002"}

    def test_default_all_excludes_indices(self, catalog) -> None:
        """universe='all'：返回列表（非 None）且不含 880xxx 指数。"""
        from datetime import date, timedelta

        stale = (date.today() - timedelta(days=90)).isoformat()
        _seed_stale_prices(catalog, stale)

        assets = resolve_universe(catalog, "all")
        assert assets is not None, "默认 universe 应返回排除指数的资产列表，而非 None"
        assert "SSE:880004" not in assets
        assert "SSE:881193" not in assets
        assert all(":88" not in a.split(":", 1)[1][:2] for a in assets)
        assert set(assets) == {
            "SSE:600000", "SSE:600001", "SSE:688001",
            "SZSE:000001", "SZSE:300001", "SZSE:300002",
        }


class TestICExcludesIndices:
    def test_ic_computation_excludes_indices(self) -> None:
        """IC 计算样本无指数：指数极值因子值不应拉低股票间完美秩相关。"""
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

        assets = [f"SSE:60000{i}" for i in range(6)]
        dates = [f"2024-01-{d:02d}" for d in range(1, 9)]
        for i, a in enumerate(assets):
            for j, d in enumerate(dates):
                close = 10.0 + j * 0.1 * (i + 1)  # 收益随 i 递增
                stub.execute(
                    "INSERT INTO silver_prices_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [a, d, close * 0.99, close * 1.01, close * 0.98, close, 100.0, 1000.0],
                )
                stub.execute(
                    "INSERT INTO gold_factor_values VALUES ('fsv_t', 'f1', ?, ?, ?)",
                    [d, a, float(i)],
                )
        # 指数：因子值极高但收益递减（若进入样本会显著拉低 IC）
        for j, d in enumerate(dates):
            close = 100.0 - j * 1.0
            stub.execute(
                "INSERT INTO silver_prices_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["SSE:880004", d, close, close, close, close, 100.0, 1000.0],
            )
            stub.execute(
                "INSERT INTO gold_factor_values VALUES ('fsv_t', 'f1', ?, ?, ?)",
                [d, "SSE:880004", 100.0],
            )
        stub.execute(
            "INSERT INTO meta_factor_analytics "
            "(job_id, factor_name, feature_set_version, horizon_days, status, submitted_at) "
            "VALUES ('job_ic', 'f1', 'fsv_t', 1, 'pending', '2024-01-01')"
        )

        try:
            body = factors_routes.ICComputeBody(
                factor_name="f1", feature_set_version="fsv_t", horizon_days=1
            )
            factors_routes._compute_ic("job_ic", body, stub)

            status = stub.query(
                "SELECT status, summary_json FROM meta_factor_analytics WHERE job_id = 'job_ic'"
            ).to_dicts()[0]
            assert status["status"] == "done", status
            import json
            summary = json.loads(status["summary_json"])
            # 6 只股票 value/return 完美同序 → IC=1；若指数混入则 ≈0.36
            assert summary["mean_ic"] > 0.99, (
                f"IC 样本应排除指数，mean_ic={summary['mean_ic']}"
            )
        finally:
            factors_routes._ic_summary_table_ensured = False
