"""Exact equivalence gate for the vectorized ``_build_tradability_today`` (P1).

The loop implementation was the recorded baseline (Task 0 fixtures). The
vectorized rewrite must reproduce it **bit-for-bit** on both fixture suites
(synthetic + real_sample), including every boundary construct:

- ST-named asset → current logic has no ST branch (board → MAIN → ±10%),
  so its ±5% moves are NOT flagged.
- ``-0.005`` absolute band arithmetic replicated exactly.
- ``close == high`` / ``close == low`` joint conditions preserved.
- Missing ``is_suspended`` column → ``None`` short-circuit.
- Assets without a prev_close (new listing) → row present, flags ``False``.
- detect_board prefix dead code: exchange-prefixed ids ("SSE:…", "SZSE:…",
  "BSE:…") all resolve to MAIN ±10% — replicated as-is (do not "fix").

Run: conda run -n cQuanty python -m pytest \
     python/tests/unit/test_tradability_vectorized.py --no-cov -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../quant
assert _REPO_ROOT.name == "quant"
sys.path.insert(0, str(_REPO_ROOT / "python"))

from cquant.backtest_vector.engine import VectorBacktestEngine  # noqa: E402

_GEN_PATH = _REPO_ROOT / "python/tests/fixtures/perf_equiv/generate_fixtures.py"
_SYNTH_DIR = _GEN_PATH.parent / "synthetic"
_REAL_DIR = _GEN_PATH.parent / "real_sample"
_REAL_CATALOG = _REPO_ROOT / "data" / "catalog.duckdb"

_spec = importlib.util.spec_from_file_location("perf_equiv_gen", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _recompute(prices: pl.DataFrame, days: list[date]) -> pl.DataFrame:
    """Re-run the (vectorized) production function per recorded day."""
    frames = [
        f for f in (VectorBacktestEngine._build_tradability_today(prices, d) for d in days)
        if f is not None
    ]
    merged = pl.concat(frames) if frames else pl.DataFrame()
    return merged.sort(["trade_date", "asset_id"])


def _fixture(dir_: Path) -> pl.DataFrame:
    return pl.read_parquet(dir_ / "tradability_all.parquet").sort(["trade_date", "asset_id"])


# ---------------------------------------------------------------------------
# Synthetic suite — exact, full frame
# ---------------------------------------------------------------------------

def test_synthetic_exact_equivalence() -> None:
    prices = gen.build_synthetic_prices()
    fix = _fixture(_SYNTH_DIR)
    days = fix["trade_date"].unique().sort().to_list()
    rec = _recompute(prices, days)
    assert_frame_equal(rec, fix, check_exact=True)


def test_synthetic_all_recorded_days_non_none() -> None:
    """Every recorded fixture day must produce a frame (no None regression)."""
    prices = gen.build_synthetic_prices()
    fix = _fixture(_SYNTH_DIR)
    for d in fix["trade_date"].unique().sort().to_list():
        out = VectorBacktestEngine._build_tradability_today(prices, d)
        assert out is not None, f"day {d} unexpectedly returned None"


# ---------------------------------------------------------------------------
# Boundary-specific gates (each construct in isolation)
# ---------------------------------------------------------------------------

def test_boundary_st_asset_not_flagged() -> None:
    """ST-named ±5% mover: current logic uses MAIN ±10% — must stay unflagged."""
    prices = gen.build_synthetic_prices()
    fix = _fixture(_SYNTH_DIR)
    st = fix.filter(pl.col("asset_id") == gen.ST_ASSET)
    assert st.height > 0
    assert st.filter(pl.col("is_limit_up") | pl.col("is_limit_down")).height == 0

    # Vectorized side agrees day by day.
    for d in st["trade_date"].unique().sort().to_list()[:20]:
        out = VectorBacktestEngine._build_tradability_today(prices, d)
        row = out.filter(pl.col("asset_id") == gen.ST_ASSET)
        assert not row["is_limit_up"].any() and not row["is_limit_down"].any()


def test_boundary_limit_up_down_flagged() -> None:
    prices = gen.build_synthetic_prices()
    fix = _fixture(_SYNTH_DIR)
    up = fix.filter((pl.col("asset_id") == gen.LIMIT_UP_ASSET) & pl.col("is_limit_up"))
    down = fix.filter((pl.col("asset_id") == gen.LIMIT_DOWN_ASSET) & pl.col("is_limit_down"))
    assert up.height >= 40 and down.height >= 40

    for d in up["trade_date"].unique().sort().to_list()[:20]:
        out = VectorBacktestEngine._build_tradability_today(prices, d)
        assert out.filter(
            (pl.col("asset_id") == gen.LIMIT_UP_ASSET) & pl.col("is_limit_up")).height == 1
    for d in down["trade_date"].unique().sort().to_list()[:20]:
        out = VectorBacktestEngine._build_tradability_today(prices, d)
        assert out.filter(
            (pl.col("asset_id") == gen.LIMIT_DOWN_ASSET) & pl.col("is_limit_down")).height == 1


def test_boundary_new_listing_no_prev_close_flags_false() -> None:
    """First price day has no prev_close: row present, both flags False."""
    prices = gen.build_synthetic_prices()
    first_day = (
        prices.filter(pl.col("asset_id") == gen.NEW_LISTING_ASSET)["trade_date"].min()
    )
    out = VectorBacktestEngine._build_tradability_today(prices, first_day)
    row = out.filter(pl.col("asset_id") == gen.NEW_LISTING_ASSET)
    assert row.height == 1
    assert not row["is_limit_up"][0] and not row["is_limit_down"][0]


def test_boundary_missing_is_suspended_returns_none() -> None:
    prices = gen.build_synthetic_prices().drop("is_suspended")
    days = prices["trade_date"].unique().sort().to_list()
    out = VectorBacktestEngine._build_tradability_today(prices, days[len(days) // 2])
    assert out is None


def test_boundary_no_rows_today_returns_none() -> None:
    prices = gen.build_synthetic_prices()
    out = VectorBacktestEngine._build_tradability_today(
        prices, date(1990, 1, 1))
    assert out is None


def test_boundary_no_prior_history_all_flags_false() -> None:
    """First trade day in the whole frame: every asset lacks prev_close."""
    prices = gen.build_synthetic_prices()
    first_day = prices["trade_date"].min()
    out = VectorBacktestEngine._build_tradability_today(prices, first_day)
    assert out is not None
    assert not out["is_limit_up"].any() and not out["is_limit_down"].any()


# ---------------------------------------------------------------------------
# Real-sample suite — exact against committed fixture (catalog read-only)
# ---------------------------------------------------------------------------

def _load_real_prices(meta: dict, dmax: date) -> pl.DataFrame | None:
    """Reproduce the engine's price input for the recorded real-sample run.

    Same production path as the generator: ``adjusted_ohlc_sql`` over the
    catalog rows it loaded (meta ``date_range``), then forward-fill +
    delisting trim — the exact frame ``_build_tradability_today`` saw.
    """
    if not _REAL_CATALOG.exists():
        return None
    import duckdb

    from cquant.backtest_vector.prices import adjusted_ohlc_sql
    from cquant.backtest_vector.run import _forward_fill_long_prices, _handle_delisting
    from cquant.datahub.universe import INDEX_EXCLUSION_SQL

    asset_ids = meta["asset_ids"]
    lo, hi = meta["date_range"]
    try:
        con = duckdb.connect(str(_REAL_CATALOG), read_only=True)
    except Exception:
        return None
    try:
        # INDEX_EXCLUSION_SQL: the default universe ('all') drops sector
        # indices (SSE 880xxx/881xxx) — same predicate the runner applied,
        # hence 41 of the 50 recorded assets appear in tradability rows.
        query = (
            adjusted_ohlc_sql()
            + f" WHERE {INDEX_EXCLUSION_SQL}"
            + " AND trade_date >= ? AND trade_date <= ?"
            + f" AND asset_id IN ({','.join('?' * len(asset_ids))})"
            + " ORDER BY asset_id, trade_date"
        )
        df = con.execute(query, [lo, hi] + asset_ids).pl()
    finally:
        con.close()
    if df.is_empty():
        return None
    if df["trade_date"].dtype == pl.Utf8:
        df = df.with_columns(pl.col("trade_date").str.to_date())
    df = _forward_fill_long_prices(df)
    return _handle_delisting(df, dmax)


@pytest.mark.skipif(not (_REAL_DIR / "tradability_all.parquet").exists(),
                    reason="real-sample fixture not recorded")
def test_real_sample_exact_equivalence() -> None:
    import json

    fix = _fixture(_REAL_DIR)
    meta = json.loads((_REAL_DIR / "meta.json").read_text())
    prices = _load_real_prices(meta, fix["trade_date"].max())
    if prices is None:
        pytest.skip("real catalog unavailable (missing or locked)")
    assert prices.height > 0

    days = fix["trade_date"].unique().sort().to_list()
    rec = _recompute(prices, days)
    assert_frame_equal(rec, fix, check_exact=True)


# ---------------------------------------------------------------------------
# Microbenchmark (opt-in): single rebalance day, 5000 assets × 500 days
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("CQUANT_PERF_BENCH") != "1",
                    reason="set CQUANT_PERF_BENCH=1 to run the microbenchmark")
def test_microbenchmark_5000_assets_single_day() -> None:
    import numpy as np
    import time

    rng = np.random.default_rng(7)
    n_assets, n_days = 5000, 500
    assets = [f"SSE:{600000 + i:06d}" for i in range(n_assets)]
    base = _trade_days_bench(n_days)
    closes = 10 + rng.random(n_assets) * 40

    rows = {"asset_id": [], "trade_date": [], "open": [], "high": [], "low": [],
            "close": [], "is_suspended": []}
    for d in base:
        ret = rng.normal(0.0005, 0.015, n_assets)
        closes = np.round(closes * (1 + ret), 2)
        for ai in range(n_assets):
            c = float(closes[ai])
            rows["asset_id"].append(assets[ai])
            rows["trade_date"].append(d)
            rows["open"].append(c)
            rows["high"].append(c)
            rows["low"].append(c)
            rows["close"].append(c)
            rows["is_suspended"].append(False)
    prices = pl.DataFrame(rows)

    # Park a 10th of the book at the exact +10% limit on the bench day so the
    # limit-up branch is genuinely exercised (close == high == prev*1.10).
    prev = prices.filter(pl.col("trade_date") == base[-2]).sort("asset_id")
    at_limit = prev.with_columns(
        (pl.col("close") * 1.10).round(2).alias("close")
    ).with_columns(pl.col("close").alias("high"))
    at_limit_ids = at_limit["asset_id"].to_list()[: n_assets // 10]
    prices = prices.filter(
        ~((pl.col("trade_date") == base[-1]) & pl.col("asset_id").is_in(at_limit_ids))
    )
    prices = pl.concat([
        prices,
        at_limit.filter(
            (pl.col("trade_date") == base[-2])
        ).with_columns(pl.lit(base[-1]).alias("trade_date"))
        .filter(pl.col("asset_id").is_in(at_limit_ids))
        .select(prices.columns),
    ]).sort(["asset_id", "trade_date"])

    td = base[-1]
    t0 = time.perf_counter()
    out = VectorBacktestEngine._build_tradability_today(prices, td)
    dt = time.perf_counter() - t0
    print(f"\n[bench] 5000x500 single-day tradability: {dt:.4f}s")
    assert out is not None and out.height == n_assets
    assert out["is_limit_up"].sum() > 0, "bench frame must exercise the limit-up branch"
    assert dt < 0.5, f"microbenchmark budget exceeded: {dt:.3f}s >= 0.5s"


def _trade_days_bench(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days
