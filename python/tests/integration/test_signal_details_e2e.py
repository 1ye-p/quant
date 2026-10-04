"""B2 e2e smoke: signal details must flow through the production backtest path.

生产路径（``BacktestRunner.run`` —— 即 POST /backtests 处理器最终调用的同一
装配）→ ``gold_bt_signal_details`` 落盘 → 读侧 ``GET /backtests/{run_id}/signals``
双语义（dates / 明细）→ 缺失因子告警 tag → 边界行数。

未物化因子的构造（先核实过 DSL 校验行为）：
``StrategyDSL.from_dict`` 在 runner 注入的校验上下文里会拒绝完全未知的因子名
（known_factors = BUILTIN ∪ gold_factor_values 全部 factor_name），因此「引用
一个 fixture 未物化的名字」会在构造期失败而非产生 missing_factors。故采用
「因子存在但运行特征集无数据」路径：``ghost`` 物化在**另一个**
feature_set_version 下（通过 known_factors 校验），但回测运行用的
feature_set 只含 ``mom`` → 特征宽表无 ``ghost`` 列 → missing_factors。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from cquant.api_server import deps
from cquant.api_server.app import app
from cquant.backtest_vector.run import BacktestRunSpec, BacktestRunner
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

N_DAYS = 30
ASSETS = ["SSE:600036", "SSE:000001", "SSE:600519"]
FEATURE_SET = "fsv_sig_e2e"        # 回测运行用的特征集（只物化 mom）
OTHER_FEATURE_SET = "fsv_sig_other"  # ghost 挂靠的特征集（运行特征集不含它）
GHOST = "ghost"

DATES = [date(2025, 7, 1) + timedelta(days=i) for i in range(N_DAYS)]

DSL_SPEC: dict = {
    "name": "sig_e2e",
    "score": [
        {"factor": "mom", "weight": 1.0},
        {"factor": GHOST, "weight": 1.0},   # 运行特征集无此列 → missing
    ],
    "position": {"method": "equal_weight"},
}

TOP_N = 2


@pytest.fixture()
def sig_catalog(tmp_path, monkeypatch):
    """Catalog with prices, `mom` in the run feature set, `ghost` only in another."""
    monkeypatch.chdir(tmp_path)  # data/backtest_artifacts CWD-relative
    cat = Catalog(db_path=tmp_path / "sig_e2e.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    conn = cat._get_conn()

    rng = np.random.default_rng(11)
    price_rows = []
    p = {a: 50.0 for a in ASSETS}
    for d in DATES:
        for a in ASSETS:
            p[a] *= 1 + rng.normal(0.001, 0.01)
            price_rows.append({
                "asset_id": a, "trade_date": d,
                "open": p[a], "high": p[a] * 1.01, "low": p[a] * 0.99,
                "close": p[a], "volume": 1e6, "amount": p[a] * 1e6,
                "adj_factor": 1.0, "adj_close": p[a],
                "is_suspended": False, "source": "test",
            })
    dfp = pl.DataFrame(price_rows)
    conn.register("_px", dfp.to_arrow())
    conn.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, amount, "
        " adj_factor, adj_close, is_suspended, source) "
        "SELECT asset_id, trade_date, open, high, low, close, volume, amount, "
        "       adj_factor, adj_close, is_suspended, source FROM _px"
    )
    conn.unregister("_px")

    def _insert_factors(feature_set: str, names: list[str]) -> None:
        # deterministic per-asset levels -> stable cross-section
        values = {
            name: {a: float(ASSETS.index(a)) + 0.5 * i
                   for a in ASSETS}
            for i, name in enumerate(names)
        }
        rows = [
            {"feature_set_version": feature_set, "factor_name": name,
             "trade_date": d, "asset_id": a, "value": values[name][a]}
            for d in DATES for a in ASSETS for name in names
        ]
        dff = pl.DataFrame(rows)
        conn.register("_fac", dff.to_arrow())
        conn.execute(
            "INSERT INTO gold_factor_values "
            "(feature_set_version, factor_name, trade_date, asset_id, value) "
            "SELECT feature_set_version, factor_name, trade_date, asset_id, value "
            "FROM _fac"
        )
        conn.unregister("_fac")

    _insert_factors(FEATURE_SET, ["mom"])
    # ghost 只物化在另一个特征集下：通过 DSL known_factors 校验，
    # 但运行特征集（FEATURE_SET）无此列 → missing_factors 路径。
    _insert_factors(OTHER_FEATURE_SET, [GHOST])
    return cat


@pytest.fixture()
def client(sig_catalog, monkeypatch):
    """TestClient bound to the e2e catalog (read-side verification)."""
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: sig_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, sig_catalog
    app.dependency_overrides = {}


def _run_dsl_backtest(cat) -> str:
    runner = BacktestRunner(cat)
    return runner.run(BacktestRunSpec(
        dataset_version="v1",
        strategy_id="sig_e2e",
        start_date=DATES[3],
        end_date=DATES[-1],
        feature_set_version=FEATURE_SET,
        strategy_type="DSL",
        dsl_spec=DSL_SPEC,
        top_n=TOP_N,
        initial_cash=Decimal("100000"),
        tags={"dsl_spec": DSL_SPEC, "top_n": TOP_N},
    ))


def _run_tags(cat, run_id: str) -> dict:
    raw = cat.query(
        "SELECT tags FROM gold_backtest_runs WHERE run_id = ?", [run_id]
    )["tags"][0]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return dict(parsed or {})


def test_signal_details_end_to_end(client) -> None:
    """DSL 策略 + 一个未物化因子：落盘 → 读侧双语义 → 告警 tag → 边界行数。"""
    c, cat = client
    run_id = _run_dsl_backtest(cat)

    # 1. 落盘有行，且字段完整（score/rank/action）
    rows = cat.query(
        "SELECT trade_date, asset_id, score, rank, action FROM "
        "gold_bt_signal_details WHERE run_id = ?",
        [run_id],
    )
    assert rows.height > 0, "production DSL run persisted no signal detail rows"
    assert rows["score"].null_count() < rows.height  # 有打分行
    assert set(rows["action"].to_list()) <= {"enter", "hold", "exit", "candidate"}

    # 2. GET signals 无 date → dates 含明细表中的调仓日（数据自洽）
    resp = c.get(f"/api/v1/backtests/{run_id}/signals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] > 0
    expected_dates = {
        str(d) for d in rows["trade_date"].to_list()
    }
    assert expected_dates.issubset(set(body["dates"]))

    # 3. GET signals 有 date → 打分/排名/action 明细
    some_date = body["dates"][0]
    resp = c.get(
        f"/api/v1/backtests/{run_id}/signals",
        params={"date": some_date},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) > 0
    scored = [it for it in items if it["score"] is not None]
    assert scored, f"no scored items on {some_date}"
    assert all(it["rank"] is not None for it in scored)
    # 分项因子分只含 mom（ghost 无数据，不产生分项列）
    assert all(set(it["factor_scores"]) == {"mom"} for it in scored)

    # 4. tags.signals_missing_factors 含 ghost
    tags = _run_tags(cat, run_id)
    missing = json.loads(tags.get("signals_missing_factors", "[]"))
    assert GHOST in missing

    # 5. 单调仓日行数 ≤ 4×top_n（D2-A 边界）
    per_day = cat.query(
        "SELECT trade_date, COUNT(*) AS n FROM gold_bt_signal_details "
        "WHERE run_id = ? GROUP BY trade_date",
        [run_id],
    )
    assert all(n <= 4 * TOP_N for n in per_day["n"].to_list())


def test_unsupported_strategy_no_details(client) -> None:
    """StaticTopN run → 404 reason=unsupported_strategy_type（类型先于明细判定）。"""
    c, cat = client
    runner = BacktestRunner(cat)
    run_id = runner.run(BacktestRunSpec(
        dataset_version="v1",
        strategy_id="static_e2e",
        start_date=DATES[3],
        end_date=DATES[-1],
        feature_set_version=FEATURE_SET,
        strategy_type="StaticTopN",
        sort_factor="mom",
        top_n=1,
        initial_cash=Decimal("100000"),
    ))

    resp = c.get(f"/api/v1/backtests/{run_id}/signals")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unsupported_strategy_type"
