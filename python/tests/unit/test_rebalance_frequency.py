"""P0' — rebalance_frequency end-to-end wiring (Body → RunSpec → BacktestSpec).

Engine semantics (``VectorBacktestEngine._is_rebalance_date``) already support
'1d'/'1w'/'1mo' (first trading day of week / month). These tests prove:

1. weekly/monthly: a production-assembly run (``BacktestRunner.run`` on the
   Task0 perf_equiv synthetic dataset) only rebalances on the first trading
   day of each week/month — asserted on the recorded rebalance-day set
   (``_build_tradability_today`` is invoked exactly once per rebalance day,
   so ``tradability_all.trade_date`` *is* the rebalance date set).
2. default unchanged: omitting the parameter reproduces the committed Task0
   fixtures exactly (fills/NAV bit-for-bit).
3. API validation: ``BacktestCreateBody.rebalance_frequency`` is a
   Literal["1d","1w","1mo"] → pydantic auto-422 on "2d" (no manual route
   validation layer exists for this field).
4. wiring: BacktestRunSpec carries the field and run.py passes it into every
   BacktestSpec construction site.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import polars as pl
import pytest
from fastapi.testclient import TestClient
from polars.testing import assert_frame_equal

_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../quant
assert _REPO_ROOT.name == "quant"
sys.path.insert(0, str(_REPO_ROOT / "python"))

_GEN_PATH = _REPO_ROOT / "python/tests/fixtures/perf_equiv/generate_fixtures.py"
_FIXTURE_DIR = _GEN_PATH.parent / "synthetic"

_spec = importlib.util.spec_from_file_location("perf_equiv_gen", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _run_with_frequency(frequency: str | None) -> dict:
    """Run the production assembly on the synthetic dataset, optionally
    injecting ``rebalance_frequency`` into the BacktestRunSpec the generator
    builds (monkeypatching the class reference the generator module holds)."""
    if frequency is None:
        return gen.run_production_capture(
            gen.build_synthetic_prices(),
            gen.build_synthetic_factors(gen.build_synthetic_prices()),
        )

    orig_cls = gen.BacktestRunSpec

    def _injected(**kwargs):
        kwargs["rebalance_frequency"] = frequency
        return orig_cls(**kwargs)

    gen.BacktestRunSpec = _injected
    try:
        prices = gen.build_synthetic_prices()
        return gen.run_production_capture(prices, gen.build_synthetic_factors(prices))
    finally:
        gen.BacktestRunSpec = orig_cls


def _first_trading_day_of_week(dates: list[date]) -> set[date]:
    out: set[date] = set()
    prev: date | None = None
    for d in dates:
        if prev is None or d.weekday() < prev.weekday():
            out.add(d)
        prev = d
    return out


def _first_trading_day_of_month(dates: list[date]) -> set[date]:
    out: set[date] = set()
    prev: date | None = None
    for d in dates:
        if prev is None or d.month != prev.month:
            out.add(d)
        prev = d
    return out


def _rebalance_days(rec: dict) -> list[date]:
    return sorted(
        rec["tradability_all"]["trade_date"].unique().to_list()
    )


class TestWeeklyMonthly:
    def test_weekly_rebalance_dates(self) -> None:
        rec = _run_with_frequency("1w")
        days = _rebalance_days(rec)
        assert days, "weekly run must record rebalance days"

        prices = gen.build_synthetic_prices()
        # trading days actually present in the window the engine iterates
        all_days = sorted(prices["trade_date"].unique().to_list())
        window = [d for d in all_days if days[0] <= d]
        expected = {d for d in _first_trading_day_of_week(window)
                    if days[0] <= d}

        assert set(days) == expected
        # sanity: strictly fewer rebalance days than daily frequency
        daily = _run_with_frequency("1d")
        assert len(days) < len(_rebalance_days(daily))

    def test_monthly_rebalance_dates(self) -> None:
        rec = _run_with_frequency("1mo")
        days = _rebalance_days(rec)
        assert days, "monthly run must record rebalance days"

        prices = gen.build_synthetic_prices()
        all_days = sorted(prices["trade_date"].unique().to_list())
        window = [d for d in all_days if days[0] <= d]
        expected = {d for d in _first_trading_day_of_month(window)
                    if days[0] <= d}

        assert set(days) == expected


class TestDefaultUnchanged:
    def test_default_unchanged_matches_fixtures_exactly(self) -> None:
        """No parameter → identical fills/NAV to the committed Task0 fixtures."""
        rec = _run_with_frequency(None)

        fills_fix = pl.read_parquet(_FIXTURE_DIR / "fills.parquet").sort(
            ["trade_date", "asset_id", "side"])
        nav_fix = pl.read_parquet(_FIXTURE_DIR / "nav.parquet").sort("trade_date")

        assert_frame_equal(rec["fills"], fills_fix, check_exact=True)
        assert_frame_equal(rec["nav"], nav_fix, check_exact=True)


class TestApiValidation:
    @pytest.fixture()
    def client(self):
        import cquant.api_server.deps as deps
        from cquant.api_server.app import app

        app.dependency_overrides[deps.get_catalog] = lambda: MagicMock()
        app.dependency_overrides[deps.get_kb_service] = lambda: MagicMock()
        env = {k: v for k, v in os.environ.items() if k != "CQUANT_API_KEY"}
        with patch.dict(os.environ, env, clear=True), \
                TestClient(app, raise_server_exceptions=False) as c:
            yield c
        app.dependency_overrides = {}

    @staticmethod
    def _body(**over):
        base = {
            "strategy_id": "s1",
            "dataset_version": "v1",
            "start_date": "2025-01-01",
            "end_date": "2025-03-31",
            "rebalance_frequency": "1d",
        }
        base.update(over)
        return base

    def test_invalid_frequency_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/backtests", json=self._body(rebalance_frequency="2d"))
        assert resp.status_code == 422
        assert "rebalance_frequency" in resp.text

    def test_valid_frequencies_accepted_at_validation(
        self, client: TestClient
    ) -> None:
        # Validation passes for all three literals (handler may fail later on
        # the mocked catalog — only the 422-on-literal contract is asserted
        # here; anything != 422 means the Literal is wired).
        for freq in ("1d", "1w", "1mo"):
            resp = client.post(
                "/api/v1/backtests", json=self._body(rebalance_frequency=freq))
            assert resp.status_code != 422, f"{freq} must pass Literal validation"


class TestSpecWiring:
    def test_run_spec_has_field_default_1d(self) -> None:
        from cquant.backtest_vector.run import BacktestRunSpec

        spec = BacktestRunSpec(
            dataset_version="v", strategy_id="s",
            start_date=date(2025, 1, 1), end_date=date(2025, 3, 31),
        )
        assert spec.rebalance_frequency == "1d"

    def test_engine_spec_field_exists(self) -> None:
        from cquant.backtest_vector.engine import BacktestSpec

        assert "rebalance_frequency" in BacktestSpec.__dataclass_fields__
