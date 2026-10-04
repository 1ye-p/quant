"""B4a：gold_positions 幻影表修复 — per-asset 落盘单测.

`gold_positions` 此前被 correlation / factor-exposure / risk-contribution
三个端点查询，但既无 DDL 也无写入方（任何 run 都是 500）。本文件锁定：

1. 新 run 后 gold_positions 行与 result.positions 逐行一致（列映射）；
2. 同 run 重跑幂等（PK upsert 不翻倍）；
3. gold_risk_snapshots 聚合行为零改动（与历史快照逻辑一致）；
4. 持久化失败 → tag ``positions_persisted=false`` + 错误摘要，不抛出。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from cquant.backtest_vector.run import BacktestRunner
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

_DATES = [date(2025, 1, d) for d in (2, 3, 6, 7, 8)]


@pytest.fixture()
def catalog(tmp_path):
    cat = Catalog(db_path=tmp_path / "goldpos_test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


def _make_result(
    positions: pl.DataFrame,
    returns: list[float] | None = None,
):
    """最小 result 桩：只提供 _persist_positions 访问的属性。"""
    rets = returns if returns is not None else [0.01] * len(_DATES)
    portfolio_returns = pl.DataFrame({
        "trade_date": _DATES,
        "portfolio_return": rets,
        "nav": [100000 * (1.01 ** (i + 1)) for i in range(len(rets))],
    })
    spec = SimpleNamespace(
        initial_cash=Decimal("100000"),
        extra={},
        benchmark_asset_id="",
        prices=pl.DataFrame(),
    )
    return SimpleNamespace(
        strategy_id="goldpos_unit",
        spec=spec,
        portfolio_returns=portfolio_returns,
        positions=positions,
    )


def _positions_df() -> pl.DataFrame:
    """[trade_date, asset_id, target_weight] — engine weights_df 真实列名。"""
    return pl.DataFrame({
        "trade_date": [d for d in _DATES for _ in range(2)],
        "asset_id": ["SSE:600000", "SSE:600036"] * len(_DATES),
        "target_weight": [0.5, 0.5] * len(_DATES),
    })


def _insert_run_row(cat: Catalog, run_id: str) -> None:
    cat.execute(
        "INSERT OR REPLACE INTO gold_backtest_runs "
        "(run_id, engine, strategy_id, dataset_version, started_at, status, tags) "
        "VALUES (?, 'vector', 'goldpos_unit', 'v1', now(), 'completed', NULL)",
        [run_id],
    )


# ── 1+2: 行级一致 + 幂等 ────────────────────────────────────────────────────

class TestGoldPositionsRows:
    def test_rows_match_positions_df(self, catalog) -> None:
        cat = catalog
        result = _make_result(_positions_df())
        BacktestRunner(cat)._persist_positions(result, "run_goldpos_1")

        stored = cat.query(
            "SELECT run_id, trade_date, asset_id, weight FROM gold_positions "
            "WHERE run_id = ? ORDER BY trade_date, asset_id",
            ["run_goldpos_1"],
        )
        expected = (
            _positions_df()
            .sort("trade_date", "asset_id")
            .with_columns(pl.lit("run_goldpos_1").alias("run_id"))
            .select("run_id", "trade_date", "asset_id",
                    pl.col("target_weight").alias("weight"))
        )
        assert stored.height == expected.height == 10
        assert stored["asset_id"].to_list() == expected["asset_id"].to_list()
        assert stored["trade_date"].to_list() == expected["trade_date"].to_list()
        assert stored["weight"].to_list() == pytest.approx(
            expected["weight"].to_list()
        )

    def test_rerun_same_run_id_is_idempotent(self, catalog) -> None:
        cat = catalog
        result = _make_result(_positions_df())
        runner = BacktestRunner(cat)
        runner._persist_positions(result, "run_goldpos_1")
        runner._persist_positions(result, "run_goldpos_1")

        n = cat.query(
            "SELECT COUNT(*) AS n FROM gold_positions WHERE run_id = ?",
            ["run_goldpos_1"],
        ).item(0, "n")
        assert n == 10, f"PK upsert must not duplicate rows (got {n})"


# ── 3: gold_risk_snapshots 零改动 ───────────────────────────────────────────

class TestRiskSnapshotsUnchanged:
    def test_snapshot_rows_still_written(self, catalog) -> None:
        """聚合逻辑保持：per-portfolio_returns 一天一条快照，杠杆来自权重。"""
        cat = catalog
        result = _make_result(_positions_df())
        BacktestRunner(cat)._persist_positions(result, "run_goldpos_1")

        snaps = cat.query(
            "SELECT * FROM gold_risk_snapshots WHERE run_id = ? ORDER BY snapshot_ts",
            ["run_goldpos_1"],
        )
        assert snaps.height == len(_DATES)
        # equal-weight 2×0.5 → gross=1.0, net=1.0
        assert snaps["gross_leverage"].to_list() == pytest.approx([1.0] * len(_DATES))
        assert snaps["net_leverage"].to_list() == pytest.approx([1.0] * len(_DATES))
        # var_95 需 >1 个收益样本：首日为 null，其后非空
        assert snaps["var_95"].null_count() == 1


# ── 4: 失败路径 tag 戳 ──────────────────────────────────────────────────────

class TestPersistFailureSurfaced:
    def test_failure_stamps_tag_and_does_not_raise(self, catalog, monkeypatch, caplog) -> None:
        cat = catalog
        _insert_run_row(cat, "run_goldpos_fail")
        original_upsert = cat.upsert

        def exploding_upsert(table, *args, **kwargs):
            if table == "gold_positions":
                raise RuntimeError("simulated positions write failure")
            return original_upsert(table, *args, **kwargs)

        monkeypatch.setattr(cat, "upsert", exploding_upsert)
        result = _make_result(_positions_df())

        with caplog.at_level("ERROR", logger="cquant.backtest_vector.run"):
            # 不得抛出：回测结果不受持久化失败影响
            BacktestRunner(cat)._persist_positions(result, "run_goldpos_fail")

        stored = cat.query(
            "SELECT * FROM gold_positions WHERE run_id = ?", ["run_goldpos_fail"]
        )
        assert stored.is_empty()

        raw = cat.query(
            "SELECT tags FROM gold_backtest_runs WHERE run_id = ?",
            ["run_goldpos_fail"],
        )["tags"][0]
        assert raw, "run tags must carry the failure stamp"
        tags = json.loads(raw) if isinstance(raw, str) else dict(raw)
        assert tags.get("positions_persisted") is False
        assert "simulated positions write failure" in tags.get("positions_error", "")

        assert any(
            "gold_positions" in rec.message and "run_goldpos_fail" in rec.message
            for rec in caplog.records
        ), "failure must be logged at error level with run_id"

    def test_success_path_no_failure_tag(self, catalog) -> None:
        cat = catalog
        _insert_run_row(cat, "run_goldpos_ok")
        BacktestRunner(cat)._persist_positions(
            _make_result(_positions_df()), "run_goldpos_ok"
        )
        raw = cat.query(
            "SELECT tags FROM gold_backtest_runs WHERE run_id = ?",
            ["run_goldpos_ok"],
        )["tags"][0]
        if raw:
            tags = json.loads(raw) if isinstance(raw, str) else dict(raw)
            assert tags.get("positions_persisted") is not False
