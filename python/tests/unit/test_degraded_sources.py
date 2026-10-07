"""F4 因子集降级可见性测试。

覆盖：
- vibe_bridge._compat.VIBE_AVAILABLE=False 分支：三个 zoo 全部记录
  ``submodule_unavailable`` 降级（且补 log，不再零日志）。
- load_zoo 抛异常分支：记录 ``str(exc)[:200]`` 作为 reason。
- get_degraded_sources() 返回拷贝（外部修改不污染内部状态）。
- /factors/available 响应追加 ``degraded_sources`` 字段（向后兼容）。
- 正常态（无降级）返回空列表。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
import cquant.factorlab.factors as factors_mod
import cquant.vibe_bridge._compat as vibe_compat
from cquant.api_server.app import app
from cquant.datahub.catalog import Catalog

_REPO_ROOT = Path(__file__).resolve().parents[3]

_ZOOS = {"qlib158", "alpha101", "gtja191"}


@pytest.fixture(autouse=True)
def _restore_factors_module():
    """测试后恢复 factors 模块真实状态，避免污染其他用例。"""
    yield
    importlib.reload(factors_mod)


def _reload_with(monkeypatch, vibe_available: bool, load_zoo=None):
    monkeypatch.setattr(vibe_compat, "VIBE_AVAILABLE", vibe_available)
    if load_zoo is not None:
        import cquant.vibe_bridge.alpha_zoo as alpha_zoo

        monkeypatch.setattr(alpha_zoo, "load_zoo", load_zoo)
    importlib.reload(factors_mod)
    return factors_mod


# ── 分支一：submodule 不可用 ─────────────────────────────────────────────────


def test_degraded_when_vibe_unavailable(monkeypatch):
    mod = _reload_with(monkeypatch, vibe_available=False)
    degraded = mod.get_degraded_sources()
    assert {d["source"] for d in degraded} == _ZOOS
    assert all(d["reason"] == "submodule_unavailable" for d in degraded)


def test_degraded_unavailable_logged(monkeypatch, caplog):
    with caplog.at_level("INFO", logger="cquant.factorlab.factors"):
        _reload_with(monkeypatch, vibe_available=False)
    assert any("Vibe-Trading" in r.message for r in caplog.records if r.levelno <= 20)


# ── 分支二：load_zoo 失败 ────────────────────────────────────────────────────


def test_degraded_when_load_zoo_raises(monkeypatch):
    def boom(name: str):
        raise RuntimeError(f"zoo {name} exploded " + "x" * 500)

    mod = _reload_with(monkeypatch, vibe_available=True, load_zoo=boom)
    degraded = mod.get_degraded_sources()
    assert len(degraded) == 3
    assert all("zoo" in d["reason"] and "exploded" in d["reason"] for d in degraded)
    # reason 截断到 200 字符
    assert all(len(d["reason"]) <= 200 for d in degraded)


def test_no_degraded_when_all_zoos_load(monkeypatch):
    mod = _reload_with(monkeypatch, vibe_available=True, load_zoo=lambda name: [])
    assert mod.get_degraded_sources() == []


# ── 拷贝语义 ────────────────────────────────────────────────────────────────


def test_get_degraded_sources_returns_copy(monkeypatch):
    mod = _reload_with(monkeypatch, vibe_available=False)
    snapshot = mod.get_degraded_sources()
    assert snapshot
    snapshot.clear()
    assert mod.get_degraded_sources()  # 内部状态未被外部修改破坏


# ── /available 端点 ─────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    cat = Catalog(db_path=tmp_path / "test.duckdb", repo_root=_REPO_ROOT)
    cat.initialize()
    app.dependency_overrides[deps.get_catalog] = lambda: cat
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


def test_available_endpoint_includes_degraded_sources(client, monkeypatch):
    monkeypatch.setattr(
        factors_mod,
        "get_degraded_sources",
        lambda: [{"source": "qlib158", "reason": "submodule_unavailable"}],
    )
    resp = client.get("/api/v1/factors/available")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded_sources"] == [
        {"source": "qlib158", "reason": "submodule_unavailable"}
    ]
    # 向后兼容：既有字段仍在
    assert "factors" in body and "categories" in body and "total" in body


def test_available_endpoint_empty_degraded_when_healthy(client, monkeypatch):
    monkeypatch.setattr(factors_mod, "get_degraded_sources", lambda: [])
    resp = client.get("/api/v1/factors/available")
    assert resp.status_code == 200
    assert resp.json()["degraded_sources"] == []
