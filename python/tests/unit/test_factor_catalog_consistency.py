"""C3 因子名单一致性 + D3 自定义因子消费侧测试。

/available 必须 = 已物化 ∪ 可注册（BUILTIN_FACTORS）∪ 自定义；
/definitions 与 /available 同源；自定义因子可被物化管线消费。
"""

from __future__ import annotations

import asyncio
from datetime import date

import duckdb
import polars as pl
import pytest

from cquant.api_server.routes import factors as factors_routes
from cquant.factorlab.custom_factor_loader import load_custom_factors
from cquant.factorlab.factors import BUILTIN_FACTORS
from cquant.factorlab.factors.expression_factor import ExpressionFactor
from cquant.factorlab.factor import FactorContext


class StubCatalog:
    """最小 Catalog stub：duckdb 内存库，仅实现 execute/query/upsert。"""

    def __init__(self) -> None:
        self.con = duckdb.connect(":memory:")

    def execute(self, sql: str, params: list | None = None) -> None:
        self.con.execute(sql, params or [])

    def query(self, sql: str, params: list | None = None) -> pl.DataFrame:
        return self.con.execute(sql, params or []).pl()

    def upsert(self, table: str, cols: list[str], rows: list[tuple], keys: list[str]) -> None:
        # 简化实现：先删后插（测试用途足够）
        key_cols = [c for c in cols if c in keys]
        placeholders = ", ".join(["?"] * len(cols))
        for row in rows:
            if key_cols:
                where = " AND ".join(f"{k} = ?" for k in key_cols)
                self.con.execute(f"DELETE FROM {table} WHERE {where}", [row[cols.index(k)] for k in key_cols])
            self.con.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", list(row)
            )


@pytest.fixture()
def catalog() -> StubCatalog:
    stub = StubCatalog()
    stub.execute(
        "CREATE TABLE gold_factor_values ("
        "feature_set_version VARCHAR, factor_name VARCHAR, trade_date DATE,"
        "asset_id VARCHAR, value DOUBLE)"
    )
    # ret_20d 已物化
    stub.execute(
        "INSERT INTO gold_factor_values VALUES ('fsv_test', 'ret_20d', '2025-01-06', '000001.SZ', 0.05)"
    )
    yield stub
    factors_routes._invalidate_factors_cache()
    factors_routes._custom_factor_table_ensured = False


def _available(catalog: StubCatalog) -> dict:
    return asyncio.run(factors_routes.list_available_factors(catalog=catalog))


def _definitions(catalog: StubCatalog) -> dict:
    return asyncio.run(factors_routes.factor_definitions(catalog=catalog))


def _insert_custom(catalog: StubCatalog, name: str, expression: str) -> str:
    catalog.execute(
        "INSERT INTO meta_custom_factors (factor_id, name, expression, description) "
        "VALUES (?, ?, ?, '')",
        [f"cf_{name}", name, expression],
    )
    return f"cf_{name}"


# ── 1. 硬指标：ret_20d 必须出现在 /available ─────────────────────────────────

def test_ret_20d_in_available(catalog: StubCatalog) -> None:
    result = _available(catalog)
    names = {f["name"] for f in result["factors"]}
    assert "ret_20d" in names
    ret = next(f for f in result["factors"] if f["name"] == "ret_20d")
    assert ret["status"] == "materialized"


# ── 2. 所有 runnable 因子要么已物化、要么在注册表 ───────────────────────────

def test_available_all_runnable(catalog: StubCatalog) -> None:
    result = _available(catalog)
    builtin_names = {f.name for f in BUILTIN_FACTORS}
    materialized = set(
        catalog.query("SELECT DISTINCT factor_name FROM gold_factor_values")["factor_name"].to_list()
    )
    assert result["factors"], "runnable 列表不应为空"
    for item in result["factors"]:
        assert item["status"] in ("materialized", "ready")
        assert item["name"] in builtin_names or item["name"] in materialized or item["is_custom"], (
            f"因子 {item['name']} 既未物化也不在注册表，不应出现在 runnable 列表"
        )
    # reference 类不进默认列表
    for item in result.get("reference_factors", []):
        assert item["status"] == "reference"


# ── 3. /definitions 与 /available 同源 ──────────────────────────────────────

def test_definitions_same_source(catalog: StubCatalog) -> None:
    available = _available(catalog)
    definitions = _definitions(catalog)
    available_all = {f["name"] for f in available["factors"]} | {
        f["name"] for f in available.get("reference_factors", [])
    }
    def_names = {d["name"] for d in definitions["items"]}
    missing = def_names - available_all
    assert not missing, f"/definitions 中有名字不在 /available 全集: {sorted(missing)[:10]}"


# ── 4. 自定义因子合并进 /available + CRUD cache 失效 ─────────────────────────

def test_custom_factor_merged_and_cache_invalidation(catalog: StubCatalog) -> None:
    factors_routes._invalidate_factors_cache()
    result = _available(catalog)
    assert not any(f["name"] == "my_test_alpha" for f in result["factors"])

    # 通过 CRUD 端点创建（含 cache 失效）
    body = factors_routes.CustomFactorCreateBody(
        name="my_test_alpha", expression="close / ma(close, 5)", description=""
    )
    asyncio.run(factors_routes.create_custom_factor(body=body, catalog=catalog))

    result = _available(catalog)
    item = next(f for f in result["factors"] if f["name"] == "my_test_alpha")
    assert item["is_custom"] is True
    assert item["status"] == "ready"
    assert item["category"] == "自定义因子"

    # 删除后 cache 失效，第二次请求不再包含
    factor_id = catalog.query("SELECT factor_id FROM meta_custom_factors WHERE name = 'my_test_alpha'")["factor_id"][0]
    asyncio.run(factors_routes.delete_custom_factor(factor_id=factor_id, catalog=catalog))
    result = _available(catalog)
    assert not any(f["name"] == "my_test_alpha" for f in result["factors"])


# ── 5. 端到端：custom 定义 → loader → ExpressionFactor 可 compute ───────────

def test_custom_factor_end_to_end(catalog: StubCatalog) -> None:
    factors_routes._ensure_custom_factor_table(catalog)
    _insert_custom(catalog, "my_mom_2d", "close / close.shift(2) - 1")

    factors = load_custom_factors(catalog)
    assert len(factors) == 1
    f = factors[0]
    assert isinstance(f, ExpressionFactor)
    assert f.name == "my_mom_2d"

    frame = pl.DataFrame({
        "asset_id": ["A"] * 4,
        "trade_date": [date(2025, 1, d) for d in range(2, 6)],
        "close": [10.0, 11.0, 12.0, 13.0],
    })
    ctx = FactorContext(as_of_date=date(2025, 1, 5))
    series = f.compute(frame, ctx)
    assert series.name == "my_mom_2d"
    assert len(series) == len(frame)
    # 第3行: 12/10 - 1 = 0.2
    assert series[2] == pytest.approx(0.2)

    # loader 对无表/坏库容错
    empty_stub = StubCatalog()
    empty_stub.con.execute("DROP TABLE IF EXISTS meta_custom_factors")  # 无表
    assert load_custom_factors(empty_stub) == [] or all(
        isinstance(x, ExpressionFactor) for x in load_custom_factors(empty_stub)
    )
