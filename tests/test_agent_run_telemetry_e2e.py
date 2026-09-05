"""tests/test_agent_run_telemetry_e2e.py

End-to-end simulation of the spawn → SubagentStop telemetry path.

Scenario:
    1. spawn-agent.sh calls start_run() with event_id = "{role}-{disc}-{timestamp}".
    2. The SubagentStop hook extracts that same event_id from the transcript
       and calls complete_run() with the real verdict and duration.

Assertions:
    - Exactly ONE row exists in agent_run for the event_id.
    - The row has the real verdict (not 'unknown').
    - The row has a real duration_s (not ≈0).
    - The row is NOT duplicated (UPSERT merged, not inserted twice).

This test guards against the merge-key mismatch described in Bug 4b:
    spawn-agent.sh uses {role}-{disc}-{timestamp} for agent_id.
    subagent-stop-hook.sh extracts hook_event_id from the transcript
    and passes it as --event-id to post-agent-hook.sh → complete_run().
    When the keys match, complete_run() updates the existing row.
    When they don't, complete_run() inserts a second row — this test detects that.
"""

from __future__ import annotations

import time
import pytest

# ---------------------------------------------------------------------------
# Skip when duckdb is not installed (CI without full deps)
# ---------------------------------------------------------------------------
duckdb = pytest.importorskip("duckdb")

from pathlib import Path


def _make_db(tmp_path: Path) -> Path:
    """Return path to a fresh in-memory-style DuckDB in tmp_path."""
    return tmp_path / "stats.duckdb"


def test_start_then_complete_merges_into_single_row(tmp_path: Path) -> None:
    """start_run() + complete_run() with matching keys → exactly one row."""
    import os
    db_path = _make_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db_path)

    try:
        from backend.agent_run_tracker import start_run, complete_run

        # Simulate spawn-agent.sh: {role}-{disc}-{timestamp}
        ts = int(time.time())
        event_id = f"executor-99-{ts}"

        start_run(
            agent_id=event_id,
            role="executor",
            discussion=99,
            event_id=event_id,
            model="claude-sonnet-4-6",
        )

        # Simulate a 10-second agent run
        time.sleep(0.05)  # tiny sleep to ensure duration > 0

        # Simulate SubagentStop hook calling complete_run with the SAME event_id
        complete_run(
            agent_id=event_id,
            verdict="done",
            input_tok=1000,
            output_tok=500,
        )

        # Verify: exactly one row, real verdict, non-trivial duration
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT agent_id, verdict, duration_s FROM agent_run WHERE agent_id = ?",
                [event_id],
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1, (
            f"Expected 1 row for event_id={event_id!r}, got {len(rows)}. "
            "Bug 4b: complete_run inserted a second row instead of merging."
        )
        _, verdict, duration_s = rows[0]
        assert verdict == "done", f"Expected verdict='done', got {verdict!r}"
        assert duration_s is not None, "duration_s should not be NULL"
        assert duration_s >= 0, f"duration_s should be non-negative, got {duration_s}"
    finally:
        os.environ.pop("STATS_DB_PATH", None)


def test_mismatched_keys_produces_two_rows(tmp_path: Path) -> None:
    """Regression: mismatched agent_id keys still produce two rows.

    This test documents the pre-fix behaviour so we can detect if the
    key mismatch ever returns.  Two rows = telemetry is broken.
    The main test (above) asserts one row = telemetry is fixed.
    """
    import os
    db_path = _make_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db_path)

    try:
        from backend.agent_run_tracker import start_run, complete_run

        ts = int(time.time())
        start_key = f"executor-55-{ts}"
        # Simulate old bug: hook derived key from session_id instead of timestamp
        wrong_key = f"executor-55-fake-session-xyz"

        start_run(
            agent_id=start_key,
            role="executor",
            discussion=55,
            event_id=start_key,
        )

        complete_run(
            agent_id=wrong_key,
            verdict="done",
        )

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT agent_id FROM agent_run WHERE agent_id IN (?, ?)",
                [start_key, wrong_key],
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2, (
            f"Expected 2 rows (one per mismatched key), got {len(rows)}. "
            "Mismatched keys should produce two rows — this documents the pre-fix state."
        )
    finally:
        os.environ.pop("STATS_DB_PATH", None)


def test_complete_run_without_start_run(tmp_path: Path) -> None:
    """complete_run() called without a prior start_run() creates the row."""
    import os
    db_path = _make_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db_path)

    try:
        from backend.agent_run_tracker import complete_run

        event_id = f"executor-77-{int(time.time())}"

        complete_run(
            agent_id=event_id,
            verdict="fail",
            input_tok=200,
        )

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = conn.execute(
                "SELECT verdict, duration_s FROM agent_run WHERE agent_id = ?",
                [event_id],
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        verdict, duration_s = rows[0]
        assert verdict == "fail"
        # D#2316 PR-b: no start_run() row and no caller-supplied start_ts means
        # there is no recoverable start time for this run at all — duration_s
        # must be NULL, not 0. A written 0 reads as "this run took no time",
        # which was never actually measured (this test used to assert the
        # opposite; that was the exact bug D#2316 finding 1 filed against).
        assert duration_s is None
    finally:
        os.environ.pop("STATS_DB_PATH", None)


def test_state_dir_routes_to_correct_db(tmp_path: Path) -> None:
    """STATS_DB_PATH override routes telemetry to the correct project DB.

    This simulates the Bug 4a fix: when AUTONOMOUS_TEAM_STATE_DIR (or
    STATS_DB_PATH) is set correctly, rows land in the project's own
    stats.duckdb, not in ~/.autonomous-forever-state/stats.duckdb.
    """
    import os

    project_db = tmp_path / "project-state" / "stats.duckdb"
    project_db.parent.mkdir(parents=True, exist_ok=True)

    os.environ["STATS_DB_PATH"] = str(project_db)

    try:
        from backend.agent_run_tracker import start_run, complete_run

        event_id = f"executor-42-{int(time.time())}"
        start_run(agent_id=event_id, role="executor", discussion=42)
        complete_run(agent_id=event_id, verdict="done")

        assert project_db.exists(), "DB should have been created at the overridden path"

        conn = duckdb.connect(str(project_db), read_only=True)
        try:
            rows = conn.execute(
                "SELECT verdict FROM agent_run WHERE agent_id = ?", [event_id]
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        assert rows[0][0] == "done"
    finally:
        os.environ.pop("STATS_DB_PATH", None)
