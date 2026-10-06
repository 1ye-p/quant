#!/usr/bin/env python3
"""大股票池回测性能基准——仅测量，不优化。

测量维度：1000/3000/5000 股 × 250/500 日。
记录：wall_time / peak_memory / duckdb_query_time，并按阶段拆分
(price load / pivot / fill simulate) 以定位瓶颈。

设计说明
--------
此脚本刻意避开完整的 ``BacktestRunner``/``Catalog`` 装配——完整装配需要
磁盘 catalog、特征表、universe 解析等基础设施，会把测量噪声混入我们关心的
回测关键路径。改为：

1. 在 *内存* DuckDB 中 ``CREATE TABLE silver_prices_1d`` 并批量插入合成
   随机 OHLCV（universe_size × n_days 行），schema 与生产 silver 层一致。
2. 直接调用生产关键路径的等价实现：
   - price load：``adjusted_ohlc_sql()`` SELECT → polars（复用生产 SQL 片段）
   - pivot：``VectorBacktestEngine._build_price_matrix``
   - fill simulate：``AShareFillSimulator.simulate``
3. 每个阶段用 ``time.perf_counter`` 计时；DuckDB 查询时间用独立计时
   分量单独记录；整体峰值内存用 ``tracemalloc`` 记录。
4. 输出 JSON 报告 + 控制台表格。

用法
----
::

    # 默认全套 6 个维度
    python scripts/benchmark_backtest.py

    # 仅指定维度
    python scripts/benchmark_backtest.py --dims 1000x250 5000x500

    # 输出 JSON 报告到文件（显式路径）
    python scripts/benchmark_backtest.py --out benchmark_report.json

    # 按 tag 写入默认路径 artifacts/benchmarks/{date}_{tag}.json
    python scripts/benchmark_backtest.py --tag pre_perf
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import time
import tracemalloc
from datetime import date, timedelta
from decimal import Decimal

import duckdb
import numpy as np
import polars as pl

# 当作为仓库脚本运行时，确保可导入 cquant 包。
if __package__ in (None, ""):
    import importlib.util
    from pathlib import Path

    _repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_repo_root / "python"))

from cquant.backtest_vector.engine import VectorBacktestEngine  # noqa: E402
from cquant.backtest_vector.fill_simulator import AShareFillSimulator  # noqa: E402
from cquant.backtest_vector.prices import adjusted_ohlc_sql  # noqa: E402

logger = logging.getLogger("benchmark_backtest")

# 默认测量矩阵：(universe_size, n_days)
DEFAULT_DIMS: list[tuple[int, int]] = [
    (1000, 250),
    (1000, 500),
    (3000, 250),
    (3000, 500),
    (5000, 250),
    (5000, 500),
]

# 每个 rebalance 买入的标的数（等权）。保持小而稳定以聚焦于数据/撮合路径，
# 而非策略本身的开销。
TOP_N = 10
# 初始资金，沿用生产默认（100 万）。
INITIAL_CASH = Decimal("1_000_000")


def _build_synthetic_prices(
    universe_size: int,
    n_days: int,
    rng: np.random.Generator,
) -> pl.DataFrame:
    """生成合成随机 OHLCV 长表（universe_size × n_days 行）。

    价格起点约 5~50 元，每日几何布朗扰动；volume/amount 随机正数。
    所有列与生产 ``silver_prices_1d`` 对齐（含 adj_factor/adj_close/
    is_suspended/source），使 ``adjusted_ohlc_sql()`` 可直接套用。
    """
    total = universe_size * n_days

    # asset_id：SSE:000001..SSSS —— 股票池过大时仍落在合法前缀内。
    symbols = np.arange(universe_size, dtype=np.int64)
    asset_ids = np.array([f"SSE:{s:06d}" for s in symbols])

    # 完整 (asset × date) 笛卡尔积
    aid_col = np.tile(asset_ids, n_days)

    # 交易日：从基准日起顺序递增（跳过周末以贴近真实交易日密度）。
    base = date(2023, 1, 2)
    raw_dates: list[date] = []
    d = base
    while len(raw_dates) < n_days:
        if d.weekday() < 5:  # 周一..周五
            raw_dates.append(d)
        d += timedelta(days=1)
    date_col = np.repeat(np.array(raw_dates, dtype="datetime64[D]"), universe_size)

    # 每只股票的起点 close（5~50 元），漂移 + 波动率
    start_prices = rng.uniform(5.0, 50.0, size=universe_size)
    # 几何随机游走：shape (n_days, universe)
    daily_ret = rng.normal(loc=0.0, scale=0.02, size=(n_days, universe_size))
    close_paths = start_prices[None, :] * np.exp(np.cumsum(daily_ret, axis=0))
    close_flat = close_paths.reshape(total, order="C")

    # OHLC：围绕 close 的日内区间
    spread = rng.uniform(0.0, 0.02, size=total)
    open_flat = close_flat * (1.0 - spread * rng.uniform(0, 1, size=total))
    high_flat = np.maximum(open_flat, close_flat) * (1.0 + spread)
    low_flat = np.minimum(open_flat, close_flat) * (1.0 - spread)
    # 确保 high >= open/close >= low
    high_flat = np.maximum.reduce([high_flat, open_flat, close_flat])
    low_flat = np.minimum.reduce([low_flat, open_flat, close_flat])

    volume_flat = rng.integers(100_000, 10_000_000, size=total).astype(np.float64)
    amount_flat = volume_flat * close_flat  # 近似成交额

    df = pl.DataFrame({
        "asset_id": aid_col,
        "trade_date": pl.Series(date_col).cast(pl.Date),
        "open": open_flat,
        "high": high_flat,
        "low": low_flat,
        "close": close_flat,
        "volume": volume_flat,
        "amount": amount_flat,
        "adj_factor": np.ones(total, dtype=np.float64),
        "adj_close": close_flat,  # 前复权 = 原始
        "is_suspended": np.zeros(total, dtype=bool),
        "source": "synthetic",
    })
    return df


def _load_into_duckdb(prices: pl.DataFrame) -> duckdb.DuckDBPyConnection:
    """在内存 DuckDB 创建 silver_prices_1d 并写入 prices。

    返回保持表打开的内存连接。schema 与生产 silver.sql 对齐（仅含本基准
    所需的列），以便 ``adjusted_ohlc_sql()`` 可直接复用。
    """
    con = duckdb.connect(database=":memory:")
    con.execute(
        """
        CREATE TABLE silver_prices_1d (
            asset_id     VARCHAR NOT NULL,
            trade_date   DATE    NOT NULL,
            open         DOUBLE  NOT NULL,
            high         DOUBLE  NOT NULL,
            low          DOUBLE  NOT NULL,
            close        DOUBLE  NOT NULL,
            volume       DOUBLE  NOT NULL,
            amount       DOUBLE,
            adj_factor   DOUBLE  DEFAULT 1,
            adj_close    DOUBLE,
            is_suspended BOOLEAN DEFAULT FALSE,
            limit_up     DOUBLE,
            limit_down   DOUBLE,
            source       VARCHAR NOT NULL,
            ingestion_id VARCHAR,
            PRIMARY KEY (asset_id, trade_date)
        )
        """
    )
    con.execute("CREATE INDEX idx_silver_prices_1d_date ON silver_prices_1d (trade_date)")
    # 批量注册 polars 表后 INSERT，避免 row-by-row 慢路径。显式列出列以对齐
    # 生产 schema（表含 limit_up/limit_down/ingestion_id 等可空列，合成数据不填）。
    con.register("_prices_view", prices.to_arrow())
    con.execute(
        """
        INSERT INTO silver_prices_1d
            (asset_id, trade_date, open, high, low, close, volume, amount,
             adj_factor, adj_close, is_suspended, source)
        SELECT asset_id, trade_date, open, high, low, close, volume, amount,
               adj_factor, adj_close, is_suspended, source
        FROM _prices_view
        """
    )
    con.unregister("_prices_view")
    return con


def _duckdb_load(con: duckdb.DuckDBPyConnection, start: date, end: date) -> pl.DataFrame:
    """复用生产 ``adjusted_ohlc_sql()`` 的价格加载查询，返回 polars 长表。

    与 ``BacktestRunner._load_prices`` 的核心查询等价（已去掉 universe
    过滤与 forward-fill，聚焦于纯 SELECT 路径开销）。
    """
    sql = adjusted_ohlc_sql() + " WHERE trade_date >= ? AND trade_date <= ? ORDER BY asset_id, trade_date"
    df = con.execute(sql, [start.isoformat(), end.isoformat()]).pl()
    if not df.is_empty() and df["trade_date"].dtype == pl.Utf8:
        df = df.with_columns(pl.col("trade_date").str.to_date())
    return df


def _build_target_weights(
    prices: pl.DataFrame,
    top_n: int,
) -> pl.DataFrame:
    """构造合成目标权重表喂给 FillSimulator。

    每个 rebalance 日（此处 = 每个交易日）选前 ``top_n`` 只股票，等权多仓。
    这是 engine._run_impl 主循环输出的 ``all_weights`` 列表的极简复刻——
    足以驱动 ``AShareFillSimulator.simulate`` 走完整撮合路径，而不必装配
    完整策略/catalog。
    """
    trade_dates = sorted(prices["trade_date"].unique().to_list())
    # sorted → 确定性 top-n 选择（unique(maintain_order=False) 的返回顺序
    # 逐次可变，曾导致同参数两次运行 fills 645 vs 712——基准不可复现）。
    asset_ids = sorted(prices["asset_id"].unique().to_list())[:top_n]
    weight = 1.0 / top_n

    rows: list[dict] = []
    # 信号在 T，成交在 T+1（next-bar execution，对齐 engine 约定）
    for i in range(1, len(trade_dates)):
        exec_date = trade_dates[i]
        for aid in asset_ids:
            rows.append({
                "trade_date": exec_date,
                "asset_id": aid,
                "target_weight": weight,
            })
    return pl.DataFrame(rows)


def benchmark(universe_size: int, n_days: int, seed: int = 42) -> dict:
    """对一个 (universe_size × n_days) 维度运行基准，返回指标字典。"""
    rng = np.random.default_rng(seed)

    gc.collect()
    tracemalloc.start()
    run_start = time.perf_counter()

    # ── 阶段 0：合成数据生成 + 写入 DuckDB（不计入回测关键路径，单独报告）
    t0 = time.perf_counter()
    prices_long = _build_synthetic_prices(universe_size, n_days, rng)
    con = _load_into_duckdb(prices_long)
    data_gen_time = time.perf_counter() - t0

    start_date = prices_long["trade_date"].min()
    end_date = prices_long["trade_date"].max()
    n_rows = universe_size * n_days

    # ── 阶段 1：DuckDB 价格加载（生产 _load_prices 等价路径）
    t0 = time.perf_counter()
    prices = _duckdb_load(con, start_date, end_date)
    duckdb_load_time = time.perf_counter() - t0

    # ── 阶段 2：价格矩阵 pivot（生产 _build_price_matrix 等价路径）
    engine = VectorBacktestEngine()
    t0 = time.perf_counter()
    price_matrix, _date_to_idx = engine._build_price_matrix(prices)
    pivot_time = time.perf_counter() - t0

    # ── 阶段 3：FillSimulator 撮合（生产 fill 模拟路径）
    target_weights = _build_target_weights(prices, TOP_N)
    fill_sim = AShareFillSimulator(max_volume_pct=0.1)
    t0 = time.perf_counter()
    fills_df, snapshots_df = fill_sim.simulate(
        target_weights=target_weights,
        prices=prices,
        initial_cash=INITIAL_CASH,
    )
    fill_time = time.perf_counter() - t0

    run_time = time.perf_counter() - run_start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 价格矩阵列数 = universe + 1（trade_date）；行数 = 交易日数
    matrix_rows = price_matrix.height
    matrix_cols = price_matrix.width
    n_fills = len(fills_df) if not fills_df.is_empty() else 0

    con.close()

    return {
        "universe_size": universe_size,
        "n_days": n_days,
        "n_price_rows": n_rows,
        "wall_time_s": round(run_time, 4),
        "peak_memory_mb": round(peak / (1024 * 1024), 2),
        "stages": {
            "data_gen_s": round(data_gen_time, 4),
            "duckdb_load_s": round(duckdb_load_time, 4),
            "pivot_s": round(pivot_time, 4),
            "fill_simulate_s": round(fill_time, 4),
        },
        "duckdb_query_time_s": round(duckdb_load_time, 4),
        "price_matrix_shape": [matrix_rows, matrix_cols],
        "n_fills": n_fills,
    }


def _parse_dims(tokens: list[str]) -> list[tuple[int, int]]:
    """解析 'UxD' 形式（如 '1000x250'）为 (universe, days) 元组列表。"""
    out: list[tuple[int, int]] = []
    for tok in tokens:
        if "x" not in tok.lower():
            raise ValueError(f"Invalid dimension '{tok}', expected 'UxD' e.g. '1000x250'")
        u, d = tok.lower().split("x", 1)
        out.append((int(u), int(d)))
    return out


def _print_table(results: list[dict]) -> None:
    """以控制台表格打印汇总（wall_time / peak_mem / 各阶段耗时）。"""
    hdr = f"{'universe':>8} {'days':>5} {'rows':>10} {'wall_s':>9} {'peak_MB':>9} {'load_s':>8} {'pivot_s':>8} {'fill_s':>8}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in results:
        s = r["stages"]
        print(
            f"{r['universe_size']:>8} {r['n_days']:>5} {r['n_price_rows']:>10} "
            f"{r['wall_time_s']:>9.3f} {r['peak_memory_mb']:>9.1f} "
            f"{s['duckdb_load_s']:>8.3f} {s['pivot_s']:>8.3f} {s['fill_simulate_s']:>8.3f}"
        )
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="大股票池回测性能基准（仅测量，不优化）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python scripts/benchmark_backtest.py --dims 1000x250 5000x500 --out report.json",
    )
    parser.add_argument(
        "--dims",
        nargs="+",
        metavar="UxD",
        help=(
            "测量维度，形如 '1000x250'（1000 股 × 250 日）。多个空格分隔。"
            f" 默认：{' '.join(f'{u}x{d}' for u, d in DEFAULT_DIMS)}"
        ),
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help=(
            "将完整 JSON 报告写入该路径（显式路径优先）。"
            " 未给出时写入默认路径 artifacts/benchmarks/{date}_{tag}.json。"
        ),
    )
    parser.add_argument(
        "--tag",
        default="run",
        help="默认输出文件名标签：artifacts/benchmarks/{date}_{tag}.json（默认 'run'）。",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="合成数据 RNG 种子（默认 42）。",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="启用 DEBUG 日志。",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    dims = _parse_dims(args.dims) if args.dims else list(DEFAULT_DIMS)

    print(f"运行基准：{len(dims)} 个维度 (top_n={TOP_N}, seed={args.seed})")
    results: list[dict] = []
    for universe_size, n_days in dims:
        label = f"{universe_size}x{n_days}"
        print(f"  → {label} ...", flush=True)
        res = benchmark(universe_size, n_days, seed=args.seed)
        results.append(res)
        print(
            f"      wall={res['wall_time_s']:.3f}s "
            f"peak_mem={res['peak_memory_mb']:.1f}MB "
            f"load={res['stages']['duckdb_load_s']:.3f}s "
            f"pivot={res['stages']['pivot_s']:.3f}s "
            f"fill={res['stages']['fill_simulate_s']:.3f}s "
            f"({res['n_fills']} fills)"
        )

    _print_table(results)

    report = {
        "description": "大股票池回测性能基准（仅测量，不优化）",
        "top_n": TOP_N,
        "seed": args.seed,
        "results": results,
    }
    from datetime import date as _date
    from pathlib import Path

    out_path = Path(args.out) if args.out else (
        Path("artifacts/benchmarks") / f"{_date.today().isoformat()}_{args.tag}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"JSON 报告已写入：{out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
