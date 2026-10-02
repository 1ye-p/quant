"""P2-7 e2e smoke（硬验收）：builtin 启用 → 刷新入库 → regime 回测真实消费。

B1 教训制度化："实现正确、接线断裂、单测全绿"。本文件证明全链贯通且
全程走生产装配路径：

    POST /builtins/{key}/enable（TestClient，真路由）
      → _run_builtin_backfill → run_external_indicator_refresh（回填）
      → run_external_indicator_refresh（增量，5 日重叠窗口）
      → BacktestRunner.run（run.py 生产装配，regime 状态机经
        _regime_sm_for_strategy → MarketSeriesContext → external_loader
        PIT 读取 silver_external_indicators）

唯一打桩点：refresh 模块的 adapters 注入口（AkshareIndicatorAdapter /
TushareIndicatorAdapter 模块属性替换为假 adapter）——零真实网络，
akshare/tushare 永不会被 import 到调用层面。

假 adapter 的合成序列确定性触发 risk_off：基准 +1.0，中段 [150, 209)
共 60 日 −1.0，DSL switch 阈值 0 一分两侧。宽窗口（回填）截断尾部
10 日模拟源端发布延迟 → 增量刷新必须补入这 10 行（行数增长断言）。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
from cquant.backtest_vector.run import BacktestRunSpec, BacktestRunner
from cquant.datahub.catalog import Catalog
from cquant.datahub.pipelines.indicator_sources import refresh as refresh_mod
from cquant.datahub.pipelines.indicator_sources.adapters import (
    IndicatorFetchError,
)
from cquant.datahub.pipelines.indicator_sources.refresh import (
    run_external_indicator_refresh,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.integration

N_DAYS = 500
START = date(2024, 1, 1)
DATES = [START + timedelta(days=i) for i in range(N_DAYS)]
ANCHOR = DATES[-1]

ASSETS = ["SSE:600036", "SSE:000001"]
FEATURE_SET = "fsv_extind_e2e"
KEY = "north_net_buy"  # builtin 注册表真实 key，candidates=("akshare",)

# 合成序列：+1.0 基准，[LO, HI) 区间 −1.0 → 确定性 risk_off
RISK_OFF_LO, RISK_OFF_HI = 150, 210
REGIME_SCALE_OFF = 0.5
SERIES: list[tuple[date, float]] = [
    (d, -1.0 if RISK_OFF_LO <= i < RISK_OFF_HI else 1.0)
    for i, d in enumerate(DATES)
]

# 回填（宽窗口）截断尾部 SOURCE_LAG_DAYS 日 → 增量必须补入
SOURCE_LAG_DAYS = 10
WIDE_WINDOW_DAYS = 30  # (end−start) 超过此值视为回填窗口
BACKFILL_ROWS = N_DAYS - SOURCE_LAG_DAYS

FAKE_SOURCE_NAME = "fake_akshare_e2e"

DSL_SPEC: dict = {
    "name": "extind_regime_e2e",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight"},
    "regime": {
        "mode": "switch",
        "initial": "risk_on",
        "indicators": {"north": KEY},
        "states": [
            {"name": "risk_on", "enter_when": "north > 0", "position_scale": 1.0},
            {
                "name": "risk_off",
                "enter_when": "north <= 0",
                "position_scale": REGIME_SCALE_OFF,
            },
        ],
    },
}


class FakeSeriesAdapter:
    """按 [start, end] 切片合成序列；宽窗口（回填）截断尾部模拟发布延迟。

    name 故意不叫 'akshare'：refresh_log/目录 source_name 断言落的是假
    adapter 名，证明数据确实来自本桩而非任何真源。
    """

    name = FAKE_SOURCE_NAME

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def fetch(self, indicator_key: str, start: date, end: date) -> pl.DataFrame:
        self.calls.append(
            {"key": indicator_key, "start": start, "end": end}
        )
        if self.error is not None:
            raise self.error
        rows = [(d, v) for d, v in SERIES if start <= d <= end]
        if (end - start).days > WIDE_WINDOW_DAYS:
            lag_cutoff = ANCHOR - timedelta(days=SOURCE_LAG_DAYS)
            rows = [(d, v) for d, v in rows if d <= lag_cutoff]
        if not rows:
            raise IndicatorFetchError(
                f"fake adapter: empty slice [{start}, {end}]"
            )
        return pl.DataFrame(
            {
                "trade_date": [r[0] for r in rows],
                "value": [r[1] for r in rows],
            },
            schema={"trade_date": pl.Date, "value": pl.Float64},
        )


def _fail_if_fetched(self, indicator_key: str, start: date, end: date) -> pl.DataFrame:
    raise AssertionError(
        f"tushare fake fetched ({indicator_key}) — north_net_buy candidates "
        "are akshare-only; tushare must never be reached"
    )


@pytest.fixture()
def e2e_catalog(tmp_path, monkeypatch):
    """行情 + 因子 + 假 adapter 注入（refresh 模块 adapters 注入口）。"""
    monkeypatch.chdir(tmp_path)  # data/backtest_artifacts CWD-relative
    cat = Catalog(db_path=tmp_path / "extind_e2e.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    conn = cat._get_conn()

    rng = np.random.default_rng(11)
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

    # factor 'mom'：确定性 per-asset level → 稳定 top-1 排序
    fac_rows = [
        {"feature_set_version": FEATURE_SET, "factor_name": "mom",
         "trade_date": d, "asset_id": a, "value": 1.0 if a == ASSETS[0] else 0.0}
        for d in DATES for a in ASSETS
    ]
    dff = pl.DataFrame(fac_rows)
    conn.register("_fac", dff.to_arrow())
    conn.execute(
        "INSERT INTO gold_factor_values "
        "(feature_set_version, factor_name, trade_date, asset_id, value) "
        "SELECT feature_set_version, factor_name, trade_date, asset_id, value FROM _fac"
    )
    conn.unregister("_fac")

    # 假 adapter 注入 refresh 模块注入口：enable 端点内部
    # _run_builtin_backfill → run_external_indicator_refresh(adapters=None)
    # 会用模块属性构造默认 adapters —— 全部替换，真 adapter 构造即失败
    fake = FakeSeriesAdapter()
    monkeypatch.setattr(refresh_mod, "AkshareIndicatorAdapter", lambda: fake)
    class _TushareFake:
        name = "fake_tushare_e2e"
        fetch = _fail_if_fetched

    monkeypatch.setattr(refresh_mod, "TushareIndicatorAdapter", _TushareFake)
    cat.fake_adapter = fake  # type: ignore[attr-defined]
    return cat


@pytest.fixture()
def client(e2e_catalog, monkeypatch):
    # 认证 fixture 沿 test_ext_indicator_catalog_api.py 模式：
    # dev 模式 + 无 key（conda 环境可能注入 CQUANT_API_KEY，先摘掉）
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: e2e_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


def _data_count(cat: Catalog, key: str) -> int:
    return cat.query(
        "SELECT COUNT(*) AS n FROM silver_external_indicators WHERE indicator_key = ?",
        [key],
    ).item(0, "n")


def _catalog_row(cat: Catalog, key: str) -> dict:
    return cat.query(
        "SELECT source_type, enabled, source_name, last_status, last_error "
        "FROM silver_external_indicator_catalog WHERE indicator_key = ?",
        [key],
    ).row(0, named=True)


def _run_regime_backtest(cat: Catalog) -> str:
    runner = BacktestRunner(cat)
    return runner.run(BacktestRunSpec(
        dataset_version="v1",
        strategy_id="extind_regime_e2e",
        start_date=DATES[50],
        end_date=ANCHOR,
        feature_set_version=FEATURE_SET,
        strategy_type="DSL",
        dsl_spec=DSL_SPEC,
        top_n=1,
        initial_cash=Decimal("100000"),
        tags={"dsl_spec": DSL_SPEC, "top_n": 1},
    ))


# ── 测试 1：builtin 启用 → 回填 → 增量 → regime 真实消费 ────────────────────


def test_builtin_indicator_to_regime_backtest_e2e(e2e_catalog, client) -> None:
    cat = e2e_catalog
    fake = cat.fake_adapter

    # 1. enable（真路由）→ 目录行 builtin/enabled + 回填摘要 ok
    resp = client.post(
        "/api/v1/datasets/external-indicators/builtins"
        f"/{KEY}/enable",
        json={"backfill_start": DATES[0].isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_type"] == "builtin"
    assert body["enabled"] is True
    assert body["backfill"]["results"][0]["status"] == "ok", body["backfill"]

    row = _catalog_row(cat, KEY)
    assert row["source_type"] == "builtin"
    assert row["enabled"] is True
    assert row["last_status"] == "ok"

    # 2. 回填入库：宽窗口 → 尾部 SOURCE_LAG_DAYS 日截断
    n_backfill = _data_count(cat, KEY)
    assert n_backfill == BACKFILL_ROWS, (
        f"backfill inserted {n_backfill} rows, expected {BACKFILL_ROWS} "
        "(full series minus simulated source lag)"
    )
    # enable 路径确实经过假 adapter（而非任何真源）
    assert fake.calls and fake.calls[0]["start"] == DATES[0]
    assert fake.calls[0]["end"] == ANCHOR

    # 3. 增量刷新：窗口 = max(trade_date) − 4 .. 锚定日，补入滞后 10 行
    fake.calls.clear()
    summary = run_external_indicator_refresh(
        cat, keys=[KEY], adapters={"akshare": fake}, inter_source_delay=0
    )
    assert summary.results[0].status == "ok", summary.results[0].error
    assert summary.results[0].source == FAKE_SOURCE_NAME
    assert fake.calls, "incremental refresh never reached the adapter"
    assert fake.calls[0]["start"] == ANCHOR - timedelta(
        days=SOURCE_LAG_DAYS + 4
    ), f"incremental window must be max(trade_date)−4, got {fake.calls[0]}"
    assert fake.calls[0]["end"] == ANCHOR

    # 4. 目录/数据/日志三面断言
    assert _catalog_row(cat, KEY)["last_status"] == "ok"
    n_total = _data_count(cat, KEY)
    assert n_total == N_DAYS, (
        f"incremental refresh grew table to {n_total}, expected {N_DAYS} "
        "(backfill + lagged tail must union to the full series)"
    )
    assert n_total > n_backfill, "incremental refresh inserted no new rows"
    log = cat.query(
        "SELECT source_name, status FROM silver_external_indicator_refresh_log "
        "WHERE indicator_key = ? AND status = 'ok' ORDER BY run_id DESC LIMIT 1",
        [KEY],
    ).row(0, named=True)
    assert log["source_name"] == FAKE_SOURCE_NAME
    assert log["status"] == "ok"

    # 5. regime 回测——走 run.py 生产装配，指标由 PIT loader 真实消费
    run_id = _run_regime_backtest(cat)
    hist_path = Path("data/backtest_artifacts") / f"{run_id}_regime.parquet"
    assert hist_path.exists(), f"regime artifact missing: {hist_path}"
    hist = pl.read_parquet(hist_path)

    assert not hist.is_empty(), (
        "regime_scale_history empty — builtin indicator never reached the "
        "regime state machine through the production backtest path"
    )
    assert {"trade_date", "desired_scale", "actual_scale"}.issubset(hist.columns)

    off = hist.filter(
        (hist["desired_scale"] - REGIME_SCALE_OFF).abs() < 1e-9
    )
    assert not off.is_empty(), (
        "risk_off never triggered — synthetic series (60-day −1.0 stretch) "
        "not consumed by regime evaluation"
    )
    de_risked = off.filter(off["actual_scale"] < 0.95)
    assert not de_risked.is_empty(), (
        "desired_scale=0.5 recorded but gross exposure never de-risked — "
        "refresh→regime wiring is broken (B1 pattern: data lands, "
        "consumption silently dead)"
    )

    # 6. 降杠杆真实执行：首次 risk_off 后出现 SELL fills
    first_off = off["trade_date"].min()
    fills = cat.query(
        "SELECT trade_date, side FROM gold_fills "
        "WHERE run_id = ? AND side = 'sell' AND trade_date >= ?",
        [run_id, first_off],
    )
    assert not fills.is_empty(), (
        f"no sell fills on/after first risk_off date {first_off} — "
        "de-risking never executed"
    )


# ── 测试 2：刷新失败必须在目录可见（不静默）──────────────────────────────────


def test_refresh_failure_visible_in_catalog(e2e_catalog, client) -> None:
    cat = e2e_catalog
    # 先 enable（ok 假 adapter）建立正常基线
    resp = client.post(
        "/api/v1/datasets/external-indicators/builtins"
        f"/{KEY}/enable",
        json={"backfill_start": DATES[0].isoformat()},
    )
    assert resp.status_code == 200, resp.text

    # 换成抛错假 adapter → 增量刷新失败
    bad = FakeSeriesAdapter(error=IndicatorFetchError("source exploded"))
    summary = run_external_indicator_refresh(
        cat, keys=[KEY], adapters={"akshare": bad}, inter_source_delay=0
    )
    assert summary.results[0].status == "error"
    assert "source exploded" in (summary.results[0].error or "")

    row = _catalog_row(cat, KEY)
    assert row["last_status"] == "error"
    assert row["last_error"], "failure silently swallowed — last_error empty"

    log = cat.query(
        "SELECT status, error FROM silver_external_indicator_refresh_log "
        "WHERE indicator_key = ? ORDER BY run_id DESC LIMIT 1",
        [KEY],
    ).row(0, named=True)
    assert log["status"] == "error"
    assert "source exploded" in log["error"]
