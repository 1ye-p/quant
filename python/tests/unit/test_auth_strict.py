"""Tests for default-deny API authentication (strict mode).

verify_api_key rejects ALL requests with 503 when CQUANT_API_KEY is unset
(default). CQUANT_AUTH_MODE=dev is the escape hatch for local development.
The session-wide conftest sets dev mode; these tests pin env explicitly.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app


@pytest.fixture()
def client():
    app.dependency_overrides[deps.get_catalog] = lambda: MagicMock()
    app.dependency_overrides[deps.get_kb_service] = lambda: MagicMock()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides = {}


def _strict_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CQUANT_API_KEY", raising=False)
    monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)


class TestStrictDefault:
    def test_no_key_returns_503(self, client: TestClient, monkeypatch) -> None:
        _strict_env(monkeypatch)
        resp = client.get("/api/v1/datasets")
        assert resp.status_code == 503
        assert "CQUANT_API_KEY" in resp.json()["detail"]

    def test_trading_endpoint_no_key_returns_503(
        self, client: TestClient, monkeypatch
    ) -> None:
        _strict_env(monkeypatch)
        resp = client.get("/api/v1/trading/account")
        assert resp.status_code == 503


class TestDevMode:
    def test_no_key_dev_mode_passes(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.delenv("CQUANT_API_KEY", raising=False)
        monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
        resp = client.get("/api/v1/datasets")
        assert resp.status_code == 200

    def test_trading_endpoint_dev_mode_still_503(
        self, client: TestClient, monkeypatch
    ) -> None:
        """dev 模式 + 无 key + 交易端点 → 503（安全关键：dev 只放行非交易端点）。"""
        monkeypatch.delenv("CQUANT_API_KEY", raising=False)
        monkeypatch.setenv("CQUANT_AUTH_MODE", "dev")
        resp = client.get("/api/v1/trading/account")
        assert resp.status_code == 503
        assert "CQUANT_API_KEY" in resp.json()["detail"]


class TestKeySet:
    def test_correct_key_passes(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get(
            "/api/v1/datasets",
            headers={"Authorization": "Bearer strict-test-key"},
        )
        assert resp.status_code == 200

    def test_wrong_key_returns_401(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get(
            "/api/v1/datasets",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    def test_missing_key_header_returns_401(
        self, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get("/api/v1/datasets")
        assert resp.status_code == 401


class TestQueryParamFallback:
    """EventSource cannot set headers — ?api_key= is the SSE credential path."""

    def test_query_param_key_passes(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get("/api/v1/datasets?api_key=strict-test-key")
        assert resp.status_code == 200

    def test_wrong_query_param_returns_401(
        self, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get("/api/v1/datasets?api_key=wrong-key")
        assert resp.status_code == 401

    def test_header_takes_precedence_over_query_param(
        self, client: TestClient, monkeypatch
    ) -> None:
        """凭据冲突时以 Header 为准（query param 仅作 EventSource 回退）。"""
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get(
            "/api/v1/datasets?api_key=wrong-key",
            headers={"Authorization": "Bearer strict-test-key"},
        )
        assert resp.status_code == 200


class TestAuthEndpoints:
    """/auth/status 公开探测 + /auth/verify 凭据校验。"""

    def test_status_public_and_reports_state(
        self, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.setenv("CQUANT_AUTH_MODE", "strict")
        resp = client.get("/api/v1/auth/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"key_configured": True, "mode": "strict"}
        assert "strict-test-key" not in resp.text  # never disclose the key

    def test_verify_ok_with_valid_key(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get(
            "/api/v1/auth/verify",
            headers={"Authorization": "Bearer strict-test-key"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_verify_rejects_invalid_key(
        self, client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setenv("CQUANT_API_KEY", "strict-test-key")
        monkeypatch.delenv("CQUANT_AUTH_MODE", raising=False)
        resp = client.get(
            "/api/v1/auth/verify",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401
