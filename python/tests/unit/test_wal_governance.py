"""WAL governance tests — graceful close, periodic checkpoint, startup self-heal.

All tests use temporary Duck files; the real ``data/catalog.duckdb`` is never
touched.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from cquant.datahub.catalog import (
    Catalog,
    _is_wal_corruption_error,
    _self_heal_wal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _wal_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".wal")


def _wal_size(db_path: Path) -> int:
    wal = _wal_path(db_path)
    return wal.stat().st_size if wal.exists() else 0


def _write_rows(catalog: Catalog, n_batches: int = 8, rows: int = 500) -> None:
    catalog.execute("CREATE TABLE IF NOT EXISTS wal_test (x INTEGER)")
    for _ in range(n_batches):
        catalog.execute(
            "INSERT INTO wal_test SELECT range FROM range(?)", [rows]
        )


def _make_corrupt_wal(db_path: Path) -> None:
    """Produce a DB + WAL pair whose WAL tail fails checksum replay.

    Copies db+wal while a connection holds un-checkpointed writes, then flips
    the last bytes of the WAL so DuckDB raises "Failure while replaying WAL
    file ... Corrupt WAL file" on next connect.
    """
    import duckdb

    src_dir = db_path.parent
    tmp_dir = src_dir / "_wal_src"
    tmp_dir.mkdir(exist_ok=True)
    src_db = tmp_dir / "src.duckdb"
    conn = duckdb.connect(str(src_db))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t SELECT range FROM range(1000)")
        shutil.copy(src_db, db_path)
        shutil.copy(str(src_db) + ".wal", str(db_path) + ".wal")
    finally:
        conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
    # corrupt the WAL tail → checksum mismatch on replay
    with open(str(db_path) + ".wal", "r+b") as f:
        f.seek(-8, os.SEEK_END)
        f.write(b"\xff" * 8)


class TestGracefulClose:
    def test_graceful_close_clears_wal(self, tmp_path: Path):
        db = tmp_path / "cat.duckdb"
        cat = Catalog(db_path=db, repo_root=REPO_ROOT)
        _write_rows(cat)
        assert _wal_size(db) > 0 or db.exists()  # WAL may be lazy; writes happened
        cat.close()
        wal = _wal_path(db)
        assert not wal.exists() or wal.stat().st_size == 0


class TestPeriodicCheckpoint:
    def test_periodic_checkpoint_shrinks_wal(self, tmp_path: Path):
        db = tmp_path / "cat.duckdb"
        cat = Catalog(db_path=db, repo_root=REPO_ROOT)
        _write_rows(cat, n_batches=16, rows=1000)
        before = _wal_size(db)
        assert before > 0, "expected a non-empty WAL before checkpoint"
        cat.checkpoint()
        after = _wal_size(db)
        assert after < before
        # data survived the checkpoint
        assert cat.query("SELECT COUNT(*) AS n FROM wal_test")["n"][0] == 16000
        cat.close()

    def test_checkpoint_thread_lifecycle(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "cat.duckdb"
        # disabled
        monkeypatch.setenv("CQUANT_CHECKPOINT_INTERVAL_SEC", "0")
        cat = Catalog(db_path=db, repo_root=REPO_ROOT)
        assert cat._checkpoint_thread is None
        cat.close()

        # enabled with short interval
        monkeypatch.setenv("CQUANT_CHECKPOINT_INTERVAL_SEC", "0.2")
        cat2 = Catalog(db_path=db, repo_root=REPO_ROOT)
        assert cat2._checkpoint_thread is not None
        assert cat2._checkpoint_thread.is_alive()
        assert cat2._checkpoint_interval == 0.2
        cat2.close()
        assert not cat2._checkpoint_thread.is_alive()
        assert cat2._stop_event.is_set()

        # invalid env falls back to default
        monkeypatch.setenv("CQUANT_CHECKPOINT_INTERVAL_SEC", "not-a-number")
        cat3 = Catalog(db_path=db, repo_root=REPO_ROOT)
        assert cat3._checkpoint_interval == 600.0
        cat3.close()

    def test_periodic_checkpoint_runs_in_background(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "cat.duckdb"
        monkeypatch.setenv("CQUANT_CHECKPOINT_INTERVAL_SEC", "0.2")
        cat = Catalog(db_path=db, repo_root=REPO_ROOT)
        try:
            _write_rows(cat, n_batches=16, rows=1000)
            deadline = time.monotonic() + 5
            while _wal_size(db) > 0 and time.monotonic() < deadline:
                time.sleep(0.1)
            assert _wal_size(db) == 0, "background checkpoint should have flushed WAL"
        finally:
            cat.close()


class TestSelfHeal:
    def test_self_heal_corrupt_wal(self, tmp_path: Path):
        db = tmp_path / "cat.duckdb"
        _make_corrupt_wal(db)
        assert _wal_path(db).exists()

        cat = Catalog(db_path=db, repo_root=REPO_ROOT)
        try:
            # connection succeeded after quarantine
            quarantined = list(tmp_path.glob("cat.duckdb.wal.corrupt-*"))
            assert len(quarantined) == 1
            assert quarantined[0].stat().st_size > 0
            # the DB itself is still usable (pre-WAL state)
            cat.execute("CREATE TABLE IF NOT EXISTS healed (x INTEGER)")
            cat.execute("INSERT INTO healed VALUES (42)")
            assert cat.query("SELECT x FROM healed")["x"].to_list() == [42]
        finally:
            cat.close()

    def test_self_heal_no_false_positive_missing_dir(self, tmp_path: Path):
        # db path is a directory → plain IO error, no WAL signature
        db_as_dir = tmp_path / "notadb.duckdb"
        db_as_dir.mkdir()
        with pytest.raises(Exception):
            Catalog(db_path=db_as_dir, repo_root=REPO_ROOT)
        assert list(tmp_path.glob("*.corrupt-*")) == []

    def test_self_heal_no_false_positive_signature_match(self):
        benign = [
            "IO Error: Is a directory",
            "IO Error: No such file or directory",
            "PermissionError: [Errno 13] Permission denied",
            "Catalog Error: Table with name foo does not exist",
        ]
        for msg in benign:
            assert not _is_wal_corruption_error(Exception(msg)), msg
        corrupt = [
            'IO Error: Failure while replaying WAL file "x.wal": Corrupt WAL file: '
            "entry at byte position 4199 computed checksum 1 does not match stored checksum 2",
            "Internal Error: WriteAheadLog deserialization failed",
        ]
        for msg in corrupt:
            assert _is_wal_corruption_error(Exception(msg)), msg

    def test_self_heal_wal_helper_no_wal(self, tmp_path: Path):
        # no WAL file → nothing to quarantine
        assert _self_heal_wal(tmp_path / "missing.duckdb") is False
        # empty WAL → nothing worth quarantining
        db = tmp_path / "empty.duckdb"
        db.write_bytes(b"duckdb")
        _wal_path(db).write_bytes(b"")
        assert _self_heal_wal(db) is False
        assert _wal_path(db).exists()
