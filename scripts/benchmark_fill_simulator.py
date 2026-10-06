#!/usr/bin/env python3
"""FillSimulator 专项基准——cProfile 分段计时，用于 Phase 0 瓶颈确认门控。

测量 ``AShareFillSimulator.simulate`` 的内部各方法耗时占比，重点回答：
``_get_price``（dict 价格查找）是否构成回测瓶颈？

报告的方法分层（按调用层级）：
- ``_build_price_lookup``：一次性预处理（建 (td, aid) -> price dict）
- ``_get_price``：(td, asset_id) -> field 的 dict 查找，最内层热点
- ``_calculate_nav``：每日快照与下单前的 NAV 计算（内部循环 _get_price）
- ``_execute_sell`` / ``_execute_buy``：撮合（含 cost_model / volume constraint）
- ``_calculate_sell_qty`` / ``_calculate_buy_qty``：下单数量计算（含可交易性校验）
- ``simulate``：顶层主循环（含所有子方法）

门控规则
--------
- ``_get_price`` 占总耗时 > 30%  →  数组化收益确认，进入 Phase 1
- FillSimulator 非主瓶颈 或 总耗时 < 5min  →  项目终止（YAGNI）

设计说明
--------
1. 复用 ``scripts/benchmark_backtest.py`` 的合成数据生成模式（numpy 几何随机
   游走 OHLCV），但 *不* 经过 DuckDB——本基准只关心 FillSimulator 内部，
   直接构造 polars 长表喂给 ``simulate``，排除 IO / pivot 噪声。
2. 关键可调参数 ``--top-n``（每个 rebalance 日持仓数）——这是真正决定卖出循环
   长度与 NAV 计算量的旋钮。默认随 universe 放大（取 universe 的 5%），以
   真实压力测试逐资产循环路径。
3. 用 cProfile 包装 ``simulate``，再按方法名聚合 tottime/cumtime，输出占比表。

用法
----
::

    # 小规模冒烟
    python scripts/benchmark_fill_simulator.py --universe 100 --days 50

    # 大规模门控
    python scripts/benchmark_fill_simulator.py --universe 1000 --days 250

    # 自定义持仓宽度（放大卖出循环）
    python scripts/benchmark_fill_simulator.py --universe 1000 --days 250 --top-n 100
"""

from __future__ import annotations

import argparse
import cProfile
import gc
import json
import logging
import pstats
import sys
import time
import tracemalloc
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import polars as pl

# 当作为仓库脚本运行时，确保可导入 cquant 包。
if __package__ in (None, ""):
    from pathlib import Path

    _repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_repo_root / "python"))

from cquant.backtest_vector.fill_simulator import AShareFillSimulator  # noqa: E402

logger = logging.getLogger("benchmark_fill_simulator")

# 初始资金，沿用生产默认（100 万）。
INITIAL_CASH = Decimal("1_000_000")

# 我们关注的方法名 → 简短标签（用于报告）。cumtime 口径会包含被调子方法，
# 因此 _get_price 的真实"独占"耗时看 tottime；它在主循环中被反复调用，
# cumtime 与 tottime 差值小，说明自身就是叶子热点。
_METHODS_OF_INTEREST = [
    "simulate",
    "_build_price_lookup",
    "_get_price",
    "_calculate_nav",
    "_calculate_sell_qty",
    "_calculate_buy_qty",
    "_execute_sell",
    "_execute_buy",
    "_check_tradability",
    "_is_price_valid",
    "_apply_volume_constraint",
    "_round_lot",
    "_can_sell",
]


def _build_synthetic_prices(
    universe_size: int,
    n_days: int,
    rng: np.random.Generator,
) -> pl.DataFrame:
    """生成合成随机 OHLCV 长表（universe_size × n_days 行），schema 对齐
    FillSimulator.simulate 的 prices 参数。

    价格起点约 5~50 元，每日几何布朗扰动。所有价格 > 0、非停牌，确保
    买入/卖出循环都走完整路径（而非因数据异常提前 return）。
    """
    total = universe_size * n_days

    asset_ids = np.array([f"SSE:{s:06d}" for s in np.arange(universe_size, dtype=np.int64)])
    aid_col = np.tile(asset_ids, n_days)

    # 交易日：从基准日起顺序递增（跳过周末）。
    base = date(2023, 1, 2)
    raw_dates: list[date] = []
    d = base
    while len(raw_dates) < n_days:
        if d.weekday() < 5:
            raw_dates.append(d)
        d += timedelta(days=1)
    date_col = np.repeat(np.array(raw_dates, dtype="datetime64[D]"), universe_size)

    start_prices = rng.uniform(5.0, 50.0, size=universe_size)
    daily_ret = rng.normal(loc=0.0, scale=0.02, size=(n_days, universe_size))
    close_paths = start_prices[None, :] * np.exp(np.cumsum(daily_ret, axis=0))
    close_flat = close_paths.reshape(total, order="C")

    spread = rng.uniform(0.0, 0.02, size=total)
    open_flat = close_flat * (1.0 - spread * rng.uniform(0, 1, size=total))
    high_flat = np.maximum.reduce([close_flat * (1.0 + spread), open_flat, close_flat])
    low_flat = np.minimum.reduce([close_flat * (1.0 - spread), open_flat, close_flat])

    volume_flat = rng.integers(1_000_000, 100_000_000, size=total).astype(np.float64)

    return pl.DataFrame({
        "asset_id": aid_col,
        "trade_date": pl.Series(date_col).cast(pl.Date),
        "open": open_flat,
        "high": high_flat,
        "low": low_flat,
        "close": close_flat,
        "volume": volume_flat,
        "adj_factor": np.ones(total, dtype=np.float64),
        "is_suspended": np.zeros(total, dtype=bool),
    })


def _build_target_weights(
    prices: pl.DataFrame,
    top_n: int,
) -> pl.DataFrame:
    """构造合成目标权重表：每个 rebalance 日选前 top_n 只股票等权多仓。

    信号在 T，成交在 T+1（next-bar execution，对齐 engine 约定）。
    top_n 越大，卖出循环越长、每日持仓越多、NAV 计算量越大——这正是压力
    测试逐资产循环 / _get_price 调用频率的旋钮。
    """
    trade_dates = sorted(prices["trade_date"].unique().to_list())
    # sorted → 确定性 top-n 选择（与 benchmark_backtest.py 同因：
    # unique(maintain_order=False) 顺序逐次可变，基准不可复现）。
    asset_ids = sorted(prices["asset_id"].unique().to_list())[:top_n]
    weight = 1.0 / top_n

    rows: list[dict] = []
    for i in range(1, len(trade_dates)):
        exec_date = trade_dates[i]
        for aid in asset_ids:
            rows.append({
                "trade_date": exec_date,
                "asset_id": aid,
                "target_weight": weight,
            })
    return pl.DataFrame(rows)


def _extract_method_stats(stats: pstats.Stats) -> dict[str, dict]:
    """从 pstats.Stats 抽取关注方法的 tottime/cumtime（秒）。

    返回 {method_short_name: {tottime, cumtime, ncalls, percall_tot}}。
    键为去掉文件路径与行号的纯方法名。
    """
    out: dict[str, dict] = {}
    # stats.stats: {(file, line, func): (cc, nc, tt, ct, callers)}
    for (file, _line, func), (cc, nc, tt, ct, _callers) in stats.stats.items():
        # 只保留 fill_simulator.py 内的方法 + 顶层 simulate
        if "fill_simulator" not in file and func != "simulate":
            continue
        if func not in _METHODS_OF_INTEREST and func not in out:
            continue
        percall_tot = (tt / nc) if nc else 0.0
        out[func] = {
            "ncalls": nc,
            "tottime": tt,
            "cumtime": ct,
            "percall_tot_ms": percall_tot * 1000,
        }
    return out


def benchmark(
    universe_size: int,
    n_days: int,
    top_n: int,
    seed: int = 42,
) -> dict:
    """对一个 (universe × days × top_n) 维度运行基准，返回指标字典。

    步骤：
    1. 生成 N 股 × T 日合成 prices + target_weights。
    2. tracemalloc 记录峰值内存（含 _build_price_lookup 的 dict）。
    3. cProfile 包装 ``simulate``，wall clock 单独计时。
    4. 聚合各方法 tottime / cumtime 占比。
    """
    rng = np.random.default_rng(seed)

    gc.collect()
    tracemalloc.start()

    # 阶段 0：合成数据（不计入 simulate 计时）
    t0 = time.perf_counter()
    prices = _build_synthetic_prices(universe_size, n_days, rng)
    target_weights = _build_target_weights(prices, top_n)
    data_gen_time = time.perf_counter() - t0

    # 阶段 1：cProfile 包装 simulate
    fill_sim = AShareFillSimulator(max_volume_pct=0.1)

    profiler = cProfile.Profile()
    t0 = time.perf_counter()
    profiler.enable()
    fills_df, snapshots_df = fill_sim.simulate(
        target_weights=target_weights,
        prices=prices,
        initial_cash=INITIAL_CASH,
    )
    profiler.disable()
    sim_wall_time = time.perf_counter() - t0

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    stats = pstats.Stats(profiler)
    method_stats = _extract_method_stats(stats)

    # 总 cumtime（simulate 顶层），用于算占比分母
    sim_cum = method_stats.get("simulate", {}).get("cumtime", sim_wall_time)
    # 用 simulate cumtime 作分母更稳定（排除 cProfile 未覆盖的 import 等）
    denom = sim_cum if sim_cum > 0 else sim_wall_time

    n_fills = len(fills_df) if not fills_df.is_empty() else 0
    n_snapshots = len(snapshots_df) if not snapshots_df.is_empty() else 0

    # 占比表
    pct: dict[str, dict] = {}
    for m, s in method_stats.items():
        pct[m] = {
            "ncalls": s["ncalls"],
            "tottime_s": round(s["tottime"], 4),
            "cumtime_s": round(s["cumtime"], 4),
            "tottime_pct": round(100.0 * s["tottime"] / denom, 2),
            "cumtime_pct": round(100.0 * s["cumtime"] / denom, 2),
            "percall_tot_us": round(s["percall_tot_ms"] * 1000, 3),
        }

    return {
        "universe_size": universe_size,
        "n_days": n_days,
        "top_n": top_n,
        "n_price_rows": universe_size * n_days,
        "data_gen_s": round(data_gen_time, 4),
        "simulate_wall_s": round(sim_wall_time, 4),
        "simulate_cumtime_s": round(sim_cum, 4),
        "peak_memory_mb": round(peak / (1024 * 1024), 2),
        "n_fills": n_fills,
        "n_snapshots": n_snapshots,
        "methods": pct,
    }


def _print_breakdown(r: dict) -> None:
    """控制台表格：各方法 tottime / cumtime / 占比。"""
    print(f"\n  FillSimulator.simulate 分段计时 (universe={r['universe_size']}, "
          f"days={r['n_days']}, top_n={r['top_n']})")
    print(f"  simulate wall = {r['simulate_wall_s']:.3f}s | "
          f"cumtime = {r['simulate_cumtime_s']:.3f}s | "
          f"peak_mem = {r['peak_memory_mb']:.1f}MB | "
          f"fills = {r['n_fills']} | snapshots = {r['n_snapshots']}")

    # 按 tottime 降序
    methods = sorted(
        r["methods"].items(),
        key=lambda kv: kv[1]["tottime_s"],
        reverse=True,
    )
    hdr = (f"  {'method':>26} {'ncalls':>12} {'tottime_s':>10} "
           f"{'tot%':>7} {'cumtime_s':>10} {'cum%':>7} {'us/call':>9}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for name, s in methods:
        if s["tottime_s"] == 0 and s["cumtime_s"] == 0:
            continue
        print(
            f"  {name:>26} {s['ncalls']:>12} {s['tottime_s']:>10.4f} "
            f"{s['tottime_pct']:>6.2f}% {s['cumtime_s']:>10.4f} "
            f"{s['cumtime_pct']:>6.2f}% {s['percall_tot_us']:>9.3f}"
        )
    print()


def _gating_decision(r: dict) -> str:
    """根据 _get_price 占比与总耗时做出门控决策。"""
    get_price = r["methods"].get("_get_price", {})
    pct = get_price.get("tottime_pct", 0.0)
    wall = r["simulate_wall_s"]

    if pct > 30.0:
        return (
            f"DECISION: PROCEED TO PHASE 1 (数组化). "
            f"_get_price 占 {pct:.2f}% > 30% 阈值，数组化收益确认。"
        )
    if wall < 300.0:
        return (
            f"DECISION: YAGNI TERMINATE (项目终止). "
            f"_get_price 占 {pct:.2f}% (<=30%)，simulate 总耗时 {wall:.1f}s < 5min；"
            f"FillSimulator 非主瓶颈，无需数组化优化。"
        )
    return (
        f"DECISION: INCONCLUSIVE. "
        f"_get_price 占 {pct:.2f}% (<=30%) 但 simulate 总耗时 {wall:.1f}s >= 5min；"
        f"瓶颈在 _get_price 之外（撮合/可交易性校验/NAV 循环），需进一步分析。"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FillSimulator 专项基准——cProfile 分段计时（Phase 0 瓶颈确认）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/benchmark_fill_simulator.py --universe 100 --days 50\n"
            "  python scripts/benchmark_fill_simulator.py --universe 1000 --days 250 --top-n 100\n"
        ),
    )
    parser.add_argument("--universe", type=int, default=1000, help="股票池规模（默认 1000）。")
    parser.add_argument("--days", type=int, default=250, help="交易日数（默认 250）。")
    parser.add_argument(
        "--top-n",
        type=int,
        default=0,
        help=(
            "每个 rebalance 日持仓数（默认 = universe 的 5%，至少 10）。"
            " 越大卖出循环越长、NAV 计算量越大。"
        ),
    )
    parser.add_argument("--seed", type=int, default=42, help="合成数据 RNG 种子（默认 42）。")
    parser.add_argument("--out", metavar="PATH", help="将完整 JSON 报告写入该路径。")
    parser.add_argument("-v", "--verbose", action="store_true", help="启用 DEBUG 日志。")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    top_n = args.top_n if args.top_n > 0 else max(10, args.universe // 20)

    print(
        f"FillSimulator 基准：universe={args.universe}, days={args.days}, "
        f"top_n={top_n} (seed={args.seed})"
    )
    print(f"  → 生成 {args.universe}×{args.days}={args.universe * args.days} 行价格 ...", flush=True)

    result = benchmark(args.universe, args.days, top_n, seed=args.seed)
    _print_breakdown(result)
    decision = _gating_decision(result)
    print(f"  {decision}\n")

    report = {
        "description": "FillSimulator 专项基准（cProfile 分段计时，Phase 0 瓶颈确认）",
        "params": {
            "universe": args.universe,
            "days": args.days,
            "top_n": top_n,
            "seed": args.seed,
        },
        "result": result,
        "gating_decision": decision,
    }
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"JSON 报告已写入：{args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
