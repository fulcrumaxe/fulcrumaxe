"""
tests/test_data_layer_duckdb_readers.py — verify the four DuckDB-backed readers
in dashboard_tui/data_layer.py.

Seeds a temp DuckDB with 3 agent_run rows and asserts all four readers
return the expected shapes / values.
"""
from __future__ import annotations

import asyncio
import importlib
import os
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# Shared DB fixture — seeds exactly 3 rows
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Point STATS_DB_PATH at a temp file, seed 3 agent_run rows, return modules."""
    db_path = str(tmp_path / "dl_test.duckdb")
    monkeypatch.setenv("STATS_DB_PATH", db_path)

    # Reload modules so they pick up the new env var
    import backend.agent_run_tracker as tracker_mod
    import backend.agent_run_reader as reader_mod
    importlib.reload(tracker_mod)
    importlib.reload(reader_mod)

    now = datetime.now(timezone.utc)

    rows = [
        # Completed executor run — 60s duration
        dict(agent_id="e1", role="executor", pr=10,
             start=now - timedelta(seconds=360),
             end=now - timedelta(seconds=300),
             duration_s=60.0),
        # Completed code-reviewer run — 30s duration
        dict(agent_id="r1", role="code-reviewer", pr=10,
             start=now - timedelta(seconds=290),
             end=now - timedelta(seconds=260),
             duration_s=30.0),
        # Open (stuck) executor run — started 40 min ago, no end
        dict(agent_id="stuck-e", role="executor", pr=None,
             start=now - timedelta(minutes=40),
             end=None,
             duration_s=None),
    ]

    import duckdb

    # Initialise schema
    tracker_mod.start_run(agent_id="__probe__", role="probe")

    for r in rows:
        tracker_mod.start_run(agent_id=r["agent_id"], role=r["role"], pr=r.get("pr"))
        # Backdate start_ts
        conn = duckdb.connect(db_path)
        conn.execute(
            "UPDATE agent_run SET start_ts = ? WHERE agent_id = ?",
            [r["start"], r["agent_id"]],
        )
        conn.close()

        if r["end"] is not None:
            tracker_mod.complete_run(
                agent_id=r["agent_id"],
                end_ts=r["end"],
                duration_s=r["duration_s"],
                verdict="done",
                input_tok=500,
                output_tok=100,
            )

    # Remove probe row
    conn = duckdb.connect(db_path)
    conn.execute("DELETE FROM agent_run WHERE agent_id = '__probe__'")
    conn.close()

    # Reload data_layer so its internal sys.path/imports resolve the same DB
    import dashboard_tui.data_layer as dl_mod
    importlib.reload(dl_mod)

    return {"dl": dl_mod, "reader": reader_mod, "db_path": db_path, "now": now}


# ---------------------------------------------------------------------------
# Helper: run async function synchronously
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# get_active_agents_buckets
# ---------------------------------------------------------------------------

class TestGetActiveAgentsBuckets:
    def test_returns_list_of_dicts(self, seeded_db):
        dl = seeded_db["dl"]
        buckets = run(dl.get_active_agents_buckets(window_hours=2, bucket_min=5))
        assert isinstance(buckets, list)

    def test_bucket_keys_present(self, seeded_db):
        dl = seeded_db["dl"]
        buckets = run(dl.get_active_agents_buckets(window_hours=2, bucket_min=5))
        if buckets:
            b = buckets[0]
            assert "bucket_ts" in b
            assert "concurrent_count" in b

    def test_nonzero_count_when_open_run_active(self, seeded_db):
        """The stuck-e run is still open — at least one bucket in a 60-min window
        that covers the last 40 minutes should count it."""
        dl = seeded_db["dl"]
        buckets = run(dl.get_active_agents_buckets(window_hours=1, bucket_min=5))
        counts = [b["concurrent_count"] for b in buckets]
        assert any(c >= 1 for c in counts), (
            f"Expected at least one non-zero bucket, got: {counts}"
        )


# ---------------------------------------------------------------------------
# get_duration_percentiles
# ---------------------------------------------------------------------------

class TestGetDurationPercentiles:
    def test_returns_list(self, seeded_db):
        dl = seeded_db["dl"]
        rows = run(dl.get_duration_percentiles(window_hours=24))
        assert isinstance(rows, list)

    def test_has_executor_and_reviewer_rows(self, seeded_db):
        dl = seeded_db["dl"]
        rows = run(dl.get_duration_percentiles(window_hours=24))
        roles = {r["role"] for r in rows}
        assert "executor" in roles
        assert "code-reviewer" in roles

    def test_row_keys(self, seeded_db):
        dl = seeded_db["dl"]
        rows = run(dl.get_duration_percentiles(window_hours=24))
        assert rows
        r = rows[0]
        assert "role" in r
        assert "p50_ms" in r
        assert "p95_ms" in r
        assert "n" in r

    def test_executor_p50_ms_reasonable(self, seeded_db):
        """Executor has 1 completed run of 60s → p50 should be ~60000ms."""
        dl = seeded_db["dl"]
        rows = run(dl.get_duration_percentiles(window_hours=24))
        exec_rows = [r for r in rows if r["role"] == "executor"]
        assert exec_rows
        p50 = exec_rows[0]["p50_ms"]
        assert p50 is not None
        # 60s run → 60000ms; allow reasonable tolerance
        assert 55_000 <= p50 <= 65_000, f"Expected ~60000ms, got {p50}"

    def test_n_counts_only_completed(self, seeded_db):
        """stuck-e has no end_ts — must not be counted in percentiles."""
        dl = seeded_db["dl"]
        rows = run(dl.get_duration_percentiles(window_hours=24))
        exec_rows = [r for r in rows if r["role"] == "executor"]
        if exec_rows:
            # Only 1 completed executor run (e1); stuck-e is excluded
            assert exec_rows[0]["n"] == 1


# ---------------------------------------------------------------------------
# get_stuck_runs_from_reports
# ---------------------------------------------------------------------------

class TestGetStuckRunsFromReports:
    def test_returns_list(self, seeded_db):
        dl = seeded_db["dl"]
        rows = run(dl.get_stuck_runs_from_reports(min_age_minutes=30))
        assert isinstance(rows, list)

    def test_includes_stuck_run(self, seeded_db):
        dl = seeded_db["dl"]
        rows = run(dl.get_stuck_runs_from_reports(min_age_minutes=30))
        ids = {r["agent_id"] for r in rows}
        assert "stuck-e" in ids

    def test_excludes_completed_runs(self, seeded_db):
        dl = seeded_db["dl"]
        rows = run(dl.get_stuck_runs_from_reports(min_age_minutes=30))
        for r in rows:
            assert r.get("end_ts") is None, f"Completed run leaked: {r['agent_id']}"

    def test_age_min_field_present(self, seeded_db):
        dl = seeded_db["dl"]
        rows = run(dl.get_stuck_runs_from_reports(min_age_minutes=30))
        for r in rows:
            assert "age_min" in r

    def test_age_min_approx_40(self, seeded_db):
        """stuck-e started 40 min ago — age_min should be ~40."""
        dl = seeded_db["dl"]
        rows = run(dl.get_stuck_runs_from_reports(min_age_minutes=30))
        stuck = [r for r in rows if r["agent_id"] == "stuck-e"]
        assert stuck
        age = stuck[0]["age_min"]
        assert age is not None
        assert 38 <= age <= 42, f"Expected ~40 min, got {age}"

    def test_fresh_run_excluded_by_threshold(self, seeded_db):
        """With a high threshold, even stuck-e might be excluded — test with 0."""
        dl = seeded_db["dl"]
        # threshold=0 → all open runs returned regardless of age
        rows = run(dl.get_stuck_runs_from_reports(min_age_minutes=0))
        ids = {r["agent_id"] for r in rows}
        assert "stuck-e" in ids


# ---------------------------------------------------------------------------
# get_run_detail
# ---------------------------------------------------------------------------

class TestGetRunDetail:
    def test_returns_dict(self, seeded_db):
        dl = seeded_db["dl"]
        result = run(dl.get_run_detail("e1"))
        assert isinstance(result, dict)

    def test_correct_agent_id(self, seeded_db):
        dl = seeded_db["dl"]
        result = run(dl.get_run_detail("e1"))
        assert result.get("agent_id") == "e1"

    def test_has_expected_fields(self, seeded_db):
        dl = seeded_db["dl"]
        result = run(dl.get_run_detail("r1"))
        assert result
        for key in ("agent_id", "role", "start_ts", "end_ts", "duration_s", "verdict"):
            assert key in result, f"Missing field: {key}"

    def test_unknown_agent_returns_empty(self, seeded_db):
        dl = seeded_db["dl"]
        result = run(dl.get_run_detail("no-such-agent"))
        assert result == {}

    def test_stuck_run_detail(self, seeded_db):
        """Detail pane for an open run returns a row with end_ts=None."""
        dl = seeded_db["dl"]
        result = run(dl.get_run_detail("stuck-e"))
        assert result.get("agent_id") == "stuck-e"
        assert result.get("end_ts") is None
