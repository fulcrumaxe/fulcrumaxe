"""
tests/test_agent_run_tracker.py — Schema creation, start_run, complete_run, backfill.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tracker(tmp_path, monkeypatch):
    """Import agent_run_tracker with STATS_DB_PATH redirected to a temp file."""
    db = str(tmp_path / "test_stats.duckdb")
    monkeypatch.setenv("STATS_DB_PATH", db)
    # Re-import so module-level _db_path() sees the env var
    import importlib
    import backend.agent_run_tracker as art
    importlib.reload(art)
    return art


@pytest.fixture
def duckdb_conn(tracker, tmp_path):
    """Return a live DuckDB connection to the test database after schema init."""
    import duckdb
    db = os.environ["STATS_DB_PATH"]
    # Trigger schema creation by calling start_run once
    tracker.start_run(
        agent_id="schema-probe",
        role="test",
    )
    conn = duckdb.connect(db)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestSchema:
    def test_table_exists_after_start_run(self, tracker):
        """start_run creates the agent_run table if absent."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="init-test", role="executor")
        conn = duckdb.connect(db)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        conn.close()
        assert "agent_run" in tables

    def test_schema_columns(self, duckdb_conn):
        """agent_run has all required columns."""
        cols = {
            row[0]
            for row in duckdb_conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='agent_run'"
            ).fetchall()
        }
        required = {
            "agent_id", "role", "discussion", "pr",
            "start_ts", "end_ts", "duration_s", "verdict",
            "model", "input_tok", "output_tok",
            "cache_read", "cache_write", "cache_creation_tokens",
            "blocked_reason", "event_id",
        }
        assert required.issubset(cols), f"Missing columns: {required - cols}"

    def test_role_start_index_exists(self, duckdb_conn):
        """idx_agent_run_role_start index is created."""
        indexes = [
            r[0]
            for r in duckdb_conn.execute(
                "SELECT index_name FROM duckdb_indexes() "
                "WHERE table_name='agent_run'"
            ).fetchall()
        ]
        assert "idx_agent_run_role_start" in indexes

    def test_pr_index_exists(self, duckdb_conn):
        """idx_agent_run_pr index is created."""
        indexes = [
            r[0]
            for r in duckdb_conn.execute(
                "SELECT index_name FROM duckdb_indexes() "
                "WHERE table_name='agent_run'"
            ).fetchall()
        ]
        assert "idx_agent_run_pr" in indexes


# ---------------------------------------------------------------------------
# start_run tests
# ---------------------------------------------------------------------------

class TestStartRun:
    def test_inserts_row(self, tracker):
        """start_run inserts exactly one row."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="run-1", role="executor", discussion=635, pr=42)
        conn = duckdb.connect(db)
        rows = conn.execute("SELECT * FROM agent_run WHERE agent_id='run-1'").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_start_ts_is_set(self, tracker):
        """start_run sets start_ts to a non-null timestamp."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        before = datetime.now(timezone.utc)
        tracker.start_run(agent_id="run-ts", role="code-reviewer")
        after = datetime.now(timezone.utc)
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT start_ts FROM agent_run WHERE agent_id='run-ts'"
        ).fetchone()
        conn.close()
        assert row is not None
        ts = row[0]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert before <= ts <= after

    def test_end_ts_is_null_on_start(self, tracker):
        """end_ts is NULL after start_run (not yet complete)."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="run-open", role="project-manager")
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT end_ts FROM agent_run WHERE agent_id='run-open'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None

    def test_idempotent_on_duplicate_agent_id(self, tracker):
        """Second start_run with same agent_id does not raise or duplicate."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="run-dup", role="executor")
        tracker.start_run(agent_id="run-dup", role="executor")
        conn = duckdb.connect(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_run WHERE agent_id='run-dup'"
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_optional_fields_stored(self, tracker):
        """discussion, pr, event_id, model are stored when provided."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(
            agent_id="run-full",
            role="security-reviewer",
            discussion=100,
            pr=55,
            event_id="security-reviewer-100-999",
            model="claude-sonnet-4-6",
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT discussion, pr, event_id, model "
            "FROM agent_run WHERE agent_id='run-full'"
        ).fetchone()
        conn.close()
        assert row is not None
        disc, pr, eid, model = row
        assert disc == 100
        assert pr == 55
        assert eid == "security-reviewer-100-999"
        assert model == "claude-sonnet-4-6"

    def test_non_fatal_on_bad_db_path(self, monkeypatch):
        """start_run does not raise even if DB path is unwritable."""
        monkeypatch.setenv("STATS_DB_PATH", "/nonexistent_dir/stats.duckdb")
        import importlib
        import backend.agent_run_tracker as art
        importlib.reload(art)
        # Should not raise — the exception is swallowed
        art.start_run(agent_id="no-raise", role="executor")


# ---------------------------------------------------------------------------
# complete_run tests
# ---------------------------------------------------------------------------

class TestCompleteRun:
    def test_sets_end_ts(self, tracker):
        """complete_run sets end_ts to a non-null value."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="comp-1", role="executor")
        tracker.complete_run(agent_id="comp-1", verdict="done")
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT end_ts FROM agent_run WHERE agent_id='comp-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is not None

    def test_sets_verdict(self, tracker):
        """complete_run stores the verdict."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="comp-v", role="code-reviewer")
        tracker.complete_run(agent_id="comp-v", verdict="pass")
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT verdict FROM agent_run WHERE agent_id='comp-v'"
        ).fetchone()
        conn.close()
        assert row[0] == "pass"

    def test_computes_duration_s(self, tracker):
        """complete_run computes duration_s from start_ts and end_ts."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="comp-dur", role="executor")
        tracker.complete_run(agent_id="comp-dur", verdict="done")
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT duration_s FROM agent_run WHERE agent_id='comp-dur'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is not None
        assert row[0] >= 0.0

    def test_stores_token_counts(self, tracker):
        """complete_run stores input/output/cache token counts."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="comp-tok", role="executor")
        tracker.complete_run(
            agent_id="comp-tok",
            verdict="done",
            input_tok=62000,
            output_tok=8400,
            cache_read=1000,
            cache_write=200,
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT input_tok, output_tok, cache_read, cache_write "
            "FROM agent_run WHERE agent_id='comp-tok'"
        ).fetchone()
        conn.close()
        assert row == (62000, 8400, 1000, 200)

    def test_stores_blocked_reason(self, tracker):
        """complete_run stores blocked_reason when provided."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="comp-blk", role="executor")
        tracker.complete_run(
            agent_id="comp-blk",
            verdict="fail",
            blocked_reason="budget_exceeded",
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT blocked_reason FROM agent_run WHERE agent_id='comp-blk'"
        ).fetchone()
        conn.close()
        assert row[0] == "budget_exceeded"

    def test_explicit_end_ts_accepted(self, tracker):
        """complete_run uses an explicitly supplied end_ts."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="comp-ets", role="executor")
        explicit_end = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
        tracker.complete_run(agent_id="comp-ets", verdict="done", end_ts=explicit_end)
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT end_ts FROM agent_run WHERE agent_id='comp-ets'"
        ).fetchone()
        conn.close()
        assert row is not None
        stored = row[0]
        if isinstance(stored, str):
            stored = datetime.fromisoformat(stored.replace("Z", "+00:00"))
        if hasattr(stored, "tzinfo") and stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        assert stored == explicit_end

    def test_non_fatal_on_missing_row(self, tracker):
        """complete_run on a missing agent_id does not raise."""
        tracker.complete_run(agent_id="ghost-run", verdict="done")

    def test_non_fatal_on_bad_db_path(self, monkeypatch):
        """complete_run does not raise if DB is unwritable."""
        monkeypatch.setenv("STATS_DB_PATH", "/nonexistent_dir/stats.duckdb")
        import importlib
        import backend.agent_run_tracker as art
        importlib.reload(art)
        art.complete_run(agent_id="no-raise", verdict="done")


# ---------------------------------------------------------------------------
# Backfill tests
# ---------------------------------------------------------------------------

class TestBackfill:
    def _make_audit(self, tmp_path: Path, entries: list[dict]) -> Path:
        audit = tmp_path / "audit.jsonl"
        with audit.open("w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return audit

    def test_inserts_rows_from_audit(self, tracker, tmp_path):
        """backfill creates agent_run rows from audit entries with event_id."""
        import duckdb
        db_path = Path(os.environ["STATS_DB_PATH"])
        entries = [
            {
                "ts": "2026-05-01T10:00:00Z",
                "source": "spawn_agent",
                "action": "spawn",
                "actor": "executor",
                "event_id": "executor-635-1001",
                "new": {
                    "role": "executor",
                    "discussion": 635,
                    "pr": 42,
                    "event_id": "executor-635-1001",
                    "model": "claude-sonnet-4-6",
                },
            },
            {
                "ts": "2026-05-01T10:05:00Z",
                "source": "post_agent_hook",
                "action": "agent_done",
                "actor": "executor",
                "event_id": "executor-635-1001",
                "new": {
                    "verdict": "done",
                    "tokens": {"input": 5000, "output": 500},
                    "event_id": "executor-635-1001",
                },
            },
        ]
        audit_path = self._make_audit(tmp_path, entries)

        import backend.agent_run_tracker as art
        n = art.backfill(audit_path=audit_path, db_path=db_path)
        assert n >= 1

        conn = duckdb.connect(str(db_path))
        row = conn.execute(
            "SELECT role, discussion, pr, verdict "
            "FROM agent_run WHERE agent_id='executor-635-1001'"
        ).fetchone()
        conn.close()
        assert row is not None
        role, disc, pr, verdict = row
        assert role == "executor"
        assert disc == 635
        assert pr == 42
        assert verdict == "done"

    def test_backfill_idempotent(self, tracker, tmp_path):
        """Running backfill twice produces no duplicate rows."""
        import duckdb
        db_path = Path(os.environ["STATS_DB_PATH"])
        entries = [
            {
                "ts": "2026-05-01T10:00:00Z",
                "source": "spawn_agent",
                "action": "spawn",
                "actor": "code-reviewer",
                "event_id": "code-reviewer-10-2001",
                "new": {
                    "role": "code-reviewer",
                    "discussion": 10,
                    "event_id": "code-reviewer-10-2001",
                },
            },
        ]
        audit_path = self._make_audit(tmp_path, entries)

        import backend.agent_run_tracker as art
        art.backfill(audit_path=audit_path, db_path=db_path)
        art.backfill(audit_path=audit_path, db_path=db_path)

        conn = duckdb.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_run WHERE agent_id='code-reviewer-10-2001'"
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_backfill_tolerates_empty_audit(self, tracker, tmp_path):
        """backfill with an empty audit file returns 0 without raising."""
        import backend.agent_run_tracker as art
        db_path = Path(os.environ["STATS_DB_PATH"])
        audit_path = tmp_path / "empty.jsonl"
        audit_path.write_text("")
        n = art.backfill(audit_path=audit_path, db_path=db_path)
        assert n == 0

    def test_backfill_tolerates_missing_audit(self, tracker, tmp_path):
        """backfill with a non-existent audit path returns 0 without raising."""
        import backend.agent_run_tracker as art
        db_path = Path(os.environ["STATS_DB_PATH"])
        audit_path = tmp_path / "nonexistent.jsonl"
        n = art.backfill(audit_path=audit_path, db_path=db_path)
        assert n == 0

    def test_backfill_skips_entries_without_event_id(self, tracker, tmp_path):
        """Audit entries without event_id are silently skipped."""
        import duckdb
        db_path = Path(os.environ["STATS_DB_PATH"])
        entries = [
            {
                "ts": "2026-05-01T11:00:00Z",
                "source": "blackboard",
                "action": "write",
                "actor": "team-lead",
                "new": {"key": "some_value"},
            }
        ]
        audit_path = self._make_audit(tmp_path, entries)

        import backend.agent_run_tracker as art
        n = art.backfill(audit_path=audit_path, db_path=db_path)
        assert n == 0

    def test_backfill_end_ts_filled_from_completion_event(self, tracker, tmp_path):
        """backfill sets end_ts when a completion event is present."""
        import duckdb
        db_path = Path(os.environ["STATS_DB_PATH"])
        entries = [
            {
                "ts": "2026-05-01T09:00:00Z",
                "source": "spawn_agent",
                "action": "spawn",
                "actor": "executor",
                "event_id": "executor-50-3001",
                "new": {
                    "role": "executor",
                    "event_id": "executor-50-3001",
                },
            },
            {
                "ts": "2026-05-01T09:15:00Z",
                "source": "post_agent_hook",
                "action": "agent_done",
                "actor": "executor",
                "event_id": "executor-50-3001",
                "new": {
                    "verdict": "done",
                    "event_id": "executor-50-3001",
                },
            },
        ]
        audit_path = self._make_audit(tmp_path, entries)

        import backend.agent_run_tracker as art
        art.backfill(audit_path=audit_path, db_path=db_path)

        conn = duckdb.connect(str(db_path))
        row = conn.execute(
            "SELECT end_ts, duration_s FROM agent_run WHERE agent_id='executor-50-3001'"
        ).fetchone()
        conn.close()
        assert row is not None
        end_ts, dur = row
        assert end_ts is not None
        assert dur is not None
        assert dur == pytest.approx(900.0, abs=1.0)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI:
    def test_start_command(self, tracker, tmp_path):
        """CLI 'start' command inserts a row."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        import backend.agent_run_tracker as art
        rc = art.main([
            "start",
            "--agent-id", "cli-start-1",
            "--role", "executor",
            "--discussion", "635",
        ])
        assert rc == 0
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT role FROM agent_run WHERE agent_id='cli-start-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "executor"

    def test_complete_command(self, tracker, tmp_path):
        """CLI 'complete' command updates a row."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        import backend.agent_run_tracker as art
        art.main(["start", "--agent-id", "cli-comp-1", "--role", "executor"])
        rc = art.main([
            "complete",
            "--agent-id", "cli-comp-1",
            "--verdict", "done",
            "--input-tokens", "1000",
            "--output-tokens", "200",
        ])
        assert rc == 0
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT verdict, input_tok, output_tok "
            "FROM agent_run WHERE agent_id='cli-comp-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "done"
        assert row[1] == 1000
        assert row[2] == 200

    def test_backfill_command(self, tracker, tmp_path):
        """CLI 'backfill' command with explicit paths exits 0."""
        db_path = os.environ["STATS_DB_PATH"]
        audit_path = tmp_path / "audit.jsonl"
        audit_path.write_text("")
        import backend.agent_run_tracker as art
        rc = art.main([
            "backfill",
            "--audit-path", str(audit_path),
            "--db-path", db_path,
        ])
        assert rc == 0

    def test_no_command_prints_help(self, tracker, capsys):
        """Calling main with no subcommand returns exit code 1."""
        import backend.agent_run_tracker as art
        rc = art.main([])
        assert rc == 1


# ---------------------------------------------------------------------------
# UPSERT / idempotency tests (D#834 Phase 1)
# ---------------------------------------------------------------------------

class TestCompleteRunUpsert:
    """complete_run uses INSERT...ON CONFLICT DO UPDATE — idempotent, creates row if absent."""

    def test_complete_without_prior_start_creates_row(self, tracker):
        """complete_run creates a new row even when start_run was never called."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.complete_run(agent_id="upsert-new", verdict="done", output_tok=500)
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT verdict, output_tok FROM agent_run WHERE agent_id='upsert-new'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "done"
        assert row[1] == 500

    def test_complete_updates_existing_row(self, tracker):
        """complete_run updates the row that start_run created."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="upsert-existing", role="executor")
        tracker.complete_run(agent_id="upsert-existing", verdict="pass", output_tok=300)
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT verdict, output_tok, end_ts FROM agent_run WHERE agent_id='upsert-existing'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "pass"
        assert row[1] == 300
        assert row[2] is not None  # end_ts set

    def test_complete_twice_does_not_duplicate(self, tracker):
        """Calling complete_run twice on the same agent_id produces exactly one row."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="upsert-dup", role="executor")
        tracker.complete_run(agent_id="upsert-dup", verdict="done")
        tracker.complete_run(agent_id="upsert-dup", verdict="done")
        conn = duckdb.connect(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM agent_run WHERE agent_id='upsert-dup'"
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_complete_stores_cache_creation_tokens(self, tracker):
        """cache_creation_tokens is stored by complete_run."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="upsert-cct", role="executor")
        tracker.complete_run(
            agent_id="upsert-cct",
            verdict="done",
            cache_creation_tokens=1500,
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT cache_creation_tokens FROM agent_run WHERE agent_id='upsert-cct'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 1500


# ---------------------------------------------------------------------------
# Token validation tests (D#834 Phase 1)
# ---------------------------------------------------------------------------

class TestTokenValidation:
    """_validate_token_count rejects negative and non-int values (fail-closed)."""

    def test_valid_zero(self, tracker):
        """Zero is a valid token count."""
        import backend.agent_run_tracker as art
        assert art._validate_token_count(0, "output_tok") == 0

    def test_valid_positive(self, tracker):
        """Positive ints pass through unchanged."""
        import backend.agent_run_tracker as art
        assert art._validate_token_count(5000, "input_tok") == 5000

    def test_none_passes_through(self, tracker):
        """None is returned as-is (means not provided)."""
        import backend.agent_run_tracker as art
        assert art._validate_token_count(None, "output_tok") is None

    def test_negative_rejected(self, tracker):
        """Negative values are rejected and None is returned."""
        import backend.agent_run_tracker as art
        assert art._validate_token_count(-1, "output_tok") is None

    def test_float_rejected(self, tracker):
        """Float values are rejected (not an int)."""
        import backend.agent_run_tracker as art
        assert art._validate_token_count(3.14, "input_tok") is None

    def test_string_rejected(self, tracker):
        """String values are rejected."""
        import backend.agent_run_tracker as art
        assert art._validate_token_count("500", "output_tok") is None

    def test_negative_token_not_stored(self, tracker):
        """complete_run with negative output_tok stores NULL, not a negative value."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="val-neg", role="executor")
        tracker.complete_run(agent_id="val-neg", verdict="done", output_tok=-5)
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT output_tok FROM agent_run WHERE agent_id='val-neg'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None  # rejected, not stored


# ---------------------------------------------------------------------------
# AC#9 — Model recording at spawn time
# ---------------------------------------------------------------------------


class TestModelRecordedAtStart:
    """D#1502 AC#9: model must be non-null on new agent_run rows.

    spawn-agent.sh now passes --model <role_frontmatter_model> to
    agent_run_tracker start.  These tests verify that start_run accepts
    and stores the model field so downstream rows are non-null.
    """

    def test_model_stored_at_start(self, tracker):
        """start_run records model when provided — simulates spawn-agent.sh path."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(
            agent_id="ac9-executor-1502-1000",
            role="executor",
            discussion=1502,
            model="sonnet",
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT model FROM agent_run WHERE agent_id='ac9-executor-1502-1000'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "sonnet", f"Expected model='sonnet', got {row[0]!r}"

    def test_model_non_null_after_start_complete(self, tracker):
        """model remains non-null through the full start → complete lifecycle."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(
            agent_id="ac9-pm-1502-2000",
            role="project-manager",
            discussion=1502,
            model="opus",
        )
        tracker.complete_run(
            agent_id="ac9-pm-1502-2000",
            verdict="done",
            input_tok=1000,
            output_tok=500,
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT model, verdict FROM agent_run WHERE agent_id='ac9-pm-1502-2000'"
        ).fetchone()
        conn.close()
        assert row is not None
        model, verdict = row
        assert model == "opus", f"model should be preserved through complete_run, got {model!r}"
        assert verdict == "done"

    def test_complete_run_fills_model_when_start_had_none(self, tracker):
        """complete_run can fill model even when start_run omitted it (COALESCE)."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        # start without model (legacy path)
        tracker.start_run(
            agent_id="ac9-legacy-3000",
            role="code-reviewer",
        )
        # complete with model (post-completion telemetry fills it)
        tracker.complete_run(
            agent_id="ac9-legacy-3000",
            verdict="pass",
            model="sonnet",
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT model FROM agent_run WHERE agent_id='ac9-legacy-3000'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "sonnet"

    def test_model_not_overwritten_by_complete_when_already_set(self, tracker):
        """COALESCE: model from start_run is preserved if complete_run sends None."""
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(
            agent_id="ac9-preserve-4000",
            role="executor",
            model="haiku",
        )
        # complete without model — should not overwrite the start value
        tracker.complete_run(
            agent_id="ac9-preserve-4000",
            verdict="done",
            model=None,
        )
        conn = duckdb.connect(db)
        row = conn.execute(
            "SELECT model FROM agent_run WHERE agent_id='ac9-preserve-4000'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "haiku", "start_run model must not be overwritten by None in complete_run"


class TestReconcileGraceWindow:
    """D#1655: reconcile_open_runs must respect a caller-supplied stale_after_min
    so spawn-agent.sh can use a short window for interactive sessions without
    changing the loop path's 30-min behavior."""

    def _age_row(self, db_path, agent_id, minutes_ago):
        import duckdb
        conn = duckdb.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE agent_run SET start_ts = NOW() - INTERVAL (? || ' minutes') "
                "WHERE agent_id = ?",
                [str(minutes_ago), agent_id],
            )
        finally:
            conn.close()

    def test_short_window_closes_row_older_than_5min(self, tracker, tmp_path):
        """A row started 10 min ago, not in live_ids, IS closed with stale_after_min=5."""
        import duckdb
        db_path = Path(os.environ["STATS_DB_PATH"])
        tracker.start_run(agent_id="d1655-short-1", role="executor")
        self._age_row(db_path, "d1655-short-1", 10)

        closed = tracker.reconcile_open_runs(
            live_ids=[], stale_after_min=5, db_path=db_path
        )
        assert closed == 1

        conn = duckdb.connect(str(db_path))
        row = conn.execute(
            "SELECT end_ts FROM agent_run WHERE agent_id='d1655-short-1'"
        ).fetchone()
        conn.close()
        assert row is not None and row[0] is not None

    def test_loop_window_preserves_same_row(self, tracker, tmp_path):
        """The same 10-min-old row is preserved (NOT closed) with stale_after_min=30
        — the loop path's grace window must be unaffected by the interactive fix."""
        import duckdb
        db_path = Path(os.environ["STATS_DB_PATH"])
        tracker.start_run(agent_id="d1655-loop-1", role="executor")
        self._age_row(db_path, "d1655-loop-1", 10)

        closed = tracker.reconcile_open_runs(
            live_ids=[], stale_after_min=30, db_path=db_path
        )
        assert closed == 0

        conn = duckdb.connect(str(db_path))
        row = conn.execute(
            "SELECT end_ts FROM agent_run WHERE agent_id='d1655-loop-1'"
        ).fetchone()
        conn.close()
        assert row is not None and row[0] is None, "loop grace window must not close a 10-min-old row"
