"""
Tests for backend/agent_run_tracker.py

Covers: start_run, complete_run, backfill, _validate_token_count, _db_path,
and the CLI entry point.

All persistence is isolated to tmp_path via the STATS_DB_PATH env var.
The real ~/.autonomous-forever-state/ is NEVER touched.

Run with:
    python3 -m pytest backend/tests/test_agent_run_tracker.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.agent_run_tracker as tracker_mod
from backend.agent_run_tracker import (
    _parse_orphan_id,
    _validate_token_count,
    attribute_orphans,
    backfill,
    complete_run,
    main,
    population,
    reconcile_open_runs,
    start_run,
)


# ---------------------------------------------------------------------------
# Fixtures — isolation via STATS_DB_PATH
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point STATS_DB_PATH to a temp file so no test ever touches real state.

    autouse=True means every test in this file gets this fixture automatically.
    """
    db_file = tmp_path / "test_stats.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(db_file))
    yield db_file


def _connect(db_file: Path):
    """Open a fresh DuckDB connection to the isolated db."""
    import duckdb
    return duckdb.connect(str(db_file))


def _fetch_run(db_file: Path, agent_id: str) -> dict | None:
    """Return the agent_run row for agent_id, or None if absent."""
    conn = _connect(db_file)
    try:
        row = conn.execute(
            "SELECT * FROM agent_run WHERE agent_id = ?", [agent_id]
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_run' ORDER BY ordinal_position"
        ).fetchall()]
        return dict(zip(cols, row))
    finally:
        conn.close()


def _fetch_all(db_file: Path) -> list[dict]:
    """Return all rows from agent_run as a list of dicts."""
    conn = _connect(db_file)
    try:
        rows = conn.execute("SELECT * FROM agent_run ORDER BY start_ts").fetchall()
        if not rows:
            return []
        cols = [d[0] for d in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_run' ORDER BY ordinal_position"
        ).fetchall()]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _db_path — env var routing
# ---------------------------------------------------------------------------


def test_db_path_uses_stats_db_path_env(tmp_path, monkeypatch):
    """STATS_DB_PATH env var takes priority over all other path logic."""
    custom = tmp_path / "custom.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(custom))
    result = tracker_mod._db_path()
    assert result == custom


# ---------------------------------------------------------------------------
# _validate_token_count
# ---------------------------------------------------------------------------


def test_validate_token_count_none_passthrough():
    assert _validate_token_count(None, "input_tok") is None


def test_validate_token_count_zero_is_valid():
    assert _validate_token_count(0, "input_tok") == 0


def test_validate_token_count_positive_passthrough():
    assert _validate_token_count(62000, "input_tok") == 62000


def test_validate_token_count_negative_rejected():
    assert _validate_token_count(-1, "input_tok") is None


def test_validate_token_count_float_rejected():
    assert _validate_token_count(1.5, "input_tok") is None  # type: ignore[arg-type]


def test_validate_token_count_string_rejected():
    assert _validate_token_count("100", "input_tok") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# start_run — recording a new run
# ---------------------------------------------------------------------------


def test_start_run_inserts_row(isolated_db):
    """start_run inserts a row with the given agent_id and role."""
    start_run(agent_id="executor-42-1000", role="executor")
    row = _fetch_run(isolated_db, "executor-42-1000")
    assert row is not None
    assert row["agent_id"] == "executor-42-1000"
    assert row["role"] == "executor"


def test_start_run_records_start_ts(isolated_db):
    """start_run sets start_ts to a recent UTC timestamp."""
    before = datetime.now(timezone.utc)
    start_run(agent_id="executor-42-1001", role="executor")
    after = datetime.now(timezone.utc)

    row = _fetch_run(isolated_db, "executor-42-1001")
    ts = row["start_ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    assert before <= ts <= after


def test_start_run_records_optional_fields(isolated_db):
    """start_run stores discussion, pr, event_id, and model when provided."""
    start_run(
        agent_id="executor-55-1002",
        role="executor",
        discussion=55,
        pr=123,
        event_id="executor-55-1002",
        model="claude-sonnet-4-6",
    )
    row = _fetch_run(isolated_db, "executor-55-1002")
    assert row["discussion"] == 55
    assert row["pr"] == 123
    assert row["event_id"] == "executor-55-1002"
    assert row["model"] == "claude-sonnet-4-6"


def test_start_run_leaves_end_ts_null(isolated_db):
    """start_run does not set end_ts — the run is still open."""
    start_run(agent_id="executor-99-1003", role="executor")
    row = _fetch_run(isolated_db, "executor-99-1003")
    assert row["end_ts"] is None


def test_start_run_idempotent_insert_or_ignore(isolated_db):
    """Calling start_run twice with the same agent_id does not raise or duplicate."""
    start_run(agent_id="executor-99-1004", role="executor")
    start_run(agent_id="executor-99-1004", role="executor")  # second call silently ignored
    all_rows = _fetch_all(isolated_db)
    matching = [r for r in all_rows if r["agent_id"] == "executor-99-1004"]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# complete_run — lifecycle transition (start → complete)
# ---------------------------------------------------------------------------


def test_complete_run_after_start_updates_row(isolated_db):
    """complete_run updates the existing row when start_run ran first."""
    start_run(agent_id="executor-10-2000", role="executor")
    complete_run(agent_id="executor-10-2000", verdict="done", input_tok=1000, output_tok=200)

    row = _fetch_run(isolated_db, "executor-10-2000")
    assert row["verdict"] == "done"
    assert row["input_tok"] == 1000
    assert row["output_tok"] == 200
    assert row["end_ts"] is not None


def test_complete_run_computes_duration(isolated_db):
    """complete_run computes duration_s from stored start_ts when not supplied."""
    start_run(agent_id="executor-10-2001", role="executor")
    # Small sleep not available; just call complete and verify duration >= 0
    complete_run(agent_id="executor-10-2001", verdict="done")

    row = _fetch_run(isolated_db, "executor-10-2001")
    assert row["duration_s"] is not None
    assert row["duration_s"] >= 0.0


def test_complete_run_accepts_explicit_duration(isolated_db):
    """complete_run stores an explicitly supplied duration_s unchanged."""
    start_run(agent_id="executor-10-2002", role="executor")
    complete_run(agent_id="executor-10-2002", verdict="done", duration_s=42.5)

    row = _fetch_run(isolated_db, "executor-10-2002")
    assert abs(row["duration_s"] - 42.5) < 0.001


def test_complete_run_without_start_creates_row(isolated_db):
    """complete_run with no prior start_run creates a row (upsert path)."""
    complete_run(
        agent_id="executor-no-start-99",
        verdict="fail",
        input_tok=500,
        output_tok=100,
    )
    row = _fetch_run(isolated_db, "executor-no-start-99")
    assert row is not None
    assert row["verdict"] == "fail"
    assert row["input_tok"] == 500
    # start_ts == end_ts for created-on-complete rows
    assert row["start_ts"] is not None
    assert row["end_ts"] is not None


def test_complete_run_stores_all_token_fields(isolated_db):
    """complete_run stores all supported token fields."""
    start_run(agent_id="executor-tok-3000", role="executor")
    complete_run(
        agent_id="executor-tok-3000",
        verdict="done",
        input_tok=60000,
        output_tok=8400,
        cache_read=1000,
        cache_write=500,
        cache_creation_tokens=200,
    )
    row = _fetch_run(isolated_db, "executor-tok-3000")
    assert row["input_tok"] == 60000
    assert row["output_tok"] == 8400
    assert row["cache_read"] == 1000
    assert row["cache_write"] == 500
    assert row["cache_creation_tokens"] == 200


def test_complete_run_stores_blocked_reason(isolated_db):
    """complete_run stores blocked_reason when the run was blocked."""
    start_run(agent_id="executor-blocked-4000", role="executor")
    complete_run(
        agent_id="executor-blocked-4000",
        verdict="fail",
        blocked_reason="sandbox blocked: git write outside worktree",
    )
    row = _fetch_run(isolated_db, "executor-blocked-4000")
    assert row["blocked_reason"] == "sandbox blocked: git write outside worktree"


def test_complete_run_stores_turn_counts(isolated_db):
    """complete_run stores first_write_turn and total_turns."""
    start_run(agent_id="executor-turns-5000", role="executor")
    complete_run(
        agent_id="executor-turns-5000",
        verdict="done",
        first_write_turn=3,
        total_turns=25,
    )
    row = _fetch_run(isolated_db, "executor-turns-5000")
    assert row["first_write_turn"] == 3
    assert row["total_turns"] == 25


def test_complete_run_coalesces_model(isolated_db):
    """complete_run fills in model when it was missing from start_run."""
    start_run(agent_id="executor-model-6000", role="executor")  # no model
    complete_run(agent_id="executor-model-6000", verdict="done", model="claude-opus-4")
    row = _fetch_run(isolated_db, "executor-model-6000")
    assert row["model"] == "claude-opus-4"


def test_complete_run_rejects_negative_tokens(isolated_db):
    """Negative token values are discarded — not stored as bad data."""
    start_run(agent_id="executor-neg-7000", role="executor")
    complete_run(
        agent_id="executor-neg-7000",
        verdict="done",
        input_tok=-1,
        output_tok=-500,
    )
    row = _fetch_run(isolated_db, "executor-neg-7000")
    assert row["input_tok"] is None
    assert row["output_tok"] is None


def test_complete_run_idempotent_second_call(isolated_db):
    """Calling complete_run twice does not raise and does not duplicate rows."""
    start_run(agent_id="executor-idem-8000", role="executor")
    complete_run(agent_id="executor-idem-8000", verdict="done")
    complete_run(agent_id="executor-idem-8000", verdict="done")
    all_rows = _fetch_all(isolated_db)
    matching = [r for r in all_rows if r["agent_id"] == "executor-idem-8000"]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Multiple runs — querying / filtering behaviour
# ---------------------------------------------------------------------------


def test_multiple_runs_stored_independently(isolated_db):
    """Multiple distinct agent_ids produce independent rows."""
    for i in range(5):
        start_run(agent_id=f"executor-multi-{i}", role="executor", discussion=100 + i)
        complete_run(agent_id=f"executor-multi-{i}", verdict="done")

    all_rows = _fetch_all(isolated_db)
    ids = {r["agent_id"] for r in all_rows}
    assert {f"executor-multi-{i}" for i in range(5)}.issubset(ids)


def test_rows_can_be_filtered_by_discussion(isolated_db):
    """DuckDB index on discussion lets us filter by discussion number."""
    start_run(agent_id="executor-disc-A", role="executor", discussion=200)
    start_run(agent_id="executor-disc-B", role="code-reviewer", discussion=201)
    complete_run(agent_id="executor-disc-A", verdict="done")
    complete_run(agent_id="executor-disc-B", verdict="pass")

    import duckdb
    conn = duckdb.connect(str(isolated_db))
    try:
        rows = conn.execute(
            "SELECT agent_id FROM agent_run WHERE discussion = 200"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "executor-disc-A"


def test_rows_can_be_filtered_by_role(isolated_db):
    """Rows with different roles remain distinguishable."""
    start_run(agent_id="executor-role-A", role="executor", discussion=300)
    start_run(agent_id="reviewer-role-B", role="code-reviewer", discussion=300)
    complete_run(agent_id="executor-role-A", verdict="done")
    complete_run(agent_id="reviewer-role-B", verdict="pass")

    import duckdb
    conn = duckdb.connect(str(isolated_db))
    try:
        rows = conn.execute(
            "SELECT agent_id FROM agent_run WHERE role = 'code-reviewer'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "reviewer-role-B"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_fetch_missing_agent_id_returns_none(isolated_db):
    """Querying for a nonexistent agent_id returns no rows."""
    start_run(agent_id="executor-real-9000", role="executor")
    row = _fetch_run(isolated_db, "executor-does-not-exist")
    assert row is None


def test_start_run_nonfatal_on_db_error(tmp_path, monkeypatch):
    """start_run swallows DB errors — the call never raises to the caller."""
    monkeypatch.setenv("STATS_DB_PATH", "/dev/null/impossible/path.duckdb")
    # Should not raise:
    start_run(agent_id="executor-error-safe", role="executor")


def test_complete_run_nonfatal_on_db_error(tmp_path, monkeypatch):
    """complete_run swallows DB errors — the call never raises to the caller."""
    monkeypatch.setenv("STATS_DB_PATH", "/dev/null/impossible/path.duckdb")
    # Should not raise:
    complete_run(agent_id="executor-error-safe", verdict="done")


# ---------------------------------------------------------------------------
# backfill — reconstructing runs from audit JSONL
# ---------------------------------------------------------------------------


def _write_audit(tmp_path: Path, entries: list[dict], suffix: str = "") -> Path:
    """Write audit entries to a temp JSONL file and return its path."""
    filename = f"audit.jsonl{suffix}"
    path = tmp_path / filename
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def test_backfill_reconstructs_complete_run(tmp_path, isolated_db):
    """backfill creates a row for an event_id with both start and complete events."""
    entries = [
        {
            "ts": "2026-05-01T10:00:00Z",
            "action": "spawn",
            "source": "spawn_agent",
            "event_id": "executor-42-1715000000",
            "actor": "executor",
            "new": json.dumps({"discussion": 42, "model": "claude-sonnet-4-6"}),
        },
        {
            "ts": "2026-05-01T10:05:00Z",
            "action": "agent_done",
            "source": "post_agent_hook",
            "event_id": "executor-42-1715000000",
            "new": json.dumps({
                "verdict": "done",
                "input_tokens": 8000,
                "output_tokens": 1200,
            }),
        },
    ]
    audit_path = _write_audit(tmp_path, entries)
    count = backfill(audit_path=audit_path, db_path=isolated_db)
    assert count == 1

    row = _fetch_run(isolated_db, "executor-42-1715000000")
    assert row is not None
    assert row["role"] == "executor"
    assert row["discussion"] == 42
    assert row["model"] == "claude-sonnet-4-6"
    assert row["verdict"] == "done"
    assert row["input_tok"] == 8000
    assert row["output_tok"] == 1200
    assert row["duration_s"] is not None
    assert row["duration_s"] > 0  # 5 minutes elapsed


def test_backfill_start_only_creates_open_row(tmp_path, isolated_db):
    """backfill creates an open row (end_ts=NULL) for a start-only event_id."""
    entries = [
        {
            "ts": "2026-05-01T11:00:00Z",
            "action": "spawn",
            "source": "spawn_agent",
            "event_id": "executor-99-1715001000",
            "actor": "executor",
            "new": json.dumps({"discussion": 99}),
        },
    ]
    audit_path = _write_audit(tmp_path, entries)
    count = backfill(audit_path=audit_path, db_path=isolated_db)
    assert count == 1

    row = _fetch_run(isolated_db, "executor-99-1715001000")
    assert row is not None
    assert row["end_ts"] is None
    assert row["verdict"] is None


def test_backfill_idempotent(tmp_path, isolated_db):
    """Running backfill twice on the same data does not produce duplicate rows."""
    entries = [
        {
            "ts": "2026-05-01T12:00:00Z",
            "action": "spawn",
            "source": "spawn_agent",
            "event_id": "executor-idem-bf",
            "actor": "executor",
            "new": json.dumps({"discussion": 10}),
        },
    ]
    audit_path = _write_audit(tmp_path, entries)
    backfill(audit_path=audit_path, db_path=isolated_db)
    backfill(audit_path=audit_path, db_path=isolated_db)

    all_rows = _fetch_all(isolated_db)
    matching = [r for r in all_rows if r["agent_id"] == "executor-idem-bf"]
    assert len(matching) == 1


def test_backfill_skips_entries_without_event_id(tmp_path, isolated_db):
    """Entries with no event_id are silently skipped."""
    entries = [
        {
            "ts": "2026-05-01T13:00:00Z",
            "action": "spawn",
            "source": "spawn_agent",
            # No event_id field
            "actor": "executor",
            "new": json.dumps({"discussion": 77}),
        },
    ]
    audit_path = _write_audit(tmp_path, entries)
    count = backfill(audit_path=audit_path, db_path=isolated_db)
    assert count == 0


def test_backfill_skips_entries_missing_role_and_start(tmp_path, isolated_db):
    """An event_id with no role or no start_ts is skipped (too little data)."""
    # Complete event only — no role, no start
    entries = [
        {
            "ts": "2026-05-01T14:00:00Z",
            "action": "agent_done",
            "source": "post_agent_hook",
            "event_id": "executor-role-missing",
            "new": json.dumps({"verdict": "done"}),
        },
    ]
    audit_path = _write_audit(tmp_path, entries)
    count = backfill(audit_path=audit_path, db_path=isolated_db)
    # No start event → no start_ts → row is skipped
    assert count == 0


def test_backfill_handles_corrupt_jsonl_lines(tmp_path, isolated_db):
    """Corrupt JSONL lines are skipped; valid entries still produce rows."""
    audit_path = tmp_path / "audit.jsonl"
    with audit_path.open("w") as fh:
        fh.write("{not valid json\n")
        fh.write(json.dumps({
            "ts": "2026-05-01T15:00:00Z",
            "action": "spawn",
            "source": "spawn_agent",
            "event_id": "executor-corrupt-test",
            "actor": "executor",
            "new": json.dumps({"discussion": 5}),
        }) + "\n")
    count = backfill(audit_path=audit_path, db_path=isolated_db)
    assert count == 1


def test_backfill_empty_audit_returns_zero(tmp_path, isolated_db):
    """backfill on an empty audit file returns 0."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    count = backfill(audit_path=audit_path, db_path=isolated_db)
    assert count == 0


def test_backfill_nonexistent_audit_returns_zero(tmp_path, isolated_db):
    """backfill on a non-existent audit path returns 0."""
    count = backfill(audit_path=tmp_path / "no-such-file.jsonl", db_path=isolated_db)
    assert count == 0


def test_backfill_multiple_event_ids(tmp_path, isolated_db):
    """backfill handles multiple distinct event_ids in the same file."""
    entries = []
    for i in range(3):
        entries.append({
            "ts": f"2026-05-01T1{i}:00:00Z",
            "action": "spawn",
            "source": "spawn_agent",
            "event_id": f"executor-multi-bf-{i}",
            "actor": "executor",
            "new": json.dumps({"discussion": 10 + i}),
        })
    audit_path = _write_audit(tmp_path, entries)
    count = backfill(audit_path=audit_path, db_path=isolated_db)
    assert count == 3
    all_rows = _fetch_all(isolated_db)
    assert len(all_rows) == 3


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_start_command(isolated_db):
    """CLI 'start' subcommand inserts a row and exits 0."""
    rc = main([
        "start",
        "--agent-id", "executor-cli-start",
        "--role", "executor",
        "--discussion", "77",
        "--pr", "99",
        "--model", "claude-sonnet-4-6",
    ])
    assert rc == 0
    row = _fetch_run(isolated_db, "executor-cli-start")
    assert row is not None
    assert row["role"] == "executor"
    assert row["discussion"] == 77
    assert row["pr"] == 99


def test_cli_complete_command(isolated_db):
    """CLI 'complete' subcommand upserts completion data and exits 0."""
    main([
        "start",
        "--agent-id", "executor-cli-complete",
        "--role", "executor",
    ])
    rc = main([
        "complete",
        "--agent-id", "executor-cli-complete",
        "--verdict", "done",
        "--input-tokens", "5000",
        "--output-tokens", "800",
    ])
    assert rc == 0
    row = _fetch_run(isolated_db, "executor-cli-complete")
    assert row["verdict"] == "done"
    assert row["input_tok"] == 5000
    assert row["output_tok"] == 800


def test_cli_backfill_command(tmp_path, isolated_db):
    """CLI 'backfill' subcommand processes audit file and exits 0."""
    entries = [
        {
            "ts": "2026-05-01T09:00:00Z",
            "action": "spawn",
            "source": "spawn_agent",
            "event_id": "executor-cli-bf",
            "actor": "executor",
            "new": json.dumps({"discussion": 1}),
        },
    ]
    audit_path = _write_audit(tmp_path, entries)
    rc = main([
        "backfill",
        "--audit-path", str(audit_path),
        "--db-path", str(isolated_db),
    ])
    assert rc == 0


def test_cli_no_subcommand_exits_1():
    """CLI with no subcommand exits 1."""
    rc = main([])
    assert rc == 1


# ---------------------------------------------------------------------------
# reconcile_open_runs — ghost detection + closure
# ---------------------------------------------------------------------------


def _insert_open_run(db_file: Path, agent_id: str, role: str, age_minutes: float) -> None:
    """Insert an open agent_run row with start_ts = now - age_minutes."""
    import duckdb
    from datetime import timedelta
    conn = duckdb.connect(str(db_file))
    try:
        tracker_mod._ensure_schema(conn)
        start_ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_run (agent_id, role, start_ts)
            VALUES (?, ?, ?)
            """,
            [agent_id, role, start_ts],
        )
    finally:
        conn.close()


def test_reconcile_three_row_invariant(isolated_db):
    """Load-bearing invariant: only the stale non-live row gets closed.

    Row (a): open, age > stale_after_min, agent_id IS in live_ids → preserved
    Row (b): open, age > stale_after_min, agent_id NOT in live_ids → CLOSED
    Row (c): open, age < stale_after_min, agent_id NOT in live_ids → preserved
    """
    stale_after_min = 30
    # (a) stale + live: must be preserved
    _insert_open_run(isolated_db, "exec-live-stale", "executor", stale_after_min + 5)
    # (b) stale + not-live: must be closed
    _insert_open_run(isolated_db, "exec-ghost", "executor", stale_after_min + 5)
    # (c) fresh + not-live: must be preserved (inside grace window)
    _insert_open_run(isolated_db, "exec-fresh", "executor", stale_after_min - 5)

    live_ids = ["exec-live-stale"]
    closed = reconcile_open_runs(
        live_ids=live_ids,
        stale_after_min=stale_after_min,
        db_path=isolated_db,
    )

    assert closed == 1, f"expected 1 closed, got {closed}"

    row_a = _fetch_run(isolated_db, "exec-live-stale")
    assert row_a["end_ts"] is None, "live stale row should remain open"

    row_b = _fetch_run(isolated_db, "exec-ghost")
    assert row_b["end_ts"] is not None, "ghost stale row should be closed"
    assert row_b["verdict"] == "reconciled-stale"

    row_c = _fetch_run(isolated_db, "exec-fresh")
    assert row_c["end_ts"] is None, "fresh row should remain open"


def test_reconcile_no_open_rows(isolated_db):
    """reconcile on a DB with no open rows returns 0."""
    start_run(agent_id="exec-closed", role="executor")
    complete_run(agent_id="exec-closed", verdict="done")
    closed = reconcile_open_runs(live_ids=[], stale_after_min=0, db_path=isolated_db)
    assert closed == 0


def test_reconcile_empty_db(tmp_path, monkeypatch):
    """reconcile on a non-existent DB file returns 0 without error."""
    phantom = tmp_path / "nonexistent.duckdb"
    closed = reconcile_open_runs(live_ids=[], stale_after_min=5, db_path=phantom)
    assert closed == 0


def test_reconcile_live_ids_none_treated_as_empty(isolated_db):
    """Passing live_ids=None behaves the same as live_ids=[]."""
    _insert_open_run(isolated_db, "exec-ghost-none", "executor", 60)
    closed = reconcile_open_runs(live_ids=None, stale_after_min=30, db_path=isolated_db)
    assert closed == 1
    row = _fetch_run(isolated_db, "exec-ghost-none")
    assert row["end_ts"] is not None


def test_reconcile_all_live(isolated_db):
    """When all open rows are in live_ids, nothing is closed."""
    _insert_open_run(isolated_db, "exec-running-a", "executor", 60)
    _insert_open_run(isolated_db, "exec-running-b", "executor", 90)
    closed = reconcile_open_runs(
        live_ids=["exec-running-a", "exec-running-b"],
        stale_after_min=30,
        db_path=isolated_db,
    )
    assert closed == 0


def test_reconcile_idempotent(isolated_db):
    """Running reconcile twice does not change row count or duplicate closures."""
    _insert_open_run(isolated_db, "exec-idempotent", "executor", 60)
    closed1 = reconcile_open_runs(live_ids=[], stale_after_min=30, db_path=isolated_db)
    closed2 = reconcile_open_runs(live_ids=[], stale_after_min=30, db_path=isolated_db)
    assert closed1 == 1
    assert closed2 == 0  # already closed


def test_reconcile_sets_duration(isolated_db):
    """reconcile sets duration_s when closing a row."""
    _insert_open_run(isolated_db, "exec-dur", "executor", 60)
    reconcile_open_runs(live_ids=[], stale_after_min=30, db_path=isolated_db)
    row = _fetch_run(isolated_db, "exec-dur")
    assert row["duration_s"] is not None
    assert row["duration_s"] >= 0


def test_cli_reconcile_command(isolated_db):
    """CLI 'reconcile' subcommand closes stale ghosts and exits 0."""
    _insert_open_run(isolated_db, "exec-cli-ghost", "executor", 60)
    rc = main([
        "reconcile",
        "--stale-after-min", "30",
        "--db-path", str(isolated_db),
    ])
    assert rc == 0
    row = _fetch_run(isolated_db, "exec-cli-ghost")
    assert row["end_ts"] is not None


def test_cli_reconcile_with_live_ids(isolated_db):
    """CLI 'reconcile --live-ids' preserves live rows."""
    _insert_open_run(isolated_db, "exec-live-cli", "executor", 60)
    rc = main([
        "reconcile",
        "--stale-after-min", "30",
        "--live-ids", "exec-live-cli",
        "--db-path", str(isolated_db),
    ])
    assert rc == 0
    row = _fetch_run(isolated_db, "exec-live-cli")
    assert row["end_ts"] is None, "live row should be preserved"


def test_cli_reconcile_stdout_contains_reconciled_colon(isolated_db, capsys):
    """CLI 'reconcile' stdout contains the literal string 'reconciled:' for grepping."""
    _insert_open_run(isolated_db, "exec-stdout-check", "executor", 60)
    rc = main([
        "reconcile",
        "--stale-after-min", "30",
        "--db-path", str(isolated_db),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "reconciled:" in captured.out, (
        f"Expected 'reconciled:' in stdout, got: {captured.out!r}"
    )


def test_reconcile_auto_close_verdict_is_reconciled_stale(isolated_db):
    """Auto-closed ghost rows must have verdict='reconciled-stale', not 'reconciled'.

    run-analyst distinguishes auto-closed ghosts from real completions by this value.
    """
    _insert_open_run(isolated_db, "exec-verdict-check", "executor", 60)
    closed = reconcile_open_runs(live_ids=[], stale_after_min=30, db_path=isolated_db)
    assert closed == 1
    row = _fetch_run(isolated_db, "exec-verdict-check")
    assert row["end_ts"] is not None
    assert row["verdict"] == "reconciled-stale", (
        f"Expected verdict='reconciled-stale', got {row['verdict']!r}. "
        "run-analyst needs this exact value to distinguish ghosts from completions."
    )


# ---------------------------------------------------------------------------
# _parse_orphan_id — D#2282 finding 2: string parsing, not fuzzy matching
# ---------------------------------------------------------------------------


class TestParseOrphanId:
    def test_parses_role_discussion_hex_shape(self):
        parsed = _parse_orphan_id("executor-2263-a04b46cb3d1f97a8e")
        assert parsed == {"role": "executor", "discussion": 2263, "hex": "04b46cb3d1f97a8e"}

    def test_parses_hyphenated_role(self):
        parsed = _parse_orphan_id("code-reviewer-2264-ad45be9005939de93")
        assert parsed["role"] == "code-reviewer"
        assert parsed["discussion"] == 2264

    def test_zero_discussion_still_parses(self):
        """-0- rows parse fine — they're skipped downstream for being
        unrecoverable, not because they fail to parse."""
        parsed = _parse_orphan_id("project-manager-0-a24bded05a236c34a")
        assert parsed["discussion"] == 0

    def test_non_conforming_shape_returns_none(self):
        assert _parse_orphan_id("pah-lifecycle-test-1846431") is None

    def test_missing_a_prefix_on_hex_returns_none(self):
        assert _parse_orphan_id("executor-2263-04b46cb3d1f97a8e") is None

    def test_empty_string_returns_none(self):
        assert _parse_orphan_id("") is None


# ---------------------------------------------------------------------------
# population() — D#2282 PR-b: measured fraction of agent_run rows with tokens
# ---------------------------------------------------------------------------


def _insert_run(
    db_file: Path,
    agent_id: str,
    role: str,
    discussion: int | None = None,
    input_tok: int = 0,
    output_tok: int = 0,
    verdict: str | None = None,
    age_minutes: float = 0,
) -> None:
    """Insert a completed-shaped agent_run row for population/attribution tests."""
    import duckdb
    from datetime import timedelta
    conn = duckdb.connect(str(db_file))
    try:
        tracker_mod._ensure_schema(conn)
        start_ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_run
                (agent_id, role, discussion, start_ts, input_tok, output_tok, verdict)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [agent_id, role, discussion, start_ts, input_tok, output_tok, verdict],
        )
    finally:
        conn.close()


class TestPopulation:
    def test_db_absent_reports_zero_rows_not_traceback(self, tmp_path):
        """Spec item 10: a missing DB file degrades to a zero-row report,
        exit-0-shaped, with a stderr note — never a traceback."""
        missing = tmp_path / "does-not-exist.duckdb"
        result = population(db_path=missing)
        assert result["rows"] == 0
        assert result["with_tokens"] == 0
        assert result["rate"] == 0.0
        assert result["scope"]
        assert result["host"]

    def test_rate_computed_from_populated_rows(self, isolated_db):
        _insert_run(isolated_db, "a-1", "executor", discussion=1, input_tok=10, output_tok=5)
        _insert_run(isolated_db, "a-2", "executor", discussion=1, input_tok=0, output_tok=0)
        _insert_run(isolated_db, "a-3", "executor", discussion=1, input_tok=0, output_tok=0)
        _insert_run(isolated_db, "a-4", "executor", discussion=1, input_tok=0, output_tok=0)
        result = population(db_path=isolated_db)
        assert result["rows"] == 4
        assert result["with_tokens"] == 1
        assert result["rate"] == pytest.approx(0.25)

    def test_empty_table_reports_zero_rate_not_division_error(self, isolated_db):
        import duckdb
        conn = duckdb.connect(str(isolated_db))
        tracker_mod._ensure_schema(conn)
        conn.close()
        result = population(db_path=isolated_db)
        assert result["rows"] == 0
        assert result["rate"] == 0.0

    def test_since_filters_to_recent_rows_and_names_it_in_scope(self, isolated_db):
        _insert_run(isolated_db, "old-1", "executor", discussion=1, age_minutes=600)
        _insert_run(isolated_db, "new-1", "executor", discussion=1, age_minutes=1)
        # A cutoff between the two ages excludes the 600-minutes-old row and
        # includes the 1-minute-old one.
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        result = population(since_iso=cutoff, db_path=isolated_db)
        assert result["rows"] == 1
        assert cutoff in result["scope"]

    def test_orphan_rows_and_parseable_counted_separately(self, isolated_db):
        _insert_run(isolated_db, "executor-42-adeadbeef00000001", "orphan-unmatched", input_tok=1, output_tok=1)
        _insert_run(isolated_db, "pah-lifecycle-test-1846431", "orphan-unmatched", input_tok=0, output_tok=0)
        result = population(db_path=isolated_db)
        assert result["orphan_rows"] == 2
        assert result["orphan_id_parseable"] == 1

    def test_cli_population_json(self, isolated_db, capsys):
        _insert_run(isolated_db, "a-1", "executor", discussion=1, input_tok=10, output_tok=5)
        rc = main(["population", "--json", "--db-path", str(isolated_db)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["rows"] == 1
        assert payload["with_tokens"] == 1


# ---------------------------------------------------------------------------
# attribute_orphans() — D#2282 PR-c: orphan attribution + dedupe + supersession
# ---------------------------------------------------------------------------


class TestAttributeOrphans:
    def test_dry_run_writes_nothing(self, isolated_db):
        _insert_run(isolated_db, "executor-99-adeadbeef00000001", "orphan-unmatched", input_tok=10, output_tok=20)
        result = attribute_orphans(dry_run=True, db_path=isolated_db)
        assert result["dry_run"] is True
        assert result["candidates"] == 1
        assert result["rows_written"] == 1  # projected, not actually written
        row = _fetch_run(isolated_db, "executor-99-adeadbeef00000001")
        assert row["role"] == "orphan-unmatched", "dry run must not write"
        assert row["discussion"] is None

    def test_real_run_promotes_role_and_discussion(self, isolated_db):
        _insert_run(isolated_db, "executor-99-adeadbeef00000001", "orphan-unmatched", input_tok=10, output_tok=20)
        result = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert result["rows_written"] == 1
        row = _fetch_run(isolated_db, "executor-99-adeadbeef00000001")
        assert row["role"] == "executor"
        assert row["discussion"] == 99

    def test_zero_discussion_rows_are_never_promoted(self, isolated_db):
        """-0- rows are not recoverable from the id — must stay orphaned."""
        _insert_run(isolated_db, "executor-0-adeadbeef00000001", "orphan-unmatched", input_tok=10, output_tok=20)
        result = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert result["candidates"] == 0
        assert result["skipped_zero_discussion"] == 1
        row = _fetch_run(isolated_db, "executor-0-adeadbeef00000001")
        assert row["role"] == "orphan-unmatched"

    def test_unparseable_ids_are_skipped(self, isolated_db):
        _insert_run(isolated_db, "pah-lifecycle-test-1846431", "orphan-unmatched", input_tok=10, output_tok=20)
        result = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert result["skipped_unparseable"] == 1
        assert result["rows_written"] == 0

    def test_unknown_role_prefix_is_skipped_not_guessed(self, isolated_db):
        """A parsed role outside CLAUDE.md's named-role set must not be
        promoted — skip rather than invent a role (Implementation Notes)."""
        _insert_run(isolated_db, "totally-made-up-role-42-adeadbeef00000001", "orphan-unmatched", input_tok=10, output_tok=20)
        result = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert result["skipped_invalid_role"] == 1
        row = _fetch_run(isolated_db, "totally-made-up-role-42-adeadbeef00000001")
        assert row["role"] == "orphan-unmatched"

    def test_duplicate_session_keeps_larger_snapshot_only(self, isolated_db):
        """D#2282 finding 3's exact regression fixture: the same session
        logged under two agent_ids sharing an a<hex> suffix must contribute
        exactly one row's tokens — the larger snapshot — not both summed."""
        _insert_run(
            isolated_db, "executor-2263-a04b46cb3d1f97a8e", "orphan-unmatched",
            input_tok=406, output_tok=51501, age_minutes=10,
        )
        _insert_run(
            isolated_db, "executor-0-a04b46cb3d1f97a8e", "orphan-unmatched",
            input_tok=498, output_tok=53135, age_minutes=5,
        )
        result = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert result["duplicate_groups"] == 1
        assert result["rows_superseded"] == 1

        rows = _fetch_all(isolated_db)
        by_id = {r["agent_id"]: r for r in rows}
        # The larger snapshot (53,633 I/O) is the one promoted to disc 2263.
        winner = by_id["executor-0-a04b46cb3d1f97a8e"]
        assert winner["discussion"] == 2263
        assert winner["role"] == "executor"
        loser = by_id["executor-2263-a04b46cb3d1f97a8e"]
        assert loser["verdict"] == "superseded"
        assert loser["role"] == "orphan-unmatched"

        total = sum(
            (r["input_tok"] or 0) + (r["output_tok"] or 0)
            for r in rows
            if r["discussion"] == 2263
        )
        assert total == 53633, "must not sum both snapshots (105,540)"

    def test_idempotent_second_run_finds_nothing(self, isolated_db):
        _insert_run(isolated_db, "executor-99-adeadbeef00000001", "orphan-unmatched", input_tok=10, output_tok=20)
        first = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert first["rows_written"] == 1
        second = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert second["candidates"] == 0
        assert second["rows_written"] == 0

    def test_zero_token_ghost_row_is_superseded(self, isolated_db):
        """A pre-registered zero-token draft for the same (role, discussion)
        as a newly-attributed orphan must be marked superseded, not left as
        a live zero (acceptance #14)."""
        _insert_run(isolated_db, "executor-2263-1788420051", "executor", discussion=2263, input_tok=0, output_tok=0)
        _insert_run(isolated_db, "executor-2263-a04b46cb3d1f97a8e", "orphan-unmatched", input_tok=406, output_tok=51501)
        result = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert result["zero_token_superseded"] == 1
        ghost = _fetch_run(isolated_db, "executor-2263-1788420051")
        assert ghost["verdict"] == "superseded"

    def test_lock_contention_degrades_to_zero_no_traceback(self, isolated_db, monkeypatch):
        """Spec constraint: must degrade to a no-op on lock contention,
        never raise, never partially write."""
        _insert_run(isolated_db, "executor-99-adeadbeef00000001", "orphan-unmatched", input_tok=10, output_tok=20)

        import duckdb as real_duckdb

        def _raise_lock_error(*args, **kwargs):
            raise real_duckdb.IOException("Could not set lock on file (simulated)")

        monkeypatch.setattr(real_duckdb, "connect", _raise_lock_error)
        result = attribute_orphans(dry_run=False, db_path=isolated_db)
        assert result["rows_written"] == 0
        assert result["candidates"] == 0

    def test_cli_attribute_orphans_dry_run_json(self, isolated_db, capsys):
        _insert_run(isolated_db, "executor-99-adeadbeef00000001", "orphan-unmatched", input_tok=10, output_tok=20)
        rc = main(["attribute-orphans", "--dry-run", "--json", "--db-path", str(isolated_db)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert payload["candidates"] == 1
        row = _fetch_run(isolated_db, "executor-99-adeadbeef00000001")
        assert row["role"] == "orphan-unmatched"
