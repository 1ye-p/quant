"""A2-2 e2e: Combo 混编（DSL + MultiFactor 子策略）与各自单跑一致。

生产路径（``BacktestRunner.run`` 全装配，非直接构造策略）跑三次回测：

1. Combo(strategy_type="Combo", sub=[DSL 子, MultiFactor 子], equal_weight)
2. DSL 单跑（与 Combo 内 DSL 子同参数）
3. MultiFactor 单跑（与 Combo 内 MultiFactor 子同参数）

对比口径（Combo 无 ``last_score_detail``，故 gold_bt_signal_details 对
Combo run 为空——子策略级快照取自两次单跑的落盘分数）：

- 子策略贡献快照：单跑 run 的 ``gold_bt_signal_details``（每日全截面
  score/rank，B2 语义：``last_score_detail`` 在 ``head(top_n)`` 之前截取）。
  子策略当日 emission = rank <= top_n 的资产集，strength = score。
- Combo 聚合语义（CompositeStrategy.equal_weight + 引擎默认 sizing）：
  combined_strength(a) = 0.5 * score_dsl(a) [a∈DSL top] + 0.5 * score_mf(a)
  [a∈MF top]；活跃集 = combined_strength > 0；权重 = 1/|活跃集|。
- 执行偏移：信号日 T → ``gold_signals`` 落在 T+1（next-bar execution）。

断言：Combo run 的 ``gold_signals`` (trade_date, asset_id, target_weight)
与上述推导逐日一致；且两次单跑自身的 ``gold_signals`` 分别等于
DSL 的 top picks（EqualWeightSizer 不过滤负强度）与 MF 的正强度 picks
（引擎默认 sizing），闭合「贡献 == 单跑」的环路。
"""

from __future__ import annotations

import json
import math
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

N_DAYS = 46
ASSETS = ["SSE:600036", "SSE:000001", "SSE:600519"]
FEATURE_SET = "fsv_combo_e2e"
TOP_N = 2

DATES = [date(2025, 3, 3) + timedelta(days=i) for i in range(N_DAYS)]

DSL_SPEC: dict = {
    "name": "combo_dsl_child",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight"},
}
MF_WEIGHTS = {"mom": 0.6, "vol": -0.4}

DSL_CHILD_CFG = {
    "strategy_id": "dsl_child",
    "strategy_type": "DSL",
    "dsl_spec": DSL_SPEC,
    "top_n": TOP_N,
}
MF_CHILD_CFG = {
    "strategy_id": "mf_child",
    "strategy_type": "MultiFactor",
    "factor_weights": MF_WEIGHTS,
    "top_n": TOP_N,
}


def _factor_value(name: str, asset_idx: int, day_idx: int) -> float:
    """Deterministic, date-varying cross-section (ranks flip across days)."""
    if name == "mom":
        return math.sin(0.37 * day_idx + 2.1 * asset_idx)
    return math.cos(0.43 * day_idx + 1.3 * asset_idx)


@pytest.fixture()
def combo_catalog(tmp_path, monkeypatch):
    """Catalog with prices and two date-varying materialized factors."""
    monkeypatch.chdir(tmp_path)  # data/backtest_artifacts is CWD-relative
    cat = Catalog(db_path=tmp_path / "combo_e2e.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    conn = cat._get_conn()

    rng = np.random.default_rng(23)
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

    fac_rows = [
        {
            "feature_set_version": FEATURE_SET, "factor_name": name,
            "trade_date": d, "asset_id": a,
            "value": _factor_value(name, ASSETS.index(a), i),
        }
        for i, d in enumerate(DATES)
        for a in ASSETS
        for name in ("mom", "vol")
    ]
    dff = pl.DataFrame(fac_rows)
    conn.register("_fac", dff.to_arrow())
    conn.execute(
        "INSERT INTO gold_factor_values "
        "(feature_set_version, factor_name, trade_date, asset_id, value) "
        "SELECT feature_set_version, factor_name, trade_date, asset_id, value FROM _fac"
    )
    conn.unregister("_fac")
    return cat


@pytest.fixture()
def client(combo_catalog, monkeypatch):
    """TestClient bound to the e2e catalog (API 400 route test)."""
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: combo_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, combo_catalog
    app.dependency_overrides = {}


def _run(cat, **kw) -> str:
    base = dict(
        dataset_version="v1",
        strategy_id="combo_e2e",
        start_date=DATES[5],
        end_date=DATES[-1],
        feature_set_version=FEATURE_SET,
        top_n=TOP_N,
        initial_cash=Decimal("100000"),
    )
    base.update(kw)
    return BacktestRunner(cat).run(BacktestRunSpec(**base))


def _signal_scores(cat, run_id: str) -> dict[date, dict[str, float]]:
    """Per-date full-cross-section scores from gold_bt_signal_details.

    (B2 snapshot = last_score_detail captured before head(top_n), so the
    persisted ranks cover the whole cross-section — exactly the inputs each
    child strategy sees when run inside the Combo.)
    """
    df = cat.query(
        "SELECT trade_date, asset_id, score, rank FROM gold_bt_signal_details "
        "WHERE run_id = ? AND score IS NOT NULL",
        [run_id],
    )
    assert df.height > 0, f"run {run_id} persisted no signal detail rows"
    out: dict[date, dict[str, float]] = {}
    for row in df.iter_rows(named=True):
        td = row["trade_date"]
        td = td if isinstance(td, date) else date.fromisoformat(str(td))
        out.setdefault(td, {})[row["asset_id"]] = float(row["score"])
    return out


def _top_picks(scores: dict[str, float], top_n: int = TOP_N) -> dict[str, float]:
    """A child's SignalFrame emission: top_n assets by score (rank order)."""
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return dict(ranked[:top_n])


def _gold_signals(cat, run_id: str) -> dict[date, dict[str, float]]:
    """Per-date target weights persisted to gold_signals (exec date = T+1).

    Note: ``_persist_signals`` stores the run_id in ``signal_set_version``.
    """
    df = cat.query(
        "SELECT trade_date, asset_id, target_weight FROM gold_signals "
        "WHERE signal_set_version = ?",
        [run_id],
    )
    assert df.height > 0, f"run {run_id} persisted no gold_signals rows"
    out: dict[date, dict[str, float]] = {}
    for row in df.iter_rows(named=True):
        td = row["trade_date"]
        td = td if isinstance(td, date) else date.fromisoformat(str(td))
        out.setdefault(td, {})[row["asset_id"]] = float(row["target_weight"])
    return out


def _next_trade_date(d: date) -> date | None:
    i = DATES.index(d)
    return DATES[i + 1] if i + 1 < len(DATES) else None


def test_combo_dsl_and_multifactor_children_match_standalone(combo_catalog) -> None:
    """Combo(子=DSL, 子=MultiFactor) vs 各自单跑同参数——贡献与聚合一致。"""
    cat = combo_catalog

    combo_run = _run(
        cat,
        strategy_id="combo_mixed",
        strategy_type="Combo",
        sub_strategy_configs=[DSL_CHILD_CFG, MF_CHILD_CFG],
        combo_method="equal_weight",
    )
    dsl_run = _run(
        cat, strategy_id="dsl_solo", strategy_type="DSL", dsl_spec=DSL_SPEC,
    )
    mf_run = _run(
        cat, strategy_id="mf_solo", strategy_type="MultiFactor",
        factor_weights=MF_WEIGHTS,
    )

    dsl_scores = _signal_scores(cat, dsl_run)
    mf_scores = _signal_scores(cat, mf_run)

    # Same rebalance calendar (same spec frequency / window / price table)
    assert set(dsl_scores) == set(mf_scores)

    # ── fixture non-vacuity: children must genuinely disagree somewhere ──
    disagreements = [
        d for d in dsl_scores
        if set(_top_picks(dsl_scores[d])) != set(_top_picks(mf_scores[d]))
    ]
    assert disagreements, "fixture degenerate: DSL and MF children always agree"

    # ── per-child contribution == standalone run's own book (closes loop) ──
    dsl_book = _gold_signals(cat, dsl_run)
    mf_book = _gold_signals(cat, mf_run)
    for d in dsl_scores:
        exec_d = _next_trade_date(d)
        if exec_d is None:
            continue
        # DSL standalone: EqualWeightSizer weights |strength|>0 → all top picks
        assert set(dsl_book.get(exec_d, {})) == set(_top_picks(dsl_scores[d])), (
            f"DSL child contribution != standalone book on {d}"
        )
        # MF standalone: engine default sizing → positive-strength picks only
        assert set(mf_book.get(exec_d, {})) == {
            a for a, s in _top_picks(mf_scores[d]).items() if s > 0
        }, f"MF child contribution != standalone book on {d}"

    # ── Combo output == derived aggregation of the two standalone snapshots ──
    combo_book = _gold_signals(cat, combo_run)
    drops_occurred = False
    for d in dsl_scores:
        exec_d = _next_trade_date(d)
        if exec_d is None:
            continue
        strength: dict[str, float] = {}
        for child_scores in (dsl_scores[d], mf_scores[d]):
            for aid, s in _top_picks(child_scores).items():
                strength[aid] = strength.get(aid, 0.0) + 0.5 * s
        active = {a for a, v in strength.items() if v > 0}
        if len(active) < len(strength):
            drops_occurred = True
        expected = {a: 1.0 / len(active) for a in active} if active else {}
        actual = combo_book.get(exec_d, {})
        assert set(actual) == set(expected), (
            f"combo holdings on {exec_d}: {sorted(actual)} != derived "
            f"{sorted(expected)} (signal date {d})"
        )
        for aid, w in expected.items():
            assert actual[aid] == pytest.approx(w, abs=1e-9), (
                f"combo weight for {aid} on {exec_d}: {actual[aid]} != {w}"
            )

    # strength>0 过滤真实发生过（混编聚合语义被覆盖，而非恒等透传）
    assert drops_occurred, (
        "fixture degenerate: combined strength never filtered a picked asset"
    )


def test_combo_depth4_api_400(client) -> None:
    """POST /backtests 深度 4 嵌套 Combo → 400（A2-1 eager validator 真路由）。"""
    c, cat = client
    cat.execute(
        "INSERT INTO meta_strategy_configs "
        "(strategy_id, config_format, config_text, parsed_config, created_at, updated_at) "
        "VALUES ('combo_depth', 'json', '{}', "
        f"'{json.dumps({'strategy_type': 'Combo'})}', now(), now())"
    )
    leaf = {"strategy_type": "MultiFactor", "factor_weights": {"mom": 1.0}}
    depth4 = leaf
    for _ in range(3):
        depth4 = {
            "strategy_type": "Combo",
            "sub_strategy_configs": [depth4],
        }
    resp = c.post("/api/v1/backtests", json={
        "strategy_id": "combo_depth",
        "dataset_version": "v1",
        "start_date": str(DATES[5]),
        "end_date": str(DATES[-1]),
        "feature_set_version": FEATURE_SET,
        "strategy_type": "Combo",
        "sub_strategy_configs": [depth4],
    })
    assert resp.status_code == 400, resp.text
    assert "depth limit 3" in resp.json()["detail"]
