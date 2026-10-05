"""Equivalence fixture generation — production assembly path (BacktestRunner.run).

Records the three artifacts that the perf-batch equivalence gates (T2/T3/T5)
will compare against after refactoring the tradability / fill / ctx hot paths:

① ``{name}/tradability_all.parquet`` — per-rebalance-day full tradability flags
   ``[trade_date, asset_id, is_suspended, is_limit_up, is_limit_down]``
   captured by wrapping ``VectorBacktestEngine._build_tradability_today``
   *inside* a real production run (so the output shape is exactly what the
   engine sees, day by day).
② ``{name}/fills.parquet`` — persisted ``gold_fills`` rows for the run.
③ ``{name}/nav.parquet`` — ``gold_portfolio_snapshots`` NAV/cash series.

Fixture sets
------------
``synthetic/``
    50 assets × 120 trade days, deterministic (seed=42), with the three
    mandatory boundary constructs:
    - ST-named asset (``SSE:ST0001``) with ±5% daily moves — current logic has
      no ST branch (board detection → MAIN → ±10%), so the fixture locks the
      *current* ±10% behavior.
    - new listing without prev_close (prices start mid-window).
    - ``is_suspended`` column-missing short-circuit (recorded in meta.json as
      the None path; production SQL always emits the column, so this boundary
      is exercised via a direct engine call on a column-dropped frame).
    Plus limit-up / limit-down / suspended-range constructs for flag coverage.
``real_sample/``
    50 real assets × last 120 trade days from ``data/catalog.duckdb``
    (read-only). Requires the real catalog to regenerate; the recorded
    parquet files are the long-term reference.

Usage (from repo root, conda cQuanty)::

    python python/tests/fixtures/perf_equiv/generate_fixtures.py [--only synthetic|real]

Outputs land next to this script. Re-runs are deterministic for ``synthetic``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "python"))

from cquant.backtest_vector.engine import VectorBacktestEngine  # noqa: E402
from cquant.backtest_vector.run import BacktestRunSpec, BacktestRunner  # noqa: E402
from cquant.datahub.catalog import Catalog  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent
N_DAYS = 120
N_ASSETS = 50
SEED = 42
FEATURE_SET = "perf_equiv"
INITIAL_CASH = Decimal("1_000_000")

# Boundary asset roles (synthetic set). The remaining 44 assets are normal.
ST_ASSET = "SSE:ST0001"          # ST-named; ±5% daily moves; current logic → ±10% branch
LIMIT_UP_ASSET = "SSE:600100"     # at limit-up on even window days (close==high==prev*1.10)
LIMIT_DOWN_ASSET = "SSE:600200"   # at limit-down on even window days
SUSPENDED_ASSET = "SSE:600300"    # is_suspended=True for window days 30..59
NEW_LISTING_ASSET = "SSE:600400"  # prices start at day 60 → no prev_close on day 60
NORMAL_TOP_ASSET = "SSE:600001"   # normal asset ranked into top_n

TOP_N = 6  # top_n == number of boundary assets → every rebalance touches them

DSL_SPEC: dict = {
    "name": "perf_equiv",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight"},
}


def _trade_days(n: int, base: date = date(2025, 1, 1)) -> list[date]:
    """First n weekday dates from *base* (approximates trading calendar)."""
    days: list[date] = []
    d = base
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def build_synthetic_prices() -> pl.DataFrame:
    """Deterministic 50×120 OHLCV long table with boundary constructs."""
    days = _trade_days(N_DAYS)
    rng = np.random.default_rng(SEED)

    normals = [f"SSE:{600002 + i:06d}" for i in range(N_ASSETS - 6)]
    assets = [ST_ASSET, LIMIT_UP_ASSET, LIMIT_DOWN_ASSET, SUSPENDED_ASSET,
              NEW_LISTING_ASSET, NORMAL_TOP_ASSET] + normals

    rows: list[dict] = []
    price = {a: float(rng.uniform(10.0, 50.0)) for a in assets}

    for di, d in enumerate(days):
        for ai, a in enumerate(assets):
            prev = price[a]
            if a == NEW_LISTING_ASSET and di < 60:
                continue  # not yet listed → first row (day 60) has no prev_close

            if a == ST_ASSET:
                ret = 0.05 if di % 2 == 0 else -0.05  # ST-sized moves, no ST rule applied
            elif a == LIMIT_UP_ASSET and di >= 20 and di % 2 == 0:
                ret = 0.10  # rounds to the ±10% limit branch
            elif a == LIMIT_DOWN_ASSET and di >= 20 and di % 2 == 0:
                ret = -0.10
            else:
                ret = float(rng.normal(0.0005, 0.015))

            close = round(prev * (1.0 + ret), 2)
            spread = float(rng.uniform(0.002, 0.015))
            if a == LIMIT_UP_ASSET and ret >= 0.10:
                # one-way limit-up bar: open == high == close == limit price
                open_, high, low = close, close, round(close * (1.0 - spread / 2), 2)
            elif a == LIMIT_DOWN_ASSET and ret <= -0.10:
                # one-way limit-down bar: open == low == close == limit price
                open_, low, high = close, close, round(close * (1.0 + spread / 2), 2)
            else:
                open_ = round(close * (1.0 - spread), 2)
                high = round(max(open_, close) * (1.0 + spread / 2), 2)
                low = round(min(open_, close) * (1.0 - spread / 2), 2)
            high = max(high, open_, close)
            low = min(low, open_, close)

            suspended = a == SUSPENDED_ASSET and 30 <= di < 60
            volume = 0.0 if suspended else float(rng.integers(500_000, 20_000_000))
            rows.append({
                "asset_id": a, "trade_date": d,
                "open": open_, "high": high, "low": low, "close": close,
                "volume": volume, "amount": volume * close,
                "adj_factor": 1.0, "adj_close": float(close),
                "is_suspended": suspended, "source": "perf_equiv_synth",
            })
            price[a] = close

    return pl.DataFrame(rows).sort(["asset_id", "trade_date"])


def build_synthetic_factors(prices: pl.DataFrame) -> pl.DataFrame:
    """Factor 'mom': boundary assets strictly top-6, normals strictly below."""
    boundary_rank = {
        ST_ASSET: 6.0, LIMIT_UP_ASSET: 5.5, LIMIT_DOWN_ASSET: 5.0,
        SUSPENDED_ASSET: 4.5, NEW_LISTING_ASSET: 4.0, NORMAL_TOP_ASSET: 3.5,
    }
    others = sorted(a for a in prices["asset_id"].unique().to_list()
                    if a not in boundary_rank)
    other_val = {a: 3.0 - i / 1000.0 for i, a in enumerate(others)}
    rank = {**boundary_rank, **other_val}

    return pl.DataFrame([
        {"feature_set_version": FEATURE_SET, "factor_name": "mom",
         "trade_date": d, "asset_id": a, "value": rank[a]}
        for d in prices["trade_date"].unique().sort().to_list()
        for a in prices["asset_id"].unique().to_list()
    ])


def load_real_sample() -> tuple[pl.DataFrame, pl.DataFrame] | None:
    """50 real assets × last 120 trade days from the real catalog (read-only)."""
    import duckdb

    db = _REPO_ROOT / "data" / "catalog.duckdb"
    if not db.exists():
        return None
    try:
        con = duckdb.connect(str(db), read_only=True)
    except Exception:
        return None

    try:
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM silver_prices_1d ORDER BY trade_date DESC LIMIT 120"
        ).fetchall()]
        dates.reverse()
        start, end = dates[0], dates[-1]

        assets = [r[0] for r in con.execute(
            "SELECT asset_id FROM silver_prices_1d WHERE trade_date = ? "
            "AND is_suspended = FALSE ORDER BY asset_id",
            [start.isoformat()],
        ).fetchall()]
        if len(assets) < N_ASSETS:
            return None
        step = max(1, len(assets) // N_ASSETS)
        picked = assets[::step][:N_ASSETS]

        prices = con.execute(
            "SELECT asset_id, trade_date, open, high, low, close, volume, amount, "
            "       adj_factor, adj_close, is_suspended, source "
            "FROM silver_prices_1d WHERE trade_date BETWEEN ? AND ? "
            "  AND asset_id IN (" + ",".join("?" * len(picked)) + ") "
            "ORDER BY asset_id, trade_date",
            [start.isoformat(), end.isoformat()] + picked,
        ).pl()

        # mom factor: 20-day return from real closes (deterministic given data)
        mom = (prices.sort(["asset_id", "trade_date"])
               .group_by("asset_id", maintain_order=True)
               .agg(pl.col("close").pct_change(20).fill_null(0.0).alias("mom"),
                    pl.col("trade_date").alias("trade_date"))
               .explode(["trade_date", "mom"]))
        factors = prices.join(mom, on=["asset_id", "trade_date"], how="inner").select(
            pl.lit(FEATURE_SET).alias("feature_set_version"),
            pl.lit("mom").alias("factor_name"),
            "trade_date", "asset_id",
            pl.col("mom").cast(pl.Float64).alias("value"),
        )
        return prices, factors
    finally:
        con.close()


def _insert(con, df: pl.DataFrame, table: str, columns: list[str]) -> None:
    con.register("_fixture_view", df.to_arrow())
    con.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"SELECT {', '.join(columns)} FROM _fixture_view"
    )
    con.unregister("_fixture_view")


PRICE_COLS = ["asset_id", "trade_date", "open", "high", "low", "close", "volume",
              "amount", "adj_factor", "adj_close", "is_suspended", "source"]
FACTOR_COLS = ["feature_set_version", "factor_name", "trade_date", "asset_id", "value"]


def run_production_capture(
    prices: pl.DataFrame,
    factors: pl.DataFrame,
) -> dict:
    """Build a sandbox catalog, run BacktestRunner.run (production assembly),
    capture tradability frames + fills + NAV. Returns recorded artifacts."""
    captured: dict = {"trad_frames": [], "none_days": []}
    orig = VectorBacktestEngine._build_tradability_today

    def _wrap(prices_df, td):
        out = orig(prices_df, td)
        if out is None:
            captured["none_days"].append(str(td))
        else:
            captured["trad_frames"].append(out)
        return out

    with tempfile.TemporaryDirectory(prefix="perf_equiv_") as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)  # run artifacts land in CWD-relative data/backtest_artifacts
        try:
            cat = Catalog(db_path=Path(tmp) / "perf_equiv.duckdb", repo_root=_REPO_ROOT)
            cat.initialize()
            conn = cat._get_conn()
            _insert(conn, prices, "silver_prices_1d", PRICE_COLS)
            _insert(conn, factors, "gold_factor_values", FACTOR_COLS)

            days = sorted(prices["trade_date"].unique().to_list())
            VectorBacktestEngine._build_tradability_today = staticmethod(_wrap)
            try:
                runner = BacktestRunner(cat)
                run_id = runner.run(BacktestRunSpec(
                    dataset_version="perf_equiv",
                    strategy_id="perf_equiv",
                    start_date=days[10],
                    end_date=days[-1],
                    feature_set_version=FEATURE_SET,
                    strategy_type="DSL",
                    dsl_spec=DSL_SPEC,
                    top_n=TOP_N,
                    initial_cash=INITIAL_CASH,
                    random_seed=SEED,
                    warmup_days=5,
                    tags={"perf_equiv": True},
                ))
            finally:
                VectorBacktestEngine._build_tradability_today = orig

            fills = cat.query(
                "SELECT trade_date, asset_id, side, qty, price, notional, "
                "       commission, stamp_duty, slippage, total_cost "
                "FROM gold_fills WHERE run_id = ? "
                "ORDER BY trade_date, asset_id, side",
                [run_id],
            )
            nav = cat.query(
                "SELECT trade_date, nav, cash, portfolio_return "
                "FROM gold_portfolio_snapshots WHERE run_id = ? ORDER BY trade_date",
                [run_id],
            )

            # Boundary: is_suspended column missing → short-circuit None.
            # Production SQL always emits the column, so exercise the engine
            # directly on a column-dropped frame (any trade date).
            no_col = prices.drop("is_suspended")
            missing_col_result = orig(no_col, days[len(days) // 2])
            captured["missing_is_suspended_is_none"] = missing_col_result is None
        finally:
            os.chdir(cwd)

    trad_all = (pl.concat(captured["trad_frames"]) if captured["trad_frames"]
                else pl.DataFrame())
    return {
        "tradability_all": trad_all.sort(["trade_date", "asset_id"]),
        "fills": fills,
        "nav": nav,
        "none_days": captured["none_days"],
        "missing_is_suspended_is_none": captured["missing_is_suspended_is_none"],
        "n_rebalance_days_recorded": len(captured["trad_frames"]),
    }


def _write_set(name: str, prices: pl.DataFrame, factors: pl.DataFrame, meta: dict) -> None:
    print(f"[{name}] prices={prices.height} factors={factors.height}")
    rec = run_production_capture(prices, factors)
    out = FIXTURE_DIR / name
    out.mkdir(parents=True, exist_ok=True)
    rec["tradability_all"].write_parquet(out / "tradability_all.parquet")
    rec["fills"].write_parquet(out / "fills.parquet")
    rec["nav"].write_parquet(out / "nav.parquet")
    meta = {
        **meta,
        "recorded": {
            "n_rebalance_days_recorded": rec["n_rebalance_days_recorded"],
            "tradability_rows": rec["tradability_all"].height,
            "fills_rows": rec["fills"].height,
            "nav_rows": rec["nav"].height,
            "tradability_none_days": rec["none_days"],
            "missing_is_suspended_is_none": rec["missing_is_suspended_is_none"],
        },
    }
    (out / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[{name}] trad={meta['recorded']['tradability_rows']} rows / "
          f"{meta['recorded']['n_rebalance_days_recorded']} days, "
          f"fills={meta['recorded']['fills_rows']}, nav={meta['recorded']['nav_rows']}, "
          f"none_days={rec['none_days']}, "
          f"missing_col_none={rec['missing_is_suspended_is_none']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate perf equivalence fixtures")
    parser.add_argument("--only", choices=["synthetic", "real"], default=None)
    args = parser.parse_args(argv)

    git_rev = os.popen("git rev-parse --short HEAD").read().strip()

    if args.only in (None, "synthetic"):
        prices = build_synthetic_prices()
        factors = build_synthetic_factors(prices)
        _write_set("synthetic", prices, factors, {
            "source": "synthetic (seed=42, deterministic)",
            "git_rev": git_rev,
            "seed": SEED,
            "n_assets": N_ASSETS, "n_days": N_DAYS,
            "boundary_assets": {
                "st": ST_ASSET,
                "limit_up": LIMIT_UP_ASSET,
                "limit_down": LIMIT_DOWN_ASSET,
                "suspended": SUSPENDED_ASSET,
                "new_listing_no_prev_close": NEW_LISTING_ASSET,
            },
            "notes": (
                "ST 资产无现行 ST 分支：board 检测 → MAIN → ±10%（fixture 锁定现行行为）。"
                "missing-is_suspended 边界为引擎直调（生产 SQL 恒产出该列）。"
                "regenerate: python python/tests/fixtures/perf_equiv/generate_fixtures.py --only synthetic"
            ),
        })

    if args.only in (None, "real"):
        real = load_real_sample()
        if real is None:
            print("[real] real catalog unavailable (data/catalog.duckdb missing or "
                  "locked) — skipped; synthetic set is the reference")
            return 0
        prices, factors = real
        _write_set("real_sample", prices, factors, {
            "source": "real catalog (data/catalog.duckdb, read-only)",
            "git_rev": git_rev,
            "n_assets": prices["asset_id"].n_unique(),
            "n_days": prices["trade_date"].n_unique(),
            "date_range": [str(prices["trade_date"].min()), str(prices["trade_date"].max())],
            "asset_ids": sorted(prices["asset_id"].unique().to_list()),
            "notes": (
                "regenerate 需要真实 catalog 可读；录制产物是长期对照基准。"
                " mom 因子为真实收盘 20 日收益（确定性给定数据）。"
            ),
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
