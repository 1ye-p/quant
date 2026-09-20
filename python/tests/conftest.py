"""Shared pytest fixtures for cQuant test suite."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from cquant.core.enums import AssetClass, Currency, Exchange
from cquant.core.types import Asset


@pytest.fixture(autouse=True, scope="session")
def _api_dev_auth_mode():
    """Run the suite under CQUANT_AUTH_MODE=dev.

    API auth is default-deny (503 without CQUANT_API_KEY). Most API tests
    exercise endpoints without a key and rely on the historical permissive
    behavior; dev mode preserves that. Tests that assert strict behavior
    (test_auth_strict.py) override this via monkeypatch.
    """
    os.environ.setdefault("CQUANT_AUTH_MODE", "dev")
    yield


@pytest.fixture
def sample_asset() -> Asset:
    return Asset(
        asset_id="SSE:600036",
        symbol="600036",
        exchange=Exchange.SSE,
        asset_class=AssetClass.EQUITY,
        currency=Currency.CNY,
        name="招商银行",
        lot_size=100,
        tick_size=Decimal("0.01"),
    )


@pytest.fixture
def sample_prices() -> pl.DataFrame:
    """Minimal price DataFrame for unit tests (5 assets × 10 days)."""
    import random
    from datetime import timedelta

    random.seed(42)
    rows = []
    base_date = date(2026, 1, 5)  # Monday
    assets = ["SSE:600036", "SSE:601318", "SZSE:000858", "SZSE:002415", "SSE:600519"]

    for asset_id in assets:
        price = 50.0
        for i in range(10):
            d = base_date + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            ret = random.uniform(-0.03, 0.03)
            price *= (1 + ret)
            rows.append({
                "asset_id": asset_id,
                "trade_date": d,
                "open": round(price * 0.999, 2),
                "high": round(price * 1.005, 2),
                "low": round(price * 0.995, 2),
                "close": round(price, 2),
                "volume": float(random.randint(100_000, 10_000_000)),
                "amount": float(random.randint(10_000_000, 1_000_000_000)),
                "is_suspended": False,
                "adj_factor": 1.0,
            })

    return pl.DataFrame(rows)
