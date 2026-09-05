"""
tests/test_cleanup_pre_845_pm_dupes.py — verify cleanup-pre-845-pm-dupes.sh
identifies and marks the correct agent_run rows as 'superseded'.

Fixture:
  - 3 rows with end_ts IS NULL
  - 2 are project-managers with start_ts before the PR #845 cutoff (2026-05-14 10:25Z)
  - 1 is a project-manager but started AFTER the cutoff → must not be touched

DuckDB is single-writer, so the fixture closes its connection before running the
subprocess, then reopens for verification.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import duckdb

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cron" / "cleanup-pre-845-pm-dupes.sh"

PRE_1  = datetime(2026, 5, 14, 10, 20, 0, tzinfo=timezone.utc)  # before cutoff
PRE_2  = datetime(2026, 5, 14, 10, 22, 0, tzinfo=timezone.utc)  # before cutoff
POST   = datetime(2026, 5, 14, 10, 30, 0, tzinfo=timezone.utc)  # after cutoff


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Create a temp DuckDB with 3 NULL-end_ts rows and return its path."""
    db = str(tmp_path / "test_stats.duckdb")
    monkeypatch.setenv("STATS_DB_PATH", db)

    import importlib
    import backend.agent_run_tracker as art
    importlib.reload(art)

    conn = duckdb.connect(db)
    art._ensure_schema(conn)

    rows = [
        ("pm-orphan-1", "project-manager", PRE_1),
        ("pm-orphan-2", "project-manager", PRE_2),
        ("pm-after-cutoff", "project-manager", POST),
    ]
    for agent_id, role, start_ts in rows:
        conn.execute(
            "INSERT INTO agent_run (agent_id, role, start_ts) VALUES (?, ?, ?)",
            [agent_id, role, start_ts],
        )
    conn.commit()
    conn.close()  # release lock so subprocess can open the file
    return db


def _read_row(db: str, agent_id: str) -> dict:
    conn = duckdb.connect(db)
    try:
        row = conn.execute(
            "SELECT end_ts, verdict FROM agent_run WHERE agent_id = ?",
            [agent_id],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"Row {agent_id!r} not found"
    return {"end_ts": row[0], "verdict": row[1]}


def _run(db: str, *extra_args):
    return subprocess.run(
        ["bash", str(SCRIPT), *extra_args],
        capture_output=True, text=True,
        env={**os.environ, "STATS_DB_PATH": db},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCleanupScript:
    def test_identifies_pre_cutoff_rows(self, db_path):
        """Dry-run must report exactly 2 candidates and leave rows untouched."""
        result = _run(db_path, "--dry-run")
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        output = result.stdout + result.stderr
        assert "found 2 orphaned PM row(s)" in output, f"Unexpected output:\n{output}"
        assert "pm-orphan-1" in output
        assert "pm-orphan-2" in output
        # rows must be untouched after dry-run
        assert _read_row(db_path, "pm-orphan-1")["end_ts"] is None
        assert _read_row(db_path, "pm-orphan-2")["end_ts"] is None

    def test_does_not_touch_post_cutoff_row(self, db_path):
        """PM row started after the cutoff must stay NULL/unchanged."""
        result = _run(db_path)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        row = _read_row(db_path, "pm-after-cutoff")
        assert row["end_ts"] is None, "post-cutoff row should not be touched"
        assert row["verdict"] is None, "post-cutoff verdict should remain NULL"

    def test_marks_pre_cutoff_rows_superseded(self, db_path):
        """Both pre-cutoff PM rows get end_ts set and verdict='superseded'."""
        result = _run(db_path)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        output = result.stdout + result.stderr
        assert "updated 2 row(s)" in output, f"Expected 2 rows updated:\n{output}"
        for agent_id in ("pm-orphan-1", "pm-orphan-2"):
            row = _read_row(db_path, agent_id)
            assert row["end_ts"] is not None, f"{agent_id}: end_ts should be set"
            assert row["verdict"] == "superseded", f"{agent_id}: verdict should be 'superseded'"

    def test_idempotent(self, db_path):
        """Running the script twice must not raise errors; second run finds 0 candidates."""
        r1 = _run(db_path)
        r2 = _run(db_path)
        assert r1.returncode == 0, f"First run failed: {r1.stderr}"
        assert r2.returncode == 0, f"Second run failed: {r2.stderr}"
        assert "found 0 orphaned PM row(s)" in (r2.stdout + r2.stderr)
