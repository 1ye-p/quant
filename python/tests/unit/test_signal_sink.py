"""B2：引擎采集（读后即清）+ runner signal-detail sink.

契约（task-6）:
1. 引擎在 generate_signals 返回后立即快照 ``last_score_detail`` /
   ``missing_factors`` 并清空策略属性（WF fold 复用实例时无上一 fold 残留）；
2. runner 末批量 upsert gold_bt_signal_details，action 四类
   （enter/hold/exit/candidate，对照 detail asset 集与两期持仓集）；
3. 边界裁剪（D2-A）：每日 持仓全集 + 退出票 + Top 候选 ≤ 4×top_n；
4. missing_factors 汇总 → tags["signals_missing_factors"]（无缺失不写 key）；
5. sink 失败沿 fills 模式：tags signals_persisted=false + 错误摘要，
   不影响回测结果状态；
6. WF 形态：每 fold 一行 gold_bt_runs（fold 经 _run_single 各自落明细），
   父聚合 run（_persist_walk_forward_result，不经过引擎）无明细。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from cquant.backtest_vector.engine import BacktestSpec, VectorBacktestEngine
from cquant.backtest_vector.run import BacktestRunner
from cquant.backtest_vector.strategy import Strategy, StrategyContext
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

_DAY = date(2025, 6, 2)


# ── 夹具 ─────────────────────────────────────────────────────────────────────

@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "sigdetail_test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _insert_run_row(cat: Catalog, run_id: str, tags: str | None = "null") -> None:
    cat.execute(
        "INSERT OR REPLACE INTO gold_backtest_runs "
        "(run_id, engine, strategy_id, dataset_version, started_at, status, tags) "
        "VALUES (?, 'vector', 'sigdetail_unit', 'v1', now(), 'completed', ?)",
        [run_id, tags],
    )


def _detail_df(n: int = 8) -> pl.DataFrame:
    """n 资产全截面打分明细：asset_id / score / rank + `_w_` 分项列。"""
    return pl.DataFrame({
        "asset_id": [f"A{i}" for i in range(n)],
        "score": [float(n - i) for i in range(n)],
        "rank": list(range(1, n + 1)),
        "_w_momentum": [float(n - i) * 0.6 for i in range(n)],
        "_w_value": [float(n - i) * 0.4 for i in range(n)],
    })


def _engine_record(
    prev: dict[str, float],
    new: dict[str, float],
    detail: pl.DataFrame | None = None,
    td: date = _DAY,
) -> dict:
    """引擎采集记录的最小形态（engine signal_details 列表元素）。"""
    return {
        "trade_date": td,
        "detail": _detail_df() if detail is None else detail,
        "prev_weights": prev,
        "new_weights": new,
    }


def _result(records: list[dict], missing: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        signal_details=records,
        missing_factors=missing or [],
    )


def _rows(cat: Catalog, run_id: str) -> pl.DataFrame:
    return cat.query(
        "SELECT trade_date, asset_id, score, rank, action, prev_weight, "
        "new_weight, factor_scores_json FROM gold_bt_signal_details "
        "WHERE run_id = ? ORDER BY trade_date, asset_id",
        [run_id],
    )


def _tags(cat: Catalog, run_id: str) -> dict:
    row = cat.query("SELECT tags FROM gold_backtest_runs WHERE run_id = ?", [run_id])
    assert not row.is_empty()
    raw = row["tags"][0]
    if raw is None:
        return {}
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return dict(parsed or {})


# ── 1: action diff 四类 ──────────────────────────────────────────────────────

class TestActionDiff:
    def test_action_diff_four_classes(self, catalog) -> None:
        prev = {"A0": 0.4, "A4": 0.3, "X_UNS": 0.3}   # A4 scored-dropped; X_UNS unscored exit
        new = {"A0": 0.5, "A1": 0.5}                  # A0 hold; A1 enter
        runner = BacktestRunner(catalog)
        _insert_run_row(catalog, "run_ad")
        runner._persist_signal_details(
            _result([_engine_record(prev, new)]), "run_ad",
            SimpleNamespace(top_n=10),
        )
        rows = _rows(catalog, "run_ad")
        by_asset = {r["asset_id"]: r for r in rows.iter_rows(named=True)}

        assert by_asset["A1"]["action"] == "enter"
        assert by_asset["A1"]["prev_weight"] == 0.0
        assert by_asset["A1"]["new_weight"] == 0.5

        assert by_asset["A0"]["action"] == "hold"
        assert by_asset["A0"]["prev_weight"] == 0.4

        # scored-dropped exit keeps its factor scores
        assert by_asset["A4"]["action"] == "exit"
        fs = json.loads(by_asset["A4"]["factor_scores_json"])
        assert set(fs) == {"momentum", "value"}  # `_w_` 前缀已剥
        # unscored exit → null score / null factor json
        assert by_asset["X_UNS"]["action"] == "exit"
        assert by_asset["X_UNS"]["score"] is None

        # scored, neither prev nor new → candidate
        assert by_asset["A2"]["action"] == "candidate"
        assert by_asset["A2"]["new_weight"] == 0.0

    def test_factor_scores_json_strips_prefix(self, catalog) -> None:
        runner = BacktestRunner(catalog)
        _insert_run_row(catalog, "run_fs")
        runner._persist_signal_details(
            _result([_engine_record({}, {"A1": 1.0})]), "run_fs",
            SimpleNamespace(top_n=5),
        )
        row = _rows(catalog, "run_fs").filter(pl.col("asset_id") == "A1")
        fs = json.loads(row["factor_scores_json"][0])
        assert fs == {"momentum": pytest.approx(7 * 0.6), "value": pytest.approx(7 * 0.4)}


# ── 2: 边界裁剪 ≤ 4×top_n ────────────────────────────────────────────────────

class TestBoundaryTrim:
    def test_boundary_trim_rows_le_4x_topn(self, catalog) -> None:
        n = 40
        prev = {"A5": 1 / 3, "A6": 1 / 3, "A7": 1 / 3}   # ranked 6-8 → exits
        new = {"A0": 1 / 3, "A1": 1 / 3, "A2": 1 / 3}    # ranks 1-3 → enters
        runner = BacktestRunner(catalog)
        _insert_run_row(catalog, "run_trim")
        runner._persist_signal_details(
            _result([_engine_record(prev, new, detail=_detail_df(n))]),
            "run_trim",
            SimpleNamespace(top_n=3),  # cap = 12
        )
        rows = _rows(catalog, "run_trim")
        # 3 enter + 3 exit + 6 candidate (budget 12 - 6 keep) = 12
        assert rows.height == 12
        actions = {r["asset_id"]: r["action"] for r in rows.iter_rows(named=True)}
        assert sum(a == "enter" for a in actions.values()) == 3
        assert sum(a == "exit" for a in actions.values()) == 3
        kept_candidates = sorted(
            int(a[1:]) for a, act in actions.items() if act == "candidate"
        )
        # 候选按 rank 裁剪：持仓/退出之外 rank 最小的 6 个
        # （A0-A2 enter、A5-A7 exit，候选留 A3、A4、A8、A9、A10、A11）
        assert kept_candidates == [3, 4, 8, 9, 10, 11]

    def test_boundary_trim_multi_date_cap(self, catalog) -> None:
        runner = BacktestRunner(catalog)
        _insert_run_row(catalog, "run_md")
        recs = [
            _engine_record({"B0": 1.0}, {"C0": 1.0}, detail=_detail_df(30), td=_DAY + timedelta(days=i))
            for i in range(3)
        ]
        runner._persist_signal_details(
            _result(recs), "run_md", SimpleNamespace(top_n=2),  # cap = 8
        )
        rows = _rows(catalog, "run_md")
        per_day = rows.group_by("trade_date").len()
        assert all(cnt <= 8 for cnt in per_day["len"].to_list())


# ── 3: 引擎读后即清 ─────────────────────────────────────────────────────────

class _DetailStrategy(Strategy):
    """按 MultiFactor 生命周期暴露 last_score_detail / missing_factors。"""

    required_history_days = 0

    def __init__(self) -> None:
        self.last_score_detail: pl.DataFrame | None = None
        self.missing_factors: list[str] = []
        self.calls = 0

    @property
    def strategy_id(self) -> str:
        return "detail_stub"

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        self.last_score_detail = None
        self.missing_factors = []
        self.calls += 1
        if self.calls == 1:
            self.missing_factors = ["phantom_factor"]
        assets = ["A0", "A1", "A2"]
        scores = [3.0, 2.0, 1.0]
        self.last_score_detail = pl.DataFrame({
            "asset_id": assets,
            "score": scores,
            "rank": [1, 2, 3],
            "_w_momentum": scores,
        })
        return pl.DataFrame({
            "asset_id": assets[:2],
            "signal_date": [ctx.as_of_date] * 2,
            "direction": ["long"] * 2,
            "strength": [1.0, 1.0],
            "confidence": [1.0, 1.0],
        })


def _engine_prices(days: int = 6) -> pl.DataFrame:
    rows = []
    start = date(2025, 3, 3)
    for i in range(days):
        d = start + timedelta(days=i)
        for a, base in (("A0", 10.0), ("A1", 20.0), ("A2", 30.0)):
            p = base * (1 + 0.001 * i)
            rows.append({
                "trade_date": d, "asset_id": a,
                "open": p, "high": p * 1.01, "low": p * 0.99,
                "close": p, "volume": 1e6, "amount": p * 1e6,
                "is_suspended": False,
            })
    return pl.DataFrame(rows)


class TestEngineCapture:
    def test_read_after_clear(self) -> None:
        strat = _DetailStrategy()
        spec = BacktestSpec(
            strategy=strat,
            prices=_engine_prices(),
            start_date=date(2025, 3, 3),
            end_date=date(2025, 3, 8),
            initial_cash=Decimal("100000"),
            rebalance_frequency="1d",
        )
        result = VectorBacktestEngine().run(spec)

        assert strat.calls >= 5
        # 读后即清：引擎跑完后策略属性为 None（WF 复用安全）
        assert strat.last_score_detail is None
        assert strat.missing_factors == []
        # 采集上下文持有每日快照
        assert len(result.signal_details) == strat.calls
        assert all(r["detail"] is not None for r in result.signal_details)
        assert all(r["detail"].height == 3 for r in result.signal_details)
        # missing_factors 每日快照合并去重
        assert result.missing_factors == ["phantom_factor"]

    def test_plain_strategy_without_attribute_is_safe(self) -> None:
        class _Plain(Strategy):
            required_history_days = 0

            @property
            def strategy_id(self) -> str:
                return "plain"

            def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
                return pl.DataFrame({
                    "asset_id": ["A0"],
                    "signal_date": [ctx.as_of_date],
                    "direction": ["long"],
                    "strength": [1.0],
                    "confidence": [1.0],
                })

        spec = BacktestSpec(
            strategy=_Plain(),
            prices=_engine_prices(),
            start_date=date(2025, 3, 3),
            end_date=date(2025, 3, 5),
            initial_cash=Decimal("100000"),
            rebalance_frequency="1d",
        )
        result = VectorBacktestEngine().run(spec)
        # 无能力策略：记录仍采集（持仓 diff），detail 恒为 None → sink 跳过
        assert all(r["detail"] is None for r in result.signal_details)
        assert result.missing_factors == []


# ── 4: WF 形态（每 fold 一行 run，各自落明细；父 run 无明细） ────────────────

class TestWalkForward:
    def test_wf_folds_persist_per_fold(self, catalog) -> None:
        runner = BacktestRunner(catalog)
        _insert_run_row(catalog, "wf_parent")
        fold_ids = []
        for i in range(2):
            fold_run = f"wf_fold_{i}"
            _insert_run_row(catalog, fold_run)
            fold_ids.append(fold_run)
            # fold 经 _run_single → sink 用 fold 自己的 run_id
            runner._persist_signal_details(
                _result([_engine_record({"A5": 1.0}, {"A0": 1.0}, td=_DAY + timedelta(days=i))]),
                fold_run,
                SimpleNamespace(top_n=5),
            )
        for fold_run in fold_ids:
            fold_rows = _rows(catalog, fold_run)
            assert fold_rows.height > 0
            actions = set(fold_rows["action"].to_list())
            assert "enter" in actions and "exit" in actions
        # 父聚合 run（_persist_walk_forward_result，不经过引擎）无明细
        assert _rows(catalog, "wf_parent").height == 0


# ── 5: sink 失败不影响 run ──────────────────────────────────────────────────

class TestSinkFailure:
    def test_sink_failure_run_still_completed(self, catalog, monkeypatch) -> None:
        _insert_run_row(catalog, "run_fail")

        def _boom(*args, **kwargs):
            raise RuntimeError("duckdb exploded")

        monkeypatch.setattr(catalog, "upsert", _boom)
        runner = BacktestRunner(catalog)
        # 不抛出
        runner._persist_signal_details(
            _result([_engine_record({}, {"A1": 1.0})]), "run_fail",
            SimpleNamespace(top_n=5),
        )
        status = catalog.query(
            "SELECT status FROM gold_backtest_runs WHERE run_id = 'run_fail'"
        )["status"][0]
        assert status == "completed"  # run 状态不受影响
        tags = _tags(catalog, "run_fail")
        assert tags["signals_persisted"] is False
        assert "RuntimeError" in tags["signals_error"]
        assert "duckdb exploded" in tags["signals_error"]


# ── 6: missing_factors tag ──────────────────────────────────────────────────

class TestMissingFactorsTag:
    def test_missing_factors_tag(self, catalog) -> None:
        runner = BacktestRunner(catalog)
        _insert_run_row(catalog, "run_mf")
        runner._persist_signal_details(
            _result([], missing=["f_b", "f_a", "f_b"]), "run_mf",
            SimpleNamespace(top_n=5),
        )
        tags = _tags(catalog, "run_mf")
        assert json.loads(tags["signals_missing_factors"]) == ["f_a", "f_b"]

    def test_no_missing_no_key(self, catalog) -> None:
        runner = BacktestRunner(catalog)
        _insert_run_row(catalog, "run_nomf")
        runner._persist_signal_details(
            _result([_engine_record({}, {"A1": 1.0})]), "run_nomf",
            SimpleNamespace(top_n=5),
        )
        tags = _tags(catalog, "run_nomf")
        assert "signals_missing_factors" not in tags


# ── 7: regime scale-0 全清仓日 exit 可见（prev 快照前移） ────────────────────

class _ScriptedRegime:
    """date → position_scale（复刻 test_regime_engine.ScriptedRegime 最小形态）。"""

    def __init__(self, scales: dict[date, float]) -> None:
        from cquant.strategy_dsl.regime import RegimeResult
        self._RegimeResult = RegimeResult
        self._scales = scales

    def evaluate(self, as_of_date: date):
        scale = self._scales.get(as_of_date, 1.0)
        return self._RegimeResult(
            position_scale=scale, state="scripted", as_of_date=as_of_date,
        )


class _RegimeDetailStrategy(_DetailStrategy):
    """带打分明细的 buy-and-hold（买入 A0/A1，截面含 A0-A2 打分）。"""

    def generate_signals(self, ctx: StrategyContext) -> pl.DataFrame:
        _DetailStrategy.generate_signals(self, ctx)
        return pl.DataFrame({
            "asset_id": ["A0", "A1"],
            "signal_date": [ctx.as_of_date] * 2,
            "direction": ["long"] * 2,
            "strength": [1.0, 1.0],
            "confidence": [1.0, 1.0],
        })


def _june_prices(days: int = 14) -> pl.DataFrame:
    """2025-06-02（周一）起的 A0/A1/A2 价格帧。"""
    rows = []
    start = date(2025, 6, 2)
    for i in range(days):
        d = start + timedelta(days=i)
        for a, base in (("A0", 10.0), ("A1", 20.0), ("A2", 30.0)):
            p = base * (1 + 0.001 * i)
            rows.append({
                "trade_date": d, "asset_id": a,
                "open": p, "high": p * 1.01, "low": p * 0.99,
                "close": p, "volume": 1e6, "amount": p * 1e6,
                "is_suspended": False,
            })
    return pl.DataFrame(rows)


class TestRegimeForceExitVisibility:
    START = date(2025, 6, 2)          # Monday
    DERISK = date(2025, 6, 9)         # next weekly rebalance: scale 0 → full clear

    def _run(self):
        strat = _RegimeDetailStrategy()
        spec = BacktestSpec(
            strategy=strat,
            prices=_june_prices(),
            start_date=self.START,
            end_date=self.START + timedelta(days=13),
            initial_cash=Decimal("100000"),
            rebalance_frequency="1w",
            regime_sm=_ScriptedRegime({self.DERISK: 0.0}),
        )
        return VectorBacktestEngine().run(spec)

    def test_regime_force_exits_classified_as_exit(self) -> None:
        result = self._run()
        rec = next(r for r in result.signal_details if r["trade_date"] == self.DERISK)
        # prev = 清仓前真实持仓；new = 清仓后（空仓）
        assert set(rec["prev_weights"]) == {"A0", "A1"}
        assert all(w > 0 for w in rec["prev_weights"].values())
        assert rec["new_weights"] == {}
        # 分类侧：exit 行（prev_weight=清仓前权重、new_weight=0），非 candidate/缺行
        # 行结构: (run_id, trade_date, asset_id, score, rank, action,
        #          prev_weight, new_weight, factor_scores_json)
        rows = BacktestRunner._signal_detail_rows(rec, "run_x", 5)
        by_asset = {r[2]: r for r in rows}
        assert set(by_asset) >= {"A0", "A1"}
        for aid in ("A0", "A1"):
            row = by_asset[aid]
            assert row[5] == "exit", f"{aid} should be exit, got {row[5]}"
            assert row[6] == pytest.approx(rec["prev_weights"][aid])
            assert row[7] == 0.0
