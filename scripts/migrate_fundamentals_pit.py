#!/usr/bin/env python3
"""migrate_fundamentals_pit.py — Backfill announce_date for silver_fundamentals.

Thin wrapper over ``cquant.datahub.pipelines.announce_backfill`` (D4-A 固定
披露截止日，取代历史 +N 天偏移——单一实现原则)。覆盖两类行（幂等）：

  - source='akshare' 且 announce_date = report_date（前视行）
  - announce_date IS NULL（历史遗留行，包括 tushare NULL 行——现实 tushare
    行有 f_ann_date 不为 NULL，风险纯理论）

tushare 非空行不会被修改。``--dry-run`` 只统计不写库。

Usage::

    python scripts/migrate_fundamentals_pit.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path so ``cquant`` is importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def migrate(catalog, *, dry_run: bool = False) -> dict:
    """Delegate to backfill_announce_dates and return its stats dict."""
    from cquant.datahub.pipelines.announce_backfill import backfill_announce_dates

    logger.info(
        "Starting announce_date migration on silver_fundamentals (dry_run=%s) ...",
        dry_run,
    )
    stats = backfill_announce_dates(catalog, dry_run=dry_run)
    logger.info("Migration stats: %s", json.dumps(stats, default=str))
    return stats


if __name__ == "__main__":
    from cquant.datahub.catalog import Catalog

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="只统计候选行，不写库"
    )
    args = parser.parse_args()

    db_path = _REPO_ROOT / "data" / "catalog.duckdb"
    logger.info("Connecting to DuckDB at %s ...", db_path)

    cat = Catalog(db_path=str(db_path))
    try:
        migrate(cat, dry_run=args.dry_run)
    finally:
        cat._backend.close()
        logger.info("Connection closed.")
