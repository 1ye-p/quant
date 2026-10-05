"""A4/D7-A: DSL universe 字段接线（backtests 路由解析优先级）。

优先级：``body.universe_id``（显式）> ``dsl_spec.universe``（非 "all" 时作为
引擎 preset 名直达 ``resolve_universe``）> ``parsed["universe_id"]``（默认
"all"）。消除 dsl_spec.universe「只校验未消费」的死字段。

另覆盖：
- "all" 路径与既有行为 bit-for-bit 一致（legacy 组合矩阵对照断言）；
- 无效 preset 名：沿 ``resolve_universe`` 既有行为（未知 preset 走默认
  全市场分支，等同 "all"），路由不做额外拦截。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
import cquant.api_server.routes.backtests as bt_routes
from cquant.api_server.app import app
from cquant.backtest_vector.universe import resolve_universe
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

# ── 单元：_resolve_request_universe 优先级 ─────────────────────────────────


class TestResolveRequestUniverse:
    def test_dsl_preset_used_when_body_is_all(self) -> None:
        assert (
            bt_routes._resolve_request_universe("all", {"universe": "sse"}, {})
            == "sse"
        )

    def test_body_explicit_wins_over_dsl(self) -> None:
        assert (
            bt_routes._resolve_request_universe("szse", {"universe": "sse"}, {})
            == "szse"
        )

    def test_all_bit_for_bit_with_legacy_expression(self) -> None:
        """dsl_spec 无非 "all" universe 时，与旧表达式逐组合一致。"""
        legacy_bodies = ["all", "kcb"]
        legacy_parseds = [{}, {"universe_id": "sse"}, {"universe_id": "all"}]
        dsl_specs = [None, {}, {"universe": "all"}, {"score": []}]
        for body_u in legacy_bodies:
            for parsed in legacy_parseds:
                for dsl in dsl_specs:
                    legacy = (
                        body_u if body_u != "all" else parsed.get("universe_id", "all")
                    )
                    assert (
                        bt_routes._resolve_request_universe(body_u, dsl, parsed)
                        == legacy
                    ), (body_u, parsed, dsl)

    def test_invalid_preset_passthrough(self) -> None:
        """无效 preset 名原样传入 resolve_universe（沿既有默认分支行为）。"""
        assert (
            bt_routes._resolve_request_universe("all", {"universe": "nope"}, {})
            == "nope"
        )


class TestResolveUniverseInvalidPreset:
    def test_unknown_preset_behaves_like_all(self) -> None:
        cat = MagicMock()
        calls: list[tuple[str, list | None]] = []

        def mock_query(sql, params=None):
            calls.append((sql, params))
            return pl.DataFrame({"asset_id": ["SSE:600000"]})

        cat.query.side_effect = mock_query
        known = resolve_universe(cat, "all")
        invalid = resolve_universe(cat, "nope")
        assert known == invalid == ["SSE:600000"]
        # 同一默认全市场 SQL、无绑定参数
        assert calls[0] == calls[1]


# ── 路由：POST /backtests spec.universe_id 接线 ────────────────────────────

_DSL_SPEC = {
    "name": "uni_wire",
    "universe": "sse",
    "frequency": "daily",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight", "params": {}, "constraints": {}},
    "risk": [],
}


@pytest.fixture()
def wiring_catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cat = Catalog(db_path=tmp_path / "uni_wire.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    cat.execute(
        "INSERT INTO meta_strategy_configs "
        "(strategy_id, config_format, config_text, parsed_config, created_at, updated_at) "
        "VALUES ('uni_wire', 'json', '{}', "
        f"'{json.dumps({'strategy_type': 'DSL', 'dsl_spec': _DSL_SPEC})}', now(), now())"
    )
    return cat


@pytest.fixture()
def client(wiring_catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: wiring_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, wiring_catalog
    app.dependency_overrides = {}


def _post_capture(c, monkeypatch, **overrides) -> str:
    """POST /backtests 并捕获传入 _run_backtest 的 spec.universe_id。"""
    captured: dict[str, str] = {}
    real_run = bt_routes._run_backtest

    def fake_run(catalog, spec):
        captured["universe_id"] = spec.universe_id
        return "run-captured"

    monkeypatch.setattr(bt_routes, "_run_backtest", fake_run)
    try:
        resp = c.post("/api/v1/backtests", json={
            "strategy_id": "uni_wire",
            "dataset_version": "v1",
            "start_date": "2025-01-06",
            "end_date": "2025-03-31",
            "feature_set_version": "fsv_wire",
            "strategy_type": "DSL",
            **overrides,
        })
        assert resp.status_code == 201, resp.text
    finally:
        monkeypatch.setattr(bt_routes, "_run_backtest", real_run)
    return captured["universe_id"]


class TestBacktestRouteUniverseWiring:
    def test_dsl_universe_sse_wired_to_spec(self, client, monkeypatch) -> None:
        c, _ = client
        assert _post_capture(c, monkeypatch, dsl_spec=dict(_DSL_SPEC)) == "sse"

    def test_dsl_universe_all_bit_for_bit(self, client, monkeypatch) -> None:
        c, _ = client
        spec = dict(_DSL_SPEC, universe="all")
        assert _post_capture(c, monkeypatch, dsl_spec=spec) == "all"

    def test_body_universe_id_overrides_dsl(self, client, monkeypatch) -> None:
        c, _ = client
        assert _post_capture(
            c, monkeypatch, dsl_spec=dict(_DSL_SPEC), universe_id="szse"
        ) == "szse"

    def test_invalid_preset_passed_through(self, client, monkeypatch) -> None:
        c, _ = client
        spec = dict(_DSL_SPEC, universe="nope")
        assert _post_capture(c, monkeypatch, dsl_spec=spec) == "nope"

    def test_saved_config_dsl_spec_universe_used(
        self, client, monkeypatch
    ) -> None:
        """dsl_spec 未随 body 传入时，落库策略配置中的 dsl_spec.universe 生效。"""
        c, _ = client
        assert _post_capture(c, monkeypatch) == "sse"
