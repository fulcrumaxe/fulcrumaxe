"""Tests for test-fixture agent_id filtering in stuck_runs().

Acceptance criteria:
1. Without the filter, an 'idem-test-...' agent appears in stuck results.
2. With the filter active, that same agent is excluded.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import duckdb

import backend.agent_run_reader as reader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_with_stuck_agents(path: str) -> None:
    """Create a DuckDB file with two stuck runs: one test-fixture, one real."""
    conn = duckdb.connect(path)
    conn.execute("""
        CREATE TABLE agent_run (
            agent_id      VARCHAR,
            role          VARCHAR,
            discussion    INTEGER,
            pr            INTEGER,
            start_ts      TIMESTAMPTZ,
            end_ts        TIMESTAMPTZ,
            duration_s    DOUBLE,
            verdict       VARCHAR,
            model         VARCHAR,
            input_tok     INTEGER,
            output_tok    INTEGER,
            cache_read    INTEGER,
            cache_write   INTEGER,
            blocked_reason VARCHAR,
            event_id      VARCHAR
        )
    """)
    # A real stuck agent — started 2h ago, no end_ts
    real_start = datetime.now(timezone.utc) - timedelta(hours=2)
    # A test-fixture stuck agent — same age, should be filtered
    test_start = datetime.now(timezone.utc) - timedelta(hours=2)

    conn.execute(
        "INSERT INTO agent_run VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
        ["executor-real-12345", "executor", 100, None, real_start],
    )
    conn.execute(
        "INSERT INTO agent_run VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
        ["idem-test-1778585181", "executor", None, None, test_start],
    )
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_without_filter_idem_test_appears(tmp_path: Path) -> None:
    """Sanity check: the idem-test agent IS in the DB as a stuck row."""
    db_file = str(tmp_path / "stats.duckdb")
    _make_db_with_stuck_agents(db_file)

    # Bypass the filter by querying directly — confirm the fixture is present.
    conn = duckdb.connect(db_file, read_only=True)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=1800)
    rows = conn.execute(
        "SELECT agent_id FROM agent_run WHERE end_ts IS NULL AND start_ts < ?",
        [cutoff],
    ).fetchall()
    conn.close()

    agent_ids = [r[0] for r in rows]
    assert "idem-test-1778585181" in agent_ids, (
        "Test fixture should appear when filter is not applied"
    )


def test_stuck_runs_excludes_test_fixtures(tmp_path: Path) -> None:
    """stuck_runs() must not return idem-test-... or test-... agent_ids."""
    db_file = str(tmp_path / "stats.duckdb")
    _make_db_with_stuck_agents(db_file)

    with patch.object(reader, "_db_path", return_value=Path(db_file)):
        # Re-open as read-write so reader._connect can open read_only=True on it
        result = reader.stuck_runs(threshold_seconds=1800)

    agent_ids = [r["agent_id"] for r in result]
    assert "idem-test-1778585181" not in agent_ids, (
        "idem-test-... fixtures must be filtered from stuck_runs"
    )
    assert "executor-real-12345" in agent_ids, (
        "Real stuck agents must still appear"
    )


def test_stuck_runs_excludes_test_prefix(tmp_path: Path) -> None:
    """stuck_runs() must also filter agent_ids starting with 'test-'."""
    db_file = str(tmp_path / "stats.duckdb")
    conn = duckdb.connect(db_file)
    conn.execute("""
        CREATE TABLE agent_run (
            agent_id VARCHAR, role VARCHAR, discussion INTEGER, pr INTEGER,
            start_ts TIMESTAMPTZ, end_ts TIMESTAMPTZ, duration_s DOUBLE,
            verdict VARCHAR, model VARCHAR, input_tok INTEGER, output_tok INTEGER,
            cache_read INTEGER, cache_write INTEGER, blocked_reason VARCHAR,
            event_id VARCHAR
        )
    """)
    old_start = datetime.now(timezone.utc) - timedelta(hours=3)
    conn.execute(
        "INSERT INTO agent_run VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
        ["test-fixture-abc", "executor", None, None, old_start],
    )
    conn.close()

    with patch.object(reader, "_db_path", return_value=Path(db_file)):
        result = reader.stuck_runs(threshold_seconds=1800)

    agent_ids = [r["agent_id"] for r in result]
    assert "test-fixture-abc" not in agent_ids, (
        "test-... fixtures must be filtered from stuck_runs"
    )
