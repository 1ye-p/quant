"""cquant.scheduler.catalog_backup — daily catalog backup with rotation (F3).

Backup pipeline (pure, unit-testable):

1. ``CHECKPOINT`` the live catalog via its existing write handle (flushes the
   WAL into ``catalog.duckdb`` so the file copy is self-contained). If the
   catalog is read-only or checkpointing fails, we still copy — a
   possibly-stale-but-consistent copy beats no copy (the WAL is replayed only
   via a DuckDB connection, never by copying).
2. ``shutil.copy2`` the database file to a temp name next to the destination,
   then gzip (``compresslevel=6``) to ``catalog-YYYYMMDD.duckdb.gz``.
   gzip over zstd per D4-A: stdlib, zero new dependencies.
3. Rotate: keep the newest 7 daily backups; Monday-dated files are weekly
   copies and keep the newest 4.

``run_catalog_backup`` retries the copy+gzip step exactly once on failure and
returns ``{"error": ...}`` if both attempts fail (never raises to the caller —
it runs inside a cron job).

``verify_backup`` is the restore drill: gunzip to a temp file, ATTACH
read-only, count key tables and sample the newest rows.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_DIR = Path("data/backups")

# Key tables verified during a restore drill (missing tables are tolerated —
# reported as None rather than failing the drill).
KEY_TABLES = ("silver_prices_1d", "silver_assets", "silver_fundamentals")


def _catalog_db_path(catalog: Any) -> Path:
    path = getattr(catalog, "_db_path", None)
    if path is None:
        raise ValueError(
            "catalog has no _db_path attribute; pass the cquant Catalog instance"
        )
    return Path(path)


def _backup_name(day: date) -> str:
    return f"catalog-{day.strftime('%Y%m%d')}.duckdb.gz"


def rotate_backups(
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    daily_keep: int = 7,
    weekly_keep: int = 4,
) -> dict:
    """Apply retention: newest *daily_keep* daily + newest *weekly_keep* weekly.

    Weekly copies are backups whose filename date is a Monday. Returns a
    summary ``{"deleted": [names], "kept_daily": n, "kept_weekly": n}``.
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return {"deleted": [], "kept_daily": 0, "kept_weekly": 0}

    dated: list[tuple[date, Path]] = []
    for p in backup_dir.glob("catalog-????????.duckdb.gz"):
        try:
            stamp = p.stem.split("-")[1].split(".")[0]
            dated.append((datetime.strptime(stamp, "%Y%m%d").date(), p))
        except (IndexError, ValueError):
            continue  # foreign filename — leave untouched

    weekly = sorted(((d, p) for d, p in dated if d.weekday() == 0), reverse=True)
    daily = sorted(((d, p) for d, p in dated if d.weekday() != 0), reverse=True)

    deleted: list[str] = []
    for _, p in daily[daily_keep:]:
        p.unlink(missing_ok=True)
        deleted.append(p.name)
    for _, p in weekly[weekly_keep:]:
        p.unlink(missing_ok=True)
        deleted.append(p.name)

    return {
        "deleted": deleted,
        "kept_daily": min(len(daily), daily_keep),
        "kept_weekly": min(len(weekly), weekly_keep),
    }


def run_catalog_backup(
    catalog: Any, backup_dir: Path = DEFAULT_BACKUP_DIR
) -> dict:
    """Backup the catalog database; never raises. See module docstring.

    Returns ``{"path", "size", "elapsed", "rotated"}`` on success or
    ``{"error"}`` after the copy step fails twice.
    """
    started = time.monotonic()
    backup_dir = Path(backup_dir)
    try:
        db_path = _catalog_db_path(catalog)
        if not db_path.exists():
            return {"error": f"catalog file not found: {db_path}"}

        # 1. CHECKPOINT via the existing (write) handle so the file copy is
        #    self-contained. Read-only handles cannot checkpoint — copy anyway.
        if getattr(catalog, "read_only", False):
            logger.info("Catalog backup: read-only handle, skipping CHECKPOINT")
        else:
            try:
                catalog.checkpoint()
            except Exception as exc:
                logger.warning("Catalog backup: CHECKPOINT failed: %s", exc)

        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / _backup_name(date.today())
        tmp_dest = dest.with_suffix(".tmp")

        # 2. copy + gzip, retried exactly once
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                tmp_dest.unlink(missing_ok=True)
                shutil.copy2(db_path, tmp_dest)
                with open(tmp_dest, "rb") as fin, gzip.open(
                    dest, "wb", compresslevel=6
                ) as fout:
                    shutil.copyfileobj(fin, fout, length=8 * 1024 * 1024)
                tmp_dest.unlink(missing_ok=True)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Catalog backup attempt %d failed: %s", attempt, exc
                )
        if last_exc is not None:
            tmp_dest.unlink(missing_ok=True)
            dest.unlink(missing_ok=True)
            return {"error": f"backup failed after retry: {last_exc}"}

        # 3. rotation
        rotated = rotate_backups(backup_dir)

        result = {
            "path": str(dest),
            "size": dest.stat().st_size,
            "elapsed": round(time.monotonic() - started, 3),
            "rotated": rotated,
        }
        logger.info(
            "Catalog backup complete: %s (%.1f MB in %.1fs, deleted %d old)",
            dest.name,
            result["size"] / 1e6,
            result["elapsed"],
            len(rotated["deleted"]),
        )
        return result
    except Exception as exc:  # defensive — runs inside a cron job
        logger.exception("Catalog backup crashed")
        return {"error": str(exc)}


def verify_backup(backup_path: Path) -> dict:
    """Restore drill: gunzip to tmp, ATTACH READ_ONLY, count + sample.

    Returns ``{"ok": bool, "tables": {name: count|None}, "latest_trade_date",
    "sample": [...]}``; failures return ``{"ok": False, "error": ...}``.
    """
    import tempfile

    import duckdb

    backup_path = Path(backup_path)
    if not backup_path.exists():
        return {"ok": False, "error": f"backup not found: {backup_path}"}

    with tempfile.TemporaryDirectory(prefix="cquant-drill-") as td:
        restored = Path(td) / "restored.duckdb"
        try:
            with gzip.open(backup_path, "rb") as fin, open(
                restored, "wb"
            ) as fout:
                shutil.copyfileobj(fin, fout, length=8 * 1024 * 1024)
            conn = duckdb.connect(str(restored), read_only=True)
        except Exception as exc:
            return {"ok": False, "error": f"restore failed: {exc}"}

        try:
            tables: dict[str, int | None] = {}
            for table in KEY_TABLES:
                try:
                    tables[table] = conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                except Exception:
                    tables[table] = None

            latest_trade_date = None
            if tables.get("silver_prices_1d"):
                row = conn.execute(
                    "SELECT MAX(trade_date) FROM silver_prices_1d"
                ).fetchone()
                latest_trade_date = str(row[0]) if row and row[0] else None

            sample: list[dict] = []
            if tables.get("silver_prices_1d"):
                cols = conn.execute(
                    "SELECT asset_id, trade_date, close FROM silver_prices_1d "
                    "ORDER BY trade_date DESC, asset_id LIMIT 3"
                ).fetchall()
                sample = [
                    {"asset_id": r[0], "trade_date": str(r[1]), "close": r[2]}
                    for r in cols
                ]

            # ok = the restored file opens and the core price table is
            # non-empty. Other key tables may legitimately be absent/empty
            # (production silver_fundamentals is 0 rows) — reported, not fatal.
            ok = (tables.get("silver_prices_1d") or 0) > 0
            return {
                "ok": ok,
                "tables": tables,
                "latest_trade_date": latest_trade_date,
                "sample": sample,
            }
        except Exception as exc:
            return {"ok": False, "error": f"verification query failed: {exc}"}
        finally:
            conn.close()
