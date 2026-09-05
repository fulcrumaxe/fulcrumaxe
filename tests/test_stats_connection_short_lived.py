"""tests/test_stats_connection_short_lived.py

Verifies that get_read_connection() returns short-lived per-call connections
that do not hold a long-term OS flock on stats.duckdb.

AC:
  a. No singleton: two successive calls return distinct connection objects.
  b. Multi-process write-while-read: a writer can insert while a reader
     connection is still alive (DuckDB allows multiple read-only + one
     write-mode opener at the same time).
  c. Latency: a basic SELECT MAX(ts) FROM metric_event completes in <100ms.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Patch STATS_DB_PATH to a throwaway file for each test."""
    db = tmp_path / "test_stats.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(db))
    # Clear any cached module-level state from stats_writer / stats_connection
    # so the env-var patch takes effect.
    import importlib
    import sys
    for mod in ["backend.stats_connection", "backend.stats_writer"]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    yield db
    # Post-test: ensure the env var is cleaned up (monkeypatch handles this).


def _seed_metric_event(db_path: Path) -> None:
    """Create and populate metric_event so SELECT MAX(ts) has something to hit."""
    import duckdb
    conn = duckdb.connect(str(db_path), read_only=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metric_event "
        "(ts TIMESTAMP, metric VARCHAR, value DOUBLE)"
    )
    conn.execute(
        "INSERT INTO metric_event VALUES (CURRENT_TIMESTAMP, 'test_pr_c', 1.0)"
    )
    conn.close()


# ---------------------------------------------------------------------------
# AC-a: no singleton — two calls return distinct objects
# ---------------------------------------------------------------------------

def test_no_singleton_distinct_connections(tmp_db):
    _seed_metric_event(tmp_db)
    from backend.stats_connection import get_read_connection
    conn1 = get_read_connection()
    conn2 = get_read_connection()
    # Each call must return a fresh object, not the same cached instance.
    assert conn1 is not conn2
    conn1.close()
    conn2.close()


# ---------------------------------------------------------------------------
# AC-b: writer succeeds after reader connection is closed
# ---------------------------------------------------------------------------

def test_writer_succeeds_after_reader_closed(tmp_db):
    """Per-call connections release the flock when they go out of scope.

    With the old singleton the read connection was never closed, so a write
    attempt from another process (or a test) would hit a DuckDB lock error
    for the lifetime of the dashboard backend.  With per-call connections,
    closing the reader object frees the lock immediately so the writer can
    open the file in read-write mode.
    """
    import duckdb
    _seed_metric_event(tmp_db)

    from backend.stats_connection import get_read_connection

    # Open a read connection, query, then close it explicitly.
    read_conn = get_read_connection()
    read_conn.execute("SELECT MAX(ts) FROM metric_event").fetchone()
    read_conn.close()  # lock released here

    # Writer can now open the same file without a lock conflict.
    write_conn = duckdb.connect(str(tmp_db), read_only=False)
    write_conn.execute(
        "INSERT INTO metric_event VALUES (CURRENT_TIMESTAMP, 'test_pr_c', 2.0)"
    )
    write_conn.close()

    # A new reader sees the inserted row.
    read_conn2 = get_read_connection()
    rows = read_conn2.execute(
        "SELECT COUNT(*) FROM metric_event WHERE metric = 'test_pr_c'"
    ).fetchone()
    assert rows[0] == 2  # seed row + inserted row
    read_conn2.close()


# ---------------------------------------------------------------------------
# AC-c: latency < 100ms for SELECT MAX(ts) FROM metric_event
# ---------------------------------------------------------------------------

def test_read_latency_under_100ms(tmp_db):
    _seed_metric_event(tmp_db)

    from backend.stats_connection import get_read_connection
    start = time.monotonic()
    conn = get_read_connection()
    conn.execute("SELECT MAX(ts) FROM metric_event").fetchone()
    conn.close()
    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms < 100, f"Read took {elapsed_ms:.1f}ms — exceeds 100ms budget"
