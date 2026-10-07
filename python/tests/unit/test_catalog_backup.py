"""Tests for cquant.scheduler.catalog_backup (F3 — catalog backup + rotation)."""

from __future__ import annotations

import gzip
import shutil
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from cquant.scheduler.catalog_backup import (
    run_catalog_backup,
    verify_backup,
    rotate_backups,
)


def _make_catalog(tmp_path: Path, rows: int = 50) -> SimpleNamespace:
    """Duck-typed catalog: real duckdb file + checkpoint()/read_only attrs."""
    db = tmp_path / "catalog.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE silver_prices_1d AS "
        "SELECT '000001.SZ' AS asset_id, "
        "       CAST(DATE '2026-01-01' + INTERVAL (i % 30) DAY AS DATE) AS trade_date, "
        "       10.0 + i AS close "
        f"FROM range({rows}) t(i)"
    )
    conn.execute(
        "CREATE TABLE silver_assets AS "
        "SELECT '000001.SZ' AS asset_id, 'active' AS status"
    )
    conn.close()

    def _checkpoint() -> None:
        c = duckdb.connect(str(db))
        c.execute("CHECKPOINT")
        c.close()

    return SimpleNamespace(_db_path=db, read_only=False, checkpoint=_checkpoint)


def _touch_backup(backup_dir: Path, day: date, size: int = 32) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / f"catalog-{day.strftime('%Y%m%d')}.duckdb.gz"
    with gzip.open(p, "wb", compresslevel=6) as f:
        f.write(b"x" * size)


class TestRunCatalogBackup:
    def test_creates_gzip_backup_with_metadata(self, tmp_path):
        cat = _make_catalog(tmp_path)
        backup_dir = tmp_path / "backups"
        result = run_catalog_backup(cat, backup_dir=backup_dir)

        assert "error" not in result, result
        out = Path(result["path"])
        assert out.exists()
        assert out.name == f"catalog-{date.today().strftime('%Y%m%d')}.duckdb.gz"
        assert out.suffixes[-2:] == [".duckdb", ".gz"]
        assert result["size"] == out.stat().st_size
        assert result["elapsed"] >= 0
        with gzip.open(out, "rb") as f:
            assert f.read(4)  # non-empty payload

    def test_retry_once_then_error_dict(self, tmp_path, monkeypatch):
        cat = _make_catalog(tmp_path)
        calls = {"n": 0}

        real_copy = shutil.copy2

        def flaky_copy(src, dst, *a, **kw):
            calls["n"] += 1
            raise OSError("simulated copy failure")

        monkeypatch.setattr("cquant.scheduler.catalog_backup.shutil.copy2", flaky_copy)
        result = run_catalog_backup(cat, backup_dir=tmp_path / "backups")
        assert "error" in result
        assert calls["n"] == 2  # initial attempt + exactly one retry


class TestRotateBackups:
    def test_daily_keep_7_weekly_keep_4(self, tmp_path):
        backup_dir = tmp_path / "backups"
        # 12 historical files ending yesterday: 10 non-Monday + 2 Monday
        days: list[date] = []
        d = date.today() - timedelta(days=1)
        mondays = 0
        while len(days) < 12:
            if d.weekday() == 0:  # Monday
                if mondays >= 2:
                    d -= timedelta(days=1)
                    continue
                mondays += 1
            days.append(d)
            d -= timedelta(days=1)
        for day in days:
            _touch_backup(backup_dir, day)

        summary = rotate_backups(backup_dir, daily_keep=7, weekly_keep=4)

        remaining = {
            p.name for p in backup_dir.glob("catalog-*.duckdb.gz")
        }
        weekly_remaining = {
            name for name in remaining
            if date(int(name[8:12]), int(name[12:14]), int(name[14:16])).weekday() == 0
        }
        daily_remaining = remaining - weekly_remaining
        assert len(daily_remaining) == 7
        assert len(weekly_remaining) == 2  # only 2 existed
        assert len(summary["deleted"]) == 3
        # newest daily survive: the 7 newest non-Monday files by date
        expected_daily = sorted(
            (x for x in days if x.weekday() != 0), reverse=True
        )[:7]
        assert daily_remaining == {
            f"catalog-{x.strftime('%Y%m%d')}.duckdb.gz" for x in expected_daily
        }


class TestVerifyBackup:
    def test_verify_counts_nonzero_and_samples(self, tmp_path):
        cat = _make_catalog(tmp_path, rows=50)
        result = run_catalog_backup(cat, backup_dir=tmp_path / "backups")
        assert "error" not in result

        report = verify_backup(Path(result["path"]))
        assert report["ok"] is True
        assert report["tables"]["silver_prices_1d"] == 50
        assert report["tables"]["silver_assets"] == 1
        assert report["tables"]["silver_fundamentals"] is None  # absent → tolerated
        assert report["latest_trade_date"] == "2026-01-30"
        sample = report["sample"]
        assert sample and sample[0]["asset_id"] == "000001.SZ"

    def test_verify_missing_file_reports_error(self, tmp_path):
        report = verify_backup(tmp_path / "nope.duckdb.gz")
        assert report["ok"] is False
        assert "error" in report
