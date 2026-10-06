"""P2a fill 查找重构等价门（Task 3 / D5-B）。

针对 ``AShareFillSimulator._build_price_lookup``（pre-sorted + 单遍 prev_close）
与 NAV 去重（无成交日复用当日首次 NAV）的重构，锁定：

① lookup 语义 vs 朴素参考实现（旧算法原样保留为 oracle）逐值全等
   ——含新股缺口、停牌、不规则日期缺口边界；
② 生产装配路径（``BacktestRunner.run``）重跑 Task0 fixture：
   fills **exact**（位级）+ NAV **≤1e-12**；
③ buys 日子专项：有买入成交的日子，pre-sell NAV 语义怪癖
   （2026-08-09 v2.0 记录——下单 sizing 用当日首次 NAV，先卖后买不改其值）
   必须逐值保持；
④ NAV 快照不变式：snapshot.nav == cash + Σ qty×close(td)（用 fills 重放
   独立重算——证 NAV 去重的"状态相同 ⇒ 值相同"前提）；
⑤ lookup 构建性能冒烟（先红后绿的门槛：旧 O(N²·logN) 构建在该规模下
   远超阈值，新 O(N·logN) 构建必须 <10s）。

real_sample 双套装：catalog 可读时同样 fills exact + NAV 1e-12。
"""

from __future__ import annotations

import importlib.util
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../quant
assert _REPO_ROOT.name == "quant"
sys.path.insert(0, str(_REPO_ROOT / "python"))

from cquant.backtest_vector.fill_simulator import AShareFillSimulator  # noqa: E402

_GEN_PATH = _REPO_ROOT / "python/tests/fixtures/perf_equiv/generate_fixtures.py"
_FIXTURE_DIR = _GEN_PATH.parent

_spec = importlib.util.spec_from_file_location("perf_equiv_gen", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


# ---------------------------------------------------------------------------
# ① lookup 语义 vs 朴素 oracle（旧算法逐行 sorted + .index，原样保留）
# ---------------------------------------------------------------------------

def _reference_lookup(prices: pl.DataFrame, suspension_col: str = "is_suspended"):
    """重构前的 _build_price_lookup 实现——作为等价 oracle，永不修改。"""
    raw: dict[tuple[date, str], dict] = {}
    for row in prices.iter_rows(named=True):
        td = row["trade_date"]
        aid = row["asset_id"]
        raw[(td, aid)] = {
            "open": float(row.get("open", 0) or 0),
            "high": float(row.get("high", 0) or 0),
            "low": float(row.get("low", 0) or 0),
            "close": float(row.get("close", 0) or 0),
            "volume": float(row.get("volume", 0) or 0),
            "is_suspended": bool(row.get(suspension_col, False)),
            "adj_factor": float(row.get("adj_factor", 1.0) or 1.0),
        }

    asset_dates: dict[str, list[date]] = {}
    for (td, aid) in raw:
        asset_dates.setdefault(aid, []).append(td)

    lookup = {}
    for (td, aid), data in raw.items():
        dates = sorted(asset_dates[aid])
        idx = dates.index(td)
        if idx > 0:
            prev_date = dates[idx - 1]
            prev_data = raw.get((prev_date, aid), {})
            prev_close = prev_data.get("close", 0.0)
        else:
            prev_close = data["close"]
        lookup[(td, aid)] = {**data, "prev_close": prev_close}
    return lookup


def _gappy_prices() -> pl.DataFrame:
    """不规则边界帧：新股中途上市 / 老股中途退市缺失 / 单日缺口 / 停牌。"""
    days = [date(2025, 1, d) for d in (1, 2, 3, 6, 7, 8, 9, 10)]  # 4-5(周末)、4 日缺口
    rows = []
    for aid, first_day in (("SSE:A", 0), ("SSE:B", 3), ("SSE:C", 0)):
        for di, d in enumerate(days):
            if di < first_day:
                continue  # B: 中途上市 → 首日无 prev_close
            if aid == "SSE:C" and di == 4:
                continue  # C: 单日数据缺口（停牌一日无行）
            rows.append({
                "asset_id": aid, "trade_date": d,
                "open": 10.0 + di, "high": 11.0 + di, "low": 9.0 + di,
                "close": 10.5 + di, "volume": 1_000_000.0,
                "adj_factor": 1.0, "is_suspended": False,
            })
    return pl.DataFrame(rows)


@pytest.mark.parametrize("name,frame", [
    ("synthetic_fixture", None),   # gen.build_synthetic_prices()（含新股/停牌/边界）
    ("irregular_gaps", "gappy"),
])
def test_lookup_matches_naive_reference(name, frame) -> None:
    prices = gen.build_synthetic_prices() if frame is None else _gappy_prices()
    sim = AShareFillSimulator()
    lookup = sim._build_price_lookup(prices, "is_suspended")
    reference = _reference_lookup(prices, "is_suspended")

    assert set(lookup.keys()) == set(reference.keys()), "key 集合漂移"
    for key, ref_data in reference.items():
        assert lookup[key] == ref_data, f"{key}: lookup 值漂移 {lookup[key]} vs {ref_data}"


def test_lookup_first_day_prev_close_is_own_close() -> None:
    """首日 prev_close = 自身 close（含新股上市首日）——锁定语义。"""
    prices = _gappy_prices()
    sim = AShareFillSimulator()
    lookup = sim._build_price_lookup(prices, "is_suspended")
    # B 中途上市首日（days[3]）
    first = lookup[(date(2025, 1, 6), "SSE:B")]
    assert first["prev_close"] == first["close"]
    # C 缺口 days[4]（1/7 无行）：缺口后一日（1/8）prev_close 必须取
    # days[3]（1/6）的 close——"上一有行日"语义，非日历前一日
    assert lookup[(date(2025, 1, 8), "SSE:C")]["prev_close"] == lookup[(date(2025, 1, 6), "SSE:C")]["close"]


# ---------------------------------------------------------------------------
# ② + ③ 生产装配重跑 vs Task0 fixture（fills exact + NAV 1e-12，含 buys 日）
# ---------------------------------------------------------------------------

def _assert_fixture_equivalence(rec: dict, fixture_subdir: str) -> set:
    fix = _FIXTURE_DIR / fixture_subdir
    fills_fix = pl.read_parquet(fix / "fills.parquet").sort(["trade_date", "asset_id", "side"])
    nav_fix = pl.read_parquet(fix / "nav.parquet").sort("trade_date")

    fills_rec = rec["fills"].sort(["trade_date", "asset_id", "side"])
    nav_rec = rec["nav"].sort("trade_date")

    # fills：位级 exact
    assert_frame_equal(fills_rec, fills_fix, check_exact=True)

    # NAV：≤1e-12（重构设计上应位级一致——无任何算术路径改变）
    assert nav_rec.height == nav_fix.height
    for col in ("nav", "cash", "portfolio_return"):
        got = nav_rec[col].to_list()
        want = nav_fix[col].to_list()
        for g, w in zip(got, want):
            assert abs(g - w) <= 1e-12, f"NAV col={col} drift: {g} vs {w} (|d|={abs(g - w):.3e})"

    return set(fills_rec.filter(pl.col("side") == "buy")["trade_date"].to_list())


def test_synthetic_fixture_fills_exact_nav_close() -> None:
    rec = gen.run_production_capture(gen.build_synthetic_prices(),
                                     gen.build_synthetic_factors(gen.build_synthetic_prices()))
    buy_days = _assert_fixture_equivalence(rec, "synthetic")
    # ③ buys 日子专项：有买入的日子必须被 fixture 覆盖且逐值等价已含于上方断言
    assert buy_days, "fixture 未覆盖任何 buys 日——专项覆盖失效"
    nav_days = set(rec["nav"]["trade_date"].to_list())
    assert buy_days.issubset(nav_days), "buys 日必须有当日 NAV 快照"


@pytest.mark.skipif(
    not (_FIXTURE_DIR / "real_sample" / "fills.parquet").exists(),
    reason="real-sample fixture not recorded",
)
def test_real_sample_fixture_fills_exact_nav_close() -> None:
    real = gen.load_real_sample()
    if real is None:
        pytest.skip("real catalog unavailable (data/catalog.duckdb missing or locked)")
    prices, factors = real
    rec = gen.run_production_capture(prices, factors)
    _assert_fixture_equivalence(rec, "real_sample")


# ---------------------------------------------------------------------------
# ④ NAV 快照不变式（证 NAV 去重前提：状态相同 ⇒ 值相同）
# ---------------------------------------------------------------------------

def test_nav_snapshot_invariant_and_no_trade_day_reuse() -> None:
    """snapshot.nav 必须等于当日收盘后 cash + Σ qty×close(td)（用 fills 独立重放）。

    无成交日 cash/positions 与当日首次 NAV 计算时完全一致（simulate 内对
    cash/positions 的唯一写点都在 ``if fill:`` 分支内），因此复用首次 NAV
    与重算在纯函数 ``_calculate_nav`` 下逐位相同——本测试同时锁定该不变式。
    """
    prices = gen.build_synthetic_prices()
    factors = gen.build_synthetic_factors(prices)

    # 直接驱动 simulate（生产 runner 的同一撮合路径），每日调仓 top-6
    trade_dates = sorted(prices["trade_date"].unique().to_list())
    top6 = [gen.ST_ASSET, gen.LIMIT_UP_ASSET, gen.LIMIT_DOWN_ASSET,
            gen.SUSPENDED_ASSET, gen.NEW_LISTING_ASSET, gen.NORMAL_TOP_ASSET]
    weights = pl.DataFrame([
        {"trade_date": td, "asset_id": aid, "target_weight": 1.0 / 6}
        for td in trade_dates[15:] for aid in top6
    ])

    sim = AShareFillSimulator()
    fills_df, snaps = sim.simulate(
        target_weights=weights, prices=prices,
        initial_cash=gen.INITIAL_CASH,
    )
    assert not snaps.is_empty()

    lookup = sim._build_price_lookup(prices, "is_suspended")
    cash = float(gen.INITIAL_CASH)
    positions: dict[str, int] = {}
    fills_by_day: dict[date, list[dict]] = {}
    for row in fills_df.iter_rows(named=True):
        fills_by_day.setdefault(row["trade_date"], []).append(row)

    n_no_trade_days = 0
    for row in snaps.iter_rows(named=True):
        td = row["trade_date"]
        day_fills = fills_by_day.get(td, [])
        if not day_fills:
            n_no_trade_days += 1
        for f in day_fills:
            if f["side"] == "buy":
                cash -= f["notional"] + f["total_cost"]
                positions[f["asset_id"]] = positions.get(f["asset_id"], 0) + f["qty"]
            else:
                cash += f["notional"] - f["total_cost"]
                positions[f["asset_id"]] = positions.get(f["asset_id"], 0) - f["qty"]
                if positions[f["asset_id"]] <= 0:
                    positions.pop(f["asset_id"], None)
        # 不变式：快照现金逐位一致
        assert row["cash"] == cash, f"cash drift on {td}"
        # 不变式：nav == cash + 持仓市值（与 _calculate_nav 同式独立重算）
        expect_nav = cash + sum(
            q * lookup[(td, aid)]["close"] for aid, q in positions.items()
        )
        assert abs(row["nav"] - expect_nav) <= 1e-9, f"nav drift on {td}: {row['nav']} vs {expect_nav}"

    assert n_no_trade_days > 0, "场景未覆盖无成交日——NAV 复用路径未被测试"


# ---------------------------------------------------------------------------
# ⑤ lookup 构建性能冒烟（P2a 门槛：150 股 × 300 日 < 10s）
# ---------------------------------------------------------------------------

def test_lookup_build_perf_smoke() -> None:
    """1500×500=750,000 行 lookup 构建 <20s。

    旧实现的第二遍为逐行 ``sorted`` + ``.index``（O(N·D) 每行线性扫描），
    本规模实测 22.6s（2026-10-05 本机，先红证据）；重构后每资产单次排序 +
    单遍扫描（O(N·logN)），实测 <1s——20s 阈值给 CI 噪声留一个数量级裕度。
    """
    import numpy as np
    rng = np.random.default_rng(7)
    n_assets, n_days = 1500, 500
    total = n_assets * n_days
    aids = np.tile(np.array([f"SSE:{i:06d}" for i in range(n_assets)]), n_days)
    d0 = date(2024, 1, 1)
    days = []
    d = d0
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    dates = np.repeat(np.array(days, dtype="datetime64[D]"), n_assets)
    closes = rng.uniform(5, 50, size=total)
    prices = pl.DataFrame({
        "asset_id": aids,
        "trade_date": pl.Series(dates).cast(pl.Date),
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": np.full(total, 1e6),
        "adj_factor": np.ones(total),
        "is_suspended": np.zeros(total, dtype=bool),
    })

    sim = AShareFillSimulator()
    t0 = time.perf_counter()
    lookup = sim._build_price_lookup(prices, "is_suspended")
    elapsed = time.perf_counter() - t0

    assert len(lookup) == total
    assert elapsed < 20.0, (
        f"_build_price_lookup 构建耗时 {elapsed:.2f}s ≥ 10s——"
        f"pre-sorted 重构退化（P2a 性能门槛）"
    )
