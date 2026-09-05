"""
tests/test_stats_writer_lock.py — regression test for D#888 PR-a.

D#888 scenario: backend.server holds a long-lived DuckDB connection.
When stats_writer tries to write, DuckDB's file lock conflicts.

Pre-fix behavior: the exception was silently swallowed by the bash hook,
which then marked the stats_metrics step complete anyway.

Post-fix behavior: stats_writer.record() raises IOError on duckdb.IOException
so that callers (and the bash hook) can detect and surface the failure.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def sw(tmp_path, monkeypatch):
    """Import stats_writer with STATS_DB_PATH redirected to a temp file."""
    db = str(tmp_path / "test_lock.duckdb")
    monkeypatch.setenv("STATS_DB_PATH", db)
    import importlib
    import backend.stats_writer as _sw
    importlib.reload(_sw)
    return _sw


def test_writer_raises_ioerror_on_lock_conflict(sw):
    """
    When duckdb.connect() raises IOException (lock conflict from another process
    holding the DB open, e.g. backend.server), record() must re-raise as IOError.

    Pre-fix: duckdb.IOException propagated as a non-IOError type; the bash hook
    did not detect it and silently marked the step complete anyway.

    Post-fix: the exception is wrapped in IOError so that:
      - The hook's exit-code check catches it
      - The hook step is NOT marked complete
      - The error is surfaced in the hook log
    """
    import duckdb

    def mock_connect(*args, **kwargs):
        raise duckdb.IOException("test: simulated cross-process lock conflict")

    # stats_writer imports duckdb lazily inside the function; patch the module
    with patch("duckdb.connect", side_effect=mock_connect):
        with pytest.raises(IOError, match="lock conflict"):
            sw.record("test_888", 1.0, "count")


def test_writer_raises_ioerror_on_lock_conflict_loop_iter(sw):
    """Same guarantee for record_loop_iter() — also uses duckdb.connect()."""
    import duckdb

    def mock_connect(*args, **kwargs):
        raise duckdb.IOException("test: simulated cross-process lock conflict")

    with patch("duckdb.connect", side_effect=mock_connect):
        with pytest.raises(IOError, match="lock conflict"):
            sw.record_loop_iter(duration_s=1.0)


def test_writer_succeeds_without_contention(sw):
    """Normal write path: no exception, metric is stored."""
    sw.record("test_888_no_contention", 42.0, "count", source="regression-test")
    # Verify it landed in the DB
    import duckdb
    db_path = os.environ["STATS_DB_PATH"]
    conn = duckdb.connect(db_path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT value FROM metric_event WHERE metric = ?",
            ["test_888_no_contention"],
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(42.0,)]
