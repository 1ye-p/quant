"""A3-2 一致性测试：IC 路由收敛 FactorEvaluator 单一实现。

基线（frozen reference）= 收敛前 ``_compute_ic`` 内联数学的逐行快照。
收敛后路由所有 IC 计算委托 ``FactorEvaluator``（路由侧只保留参数适配 +
gold_factor_ic_summary upsert + 响应组装）。

断言口径：
- 均匀面板（每日截面 == 全期 unique）：所有指标（含 turnover/net_ic）逐位一致
- 破损面板：仅 ``factor_turnover``（A3-1 已知口径修正：分母 全期unique → 当日截面）
  及连带的 ``net_ic`` 允许差异，且新值必须等于独立重算的"当日截面分母"值
- 路由门槛（IC 截面>=5、分组>=10）收敛后保持不变（evaluator 默认 3/5，
  门槛在路由侧数据适配层实现）
- 已声明修复：``ic_ttest`` / ``ic_half_life`` 收敛前因类调用实例方法 TypeError
  被静默吞掉（summary 永远缺这两个键）；收敛后恢复输出（新增键，前端可选消费）

若出现上述白名单之外的 diff != 0：BLOCKED 上报，不许改断言迁就。
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import date, timedelta

import duckdb
import numpy as np
import polars as pl
import pytest

from cquant.api_server.routes import factors as factors_routes
from cquant.factorlab.evaluation import FactorEvaluator


# ---------------------------------------------------------------------------
# Frozen reference — 收敛前 _compute_ic 内联数学快照（不许随实现改动）
# ---------------------------------------------------------------------------

def _reference_ic(merged: pl.DataFrame, ret_name: str) -> tuple[list[dict], dict]:
    import numpy as np
    from collections import defaultdict

    series = []
    for dt, group in merged.group_by("trade_date"):
        if group.height < 5:
            continue
        f_rank = group["value"].rank().to_numpy()
        r_rank = group[ret_name].rank().to_numpy()
        ic = float(np.corrcoef(f_rank, r_rank)[0, 1]) if len(f_rank) > 1 else 0.0
        series.append({"trade_date": str(dt[0]), "ic": round(ic, 6)})

    series.sort(key=lambda x: x["trade_date"])
    ic_values = [s["ic"] for s in series]
    summary = {
        "mean_ic": round(float(np.mean(ic_values)), 6) if ic_values else 0.0,
        "ir": round(float(np.mean(ic_values) / (np.std(ic_values) + 1e-12)), 4) if ic_values else 0.0,
        "hit_rate": round(float(sum(1 for v in ic_values if v > 0) / max(len(ic_values), 1)), 4),
        "observations": len(ic_values),
    }

    sorted_dates = sorted(merged["trade_date"].unique().to_list())
    date_map: dict = {dt[0]: grp for dt, grp in merged.group_by("trade_date")}
    rank_ic_decay = []
    for lag in range(1, 11):
        decay_ics = []
        for i in range(len(sorted_dates) - lag):
            date_t = sorted_dates[i]
            date_tlag = sorted_dates[i + lag]
            factor_t = date_map[date_t].select(["asset_id", "value"])
            ret_tlag = date_map.get(date_tlag, pl.DataFrame()).select(["asset_id", ret_name])
            joined = factor_t.join(ret_tlag, on="asset_id", how="inner")
            if joined.height < 5:
                continue
            f_arr = joined["value"].rank().to_numpy()
            r_arr = joined[ret_name].rank().to_numpy()
            ic_val = float(np.corrcoef(f_arr, r_arr)[0, 1])
            if not np.isnan(ic_val):
                decay_ics.append(ic_val)
        rank_ic_decay.append({
            "lag": lag,
            "ic": round(float(np.mean(decay_ics)), 6) if decay_ics else 0.0,
        })

    q_buckets: dict[int, list[float]] = defaultdict(list)
    for dt in sorted_dates:
        group = date_map[dt]
        if group.height < 10:
            continue
        sorted_g = group.sort("value")
        n = len(sorted_g)
        q_size = n // 5
        for q in range(5):
            start_idx = q * q_size
            end_idx = (q + 1) * q_size if q < 4 else n
            sliced = sorted_g.slice(start_idx, end_idx - start_idx)
            _m = sliced[ret_name].mean()
            mean_ret = float(_m) if _m is not None else 0.0
            q_buckets[q + 1].append(mean_ret)
    quantile_returns = [
        {"quantile": q, "mean_return": round(float(np.mean(vals)), 6)}
        for q, vals in sorted(q_buckets.items())
    ]

    top_n_assets = max(1, int(0.2 * merged["asset_id"].n_unique()))
    turnovers: list[float] = []
    prev_top: set[str] = set()
    for dt in sorted_dates:
        today_top = set(
            date_map[dt]
            .sort("value", descending=True)
            .head(top_n_assets)["asset_id"]
            .to_list()
        )
        if prev_top:
            overlap = len(today_top & prev_top)
            turnovers.append(1.0 - overlap / max(len(today_top), 1))
        prev_top = today_top
    factor_turnover = round(float(np.mean(turnovers)), 4) if turnovers else 0.0

    summary["rank_ic_decay"] = rank_ic_decay
    summary["quantile_returns"] = quantile_returns
    summary["factor_turnover"] = factor_turnover
    net_cost_rate = 0.003
    summary["net_ic"] = round(
        float(summary["mean_ic"]) - net_cost_rate * factor_turnover, 6
    )
    return series, summary


def _daily_denominator_turnover(merged: pl.DataFrame) -> float:
    """独立重算：Top20%、分母=当日截面（A3-1 evaluator 口径）。"""
    sorted_dates = sorted(merged["trade_date"].unique().to_list())
    prev_top: set[str] | None = None
    turnovers: list[float] = []
    for d in sorted_dates:
        day = merged.filter(pl.col("trade_date") == d)
        n_daily = len(day)
        today = day.sort("value", descending=True).head(max(1, int(0.2 * n_daily)))
        top = set(today["asset_id"].to_list())
        if prev_top is not None and top:
            turnovers.append(1.0 - len(top & prev_top) / len(top))
        prev_top = top
    return round(float(np.mean(turnovers)), 4) if turnovers else 0.0


# ---------------------------------------------------------------------------
# Fixtures：直接构造 merged 面板（factor + 前瞻收益同帧）
# ---------------------------------------------------------------------------

def _make_panel(
    n_assets: int = 20,
    n_dates: int = 12,
    seed: int = 7,
    ic_sign: float = 1.0,
    noise: float = 0.3,
    missing: dict[int, list[int]] | None = None,
) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    assets = [f"SSE:{600000 + i}" for i in range(n_assets)]
    dates = [date(2024, 1, 1) + timedelta(days=j) for j in range(n_dates)]
    rows = []
    for j, d in enumerate(dates):
        present = [
            a for i, a in enumerate(assets)
            if missing is None or i not in missing.get(j, [])
        ]
        fwd_ret = rng.normal(0, 0.02, len(present))
        factor = ic_sign * fwd_ret + rng.normal(0, noise, len(present))
        for a, r, v in zip(present, fwd_ret, factor):
            rows.append({"trade_date": d, "asset_id": a, "value": float(v), "fwd_ret": float(r)})
    return pl.DataFrame(rows).sort(["trade_date", "asset_id"])


UNIFORM_PANELS = [
    pytest.param(7, 1.0, 0.3, id="pos-ic"),
    pytest.param(11, -1.0, 0.3, id="neg-ic"),
    pytest.param(13, 0.0, 1.0, id="noise-only"),
]


# ---------------------------------------------------------------------------
# 1) 均匀面板：逐位一致（含 turnover / net_ic）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed,ic_sign,noise", UNIFORM_PANELS)
def test_uniform_panel_exact_match(seed, ic_sign, noise) -> None:
    merged = _make_panel(seed=seed, ic_sign=ic_sign, noise=noise)
    ref_series, ref_summary = _reference_ic(merged, "fwd_ret")
    new_series, new_summary = factors_routes._evaluate_ic_metrics(merged, "fwd_ret")

    assert new_series == ref_series
    for key in (
        "mean_ic", "ir", "hit_rate", "observations",
        "rank_ic_decay", "quantile_returns", "factor_turnover", "net_ic",
    ):
        assert new_summary[key] == ref_summary[key], f"uniform panel diff in {key}"


# ---------------------------------------------------------------------------
# 2) 破损面板：白名单外逐位一致；turnover 差异必须等于当日截面口径
# ---------------------------------------------------------------------------

def test_ragged_panel_whitelisted_turnover_only() -> None:
    missing = {
        3: list(range(12, 20)),  # 8 资产：IC 保留、分组门槛剔除、turnover 分母变
        5: list(range(4, 20)),   # 4 资产：IC/分组全剔除（<5）
        8: list(range(15, 20)),  # 15 资产：turnover 分母 3 vs 全局 4
    }
    merged = _make_panel(seed=42, missing=missing)
    assert merged["asset_id"].n_unique() == 20  # 全期 unique ≠ 任一单日截面

    ref_series, ref_summary = _reference_ic(merged, "fwd_ret")
    new_series, new_summary = factors_routes._evaluate_ic_metrics(merged, "fwd_ret")

    # 白名单外：逐位一致
    assert new_series == ref_series
    for key in ("mean_ic", "ir", "hit_rate", "observations", "rank_ic_decay", "quantile_returns"):
        assert new_summary[key] == ref_summary[key], f"ragged panel diff in {key}"

    # 白名单内：turnover 允许差异，但必须精确等于当日截面分母口径
    expected = _daily_denominator_turnover(merged)
    assert new_summary["factor_turnover"] == expected
    # 本 fixture 中全期 unique 分母与当日截面分母确实不同（差异被真实触发）
    assert ref_summary["factor_turnover"] != new_summary["factor_turnover"]
    # net_ic 随 turnover 连动，公式不变：mean_ic − 0.003 × turnover
    assert new_summary["net_ic"] == round(
        new_summary["mean_ic"] - 0.003 * new_summary["factor_turnover"], 6
    )


# ---------------------------------------------------------------------------
# 3) 路由门槛保持（evaluator 默认 3/5，门槛在路由侧数据适配）
# ---------------------------------------------------------------------------

def test_route_gates_preserved() -> None:
    missing = {
        2: list(range(2, 20)),   # 2 资产：evaluator 也会跳（<3）
        4: list(range(5, 20)),   # 5 资产：IC 门槛边界（>=5 保留）
        6: list(range(3, 20)),   # 3 资产：evaluator 会算、路由门槛必须剔除
        8: list(range(12, 20)),  # 8 资产：IC 保留、分组门槛剔除
    }
    merged = _make_panel(seed=99, missing=missing)

    # 佐证：裸 evaluator（无门槛）会包含 3 资产日 —— 门槛确实存在于路由侧
    ev = FactorEvaluator(factor_col="value", return_col="fwd_ret", method="rank")
    bare = ev.ic_series(
        merged.select(["trade_date", "asset_id", "value"]),
        merged.select(["trade_date", "asset_id", "fwd_ret"]),
    )["trade_date"].to_list()
    bare_dates = {str(d[0] if isinstance(d, (tuple, list)) else d) for d in bare}
    d3 = str(date(2024, 1, 1) + timedelta(days=6))
    assert d3 in bare_dates, "fixture 失效：裸 evaluator 应包含 3 资产日"

    ref_series, ref_summary = _reference_ic(merged, "fwd_ret")
    new_series, new_summary = factors_routes._evaluate_ic_metrics(merged, "fwd_ret")
    assert new_series == ref_series
    assert new_summary["quantile_returns"] == ref_summary["quantile_returns"]
    d8 = str(date(2024, 1, 1) + timedelta(days=8))
    assert d8 in {s["trade_date"] for s in new_series}  # 8 资产日进 IC、不进分组


# ---------------------------------------------------------------------------
# 4) 已声明修复：ic_ttest / ic_half_life 恢复输出（收敛前被 TypeError 吞掉）
# ---------------------------------------------------------------------------

def test_ttest_and_half_life_now_present() -> None:
    merged = _make_panel(seed=7, n_dates=40)
    _, summary = factors_routes._evaluate_ic_metrics(merged, "fwd_ret")
    assert "ic_ttest" in summary and "ic_half_life" in summary
    assert summary["ic_ttest"]["n"] == summary["observations"]


# ---------------------------------------------------------------------------
# 5) 路由接线：_compute_ic 端到端输出 == helper 输出（upsert/形状不变）
# ---------------------------------------------------------------------------

class StubCatalog:
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


def _seed_from_panel(catalog: StubCatalog, merged: pl.DataFrame, horizon: int) -> pl.DataFrame:
    """把 fwd_ret 面板反演成 close 价格（ret_{h}d 前瞻收益可由价格复现）。"""
    assets = sorted(merged["asset_id"].unique().to_list())
    dates = sorted(merged["trade_date"].unique().to_list())
    rng = np.random.default_rng(3)
    base = {a: 10.0 + i for i, a in enumerate(assets)}
    close: dict[tuple[str, date], float] = {}
    for a in assets:
        c = base[a]
        for d in dates:
            close[(a, d)] = c
            row = merged.filter(
                (pl.col("asset_id") == a) & (pl.col("trade_date") == d)
            )
            r = row["fwd_ret"][0] if row.height else float(rng.normal(0, 0.02))
            c = c * (1 + r)
    for a in assets:
        for d in dates:
            c = close[(a, d)]
            catalog.execute(
                "INSERT INTO silver_prices_1d VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [a, d, c * 0.99, c * 1.01, c * 0.98, c, 1000.0, 10000.0],
            )
            fv = merged.filter(
                (pl.col("asset_id") == a) & (pl.col("trade_date") == d)
            )
            if fv.height:
                catalog.execute(
                    "INSERT INTO gold_factor_values VALUES ('fsv_a32', 'fac_a32', ?, ?, ?)",
                    [d, a, float(fv["value"][0])],
                )
    # 复现路由的 merged（含 horizon 前瞻收益 + drop_nulls）
    factor_df = catalog.query(
        "SELECT trade_date, asset_id, value FROM gold_factor_values "
        "WHERE feature_set_version = 'fsv_a32' AND factor_name = 'fac_a32' "
        f"AND {factors_routes.INDEX_EXCLUSION_SQL} ORDER BY trade_date, asset_id"
    )
    price_df = catalog.query(
        "SELECT trade_date, asset_id, close FROM silver_prices_1d ORDER BY asset_id, trade_date"
    )
    ret_name = f"ret_{horizon}d"
    price_with_ret = price_df.sort(["asset_id", "trade_date"]).with_columns(
        (pl.col("close") / pl.col("close").shift(horizon).over("asset_id") - 1).alias(ret_name)
    )
    return factor_df.join(
        price_with_ret.select(["trade_date", "asset_id", ret_name]),
        on=["trade_date", "asset_id"], how="inner",
    ).drop_nulls()


def test_compute_ic_route_wiring(catalog: StubCatalog) -> None:
    missing = {3: list(range(12, 20)), 8: list(range(15, 20))}
    panel = _make_panel(seed=42, missing=missing)
    merged = _seed_from_panel(catalog, panel, horizon=1)

    catalog.execute(
        "INSERT INTO meta_factor_analytics "
        "(job_id, factor_name, feature_set_version, horizon_days, status, submitted_at) "
        "VALUES ('job_a32', 'fac_a32', 'fsv_a32', 1, 'pending', '2024-01-01')"
    )
    body = factors_routes.ICComputeBody(
        factor_name="fac_a32", feature_set_version="fsv_a32", horizon_days=1
    )
    factors_routes._compute_ic("job_a32", body, catalog)

    row = catalog.query(
        "SELECT status, series_json, summary_json, error_text FROM meta_factor_analytics "
        "WHERE job_id = 'job_a32'"
    ).to_dicts()[0]
    assert row["status"] == "done", row["error_text"]

    route_series = json.loads(row["series_json"])
    route_summary = json.loads(row["summary_json"])
    exp_series, exp_summary = factors_routes._evaluate_ic_metrics(merged, "ret_1d")

    assert route_series == exp_series
    for key, val in exp_summary.items():
        assert route_summary[key] == val, f"route wiring diff in {key}"
    # turnover/net_ic 为 A3-1 当日截面口径
    assert route_summary["factor_turnover"] == _daily_denominator_turnover(merged)
    # upsert 行为不变
    ups = catalog.query("SELECT * FROM gold_factor_ic_summary").to_dicts()
    assert len(ups) == 1 and ups[0]["factor_name"] == "fac_a32"
    assert str(ups[0]["window_start"]) == route_series[0]["trade_date"]
