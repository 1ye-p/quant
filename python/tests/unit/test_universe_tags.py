"""F1: universe_resolved / universe_override run tags（回测可复现性留痕）。

修复的回归：zz500 DSL 策略被 body 显式 hs300 覆盖实跑 hs300，tags 无任何
痕迹。本批只加**可见性**（tags），不改 `_resolve_request_universe` 的优先级
语义（见 test_dsl_universe_wiring.py —— ``test_body_explicit_wins_over_dsl``
固化的冲突行为保持不变）。

规则：
- ``universe_resolved``：**无条件**写入（与 DSL 一致/无 DSL/纯 body 均记）；
- ``universe_override``：仅当 dsl_spec 存在、其 universe 非 "all"、且
  resolved ≠ dsl universe 时写入，形如 ``"{dsl}->{resolved}"``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
import cquant.api_server.routes.backtests as bt_routes
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

_DSL_SPEC = {
    "name": "uni_tags",
    "universe": "sse",
    "frequency": "daily",
    "score": [{"factor": "mom", "weight": 1.0}],
    "position": {"method": "equal_weight", "params": {}, "constraints": {}},
    "risk": [],
}


@pytest.fixture()
def tags_catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cat = Catalog(db_path=tmp_path / "uni_tags.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    cat.execute(
        "INSERT INTO meta_strategy_configs "
        "(strategy_id, config_format, config_text, parsed_config, created_at, updated_at) "
        "VALUES ('uni_tags_dsl', 'json', '{}', "
        f"'{json.dumps({'strategy_type': 'DSL', 'dsl_spec': _DSL_SPEC})}', now(), now())"
    )
    cat.execute(
        "INSERT INTO meta_strategy_configs "
        "(strategy_id, config_format, config_text, parsed_config, created_at, updated_at) "
        "VALUES ('uni_tags_plain', 'json', '{}', "
        "'{\"factors\": [\"mom\"], \"top_n\": 5}', now(), now())"
    )
    return cat


@pytest.fixture()
def client(tags_catalog, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    app.dependency_overrides[deps.get_catalog] = lambda: tags_catalog
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, tags_catalog
    app.dependency_overrides = {}


def _post_capture_tags(c, monkeypatch, strategy_id: str, **overrides) -> dict:
    """POST /backtests 并捕获传入 _run_backtest 的 spec.tags。"""
    captured: dict[str, dict] = {}
    real_run = bt_routes._run_backtest

    def fake_run(catalog, spec):
        captured["tags"] = dict(spec.tags)
        return "run-captured"

    monkeypatch.setattr(bt_routes, "_run_backtest", fake_run)
    try:
        resp = c.post("/api/v1/backtests", json={
            "strategy_id": strategy_id,
            "dataset_version": "v1",
            "start_date": "2025-01-06",
            "end_date": "2025-03-31",
            "feature_set_version": "fsv_tags",
            **overrides,
        })
        assert resp.status_code == 201, resp.text
    finally:
        monkeypatch.setattr(bt_routes, "_run_backtest", real_run)
    return captured["tags"]


class TestUniverseTags:
    def test_non_dsl_run_records_universe_resolved_unconditionally(
        self, client, monkeypatch
    ) -> None:
        """非 DSL 回测也记 universe_resolved（一致时也记——可复现性）。"""
        c, _ = client
        tags = _post_capture_tags(
            c, monkeypatch,
            strategy_id="uni_tags_plain",
            strategy_type="StaticTopN",
            universe_id="idx_hs300",
        )
        assert tags["universe_resolved"] == "idx_hs300"
        assert "universe_override" not in tags

    def test_dsl_consistent_universe_no_override_tag(
        self, client, monkeypatch
    ) -> None:
        """DSL universe 与实际解析一致：记 universe_resolved，无 override。"""
        c, _ = client
        tags = _post_capture_tags(
            c, monkeypatch,
            strategy_id="uni_tags_dsl",
            strategy_type="DSL",
            dsl_spec=dict(_DSL_SPEC, universe="idx_zz500"),
            universe_id="idx_zz500",
        )
        assert tags["universe_resolved"] == "idx_zz500"
        assert "universe_override" not in tags

    def test_dsl_body_override_records_override_tag(
        self, client, monkeypatch
    ) -> None:
        """DSL sse 被 body szse 覆盖：override tag 形如 'sse->szse'。"""
        c, _ = client
        tags = _post_capture_tags(
            c, monkeypatch,
            strategy_id="uni_tags_dsl",
            strategy_type="DSL",
            dsl_spec=dict(_DSL_SPEC),
            universe_id="szse",
        )
        assert tags["universe_resolved"] == "szse"
        assert tags["universe_override"] == "sse->szse"

    def test_dsl_universe_all_no_override_tag(self, client, monkeypatch) -> None:
        """dsl universe='all' 不构成可覆盖意图：仅 universe_resolved。"""
        c, _ = client
        tags = _post_capture_tags(
            c, monkeypatch,
            strategy_id="uni_tags_dsl",
            strategy_type="DSL",
            dsl_spec=dict(_DSL_SPEC, universe="all"),
            universe_id="all",
        )
        assert tags["universe_resolved"] == "all"
        assert "universe_override" not in tags


class TestUniverseTagsHelpers:
    def test_build_universe_tags_pure(self) -> None:
        """纯函数矩阵：覆盖 tag 构造的全部边界（便于回归守卫引用）。"""
        build = bt_routes._build_universe_tags
        # 非 DSL
        assert build("idx_hs300", None) == {"universe_resolved": "idx_hs300"}
        assert build("idx_hs300", {}) == {"universe_resolved": "idx_hs300"}
        # DSL 一致 / all
        assert build("sse", {"universe": "sse"}) == {"universe_resolved": "sse"}
        assert build("all", {"universe": "all"}) == {"universe_resolved": "all"}
        # DSL 冲突
        assert build("szse", {"universe": "sse"}) == {
            "universe_resolved": "szse",
            "universe_override": "sse->szse",
        }
