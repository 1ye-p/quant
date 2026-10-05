"""B4b-1 读侧：GET /backtests/{run_id}/positions-series 端点单测.

双 metric 形态：
- weight   → [{trade_date, positions: [{asset_id, weight, industry}]}]，
  每日 top_n 聚合（权重 Top N + 其余合并 ``__other__``，other 权重=余量和）；
  industry 经 silver_assets 左连接（无映射 → None）。
- industry → 按 industry 求和的堆叠序列（每日 {industry: sum(weight)}），
  空 industry 归一为 ``'未知'``（后端归一，前端 i18n 展示）。

404 判定顺序（body 为 ``{"reason": ...}``）：
1. run_not_found      — gold_backtest_runs 无此 run
2. no_position_data   — run 存在但 gold_positions 无行（旧 run）

夹具：tmp catalog 直接插 gold_backtest_runs / gold_positions /
silver_assets 三表行（写侧由 backtest runner 负责，此处只验证读侧）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cquant.api_server import deps
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

RUN_ID = "run_pos_series_unit"


def _insert_run(cat: Catalog, run_id: str = RUN_ID) -> None:
    cat.execute(
        "INSERT INTO gold_backtest_runs "
        "(run_id, engine, strategy_id, dataset_version, started_at, status) "
        f"VALUES ('{run_id}', 'vector', 'strat', 'v1', now(), 'completed')"
    )


def _insert_assets(cat: Catalog, industries: dict[str, str | None]) -> None:
    """industries: {asset_id: industry}; None → 列保持 NULL。"""
    for asset_id, industry in industries.items():
        ind_sql = "NULL" if industry is None else f"'{industry}'"
        cat.execute(
            "INSERT INTO silver_assets "
            "(asset_id, symbol, exchange, asset_class, currency, industry, "
            " effective_from, updated_at) "
            f"VALUES ('{asset_id}', 'sym', 'SSE', 'stock', 'CNY', {ind_sql}, "
            " '2020-01-01', now())"
        )


def _insert_positions(
    cat: Catalog,
    rows: list[tuple[str, str, float]],
    run_id: str = RUN_ID,
) -> None:
    """rows: (trade_date, asset_id, weight)。"""
    for trade_date, asset_id, weight in rows:
        cat.execute(
            "INSERT INTO gold_positions (run_id, trade_date, asset_id, weight) "
            f"VALUES ('{run_id}', '{trade_date}', '{asset_id}', {weight})"
        )


@pytest.fixture()
def pos_catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # data/backtest_artifacts CWD-relative
    cat = Catalog(db_path=tmp_path / "pos_series_unit.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    return cat


@pytest.fixture()
def client(pos_catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: pos_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, pos_catalog
    app.dependency_overrides = {}


def _get(c: TestClient, run_id: str = RUN_ID, metric: str = "weight", top_n: int = 10):
    return c.get(f"/api/v1/backtests/{run_id}/positions-series",
                 params={"metric": metric, "top_n": top_n})


def test_weight_metric_shape_and_industry_join(client) -> None:
    """weight 形态：trade_date 升序、industry 经 silver_assets 左连接。"""
    c, cat = client
    _insert_run(cat)
    _insert_assets(cat, {"SSE:600036": "Finance", "SSE:600519": "Consumer"})
    _insert_positions(cat, [
        ("2025-01-03", "SSE:600036", 0.6),
        ("2025-01-03", "SSE:600519", 0.4),
        # 乱序插入第二个日期，验证升序输出
        ("2025-01-02", "SSE:600036", 0.5),
        ("2025-01-02", "SSE:600519", 0.5),
    ])

    resp = _get(c, metric="weight")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "weight"
    assert body["top_n"] == 10
    series = body["series"]
    assert [p["trade_date"] for p in series] == ["2025-01-02", "2025-01-03"]
    day1 = series[0]["positions"]
    by_asset = {p["asset_id"]: p for p in day1}
    assert by_asset["SSE:600036"]["industry"] == "Finance"
    assert by_asset["SSE:600036"]["weight"] == pytest.approx(0.5)


def test_weight_metric_no_asset_mapping_industry_null(client) -> None:
    """silver_assets 无映射行 → industry 为 None（不 500）。"""
    c, cat = client
    _insert_run(cat)
    _insert_positions(cat, [("2025-01-03", "SSE:NOPE", 1.0)])

    resp = _get(c, metric="weight")
    assert resp.status_code == 200, resp.text
    pos = resp.json()["series"][0]["positions"]
    assert pos[0]["asset_id"] == "SSE:NOPE"
    assert pos[0]["industry"] is None


def test_weight_top_n_aggregation(client) -> None:
    """11 只持仓 top_n=5 → 5 + __other__（other 权重=余量和）。"""
    c, cat = client
    _insert_run(cat)
    # 前 5 权重 0.15，其余 6 只各 0.0416666...
    inserts = []
    for i in range(11):
        w = 0.15 if i < 5 else (0.25 / 6)
        inserts.append(("2025-01-03", f"SSE:A{i:02d}", w))
    _insert_positions(cat, inserts)

    resp = _get(c, metric="weight", top_n=5)
    assert resp.status_code == 200, resp.text
    positions = resp.json()["series"][0]["positions"]
    assert len(positions) == 6
    top_ids = {p["asset_id"] for p in positions if p["asset_id"] != "__other__"}
    assert top_ids == {f"SSE:A{i:02d}" for i in range(5)}
    other = next(p for p in positions if p["asset_id"] == "__other__")
    assert other["weight"] == pytest.approx(0.25, abs=1e-9)


def test_weight_top_n_exactly_n_no_other(client) -> None:
    """持仓数 == top_n → 不产生 __other__ 条目。"""
    c, cat = client
    _insert_run(cat)
    _insert_positions(cat, [
        ("2025-01-03", "SSE:600036", 0.5),
        ("2025-01-03", "SSE:600519", 0.5),
    ])
    resp = _get(c, metric="weight", top_n=2)
    assert resp.status_code == 200, resp.text
    positions = resp.json()["series"][0]["positions"]
    assert len(positions) == 2
    assert all(p["asset_id"] != "__other__" for p in positions)


def test_industry_metric_stacked_and_unknown(client) -> None:
    """industry 形态：按 industry 求和堆叠；空 industry 归 '未知'。"""
    c, cat = client
    _insert_run(cat)
    _insert_assets(cat, {
        "SSE:600036": "Finance",
        "SSE:600519": "Consumer",
        "SSE:NOIND": None,
    })
    _insert_positions(cat, [
        ("2025-01-03", "SSE:600036", 0.4),
        ("2025-01-03", "SSE:600519", 0.3),
        ("2025-01-03", "SSE:NOIND", 0.3),
    ])

    resp = _get(c, metric="industry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "industry"
    day = body["series"][0]
    assert day["trade_date"] == "2025-01-03"
    weights = day["weights"]
    assert weights["Finance"] == pytest.approx(0.4)
    assert weights["Consumer"] == pytest.approx(0.3)
    assert weights["未知"] == pytest.approx(0.3)


def test_industry_metric_unmapped_asset_is_unknown(client) -> None:
    """silver_assets 无映射（industry NULL）同样归 '未知'。"""
    c, cat = client
    _insert_run(cat)
    _insert_positions(cat, [("2025-01-03", "SSE:GHOST", 1.0)])
    resp = _get(c, metric="industry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["series"][0]["weights"] == {"未知": pytest.approx(1.0)}


def test_industry_metric_dates_ascending(client) -> None:
    c, cat = client
    _insert_run(cat)
    _insert_positions(cat, [
        ("2025-06-02", "SSE:600036", 1.0),
        ("2025-01-02", "SSE:600036", 1.0),
        ("2025-03-03", "SSE:600036", 1.0),
    ])
    resp = _get(c, metric="industry")
    assert resp.status_code == 200, resp.text
    dates = [d["trade_date"] for d in resp.json()["series"]]
    assert dates == sorted(dates) == ["2025-01-02", "2025-03-03", "2025-06-02"]


def test_404_run_not_found(client) -> None:
    c, _ = client
    resp = _get(c, "run_does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "run_not_found"


def test_404_no_position_data(client) -> None:
    """旧 run（gold_positions 无行）→ no_position_data。"""
    c, cat = client
    _insert_run(cat)
    resp = _get(c)
    assert resp.status_code == 404
    assert resp.json()["reason"] == "no_position_data"


def test_422_invalid_metric(client) -> None:
    c, _ = client
    assert _get(c, metric="bogus").status_code == 422


def test_422_top_n_out_of_range(client) -> None:
    c, _ = client
    assert _get(c, top_n=0).status_code == 422
    assert _get(c, top_n=51).status_code == 422
