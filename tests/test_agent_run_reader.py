"""
tests/test_agent_run_reader.py — fixture-driven tests for agent_run_reader.

Tests cover:
  - by_role: filters by role and since_iso, returns correct fields
  - duration_percentiles: correct p50/p95/p99 values, empty-DB fallback
  - stuck_runs: only returns open runs older than threshold
  - roundtrip_latency: executor done → reviewer start delta
  - concurrent_active: counts active runs per time bucket
  - _recent: cross-role recent rows, limit respected
  - Empty DB: every function returns empty/None/0 gracefully
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import time
from datetime import datetime, timezone, timedelta

import pytest

# Only TestStuckCountParity below reaches into dashboard_tui; the rest of this
# file tests backend/ and must keep running in a tree without a TUI. Scoped to
# that class rather than the module for exactly that reason.
_NO_TUI = pytest.mark.skipif(
    importlib.util.find_spec("dashboard_tui") is None,
    reason="dashboard_tui/ not present in this tree",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def reader(tmp_path, monkeypatch):
    """Import agent_run_reader with STATS_DB_PATH pointed at a temp file."""
    db = str(tmp_path / "test_reader.duckdb")
    monkeypatch.setenv("STATS_DB_PATH", db)
    import backend.agent_run_reader as mod
    importlib.reload(mod)
    return mod


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    """Import agent_run_tracker with the same STATS_DB_PATH as reader."""
    # Reuse whatever STATS_DB_PATH was set by the reader fixture
    import backend.agent_run_tracker as mod
    importlib.reload(mod)
    return mod


@pytest.fixture
def populated_db(reader, tracker):
    """Seed the test DB with 5 completed runs across two roles.

    Runs layout (all times in the past):
      executor × 3  — durations 10s, 20s, 30s  (p50≈20, p95≈29.5)
      code-reviewer × 2 — durations 5s, 15s

    Also inserts:
      - 1 open executor run (stuck, > 30 min old)  agent_id="stuck-1"
      - 1 open run just 5s old (not stuck)         agent_id="fresh-open"
    """
    now = datetime.now(timezone.utc)

    runs = [
        # completed executor runs (used for percentile and roundtrip tests)
        dict(
            agent_id="exec-1", role="executor", pr=100,
            start=now - timedelta(seconds=310), end=now - timedelta(seconds=300),
        ),
        dict(
            agent_id="exec-2", role="executor", pr=100,
            start=now - timedelta(seconds=320), end=now - timedelta(seconds=300),
        ),
        dict(
            agent_id="exec-3", role="executor", pr=200,
            start=now - timedelta(seconds=330), end=now - timedelta(seconds=300),
        ),
        # completed reviewer run (used for roundtrip on pr=100)
        dict(
            agent_id="rev-1", role="code-reviewer", pr=100,
            start=now - timedelta(seconds=295), end=now - timedelta(seconds=280),
        ),
        dict(
            agent_id="rev-2", role="code-reviewer", pr=200,
            start=now - timedelta(seconds=290), end=now - timedelta(seconds=275),
        ),
        # stuck executor (open, started 35 min ago)
        dict(
            agent_id="stuck-1", role="executor", pr=None,
            start=now - timedelta(seconds=2100), end=None,
        ),
        # fresh open run (5s old — below 30 min stuck threshold)
        dict(
            agent_id="fresh-open", role="executor", pr=None,
            start=now - timedelta(seconds=5), end=None,
        ),
    ]

    import duckdb
    db = os.environ["STATS_DB_PATH"]
    conn = duckdb.connect(db)
    try:
        # Ensure schema exists via tracker
        tracker.start_run(agent_id="__schema_probe__", role="probe")
        conn.close()

        for r in runs:
            tracker.start_run(
                agent_id=r["agent_id"],
                role=r["role"],
                pr=r.get("pr"),
            )
            # Manually insert start_ts at the correct time
            conn = duckdb.connect(db)
            conn.execute(
                "UPDATE agent_run SET start_ts = ? WHERE agent_id = ?",
                [r["start"], r["agent_id"]],
            )
            conn.close()

            if r["end"] is not None:
                dur = (r["end"] - r["start"]).total_seconds()
                tracker.complete_run(
                    agent_id=r["agent_id"],
                    end_ts=r["end"],
                    duration_s=dur,
                    verdict="done",
                    input_tok=1000,
                    output_tok=200,
                )

        # Remove schema probe row
        conn = duckdb.connect(db)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__schema_probe__'")
        conn.close()
    finally:
        pass  # connections closed inline above

    return reader


# ---------------------------------------------------------------------------
# Empty-DB tests (graceful degradation)
# ---------------------------------------------------------------------------

class TestEmptyDB:
    def test_by_role_empty(self, reader):
        result = reader.by_role("executor")
        assert result == []

    def test_duration_percentiles_empty(self, reader):
        result = reader.duration_percentiles()
        assert result["p50"] is None
        assert result["p95"] is None
        assert result["p99"] is None
        assert result["sample_size"] == 0

    def test_stuck_runs_empty(self, reader):
        assert reader.stuck_runs() == []

    def test_roundtrip_latency_empty(self, reader):
        assert reader.roundtrip_latency(pr=999) is None

    def test_concurrent_active_empty(self, reader):
        # Empty DB — all buckets should have count 0
        result = reader.concurrent_active()
        assert isinstance(result, list)
        for point in result:
            assert point["count"] == 0

    def test_recent_empty(self, reader):
        assert reader._recent() == []


# ---------------------------------------------------------------------------
# by_role
# ---------------------------------------------------------------------------

class TestByRole:
    def test_returns_only_requested_role(self, populated_db):
        rows = populated_db.by_role("executor")
        assert all(r["role"] == "executor" for r in rows)

    def test_returns_expected_count(self, populated_db):
        # 3 completed + 1 stuck + 1 fresh-open = 5 executor rows total
        rows = populated_db.by_role("executor")
        assert len(rows) == 5

    def test_reviewer_role(self, populated_db):
        rows = populated_db.by_role("code-reviewer")
        assert len(rows) == 2

    def test_since_iso_filters(self, populated_db):
        # since_iso = 10 seconds ago → only "fresh-open" (started 5s ago) qualifies;
        # stuck-1 (started 35 min ago) and completed runs (300+ s ago) are excluded.
        since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        rows = populated_db.by_role("executor", since_iso=since)
        ids = {r["agent_id"] for r in rows}
        assert "fresh-open" in ids
        # stuck-1 was 35 min ago — must not appear
        assert "stuck-1" not in ids

    def test_unknown_role_returns_empty(self, populated_db):
        assert populated_db.by_role("nonexistent-role") == []

    def test_row_has_expected_fields(self, populated_db):
        rows = populated_db.by_role("code-reviewer")
        assert rows
        row = rows[0]
        expected_keys = {"agent_id", "role", "discussion", "pr", "start_ts",
                         "end_ts", "duration_s", "verdict", "model"}
        assert expected_keys.issubset(row.keys())


# ---------------------------------------------------------------------------
# duration_percentiles
# ---------------------------------------------------------------------------

class TestDurationPercentiles:
    def test_executor_percentiles_sensible(self, populated_db):
        result = populated_db.duration_percentiles(role="executor")
        assert result["sample_size"] == 3  # only completed executor rows
        assert result["p50"] is not None
        # p50 should be ≈ 20s (durations: 10, 20, 30)
        assert 15.0 <= result["p50"] <= 25.0

    def test_p95_gte_p50(self, populated_db):
        result = populated_db.duration_percentiles(role="executor")
        assert result["p95"] >= result["p50"]

    def test_p99_gte_p95(self, populated_db):
        result = populated_db.duration_percentiles(role="executor")
        assert result["p99"] >= result["p95"]

    def test_all_roles_combined(self, populated_db):
        result = populated_db.duration_percentiles()
        # 3 executor + 2 reviewer = 5 completed rows
        assert result["sample_size"] == 5

    def test_since_iso_filters_old_rows(self, populated_db):
        # A very recent since_iso means no completed runs are in range
        since = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        result = populated_db.duration_percentiles(since_iso=since)
        assert result["sample_size"] == 0
        assert result["p50"] is None


# ---------------------------------------------------------------------------
# stuck_runs
# ---------------------------------------------------------------------------

class TestStuckRuns:
    def test_returns_stuck_run(self, populated_db):
        rows = populated_db.stuck_runs(threshold_seconds=1800)
        ids = {r["agent_id"] for r in rows}
        assert "stuck-1" in ids

    def test_fresh_open_not_stuck(self, populated_db):
        rows = populated_db.stuck_runs(threshold_seconds=1800)
        ids = {r["agent_id"] for r in rows}
        assert "fresh-open" not in ids

    def test_completed_runs_excluded(self, populated_db):
        rows = populated_db.stuck_runs(threshold_seconds=1800)
        for r in rows:
            assert r["end_ts"] is None

    def test_threshold_0_returns_all_open(self, populated_db):
        # threshold=0 → all open runs (stuck-1 + fresh-open) should appear
        rows = populated_db.stuck_runs(threshold_seconds=0)
        ids = {r["agent_id"] for r in rows}
        assert "stuck-1" in ids
        assert "fresh-open" in ids

    def test_ordered_oldest_first(self, populated_db):
        rows = populated_db.stuck_runs(threshold_seconds=0)
        if len(rows) >= 2:
            ts0 = rows[0]["start_ts"]
            ts1 = rows[1]["start_ts"]
            # oldest first means ts0 <= ts1
            if isinstance(ts0, str):
                ts0 = datetime.fromisoformat(ts0.replace("Z", "+00:00"))
            if isinstance(ts1, str):
                ts1 = datetime.fromisoformat(ts1.replace("Z", "+00:00"))
            assert ts0 <= ts1

    def test_rows_are_json_serializable(self, populated_db):
        """All fields in stuck_runs rows must be JSON-serializable.

        Previously crashed with 'Object of type datetime is not JSON serializable'
        because DuckDB returns datetime objects for start_ts / end_ts columns.
        """
        rows = populated_db.stuck_runs(threshold_seconds=1800)
        assert rows, "expected at least one stuck run in populated_db"
        # json.dumps raises TypeError on non-serializable types
        payload = json.dumps({"runs": rows})
        recovered = json.loads(payload)
        assert len(recovered["runs"]) == len(rows)
        # Timestamps should be ISO-8601 strings, not datetime objects
        for row in rows:
            assert isinstance(row["start_ts"], str), (
                f"start_ts should be str, got {type(row['start_ts'])}"
            )


# ---------------------------------------------------------------------------
# roundtrip_latency
# ---------------------------------------------------------------------------

class TestRoundtripLatency:
    def test_known_pr_returns_positive_latency(self, populated_db):
        # PR 100: executor ended at now-300s, reviewer started at now-295s → ~5s latency
        latency = populated_db.roundtrip_latency(pr=100)
        assert latency is not None
        assert latency > 0
        # Should be roughly 5 seconds (executor done=now-300, reviewer start=now-295)
        assert latency < 30.0

    def test_missing_pr_returns_none(self, populated_db):
        assert populated_db.roundtrip_latency(pr=9999) is None

    def test_pr_with_no_reviewer_returns_none(self, populated_db):
        # Insert an executor run on a PR with no reviewer
        import duckdb
        db = os.environ["STATS_DB_PATH"]
        conn = duckdb.connect(db)
        now = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO agent_run (agent_id, role, pr, start_ts, end_ts, duration_s)
            VALUES ('exec-noreview', 'executor', 999, ?, ?, 10.0)
            """,
            [now - timedelta(seconds=20), now - timedelta(seconds=10)],
        )
        conn.close()
        assert populated_db.roundtrip_latency(pr=999) is None


# ---------------------------------------------------------------------------
# concurrent_active
# ---------------------------------------------------------------------------

class TestConcurrentActive:
    def test_returns_list_of_ts_count_dicts(self, populated_db):
        result = populated_db.concurrent_active(
            since_iso=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            until_iso=datetime.now(timezone.utc).isoformat(),
            bucket_seconds=60,
        )
        assert isinstance(result, list)
        for point in result:
            assert "ts" in point
            assert "count" in point
            assert isinstance(point["count"], int)

    def test_count_nonzero_when_runs_active(self, populated_db):
        # The fresh-open run is currently active; at least one bucket should have count >= 1
        result = populated_db.concurrent_active(
            since_iso=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
            until_iso=datetime.now(timezone.utc).isoformat(),
            bucket_seconds=10,
        )
        counts = [p["count"] for p in result]
        assert any(c >= 1 for c in counts)

    def test_no_overlap_window_returns_zero_counts(self, populated_db):
        # Window in the future — no runs active there
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        result = populated_db.concurrent_active(
            since_iso=(future).isoformat(),
            until_iso=(future + timedelta(minutes=5)).isoformat(),
            bucket_seconds=60,
        )
        for point in result:
            assert point["count"] == 0


# ---------------------------------------------------------------------------
# _recent
# ---------------------------------------------------------------------------

class TestRecent:
    def test_returns_rows_most_recent_first(self, populated_db):
        rows = populated_db._recent(limit=100)
        assert len(rows) > 0
        timestamps = []
        for r in rows:
            ts = r["start_ts"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            timestamps.append(ts)
        # Verify descending order
        for i in range(len(timestamps) - 1):
            assert timestamps[i] >= timestamps[i + 1]

    def test_limit_respected(self, populated_db):
        rows = populated_db._recent(limit=3)
        assert len(rows) <= 3

    def test_all_roles_included(self, populated_db):
        rows = populated_db._recent(limit=100)
        roles = {r["role"] for r in rows}
        assert "executor" in roles
        assert "code-reviewer" in roles

    def test_rows_are_json_serializable(self, populated_db):
        """All fields in _recent rows must be JSON-serializable.

        Previously crashed with 'Object of type datetime is not JSON serializable'
        because DuckDB returns datetime objects for start_ts / end_ts columns.
        """
        rows = populated_db._recent(limit=100)
        assert rows, "expected rows in populated_db"
        payload = json.dumps({"runs": rows})
        recovered = json.loads(payload)
        assert len(recovered["runs"]) == len(rows)
        for row in rows:
            assert isinstance(row["start_ts"], str), (
                f"start_ts should be str, got {type(row['start_ts'])}"
            )


# ---------------------------------------------------------------------------
# Performance: p95 < 50ms on 1000-row table
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_percentile_query_under_50ms(self, reader, tracker):
        """Seed 1000 rows and verify duration_percentiles reads in < 50ms."""
        import duckdb

        db = os.environ["STATS_DB_PATH"]
        now = datetime.now(timezone.utc)

        # Seed 1000 rows via bulk INSERT into DuckDB directly (faster than tracker loop)
        conn = duckdb.connect(db)
        try:
            # Ensure schema via tracker first
            tracker.start_run(agent_id="perf-seed-schema", role="executor")
            conn.close()

            conn = duckdb.connect(db)
            conn.execute("DELETE FROM agent_run WHERE agent_id = 'perf-seed-schema'")
            values = []
            for i in range(1000):
                start = now - timedelta(seconds=3600 - i)
                end = start + timedelta(seconds=10 + (i % 120))
                dur = (end - start).total_seconds()
                values.append(
                    f"('perf-{i}', 'executor', NULL, NULL, '{start.isoformat()}', "
                    f"'{end.isoformat()}', {dur}, 'done', NULL, NULL, NULL, NULL, NULL, NULL, 'perf-{i}')"
                )
            conn.execute(
                "INSERT INTO agent_run "
                "(agent_id, role, discussion, pr, start_ts, end_ts, "
                "duration_s, verdict, model, input_tok, output_tok, "
                "cache_read, cache_write, blocked_reason, event_id) VALUES "
                + ", ".join(values)
            )
            conn.close()
        finally:
            pass  # closed inline

        start_time = time.perf_counter()
        result = reader.duration_percentiles(role="executor")
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        assert result["sample_size"] == 1000
        assert result["p50"] is not None
        assert elapsed_ms < 50.0, f"percentile query took {elapsed_ms:.1f}ms — expected < 50ms"


# ---------------------------------------------------------------------------
# Stuck-count parity: Agent Feed and Runs page must agree (D#854 sub-5)
# ---------------------------------------------------------------------------

@_NO_TUI
class TestStuckCountParity:
    """Both screens read from agent_run DuckDB via stuck_runs().

    Seeded with N orphaned (open, >15min) runs — both sources must report N.
    """

    def _seed_orphans(self, tracker, db_path: str, count: int) -> None:
        """Insert *count* open runs aged 20 minutes into the test DB."""
        import duckdb
        now = datetime.now(timezone.utc)
        conn = duckdb.connect(db_path)
        try:
            for i in range(count):
                agent_id = f"orphan-{i}"
                tracker.start_run(agent_id=agent_id, role="executor")
                conn.execute(
                    "UPDATE agent_run SET start_ts = ? WHERE agent_id = ?",
                    [now - timedelta(minutes=20), agent_id],
                )
        finally:
            conn.close()

    def test_agent_feed_stuck_count_equals_runs_page_count(
        self, reader, tracker, monkeypatch
    ):
        """With N orphaned runs, stuck_runs() and _stuck_count() agree."""
        import importlib
        import backend.agent_run_reader as arr_mod

        db_path = os.environ["STATS_DB_PATH"]

        # Ensure schema exists
        tracker.start_run(agent_id="__parity-schema__", role="probe")
        import duckdb
        conn = duckdb.connect(db_path)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__parity-schema__'")
        conn.close()

        N = 3
        self._seed_orphans(tracker, db_path, N)

        # Runs page source: stuck_runs with 15-min threshold
        runs_page_count = len(arr_mod.stuck_runs(threshold_seconds=15 * 60))
        assert runs_page_count == N, (
            f"Runs page expected {N} stuck runs, got {runs_page_count}"
        )

        # Agent Feed source: _stuck_count() from dashboard_tui/screens/agent_feed
        import dashboard_tui.screens.agent_feed as af_screen
        importlib.reload(af_screen)
        agent_feed_count = af_screen._stuck_count()
        assert agent_feed_count == N, (
            f"Agent Feed expected {N} stuck runs, got {agent_feed_count}"
        )

        assert agent_feed_count == runs_page_count, (
            f"Agent Feed ({agent_feed_count}) and Runs page ({runs_page_count}) disagree"
        )

    def test_idem_test_rows_excluded_from_both(self, reader, tracker, monkeypatch):
        """idem-test prefixed rows are excluded by stuck_runs() and _stuck_count()."""
        import duckdb
        import importlib
        import backend.agent_run_reader as arr_mod

        db_path = os.environ["STATS_DB_PATH"]
        now = datetime.now(timezone.utc)

        # Ensure schema
        tracker.start_run(agent_id="__idem-schema__", role="probe")
        conn = duckdb.connect(db_path)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__idem-schema__'")

        # Insert one real orphan and one idem-test orphan
        tracker.start_run(agent_id="real-orphan", role="executor")
        tracker.start_run(agent_id="idem-test-orphan", role="executor")
        conn.execute(
            "UPDATE agent_run SET start_ts = ? WHERE agent_id IN ('real-orphan', 'idem-test-orphan')",
            [now - timedelta(minutes=20)],
        )
        conn.close()

        runs_page_count = len(arr_mod.stuck_runs(threshold_seconds=15 * 60))
        import dashboard_tui.screens.agent_feed as af_screen
        importlib.reload(af_screen)
        agent_feed_count = af_screen._stuck_count()

        # Both should exclude idem-test-orphan
        assert runs_page_count == agent_feed_count, (
            f"Agent Feed ({agent_feed_count}) and Runs page ({runs_page_count}) disagree on idem-test exclusion"
        )
        # real-orphan must be counted, idem-test-orphan must not
        stuck_ids = {r["agent_id"] for r in arr_mod.stuck_runs(threshold_seconds=15 * 60)}
        assert "real-orphan" in stuck_ids
        assert "idem-test-orphan" not in stuck_ids


# ---------------------------------------------------------------------------
# is_agent_reported() — backend/agent_run_verdicts.py
# ---------------------------------------------------------------------------

class TestIsAgentReported:
    """D#2232: the code that tells a real agent verdict apart from a
    reconciler/sweeper placeholder needs its own coverage — that
    distinction is exactly what this Discussion exists to fix, and a
    regression here would previously pass every existing suite silently.
    """

    def test_each_non_agent_verdict_is_not_agent_reported(self):
        from backend.agent_run_verdicts import NON_AGENT_VERDICTS, is_agent_reported

        assert NON_AGENT_VERDICTS == {
            "reconciled-stale", "superseded", "swept-test-fixture",
        }
        for verdict in NON_AGENT_VERDICTS:
            assert is_agent_reported(verdict) is False, verdict

    @pytest.mark.parametrize("verdict", ["done", "pass", "needs-fix", "fail"])
    def test_genuine_verdict_is_agent_reported(self, verdict):
        from backend.agent_run_verdicts import is_agent_reported

        assert is_agent_reported(verdict) is True

    def test_none_is_not_agent_reported(self):
        from backend.agent_run_verdicts import is_agent_reported

        assert is_agent_reported(None) is False

    def test_empty_string_is_not_agent_reported(self):
        from backend.agent_run_verdicts import is_agent_reported

        assert is_agent_reported("") is False


# ---------------------------------------------------------------------------
# Verdict masking — agent_run_reader.py::_row_to_dict via run_detail/by_role
# ---------------------------------------------------------------------------

class TestVerdictMasking:
    """A placeholder verdict must render as an unambiguous marker; a real
    agent-reported verdict must pass through untouched. Regression coverage
    for the exact failure this Discussion is about: a reader that can't
    tell a reconciler stamp from a real outcome.
    """

    def _seed(self, tracker, agent_id: str, role: str, verdict: str) -> None:
        tracker.start_run(agent_id=agent_id, role=role)
        tracker.complete_run(
            agent_id=agent_id,
            duration_s=1.0,
            verdict=verdict,
            input_tok=0,
            output_tok=0,
        )

    def test_placeholder_verdict_is_masked_in_run_detail(self, reader, tracker):
        from backend.agent_run_reader import NON_AGENT_VERDICT_MARKER

        self._seed(tracker, "masked-1", "code-reviewer", "reconciled-stale")

        row = reader.run_detail("masked-1")
        assert row["verdict"] == NON_AGENT_VERDICT_MARKER
        assert row["verdict"] != "reconciled-stale"

    def test_real_verdict_passes_through_run_detail(self, reader, tracker):
        self._seed(tracker, "real-1", "executor", "pass")

        row = reader.run_detail("real-1")
        assert row["verdict"] == "pass"

    def test_placeholder_verdict_is_masked_in_by_role(self, reader, tracker):
        from backend.agent_run_reader import NON_AGENT_VERDICT_MARKER

        self._seed(tracker, "masked-2", "executor", "superseded")

        rows = reader.by_role("executor", since_iso="2000-01-01T00:00:00+00:00")
        row = next(r for r in rows if r["agent_id"] == "masked-2")
        assert row["verdict"] == NON_AGENT_VERDICT_MARKER

    def test_real_verdict_passes_through_by_role(self, reader, tracker):
        self._seed(tracker, "real-2", "executor", "needs-fix")

        rows = reader.by_role("executor", since_iso="2000-01-01T00:00:00+00:00")
        row = next(r for r in rows if r["agent_id"] == "real-2")
        assert row["verdict"] == "needs-fix"
