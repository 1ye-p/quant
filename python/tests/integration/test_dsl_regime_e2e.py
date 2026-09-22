"""B1 e2e smoke: regime-aware DSL strategy must actually scale in production path.

Goes through BacktestRunner.run (production assembly) — NOT direct
RegimeStateMachine injection — so a wiring regression (spec parsed, validated,
but silently never scaled) is caught end-to-end.

Sentinel market indicator (``__MARKET__`` pseudo-asset in
``silver_external_indicators``) alternates every 3 days so the switch-mode
regime flips risk_on (scale 1.0) <-> risk_off (scale 0.5) repeatedly.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from cquant.backtest_vector.run import BacktestRunSpec, BacktestRunner
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

N_DAYS = 46  # 2025-01-01 .. 2025-02-15
ASSETS = ["SSE:600036", "SSE:000001"]
FEATURE_SET = "fsv_regime_e2e"
INDICATOR_KEY = "mkt_breadth"
REGIME_SCALE_OFF = 0.5

DSL_SPEC: dict = {
    "name": "regime_e2e",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight"},
    "regime": {
        "mode": "switch",
        "initial": "risk_on",
        "indicators": {"breadth": INDICATOR_KEY},
        "states": [
            {"name": "risk_on", "enter_when": "breadth > 0", "position_scale": 1.0},
            {
                "name": "risk_off",
                "enter_when": "breadth <= 0",
                "position_scale": REGIME_SCALE_OFF,
            },
        ],
    },
}

DATES = [date(2025, 1, 1) + timedelta(days=i) for i in range(N_DAYS)]
# +1 / -1 alternating every 3 days -> ~15 regime flips across the window
BREADTH = [1.0 if (i // 3) % 2 == 0 else -1.0 for i in range(N_DAYS)]


@pytest.fixture()
def regime_catalog(tmp_path, monkeypatch):
    """Catalog with prices, one materialized factor, and the sentinel indicator."""
    # artifact dir (data/backtest_artifacts) is CWD-relative -> sandbox it
    monkeypatch.chdir(tmp_path)
    cat = Catalog(db_path=tmp_path / "regime_e2e.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    conn = cat._get_conn()

    rng = np.random.default_rng(7)
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
    df = pl.DataFrame(price_rows)
    conn.register("_px", df.to_arrow())
    conn.execute(
        "INSERT INTO silver_prices_1d "
        "(asset_id, trade_date, open, high, low, close, volume, amount, "
        " adj_factor, adj_close, is_suspended, source) "
        "SELECT asset_id, trade_date, open, high, low, close, volume, amount, "
        "       adj_factor, adj_close, is_suspended, source FROM _px"
    )
    conn.unregister("_px")

    # factor 'mom': deterministic per-asset level -> stable top-1 ordering
    fac_rows = [
        {"feature_set_version": FEATURE_SET, "factor_name": "mom",
         "trade_date": d, "asset_id": a, "value": 1.0 if a == ASSETS[0] else 0.0}
        for d in DATES for a in ASSETS
    ]
    dff = pl.DataFrame(fac_rows)
    conn.register("_fac", dff.to_arrow())
    conn.execute(
        "INSERT INTO gold_factor_values "
        "(feature_set_version, factor_name, trade_date, asset_id, value) "
        "SELECT feature_set_version, factor_name, trade_date, asset_id, value FROM _fac"
    )
    conn.unregister("_fac")

    # sentinel market indicator, PIT-visible same day
    ext_rows = [
        {"source": "test", "indicator_key": INDICATOR_KEY, "asset_id": "__MARKET__",
         "trade_date": d, "value": BREADTH[i], "available_date": d}
        for i, d in enumerate(DATES)
    ]
    dfe = pl.DataFrame(ext_rows)
    conn.register("_ext", dfe.to_arrow())
    conn.execute(
        "INSERT INTO silver_external_indicators "
        "(source, indicator_key, asset_id, trade_date, value, available_date) "
        "SELECT source, indicator_key, asset_id, trade_date, value, available_date FROM _ext"
    )
    conn.unregister("_ext")
    return cat


def _run_regime_backtest(cat) -> str:
    runner = BacktestRunner(cat)
    return runner.run(BacktestRunSpec(
        dataset_version="v1",
        strategy_id="regime_e2e",
        start_date=DATES[5],
        end_date=DATES[-1],
        feature_set_version=FEATURE_SET,
        strategy_type="DSL",
        dsl_spec=DSL_SPEC,
        top_n=1,
        initial_cash=Decimal("100000"),
        tags={"dsl_spec": DSL_SPEC, "top_n": 1},
    ))


def _load_regime_history(run_id: str) -> pl.DataFrame:
    path = Path("data/backtest_artifacts") / f"{run_id}_regime.parquet"
    assert path.exists(), f"regime scale history artifact not persisted: {path}"
    return pl.read_parquet(path)


def test_dsl_regime_scaling_end_to_end(regime_catalog) -> None:
    """含 regime 段的 DSL 策略，走生产路径回测必须真实执行缩放。"""
    run_id = _run_regime_backtest(regime_catalog)
    hist = _load_regime_history(run_id)

    # 1. run 产物 regime_scale_history 非空
    assert not hist.is_empty()
    assert set(["trade_date", "desired_scale", "actual_scale"]).issubset(hist.columns)

    # 2. risk_off 区间缩放真实发生：desired=0.5 出现，且 actual < 1
    off = hist.filter(
        (hist["desired_scale"] - REGIME_SCALE_OFF).abs() < 1e-9
    )
    assert not off.is_empty(), "regime risk_off never triggered in production run"
    de_risked = off.filter(off["actual_scale"] < 0.95)
    assert not de_risked.is_empty(), (
        "desired_scale=0.5 recorded but gross exposure never de-risked — "
        "regime state machine not wired into the production backtest path"
    )

    # 3. fills 与状态切换一致：首次降杠杆（1.0 -> 0.5）后出现 SELL
    first_off = off["trade_date"].min()
    fills = regime_catalog.query(
        "SELECT trade_date, side FROM gold_fills WHERE run_id = ? AND side = 'sell' "
        "AND trade_date >= ?",
        [run_id, first_off],
    )
    assert not fills.is_empty(), (
        f"no sell fills on/after first risk_off date {first_off} — "
        "de-risking never executed"
    )


def test_dsl_regime_validation_matches_execution(regime_catalog) -> None:
    """验证套件 regime 周期统计必须与实际回测产物同源（防“统计有、执行无”再裂）。"""
    from cquant.api_server.routes.backtests import _execute_validation_suite

    run_id = _run_regime_backtest(regime_catalog)
    hist = _load_regime_history(run_id)

    # 实际执行侧：从产物 desired_scale 数状态切换次数
    scales = hist.sort("trade_date")["desired_scale"].to_list()
    exec_transitions = sum(
        1 for a, b in zip(scales, scales[1:])
        if round(a, 6) != round(b, 6)
    )
    assert exec_transitions > 0, "no regime transitions in the executed run"

    # 验证套件侧
    report = _execute_validation_suite(regime_catalog, run_id)
    steps = {s["step"]: s for s in report["steps"]}
    assert "regime_cycles" in steps, f"regime_cycles step missing: {list(steps)}"
    rc = steps["regime_cycles"]
    assert rc["status"] == "completed", rc
    assert rc["cycles"] == exec_transitions, (
        f"validation suite counts {rc['cycles']} regime cycles but the executed "
        f"run artifact shows {exec_transitions} — statistics and execution diverged"
    )
