#!/usr/bin/env python3
"""Generate the cQuant onboarding demo dataset (deterministic).

Outputs (next to this script):
- demo_prices_1d.csv      50 synthetic A-shares x ~2y daily OHLCV (geometric
                          random walk, seed=42 → reproducible byte-for-byte)
- market_breadth.csv      one market-level external indicator (advance ratio)
- demo_strategy.yaml      DSL momentum TopN strategy (validated against
                          cquant.strategy_dsl.schema.StrategyDSL)

Run:  python examples/demo_data/generate_demo_data.py
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SEED = 42
N_ASSETS = 50
START = date(2024, 1, 2)
TRADING_DAYS = 480  # ~2 years of A-share trading days
OUT_DIR = Path(__file__).resolve().parent

# 50 symbols across SSE/SZSE for realistic exchange prefixes
SYMBOLS = [
    *(f"600{i:03d}" for i in range(25)),   # SSE
    *(f"000{i:03d}" for i in range(15)),   # SZSE main
    *(f"300{i:03d}" for i in range(10)),   # SZSE ChiNext
]


def _trading_days(start: date, n: int) -> list[date]:
    """Weekday-only calendar (good enough for synthetic data)."""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def main() -> None:
    rng = np.random.default_rng(SEED)
    days = _trading_days(START, TRADING_DAYS)

    # Per-asset drift/vol so the cross-section has dispersion (momentum works)
    drift = rng.normal(0.0004, 0.0006, N_ASSETS)
    vol = rng.uniform(0.012, 0.028, N_ASSETS)

    price_path = OUT_DIR / "demo_prices_1d.csv"
    with price_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["asset_id", "trade_date", "open", "high", "low", "close", "volume", "adj_factor"])
        for i, sym in enumerate(SYMBOLS):
            asset_id = f"{'SSE' if sym.startswith('6') else 'SZSE'}:{sym}"
            # start price 10-60 CNY
            px = float(rng.uniform(10, 60))
            adj = 1.0
            for j, d in enumerate(days):
                ret = rng.normal(drift[i], vol[i])
                # occasional jump (news events) and a mild market factor
                if rng.random() < 0.01:
                    ret += rng.choice([-1, 1]) * rng.uniform(0.03, 0.07)
                open_px = px * float(rng.normal(1.0, 0.004))
                px = max(1.0, px * (1 + ret))
                high = max(open_px, px) * float(rng.normal(1.005, 0.002))
                low = min(open_px, px) * float(rng.normal(0.995, 0.002))
                volume = int(abs(rng.normal(2_000_000, 800_000)))
                # slow adj_factor drift (corporate actions)
                if j and j % 120 == 0:
                    adj *= 1.02
                w.writerow([
                    asset_id, d.isoformat(),
                    f"{open_px:.3f}", f"{high:.3f}", f"{low:.3f}", f"{px:.3f}",
                    volume, f"{adj:.6f}",
                ])
    print(f"wrote {price_path.name}: {N_ASSETS} assets x {len(days)} days")

    # Market breadth: fraction of assets with positive 1d return (lagged = rule B
    # handled at import; here value is computed on the same day).
    breadth_path = OUT_DIR / "market_breadth.csv"
    closes: dict[str, list[float]] = {s: [] for s in SYMBOLS}
    with price_path.open() as f:
        for row in csv.DictReader(f):
            closes[row["asset_id"].split(":")[1]].append(float(row["close"]))
    with breadth_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "breadth"])
        for j, d in enumerate(days):
            if j == 0:
                continue
            ups = sum(1 for s in SYMBOLS if closes[s][j] > closes[s][j - 1])
            w.writerow([d.isoformat(), f"{ups / N_ASSETS:.4f}"])
    print(f"wrote {breadth_path.name}: {len(days) - 1} rows")

    strategy_path = OUT_DIR / "demo_strategy.yaml"
    strategy_path.write_text(STRATEGY_YAML, encoding="utf-8")
    print(f"wrote {strategy_path.name}")


STRATEGY_YAML = """\
# cQuant onboarding demo strategy — DSL momentum TopN
# Score: 20d momentum, equal-weight TopN, 8% fixed stop loss.
name: demo_momentum_top10
universe: all
frequency: daily
score:
  - factor: ret_20d
    weight: 1.0
position:
  method: equal_weight
risk:
  - type: fixed_stop_loss
    params:
      stop_pct: -0.08
      exit_fraction: 1.0
"""


if __name__ == "__main__":
    main()
