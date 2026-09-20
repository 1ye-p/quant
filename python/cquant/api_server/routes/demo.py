"""Demo / onboarding endpoints — synthetic seed data + demo DSL strategy.

POST /api/v1/demo/seed  — idempotent import of examples/demo_data into the
                          catalog (silver_prices_1d + silver_assets +
                          silver_external_indicators + meta_strategy_configs +
                          ret_20d factor materialization).
GET  /api/v1/demo/status — {seeded, dataset_version, strategy_id, ...}
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
from fastapi import APIRouter, HTTPException

from cquant.api_server.deps import CatalogDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["demo"])

DEMO_DATASET_VERSION = "demo_synthetic_v1"
DEMO_STRATEGY_ID = "demo_momentum_top10"
DEMO_SOURCE = "demo"
DEMO_INDICATOR_KEY = "market_breadth"

# Fallback lookup when the bundled examples/ directory is not deployed
# alongside the package (e.g. wheel installs).
_REPO_ROOT_CANDIDATES = [
    Path(__file__).resolve().parents[4],  # repo layout: python/cquant/api_server/routes/
    Path.cwd(),
]


def demo_data_dir() -> Path:
    """Locate examples/demo_data (env override > repo candidates)."""
    env = os.environ.get("CQUANT_DEMO_DATA_DIR")
    if env:
        p = Path(env)
        if p.is_dir():
            return p
    for root in _REPO_ROOT_CANDIDATES:
        p = root / "examples" / "demo_data"
        if p.is_dir():
            return p
    raise HTTPException(
        status_code=500,
        detail="demo data directory (examples/demo_data) not found; "
               "set CQUANT_DEMO_DATA_DIR",
    )


def _demo_strategy_config() -> dict:
    """Demo strategy config (meta_strategy_configs.config_text JSON).

    Mirrors what the web StrategyDSLEditor persists: strategy_type=DSL plus
    the parsed dsl_spec so POST /backtests picks it up automatically.
    """
    import yaml

    from cquant.strategy_dsl.schema import StrategyDSL

    dsl_text = (demo_data_dir() / "demo_strategy.yaml").read_text(encoding="utf-8")
    spec = StrategyDSL.from_yaml(dsl_text)
    return {
        "strategy_type": "DSL",
        "name": spec.name,
        "dsl_spec": spec.model_dump(mode="json", exclude_none=True),
    }


def _import_prices(catalog, csv_path: Path) -> dict:
    """Upsert demo OHLCV rows into silver_prices_1d (idempotent)."""
    df = pl.read_csv(csv_path, try_parse_dates=True)
    rows = [
        (
            r["asset_id"], r["trade_date"], float(r["open"]), float(r["high"]),
            float(r["low"]), float(r["close"]), float(r["volume"]), 0.0,
            float(r["adj_factor"]), DEMO_SOURCE,
        )
        for r in df.iter_rows(named=True)
    ]
    catalog.executemany(
        """
        INSERT INTO silver_prices_1d
            (asset_id, trade_date, open, high, low, close, volume, amount,
             adj_factor, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id, trade_date) DO UPDATE SET
            open = excluded.open, high = excluded.high, low = excluded.low,
            close = excluded.close, volume = excluded.volume,
            amount = excluded.amount, adj_factor = excluded.adj_factor,
            source = excluded.source
        """,
        rows,
    )
    return {
        "rows": len(rows),
        "assets": df["asset_id"].n_unique(),
        "start_date": str(df["trade_date"].min()),
        "end_date": str(df["trade_date"].max()),
    }


def _import_assets(catalog, csv_path: Path) -> int:
    """Register demo symbols in silver_assets (INSERT OR IGNORE — idempotent)."""
    df = pl.read_csv(csv_path).select("asset_id").unique()
    first_date = str(pl.read_csv(csv_path, try_parse_dates=True)["trade_date"].min())
    rows = []
    for asset_id in df["asset_id"].to_list():
        exchange, symbol = asset_id.split(":")
        rows.append((
            asset_id, symbol, exchange, "stock", "CNY",
            f"Demo {symbol}", "", "active", 100, 0.01, first_date, None,
        ))
    catalog.executemany(
        """
        INSERT INTO silver_assets
            (asset_id, symbol, exchange, asset_class, currency, name, name_en,
             status, lot_size, tick_size, effective_from, effective_to)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id) DO NOTHING
        """,
        rows,
    )
    return len(rows)


def _import_indicator(catalog, csv_path: Path) -> int:
    """Market breadth → silver_external_indicators via the standard importer."""
    from cquant.datahub.pipelines.external_indicator_importer import (
        ExternalIndicatorImporter, ImportConfig,
    )

    importer = ExternalIndicatorImporter(catalog)
    report = importer.import_csv(
        csv_path,
        ImportConfig(
            source=DEMO_SOURCE,
            indicator_key=DEMO_INDICATOR_KEY,
            column_map={"date": "trade_date", "breadth": "value"},
            available_date_rule="A",  # breadth is observable same-day
        ),
    )
    return report.inserted


def _upsert_strategy(catalog, config: dict) -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    catalog.execute(
        """
        INSERT INTO meta_strategy_configs
            (strategy_id, config_format, config_text, parsed_config,
             universe_id, created_at, updated_at)
        VALUES (?, 'json', ?, ?, 'all', ?, ?)
        ON CONFLICT (strategy_id) DO UPDATE SET
            config_text = excluded.config_text,
            parsed_config = excluded.parsed_config,
            updated_at = excluded.updated_at
        """,
        [DEMO_STRATEGY_ID, json.dumps(config, ensure_ascii=False),
         json.dumps(config, ensure_ascii=False), now, now],
    )


def _materialize_demo_factors(catalog, start: str, end: str) -> str:
    """Materialize ret_20d so the demo DSL backtest has features. Best-effort."""
    try:
        from cquant.factorlab.factor import FactorRegistry
        from cquant.factorlab.factors import BUILTIN_FACTORS
        from cquant.factorlab.materialize import FactorMaterializer, FactorMaterializationSpec

        registry = FactorRegistry()
        for factor in BUILTIN_FACTORS:
            if factor.name == "ret_20d":
                registry.register(factor)
        materializer = FactorMaterializer(catalog, registry)
        return materializer.run(FactorMaterializationSpec(
            dataset_version=DEMO_DATASET_VERSION,
            factor_names=["ret_20d"],
            start_date=date.fromisoformat(start),
            end_date=date.fromisoformat(end),
        ))
    except Exception as exc:  # noqa: BLE001 — seeding must not fail on factors
        logger.warning("demo factor materialization failed: %s", exc)
        return ""


@router.post("/demo/seed")
async def seed_demo(catalog: CatalogDep) -> dict:
    """Idempotently import the demo dataset + strategy. Returns stats."""
    try:
        data_dir = demo_data_dir()
        prices_csv = data_dir / "demo_prices_1d.csv"
        breadth_csv = data_dir / "market_breadth.csv"
        for p in (prices_csv, breadth_csv):
            if not p.exists():
                raise HTTPException(status_code=500, detail=f"demo file missing: {p.name}")

        catalog.initialize()

        price_stats = _import_prices(catalog, prices_csv)
        assets = _import_assets(catalog, prices_csv)
        indicator_rows = _import_indicator(catalog, breadth_csv)

        config = _demo_strategy_config()
        _upsert_strategy(catalog, config)

        feature_set_version = _materialize_demo_factors(
            catalog, price_stats["start_date"], price_stats["end_date"]
        )

        # Suggested backtest window: leave a 30-trading-day warmup for ret_20d
        suggested_start = (
            date.fromisoformat(price_stats["start_date"]) + timedelta(days=45)
        ).isoformat()
        suggested_end = price_stats["end_date"]

        return {
            "seeded": True,
            "dataset_version": DEMO_DATASET_VERSION,
            "strategy_id": DEMO_STRATEGY_ID,
            "prices": price_stats,
            "assets_registered": assets,
            "indicator_rows": indicator_rows,
            "feature_set_version": feature_set_version,
            "suggested_start_date": suggested_start,
            "suggested_end_date": suggested_end,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surface a clean 500
        logger.exception("demo seed failed")
        raise HTTPException(status_code=500, detail=f"demo seed failed: {exc}") from exc


@router.get("/demo/status")
async def demo_status(catalog: CatalogDep) -> dict:
    """Whether the demo dataset + strategy are present."""
    try:
        prices = catalog.query(
            "SELECT COUNT(*) AS cnt FROM silver_prices_1d WHERE source = ?",
            [DEMO_SOURCE],
        )
        price_rows = 0 if prices.is_empty() else int(prices["cnt"][0])
    except Exception:
        price_rows = 0

    try:
        strategy = catalog.query(
            "SELECT strategy_id FROM meta_strategy_configs WHERE strategy_id = ?",
            [DEMO_STRATEGY_ID],
        )
        strategy_present = not strategy.is_empty()
    except Exception:
        strategy_present = False

    return {
        "seeded": price_rows > 0 and strategy_present,
        "price_rows": price_rows,
        "strategy_id": DEMO_STRATEGY_ID if strategy_present else None,
        "dataset_version": DEMO_DATASET_VERSION if price_rows > 0 else None,
    }
