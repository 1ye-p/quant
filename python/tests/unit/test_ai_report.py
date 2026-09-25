"""AI research report generation (Phase 4 T6) — route tests.

Covers:
1. POST /{run_id}/report triggers the (stubbed) report_writer agent, the job
   completes, the markdown lands in gold_research_reports, and the knowledge
   base mirror is invoked (spy).
2. GET /{run_id}/report returns the latest persisted report.
3. POST on a missing run → 404; GET with no report → 404.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi.testclient import TestClient

import cquant.api_server.deps as deps
from cquant.api_server.app import app
import cquant.api_server.routes.backtests as bt_routes

STUB_MD = "# 研报（stub）\n\n- 总收益率: 12.34%\n- Sharpe: 1.5\n"


class _FakeKB:
    def __init__(self) -> None:
        self.ingest_calls: list[dict] = []

    def ingest(self, request) -> object:  # spy
        self.ingest_calls.append({"uri": request.uri, "title": request.title})
        return MagicMock(doc_id="doc-1")


def _make_catalog(run_id: str = "run-123") -> tuple[MagicMock, list]:
    cat = MagicMock()
    inserts: list[tuple[str, list]] = []

    def mock_query(sql, params=None):
        if "_api_jobs" in sql:
            return pl.DataFrame(
                {"job_id": ["j1"], "job_type": ["report"], "status": ["completed"],
                 "run_id": [run_id], "error": [None]}
            )
        if "gold_backtest_runs" in sql and "status" in sql:
            q_run = (params or [run_id])[0]
            if q_run != run_id:
                return pl.DataFrame()
            return pl.DataFrame({"run_id": [run_id], "status": ["completed"]})
        if "gold_bt_analysis_runs" in sql:
            return pl.DataFrame()
        if "gold_validation_suites" in sql:
            return pl.DataFrame()
        if "gold_research_reports" in sql and "CREATE" not in sql:
            rows = [p for s, p in inserts if "gold_research_reports" in s and "CREATE" not in s]
            if not rows:
                return pl.DataFrame()
            rid, r_run, content, created = rows[-1]
            return pl.DataFrame({
                "report_id": [rid], "run_id": [r_run],
                "content_md": [content], "created_at": [created],
            })
        return pl.DataFrame()

    def mock_execute(sql, params=None):
        inserts.append((sql, params or []))
        return None

    cat.query.side_effect = mock_query
    cat.execute.side_effect = mock_execute
    cat._inserts = inserts
    return cat, inserts


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cat, _ = _make_catalog()
    kb = _FakeKB()
    app.dependency_overrides[deps.get_catalog] = lambda: cat
    app.dependency_overrides[deps.get_kb_service] = lambda: kb
    # Stub the agent invocation — no LLM provider in tests
    monkeypatch.setattr(bt_routes, "_invoke_report_writer", lambda ctx, meta: STUB_MD)
    with TestClient(app) as c:
        yield c, cat, kb
    app.dependency_overrides = {}


class TestGenerateReport:
    def test_post_completes_and_persists(self, client) -> None:
        c, cat, kb = client
        resp = c.post("/api/v1/backtests/run-123/report")
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        # Background tasks run synchronously under TestClient — job completed
        job = c.get(f"/api/v1/backtests/jobs/{job_id}").json()
        assert job["status"] == "completed"

        # Markdown persisted to gold_research_reports (agent text + chart
        # spec markers appended by _append_report_charts)
        report_inserts = [p for s, p in cat._inserts if "gold_research_reports" in s and "CREATE" not in s]
        assert report_inserts, "expected INSERT INTO gold_research_reports"
        params = report_inserts[-1]
        assert params[1] == "run-123"
        assert params[2].startswith(STUB_MD)
        assert "[CHART:metric_cards:" in params[2]

        # Knowledge-base mirror invoked (spy)
        assert len(kb.ingest_calls) == 1
        assert kb.ingest_calls[0]["uri"].endswith(".md")

    def test_get_returns_latest_report(self, client) -> None:
        c, _, _ = client
        # Generate first so a report row exists
        assert c.post("/api/v1/backtests/run-123/report").status_code == 200
        resp = c.get("/api/v1/backtests/run-123/report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == "run-123"
        assert body["content_md"].startswith(STUB_MD)
        assert "[CHART:metric_cards:" in body["content_md"]
        assert body["report_id"]

    def test_post_missing_run_404(self, client, monkeypatch) -> None:
        c, cat, _ = client
        empty_cat, _ = _make_catalog("other-run")
        app.dependency_overrides[deps.get_catalog] = lambda: empty_cat
        try:
            resp = c.post("/api/v1/backtests/nope/report")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides[deps.get_catalog] = lambda: cat

    def test_get_no_report_404(self, client, monkeypatch) -> None:
        c, cat, _ = client
        fresh_cat, _ = _make_catalog()
        app.dependency_overrides[deps.get_catalog] = lambda: fresh_cat
        try:
            resp = c.get("/api/v1/backtests/run-123/report")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides[deps.get_catalog] = lambda: cat


class TestFallbackWriter:
    def test_fallback_markdown_without_llm(self) -> None:
        meta = {
            "run_id": "r1", "strategy_id": "top10", "dataset_version": "v1",
            "metrics": {"total_return": 0.1234, "sharpe_ratio": 1.5},
            "analysis": {"psr": 0.97},
        }
        md = bt_routes._fallback_report_md(meta)
        assert "回测研究报告" in md
        assert "12.34%" in md
        assert "0.97" in md


class TestReportCharts:
    """Backlog #6: chart_generator wired into the report path."""

    @staticmethod
    def _catalog_with_nav(nav_rows: list[tuple[str, float]] | None):
        from unittest.mock import MagicMock

        cat = MagicMock()

        def mock_query(sql, params=None):
            if "gold_portfolio_snapshots" in sql:
                if nav_rows:
                    return pl.DataFrame({
                        "trade_date": [d for d, _ in nav_rows],
                        "nav": [v for _, v in nav_rows],
                    })
                return pl.DataFrame()
            return pl.DataFrame()

        cat.query.side_effect = mock_query
        return cat

    def test_appends_metric_cards_and_nav_line(self) -> None:
        from datetime import date

        cat = self._catalog_with_nav([(date(2025, 1, 2), 1.0), (date(2025, 1, 3), 1.01)])
        meta = {
            "run_id": "r1",
            "metrics": {"total_return": 0.1, "sharpe_ratio": 1.2,
                        "max_drawdown": -0.05, "win_rate": 0.6},
        }
        out = bt_routes._append_report_charts(cat, "# 报告\n正文", meta)
        assert out.startswith("# 报告\n正文")
        assert "[CHART:metric_cards:" in out
        assert "[CHART:line:" in out
        # markers parse back via ChartGenerator
        from cquant.ai_advisor.chart_generator import ChartGenerator

        markers = ChartGenerator.parse_markers(out)
        types = {m["chart_type"] for m in markers}
        assert types == {"metric_cards", "line"}

    def test_no_snapshots_still_emits_metric_cards(self) -> None:
        cat = self._catalog_with_nav(None)
        meta = {"run_id": "r1", "metrics": {}}
        out = bt_routes._append_report_charts(cat, "text-only", meta)
        assert "[CHART:metric_cards:" in out
        assert "[CHART:line:" not in out

    def test_failure_keeps_text_report(self) -> None:
        from unittest.mock import MagicMock

        cat = MagicMock()
        cat.query.side_effect = RuntimeError("boom")
        out = bt_routes._append_report_charts(cat, "keep me", {"run_id": "r1"})
        assert out == "keep me"
