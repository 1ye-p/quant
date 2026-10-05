"""Smoke equivalence test — proves the perf_equiv fixture recording pipeline.

Re-runs the *production assembly path* (``BacktestRunner.run`` — same code as
 ``tests/fixtures/perf_equiv/generate_fixtures.py``) on the deterministic
synthetic boundary dataset and asserts the three recorded artifacts match the
committed fixtures **exactly** (bit-for-bit):

① tradability flags per (asset, rebalance day)
② fills detail (price/qty/date/asset/costs)
③ NAV series

If this test is green, the fixtures are trustworthy as the equivalence gate
baseline for the T2/T3/T5 hot-path refactors (tradability / fill / ctx).
After those refactors, the same comparison must STILL be exact — any drift
means the refactor changed behavior.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../quant
assert _REPO_ROOT.name == "quant"
sys.path.insert(0, str(_REPO_ROOT / "python"))

_GEN_PATH = _REPO_ROOT / "python/tests/fixtures/perf_equiv/generate_fixtures.py"
_FIXTURE_DIR = _GEN_PATH.parent / "synthetic"

_spec = importlib.util.spec_from_file_location("perf_equiv_gen", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _rerun_synthetic() -> dict:
    prices = gen.build_synthetic_prices()
    factors = gen.build_synthetic_factors(prices)
    return gen.run_production_capture(prices, factors)


def test_fixture_files_exist() -> None:
    for name in ("tradability_all.parquet", "fills.parquet", "nav.parquet"):
        assert (_FIXTURE_DIR / name).exists(), f"missing fixture: {name}"


def test_synthetic_reproduction_matches_fixture_exactly() -> None:
    """Current implementation vs recorded fixture — must be fully identical."""
    rec = _rerun_synthetic()

    trad_fix = pl.read_parquet(_FIXTURE_DIR / "tradability_all.parquet").sort(
        ["trade_date", "asset_id"])
    fills_fix = pl.read_parquet(_FIXTURE_DIR / "fills.parquet").sort(
        ["trade_date", "asset_id", "side"])
    nav_fix = pl.read_parquet(_FIXTURE_DIR / "nav.parquet").sort("trade_date")

    assert_frame_equal(rec["tradability_all"], trad_fix, check_exact=True)
    assert_frame_equal(rec["fills"], fills_fix, check_exact=True)
    assert_frame_equal(rec["nav"], nav_fix, check_exact=True)


def test_boundary_constructs_present_in_fixture() -> None:
    trad = pl.read_parquet(_FIXTURE_DIR / "tradability_all.parquet")

    up = trad.filter((pl.col("asset_id") == gen.LIMIT_UP_ASSET) & pl.col("is_limit_up"))
    down = trad.filter((pl.col("asset_id") == gen.LIMIT_DOWN_ASSET) & pl.col("is_limit_down"))
    susp = trad.filter((pl.col("asset_id") == gen.SUSPENDED_ASSET) & pl.col("is_suspended"))
    st = trad.filter(pl.col("asset_id") == gen.ST_ASSET)

    assert up.height >= 40, f"limit-up boundary under-covered: {up.height} days"
    assert down.height >= 40, f"limit-down boundary under-covered: {down.height} days"
    assert susp.height == 30, f"suspended boundary expected 30 days, got {susp.height}"
    # ST-named asset: current logic has no ±5% ST branch (board → MAIN → ±10%),
    # so its ±5% daily moves must NOT be flagged as limit — locks current behavior.
    assert st.filter(pl.col("is_limit_up") | pl.col("is_limit_down")).height == 0

    # New listing: no tradability rows before its first price date (no prev_close
    # day) and its first recorded day carries the row (flag computation skipped).
    new_rows = trad.filter(pl.col("asset_id") == gen.NEW_LISTING_ASSET).sort("trade_date")
    assert new_rows.height > 0
    prices = gen.build_synthetic_prices().filter(pl.col("asset_id") == gen.NEW_LISTING_ASSET)
    assert new_rows["trade_date"].min() == prices["trade_date"].min()

    # Missing is_suspended column → engine short-circuits to None.
    assert trad.height > 0  # sanity: fixture not empty


def test_missing_is_suspended_short_circuits_to_none() -> None:
    from cquant.backtest_vector.engine import VectorBacktestEngine

    prices = gen.build_synthetic_prices().drop("is_suspended")
    dates = prices["trade_date"].unique().sort().to_list()
    td = dates[len(dates) // 2]
    out = VectorBacktestEngine._build_tradability_today(prices, td)
    assert out is None, "prices without is_suspended must short-circuit to None"


@pytest.mark.skipif(
    not (_GEN_PATH.parent / "real_sample" / "fills.parquet").exists(),
    reason="real-sample fixture not recorded (real catalog unavailable)",
)
def test_real_sample_fixture_shape() -> None:
    """Real-sample fixtures: shape/columns only (re-run needs the real catalog)."""
    for name, must_cols in (
        ("tradability_all.parquet", ["trade_date", "asset_id", "is_suspended",
                                     "is_limit_up", "is_limit_down"]),
        ("fills.parquet", ["trade_date", "asset_id", "side", "qty", "price"]),
        ("nav.parquet", ["trade_date", "nav"]),
    ):
        df = pl.read_parquet(_GEN_PATH.parent / "real_sample" / name)
        assert not df.is_empty()
        assert set(must_cols).issubset(df.columns)
