"""Tests for CLI lock-conflict fallback (read-only degradation)."""

from __future__ import annotations

import pytest

from cquant.cli import main as cli_main


class _FakeCatalog:
    def __init__(self, path, read_only: bool = False) -> None:
        self.read_only = read_only
        self.path = path

    def initialize(self) -> None:
        if self.read_only:
            raise RuntimeError("initialize() must not run in read-only mode")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _LockError(Exception):
    def __str__(self) -> str:
        return "IOException: Could not set lock on file ... Conflicting lock is held"


def test_no_conflict_uses_read_write(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_catalog(path, read_only: bool = False):
        calls.append({"path": path, "read_only": read_only})
        return _FakeCatalog(path, read_only=read_only)

    monkeypatch.setattr(cli_main, "Catalog", fake_catalog)
    cat = cli_main._open_catalog("x.duckdb", "status")
    assert cat.read_only is False
    assert calls == [{"path": "x.duckdb", "read_only": False}]


def test_lock_conflict_read_only_command_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[bool] = []

    def fake_catalog(path, read_only: bool = False):
        attempts.append(read_only)
        if not read_only:
            raise _LockError()
        return _FakeCatalog(path, read_only=True)

    monkeypatch.setattr(cli_main, "Catalog", fake_catalog)
    cat = cli_main._open_catalog("x.duckdb", "status")
    assert cat.read_only is True
    assert attempts == [False, True]  # first RW attempt conflicted, RO retry ok


def test_lock_conflict_write_command_exits(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def fake_catalog(path, read_only: bool = False):
        if not read_only:
            raise _LockError()
        return _FakeCatalog(path, read_only=True)

    monkeypatch.setattr(cli_main, "Catalog", fake_catalog)
    with pytest.raises(SystemExit) as exc:
        cli_main._open_catalog("x.duckdb", "ingest")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "write access" in err


def test_lock_conflict_read_only_connect_fails_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_catalog(path, read_only: bool = False):
        raise _LockError()

    monkeypatch.setattr(cli_main, "Catalog", fake_catalog)
    with pytest.raises(_LockError):
        cli_main._open_catalog("x.duckdb", "status")


def test_non_lock_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_catalog(path, read_only: bool = False):
        raise ValueError("something else")

    monkeypatch.setattr(cli_main, "Catalog", fake_catalog)
    with pytest.raises(ValueError):
        cli_main._open_catalog("x.duckdb", "status")


def test_ensure_schema_skipped_in_read_only() -> None:
    ro = _FakeCatalog("x", read_only=True)
    cli_main._ensure_schema(ro)  # must be a no-op, not raise
    rw = _FakeCatalog("x", read_only=False)
    initialized: list[bool] = []

    def fake_init():
        initialized.append(True)

    rw.initialize = fake_init  # type: ignore[method-assign]
    cli_main._ensure_schema(rw)
    assert initialized == [True]


def test_is_lock_conflict_matching() -> None:
    assert cli_main._is_lock_conflict(_LockError())
    assert not cli_main._is_lock_conflict(ValueError("boom"))
