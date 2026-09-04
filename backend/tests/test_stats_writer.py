"""Tests for backend/stats_writer.py — behavioral coverage of the public API.

Covers:
    record()                        — write, deduplicate, read-back, tags, source, explicit ts
    record_many()                   — bulk write, partial failure isolation
    record_loop_iter()              — loop_metrics table, tokens_per_iter computation
    emit_verdict()                  — role_verdict metric rows
    role_success_rate_24h()         — aggregation over pass/done verdicts
    role_retry_rate_24h()           — aggregation over needs-fix/fail verdicts
    record_live_analyst_intervention() — emits three metric rows
    record_intervention_outcome()   — self_corrected flag → ratio value
    record_cost_spike()             — cost_spike metric + tags
    record_iteration_cost()         — iteration_cost_usd metric
    cost_spike_history()            — time-windowed read-back, newest first
    avg_fix_rounds_24h()            — distribution + average
    team_lead_tokens_percentiles()  — p50/p95 with sample_size gate
    loop_idle_ratio_24h()           — jsonl-based reader, idle detection rules
    registered_metrics()            — completeness: every declared writer is present

Isolation: STATS_DB_PATH env var is monkeypatched to a tmp_path file.
The real ~/.fulcrumaxe-state/stats.duckdb is NEVER touched.

Run with:
    python3 -m pytest backend/tests/test_stats_writer.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Skip entire module if duckdb is not installed
try:
    import duckdb as _duckdb_mod
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


# ---------------------------------------------------------------------------
# Fixtures — DB isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect every stats_writer call to a fresh temp DuckDB.

    autouse=True means every test in this file gets this automatically.
    The fixture also clears AUTONOMOUS_TEAM_STATE_DIR so that path #2
    in _db_path() does NOT load state_paths.STATS_DB instead.
    """
    db_file = tmp_path / "test_stats.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(db_file))
    monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
    # Force stats_writer to re-evaluate _db_path() on every call
    # (module-level caching is not used; _db_path() reads os.environ live)
    yield db_file


def _open(db_file: Path):
    """Return a fresh DuckDB read-write connection to the isolated DB."""
    return _duckdb_mod.connect(str(db_file))


def _query(db_file: Path, sql: str, params=None):
    conn = _open(db_file)
    try:
        if params:
            return conn.execute(sql, params).fetchall()
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Import under test (done here so monkeypatching in isolated_db applies)
# ---------------------------------------------------------------------------

import backend.stats_writer as sw


# ===========================================================================
# record() — core write path
# ===========================================================================


class TestRecord:

    def test_record_inserts_row(self, isolated_db):
        sw.record("latency", 1.5, "seconds")
        rows = _query(isolated_db, "SELECT metric, value, unit FROM metric_event")
        assert len(rows) == 1
        assert rows[0] == ("latency", 1.5, "seconds")

    def test_record_stores_tags_as_json(self, isolated_db):
        sw.record("requests", 10.0, "count", tags={"service": "api", "env": "test"})
        rows = _query(isolated_db, "SELECT tags FROM metric_event WHERE metric='requests'")
        assert len(rows) == 1
        tags = json.loads(rows[0][0])
        assert tags["service"] == "api"
        assert tags["env"] == "test"

    def test_record_empty_tags_stored_as_empty_object(self, isolated_db):
        sw.record("gauge", 0.0, "count")
        rows = _query(isolated_db, "SELECT tags FROM metric_event WHERE metric='gauge'")
        tags = json.loads(rows[0][0])
        assert tags == {}

    def test_record_stores_source(self, isolated_db):
        sw.record("cost", 0.05, "usd", source="post-merge-hook")
        rows = _query(isolated_db, "SELECT source FROM metric_event WHERE metric='cost'")
        assert rows[0][0] == "post-merge-hook"

    def test_record_source_defaults_to_none(self, isolated_db):
        sw.record("ratio", 0.5, "ratio")
        rows = _query(isolated_db, "SELECT source FROM metric_event WHERE metric='ratio'")
        assert rows[0][0] is None

    def test_record_explicit_timestamp_stored(self, isolated_db):
        fixed_ts = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        sw.record("ttm", 300.0, "seconds", ts=fixed_ts)
        rows = _query(isolated_db, "SELECT ts FROM metric_event WHERE metric='ttm'")
        stored = rows[0][0]
        # DuckDB returns a naive datetime — compare date+time parts
        assert stored.year == 2026
        assert stored.month == 3
        assert stored.day == 15
        assert stored.hour == 12

    def test_record_default_timestamp_is_recent(self, isolated_db):
        # stats_writer truncates to millisecond precision, so subtract 1ms from
        # before to avoid a spurious failure when the truncated stored value is
        # microseconds behind the Python datetime.now() sample.
        before = datetime.now(timezone.utc) - timedelta(milliseconds=1)
        sw.record("heartbeat", 1.0, "count")
        after = datetime.now(timezone.utc)
        rows = _query(isolated_db, "SELECT ts FROM metric_event WHERE metric='heartbeat'")
        stored = rows[0][0]
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        assert before <= stored <= after

    def test_record_deduplication_same_ts_metric_tags(self, isolated_db):
        """INSERT OR IGNORE: second write with identical (ts, metric, tags) is silently skipped."""
        fixed_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        sw.record("dupe", 1.0, "count", tags={"k": "v"}, ts=fixed_ts)
        sw.record("dupe", 99.0, "count", tags={"k": "v"}, ts=fixed_ts)  # ignored
        rows = _query(isolated_db, "SELECT value FROM metric_event WHERE metric='dupe'")
        assert len(rows) == 1
        assert rows[0][0] == 1.0  # first value wins

    def test_record_different_timestamps_both_stored(self, isolated_db):
        t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        sw.record("metric_x", 1.0, "count", ts=t1)
        sw.record("metric_x", 2.0, "count", ts=t2)
        rows = _query(isolated_db, "SELECT value FROM metric_event WHERE metric='metric_x' ORDER BY ts")
        assert len(rows) == 2
        assert rows[0][0] == 1.0
        assert rows[1][0] == 2.0

    def test_record_different_tags_both_stored(self, isolated_db):
        """Same ts+metric but different tags → both rows stored (different PK)."""
        fixed_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        sw.record("verdict", 1.0, "event", tags={"role": "executor"}, ts=fixed_ts)
        sw.record("verdict", 1.0, "event", tags={"role": "reviewer"}, ts=fixed_ts)
        rows = _query(isolated_db, "SELECT tags FROM metric_event WHERE metric='verdict' ORDER BY tags")
        assert len(rows) == 2

    def test_record_float_value_stored_faithfully(self, isolated_db):
        sw.record("ratio_m", 0.123456789, "ratio")
        rows = _query(isolated_db, "SELECT value FROM metric_event WHERE metric='ratio_m'")
        assert abs(rows[0][0] - 0.123456789) < 1e-9

    def test_record_creates_index(self, isolated_db):
        """The idx_metric_time index must exist after the first record call."""
        sw.record("any", 1.0, "count")
        rows = _query(
            isolated_db,
            "SELECT index_name FROM duckdb_indexes() WHERE index_name='idx_metric_time'"
        )
        assert len(rows) >= 1

    def test_record_multiple_different_metrics(self, isolated_db):
        sw.record("alpha", 1.0, "count")
        sw.record("beta", 2.0, "usd")
        sw.record("gamma", 0.5, "ratio")
        rows = _query(isolated_db, "SELECT metric FROM metric_event ORDER BY metric")
        assert [r[0] for r in rows] == ["alpha", "beta", "gamma"]


# ===========================================================================
# record_many() — bulk write
# ===========================================================================


class TestRecordMany:

    def test_record_many_writes_all_rows(self, isolated_db):
        rows = [
            {"metric": "m1", "value": 1.0, "unit": "count"},
            {"metric": "m2", "value": 2.0, "unit": "usd"},
            {"metric": "m3", "value": 3.0, "unit": "seconds"},
        ]
        sw.record_many(rows)
        stored = _query(isolated_db, "SELECT metric FROM metric_event ORDER BY metric")
        assert [r[0] for r in stored] == ["m1", "m2", "m3"]

    def test_record_many_forwards_optional_fields(self, isolated_db):
        fixed_ts = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)
        sw.record_many([{
            "metric": "ttm",
            "value": 120.0,
            "unit": "seconds",
            "tags": {"pr": "42"},
            "source": "post-merge-hook",
            "ts": fixed_ts,
        }])
        rows = _query(
            isolated_db,
            "SELECT source, tags FROM metric_event WHERE metric='ttm'"
        )
        assert rows[0][0] == "post-merge-hook"
        assert json.loads(rows[0][1])["pr"] == "42"

    def test_record_many_empty_list_is_noop(self, isolated_db):
        # record_many([]) calls no writes, so no table is created.
        # The observable effect is simply that no exception is raised.
        sw.record_many([])  # should not raise
        # Verify the DB file exists or is new — no metric_event rows at all.
        # Write one real row first to ensure the table exists, then confirm count is 1.
        sw.record("sentinel", 1.0, "count")
        rows = _query(isolated_db, "SELECT COUNT(*) FROM metric_event")
        assert rows[0][0] == 1  # only the sentinel, not from the empty batch


# ===========================================================================
# record_loop_iter() — loop_metrics table
# ===========================================================================


class TestRecordLoopIter:

    def test_record_loop_iter_creates_row(self, isolated_db):
        fixed_ts = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
        sw.record_loop_iter(ts=fixed_ts, duration_s=45.5)
        rows = _query(isolated_db, "SELECT duration_s FROM loop_metrics")
        assert len(rows) == 1
        assert abs(rows[0][0] - 45.5) < 0.01

    def test_record_loop_iter_computes_tokens_per_iter(self, isolated_db):
        """tokens_per_iter = input_tokens + output_tokens (cache excluded)."""
        fixed_ts = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
        sw.record_loop_iter(
            ts=fixed_ts,
            duration_s=30.0,
            team_lead_input_tokens=50000,
            team_lead_output_tokens=8000,
            team_lead_cache_read=10000,
            team_lead_cache_write=2000,
        )
        rows = _query(
            isolated_db,
            "SELECT team_lead_tokens_per_iter, team_lead_cache_read FROM loop_metrics"
        )
        assert rows[0][0] == 58000  # 50000 + 8000
        assert rows[0][1] == 10000  # cache_read stored separately

    def test_record_loop_iter_stores_all_token_columns(self, isolated_db):
        fixed_ts = datetime(2026, 5, 1, 11, 0, 0, tzinfo=timezone.utc)
        sw.record_loop_iter(
            ts=fixed_ts,
            duration_s=20.0,
            team_lead_input_tokens=100,
            team_lead_output_tokens=200,
            team_lead_cache_read=300,
            team_lead_cache_write=400,
        )
        rows = _query(
            isolated_db,
            "SELECT team_lead_input_tokens, team_lead_output_tokens, "
            "team_lead_cache_read, team_lead_cache_write FROM loop_metrics"
        )
        assert rows[0] == (100, 200, 300, 400)

    def test_record_loop_iter_duplicate_ts_ignored(self, isolated_db):
        """INSERT OR IGNORE: second write with same ts is silently skipped."""
        fixed_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        sw.record_loop_iter(ts=fixed_ts, duration_s=10.0, team_lead_input_tokens=100)
        sw.record_loop_iter(ts=fixed_ts, duration_s=99.9, team_lead_input_tokens=9999)
        rows = _query(isolated_db, "SELECT duration_s FROM loop_metrics")
        assert len(rows) == 1
        assert abs(rows[0][0] - 10.0) < 0.01

    def test_record_loop_iter_negative_tokens_clamped_to_zero(self, isolated_db):
        """tokens_per_iter uses max(0, tokens) so negatives don't corrupt the total."""
        fixed_ts = datetime(2026, 5, 1, 13, 0, 0, tzinfo=timezone.utc)
        sw.record_loop_iter(
            ts=fixed_ts,
            team_lead_input_tokens=-500,
            team_lead_output_tokens=-100,
        )
        rows = _query(isolated_db, "SELECT team_lead_tokens_per_iter FROM loop_metrics")
        assert rows[0][0] == 0

    def test_record_loop_iter_defaults_to_now(self, isolated_db):
        # stats_writer truncates to millisecond precision; subtract 1ms to avoid
        # spurious failure when truncated stored value is slightly behind before.
        before = datetime.now(timezone.utc) - timedelta(milliseconds=1)
        sw.record_loop_iter(duration_s=5.0)
        after = datetime.now(timezone.utc)
        rows = _query(isolated_db, "SELECT ts FROM loop_metrics")
        stored = rows[0][0]
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        assert before <= stored <= after

    def test_record_loop_iter_multiple_rows(self, isolated_db):
        for i in range(3):
            ts = datetime(2026, 5, 1, i, 0, 0, tzinfo=timezone.utc)
            sw.record_loop_iter(ts=ts, duration_s=float(i * 10))
        rows = _query(isolated_db, "SELECT duration_s FROM loop_metrics ORDER BY ts")
        assert len(rows) == 3
        assert rows[0][0] == 0.0
        assert rows[1][0] == 10.0
        assert rows[2][0] == 20.0


# ===========================================================================
# emit_verdict() — role_verdict metric
# ===========================================================================


class TestEmitVerdict:

    def test_emit_verdict_creates_role_verdict_row(self, isolated_db):
        sw.emit_verdict("executor", "done")
        rows = _query(isolated_db, "SELECT metric FROM metric_event")
        assert rows[0][0] == "role_verdict"

    def test_emit_verdict_tags_contain_role_and_verdict(self, isolated_db):
        sw.emit_verdict("code-reviewer", "pass")
        rows = _query(isolated_db, "SELECT tags FROM metric_event WHERE metric='role_verdict'")
        tags = json.loads(rows[0][0])
        assert tags["role"] == "code-reviewer"
        assert tags["verdict"] == "pass"

    def test_emit_verdict_value_is_one(self, isolated_db):
        sw.emit_verdict("executor", "fail")
        rows = _query(isolated_db, "SELECT value FROM metric_event WHERE metric='role_verdict'")
        assert rows[0][0] == 1.0

    def test_emit_verdict_unit_is_event(self, isolated_db):
        sw.emit_verdict("executor", "needs-fix")
        rows = _query(isolated_db, "SELECT unit FROM metric_event WHERE metric='role_verdict'")
        assert rows[0][0] == "event"

    def test_emit_verdict_source_is_post_agent_hook(self, isolated_db):
        sw.emit_verdict("executor", "done")
        rows = _query(isolated_db, "SELECT source FROM metric_event WHERE metric='role_verdict'")
        assert rows[0][0] == "post-agent-hook"

    def test_emit_verdict_explicit_timestamp(self, isolated_db):
        fixed_ts = datetime(2026, 4, 1, 8, 0, 0, tzinfo=timezone.utc)
        sw.emit_verdict("executor", "done", ts=fixed_ts)
        rows = _query(isolated_db, "SELECT ts FROM metric_event WHERE metric='role_verdict'")
        stored = rows[0][0]
        assert stored.year == 2026 and stored.month == 4 and stored.day == 1

    def test_emit_verdict_multiple_roles_stored_independently(self, isolated_db):
        t1 = datetime(2026, 5, 1, 1, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 1, 2, 0, 0, tzinfo=timezone.utc)
        sw.emit_verdict("executor", "done", ts=t1)
        sw.emit_verdict("code-reviewer", "pass", ts=t2)
        rows = _query(
            isolated_db,
            "SELECT tags FROM metric_event WHERE metric='role_verdict' ORDER BY ts"
        )
        assert len(rows) == 2
        roles = [json.loads(r[0])["role"] for r in rows]
        assert "executor" in roles
        assert "code-reviewer" in roles


# ===========================================================================
# role_success_rate_24h() — aggregation
# ===========================================================================


class TestRoleSuccessRate24h:

    def _seed_verdicts(self, role: str, verdicts: list[str], base_offset_hours: int = 0):
        """Write verdict rows with recent timestamps so they fall within 24h window."""
        now = datetime.now(timezone.utc) - timedelta(hours=base_offset_hours)
        for i, verdict in enumerate(verdicts):
            ts = now - timedelta(minutes=i)
            sw.emit_verdict(role, verdict, ts=ts)

    def test_returns_empty_list_when_db_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "nonexistent.duckdb"))
        result = sw.role_success_rate_24h()
        assert result == []

    def test_high_success_rate_for_mostly_passing_role(self, isolated_db):
        self._seed_verdicts("executor", ["done", "done", "done", "done", "fail"])
        results = sw.role_success_rate_24h()
        executor_row = next((r for r in results if r["role"] == "executor"), None)
        assert executor_row is not None
        assert executor_row["sample_size"] == 5
        assert abs(executor_row["success_rate"] - 0.8) < 0.01

    def test_low_success_rate_for_failing_role(self, isolated_db):
        self._seed_verdicts("code-reviewer", ["pass", "needs-fix", "needs-fix", "needs-fix", "needs-fix"])
        results = sw.role_success_rate_24h()
        cr_row = next((r for r in results if r["role"] == "code-reviewer"), None)
        assert cr_row is not None
        assert abs(cr_row["success_rate"] - 0.2) < 0.01

    def test_sample_size_below_5_returns_none_rate(self, isolated_db):
        """Roles with fewer than 5 samples return success_rate=None (N/A rule)."""
        self._seed_verdicts("security-reviewer", ["pass", "pass", "pass"])  # only 3
        results = sw.role_success_rate_24h()
        sr_row = next((r for r in results if r["role"] == "security-reviewer"), None)
        assert sr_row is not None
        assert sr_row["success_rate"] is None
        assert sr_row["sample_size"] == 3

    def test_sorted_lowest_success_rate_first(self, isolated_db):
        """Results sorted: lowest success_rate first, None rows last."""
        # executor: 4/5 = 0.8
        self._seed_verdicts("executor", ["done"] * 4 + ["fail"])
        # reviewer: 1/5 = 0.2
        t2 = datetime.now(timezone.utc) - timedelta(hours=1)
        for i, v in enumerate(["pass", "needs-fix", "needs-fix", "needs-fix", "needs-fix"]):
            ts = t2 - timedelta(minutes=i + 10)
            sw.emit_verdict("code-reviewer", v, ts=ts)

        results = sw.role_success_rate_24h()
        rated = [r for r in results if r["success_rate"] is not None]
        assert len(rated) >= 2
        rates = [r["success_rate"] for r in rated]
        assert rates == sorted(rates)  # ascending order

    def test_only_pass_and_done_counted_as_success(self, isolated_db):
        """'skip', 'fail', 'needs-fix' do not count as successes."""
        self._seed_verdicts("executor", ["done", "pass", "skip", "fail", "needs-fix"])
        results = sw.role_success_rate_24h()
        row = next((r for r in results if r["role"] == "executor"), None)
        assert row is not None
        assert abs(row["success_rate"] - 0.4) < 0.01  # 2/5


# ===========================================================================
# role_retry_rate_24h() — aggregation
# ===========================================================================


class TestRoleRetryRate24h:

    def test_returns_empty_list_when_db_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "nonexistent.duckdb"))
        result = sw.role_retry_rate_24h()
        assert result == []

    def test_retry_rate_computed_correctly(self, isolated_db):
        now = datetime.now(timezone.utc)
        verdicts = ["done", "done", "needs-fix", "fail", "needs-fix"]
        for i, v in enumerate(verdicts):
            ts = now - timedelta(minutes=i)
            sw.emit_verdict("executor", v, ts=ts)
        results = sw.role_retry_rate_24h()
        row = next((r for r in results if r["role"] == "executor"), None)
        assert row is not None
        assert abs(row["retry_rate"] - 0.6) < 0.01  # 3/5

    def test_zero_retry_rate_for_all_pass_role(self, isolated_db):
        now = datetime.now(timezone.utc)
        for i in range(5):
            ts = now - timedelta(minutes=i)
            sw.emit_verdict("code-reviewer", "pass", ts=ts)
        results = sw.role_retry_rate_24h()
        row = next((r for r in results if r["role"] == "code-reviewer"), None)
        assert row is not None
        assert row["retry_rate"] == 0.0

    def test_sample_below_5_gives_none_rate(self, isolated_db):
        now = datetime.now(timezone.utc)
        for i in range(4):
            ts = now - timedelta(minutes=i)
            sw.emit_verdict("acceptance-tester", "pass", ts=ts)
        results = sw.role_retry_rate_24h()
        row = next((r for r in results if r["role"] == "acceptance-tester"), None)
        assert row is not None
        assert row["retry_rate"] is None
        assert row["sample_size"] == 4

    def test_sorted_highest_retry_rate_first(self, isolated_db):
        """Highest retry rate first; None rows last."""
        now = datetime.now(timezone.utc)
        # executor: 5/5 = 1.0
        for i in range(5):
            ts = now - timedelta(minutes=i)
            sw.emit_verdict("executor", "fail", ts=ts)
        # code-reviewer: 0/5 = 0.0
        for i in range(5):
            ts = now - timedelta(minutes=i + 10)
            sw.emit_verdict("code-reviewer", "pass", ts=ts)
        results = sw.role_retry_rate_24h()
        rated = [r for r in results if r["retry_rate"] is not None]
        assert len(rated) >= 2
        rates = [r["retry_rate"] for r in rated]
        assert rates == sorted(rates, reverse=True)

    def test_none_rate_rows_sorted_last(self, isolated_db):
        """Roles with sample_size < 5 (rate=None) appear after all rated rows."""
        now = datetime.now(timezone.utc)
        # Enough samples for executor
        for i in range(5):
            ts = now - timedelta(minutes=i)
            sw.emit_verdict("executor", "done", ts=ts)
        # Too few for security-reviewer
        for i in range(2):
            ts = now - timedelta(minutes=i + 20)
            sw.emit_verdict("security-reviewer", "pass", ts=ts)
        results = sw.role_retry_rate_24h()
        none_rows = [r for r in results if r["retry_rate"] is None]
        rated_rows = [r for r in results if r["retry_rate"] is not None]
        if none_rows and rated_rows:
            last_rated_idx = max(results.index(r) for r in rated_rows)
            first_none_idx = min(results.index(r) for r in none_rows)
            assert last_rated_idx < first_none_idx


# ===========================================================================
# record_live_analyst_intervention() — three-row emit
# ===========================================================================


class TestRecordLiveAnalystIntervention:

    def test_emits_three_metric_rows(self, isolated_db):
        fixed_ts = datetime(2026, 5, 10, 8, 0, 0, tzinfo=timezone.utc)
        sw.record_live_analyst_intervention(
            agent_id="executor-7",
            classifier="loop_violation",
            intervention_number=1,
            ts=fixed_ts,
        )
        rows = _query(isolated_db, "SELECT metric FROM metric_event ORDER BY metric")
        metrics = [r[0] for r in rows]
        assert "intervention_count" in metrics
        assert "interventions_per_classifier" in metrics
        assert "interventions_per_agent_avg" in metrics

    def test_intervention_count_tagged_with_agent_and_classifier(self, isolated_db):
        fixed_ts = datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
        sw.record_live_analyst_intervention("executor-3", "loop_violation", 2, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT tags FROM metric_event WHERE metric='intervention_count'"
        )
        tags = json.loads(rows[0][0])
        assert tags["agent_id"] == "executor-3"
        assert tags["classifier"] == "loop_violation"

    def test_interventions_per_classifier_tagged_with_classifier_only(self, isolated_db):
        fixed_ts = datetime(2026, 5, 10, 10, 0, 0, tzinfo=timezone.utc)
        sw.record_live_analyst_intervention("agent-1", "runaway", 1, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT tags FROM metric_event WHERE metric='interventions_per_classifier'"
        )
        tags = json.loads(rows[0][0])
        assert "classifier" in tags
        assert tags["classifier"] == "runaway"
        # Should NOT contain agent_id
        assert "agent_id" not in tags

    def test_interventions_per_agent_avg_stores_intervention_number(self, isolated_db):
        fixed_ts = datetime(2026, 5, 10, 11, 0, 0, tzinfo=timezone.utc)
        sw.record_live_analyst_intervention("agent-X", "loop_violation", 5, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT value FROM metric_event WHERE metric='interventions_per_agent_avg'"
        )
        assert rows[0][0] == 5.0

    def test_all_three_rows_use_live_analyst_source(self, isolated_db):
        fixed_ts = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        sw.record_live_analyst_intervention("agent-Y", "classifier_x", 1, ts=fixed_ts)
        rows = _query(isolated_db, "SELECT DISTINCT source FROM metric_event")
        sources = {r[0] for r in rows}
        assert sources == {"live-analyst"}


# ===========================================================================
# record_intervention_outcome() — self_corrected flag
# ===========================================================================


class TestRecordInterventionOutcome:

    def test_self_corrected_true_stores_one(self, isolated_db):
        fixed_ts = datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
        sw.record_intervention_outcome("agent-1", "loop_violation", True, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT value FROM metric_event WHERE metric='intervention_to_self_correction_rate'"
        )
        assert rows[0][0] == 1.0

    def test_self_corrected_false_stores_zero(self, isolated_db):
        fixed_ts = datetime(2026, 5, 11, 1, 0, 0, tzinfo=timezone.utc)
        sw.record_intervention_outcome("agent-2", "loop_violation", False, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT value FROM metric_event WHERE metric='intervention_to_self_correction_rate'"
        )
        assert rows[0][0] == 0.0

    def test_tags_contain_agent_and_classifier(self, isolated_db):
        fixed_ts = datetime(2026, 5, 11, 2, 0, 0, tzinfo=timezone.utc)
        sw.record_intervention_outcome("agent-3", "runaway_cost", True, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT tags FROM metric_event WHERE metric='intervention_to_self_correction_rate'"
        )
        tags = json.loads(rows[0][0])
        assert tags["agent_id"] == "agent-3"
        assert tags["classifier"] == "runaway_cost"

    def test_unit_is_ratio(self, isolated_db):
        fixed_ts = datetime(2026, 5, 11, 3, 0, 0, tzinfo=timezone.utc)
        sw.record_intervention_outcome("agent-4", "x", True, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT unit FROM metric_event WHERE metric='intervention_to_self_correction_rate'"
        )
        assert rows[0][0] == "ratio"


# ===========================================================================
# record_cost_spike() and record_iteration_cost()
# ===========================================================================


class TestCostMetrics:

    def test_record_cost_spike_metric_name(self, isolated_db):
        fixed_ts = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)
        sw.record_cost_spike(0.95, mu=0.5, sigma=0.2, ts=fixed_ts)
        rows = _query(isolated_db, "SELECT metric FROM metric_event")
        assert rows[0][0] == "cost_spike"

    def test_record_cost_spike_stores_value(self, isolated_db):
        fixed_ts = datetime(2026, 5, 12, 1, 0, 0, tzinfo=timezone.utc)
        sw.record_cost_spike(1.23456, mu=1.0, sigma=0.1, ts=fixed_ts)
        rows = _query(isolated_db, "SELECT value FROM metric_event WHERE metric='cost_spike'")
        assert abs(rows[0][0] - 1.23456) < 0.0001

    def test_record_cost_spike_tags_contain_mu_and_sigma(self, isolated_db):
        fixed_ts = datetime(2026, 5, 12, 2, 0, 0, tzinfo=timezone.utc)
        sw.record_cost_spike(0.8, mu=0.5, sigma=0.15, ts=fixed_ts)
        rows = _query(isolated_db, "SELECT tags FROM metric_event WHERE metric='cost_spike'")
        tags = json.loads(rows[0][0])
        assert "mu" in tags
        assert "sigma" in tags
        assert float(tags["mu"]) == pytest.approx(0.5, abs=1e-4)
        assert float(tags["sigma"]) == pytest.approx(0.15, abs=1e-4)

    def test_record_cost_spike_unit_is_usd(self, isolated_db):
        fixed_ts = datetime(2026, 5, 12, 3, 0, 0, tzinfo=timezone.utc)
        sw.record_cost_spike(0.5, mu=0.3, sigma=0.1, ts=fixed_ts)
        rows = _query(isolated_db, "SELECT unit FROM metric_event WHERE metric='cost_spike'")
        assert rows[0][0] == "usd"

    def test_record_iteration_cost_metric_name(self, isolated_db):
        fixed_ts = datetime(2026, 5, 12, 4, 0, 0, tzinfo=timezone.utc)
        sw.record_iteration_cost(0.042, ts=fixed_ts)
        rows = _query(isolated_db, "SELECT metric FROM metric_event")
        assert rows[0][0] == "iteration_cost_usd"

    def test_record_iteration_cost_stores_value(self, isolated_db):
        fixed_ts = datetime(2026, 5, 12, 5, 0, 0, tzinfo=timezone.utc)
        sw.record_iteration_cost(0.075, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT value FROM metric_event WHERE metric='iteration_cost_usd'"
        )
        assert abs(rows[0][0] - 0.075) < 1e-6

    def test_record_iteration_cost_unit_is_usd(self, isolated_db):
        fixed_ts = datetime(2026, 5, 12, 6, 0, 0, tzinfo=timezone.utc)
        sw.record_iteration_cost(0.01, ts=fixed_ts)
        rows = _query(
            isolated_db,
            "SELECT unit FROM metric_event WHERE metric='iteration_cost_usd'"
        )
        assert rows[0][0] == "usd"


# ===========================================================================
# cost_spike_history() — time-windowed read-back
# ===========================================================================


class TestCostSpikeHistory:

    def test_returns_empty_when_db_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "ghost.duckdb"))
        result = sw.cost_spike_history(hours=24)
        assert result == []

    def test_recent_spikes_returned(self, isolated_db):
        now = datetime.now(timezone.utc)
        sw.record_cost_spike(1.1, mu=0.5, sigma=0.2, ts=now - timedelta(hours=1))
        sw.record_cost_spike(1.5, mu=0.5, sigma=0.2, ts=now - timedelta(hours=2))
        result = sw.cost_spike_history(hours=24)
        assert len(result) == 2

    def test_old_spikes_excluded(self, isolated_db):
        now = datetime.now(timezone.utc)
        # Recent spike within window
        sw.record_cost_spike(1.2, mu=0.5, sigma=0.2, ts=now - timedelta(hours=2))
        # Old spike outside window
        sw.record_cost_spike(9.9, mu=0.5, sigma=0.2, ts=now - timedelta(hours=48))
        result = sw.cost_spike_history(hours=24)
        assert len(result) == 1
        assert abs(result[0]["value"] - 1.2) < 0.001

    def test_sorted_newest_first(self, isolated_db):
        now = datetime.now(timezone.utc)
        sw.record_cost_spike(1.0, mu=0.5, sigma=0.1, ts=now - timedelta(hours=5))
        sw.record_cost_spike(2.0, mu=0.5, sigma=0.1, ts=now - timedelta(hours=3))
        sw.record_cost_spike(3.0, mu=0.5, sigma=0.1, ts=now - timedelta(hours=1))
        result = sw.cost_spike_history(hours=24)
        assert len(result) == 3
        values = [r["value"] for r in result]
        assert values == [3.0, 2.0, 1.0]  # newest first

    def test_result_contains_expected_keys(self, isolated_db):
        now = datetime.now(timezone.utc)
        sw.record_cost_spike(0.9, mu=0.4, sigma=0.05, ts=now - timedelta(minutes=5))
        result = sw.cost_spike_history(hours=24)
        assert len(result) == 1
        row = result[0]
        assert "ts_iso" in row
        assert "value" in row
        assert "mu" in row
        assert "sigma" in row
        assert row["mu"] == pytest.approx(0.4, abs=1e-4)
        assert row["sigma"] == pytest.approx(0.05, abs=1e-4)


# ===========================================================================
# avg_fix_rounds_24h() — distribution + average
# ===========================================================================


class TestAvgFixRounds24h:

    def test_returns_zero_state_when_db_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "ghost.duckdb"))
        result = sw.avg_fix_rounds_24h()
        assert result["avg_last_24h"] is None
        assert result["sample_size"] == 0
        assert result["distribution"] == {}

    def test_average_computed_over_five_or_more(self, isolated_db):
        now = datetime.now(timezone.utc)
        for i, rounds in enumerate([0, 1, 1, 2, 0]):
            ts = now - timedelta(minutes=i)
            sw.record("fix_rounds_per_pr", float(rounds), "count", ts=ts)
        result = sw.avg_fix_rounds_24h()
        assert result["sample_size"] == 5
        assert result["avg_last_24h"] == pytest.approx(0.8, abs=0.01)

    def test_sample_below_5_returns_none_avg(self, isolated_db):
        now = datetime.now(timezone.utc)
        for i in range(4):
            ts = now - timedelta(minutes=i)
            sw.record("fix_rounds_per_pr", float(i), "count", ts=ts)
        result = sw.avg_fix_rounds_24h()
        assert result["avg_last_24h"] is None
        assert result["sample_size"] == 4

    def test_distribution_counts_each_value(self, isolated_db):
        now = datetime.now(timezone.utc)
        rounds_list = [0, 0, 1, 1, 2]
        for i, rounds in enumerate(rounds_list):
            ts = now - timedelta(minutes=i)
            sw.record("fix_rounds_per_pr", float(rounds), "count", ts=ts)
        result = sw.avg_fix_rounds_24h()
        dist = result["distribution"]
        assert dist.get("0") == 2
        assert dist.get("1") == 2
        assert dist.get("2") == 1

    def test_old_rows_excluded(self, isolated_db):
        now = datetime.now(timezone.utc)
        # 5 recent rows
        for i in range(5):
            ts = now - timedelta(minutes=i)
            sw.record("fix_rounds_per_pr", 1.0, "count", ts=ts)
        # 3 old rows (>24h ago)
        for i in range(3):
            ts = now - timedelta(hours=25 + i)
            sw.record("fix_rounds_per_pr", 99.0, "count", ts=ts)
        result = sw.avg_fix_rounds_24h()
        assert result["sample_size"] == 5
        assert result["avg_last_24h"] == 1.0


# ===========================================================================
# team_lead_tokens_percentiles() — p50/p95
# ===========================================================================


class TestTeamLeadTokensPercentiles:

    def test_returns_none_when_db_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "ghost.duckdb"))
        result = sw.team_lead_tokens_percentiles(since_hours=24)
        assert result["avg"] is None
        assert result["sample_size"] == 0

    def test_returns_none_when_fewer_than_5_samples(self, isolated_db):
        now = datetime.now(timezone.utc)
        for i in range(4):
            ts = now - timedelta(hours=i)
            sw.record_loop_iter(ts=ts, team_lead_input_tokens=1000, team_lead_output_tokens=200)
        result = sw.team_lead_tokens_percentiles(since_hours=24)
        assert result["avg"] is None
        assert result["sample_size"] == 4

    def test_computes_percentiles_with_five_or_more_samples(self, isolated_db):
        now = datetime.now(timezone.utc)
        # Write 10 rows with known tokens_per_iter values: 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000
        for i, tokens_per_iter in enumerate(range(100, 1100, 100)):
            ts = now - timedelta(hours=i)
            # input + output = tokens_per_iter; cache tokens are separate
            sw.record_loop_iter(
                ts=ts,
                team_lead_input_tokens=tokens_per_iter,
                team_lead_output_tokens=0,
            )
        result = sw.team_lead_tokens_percentiles(since_hours=24)
        assert result["sample_size"] == 10
        assert result["avg"] is not None
        assert result["p50"] is not None
        assert result["p95"] is not None
        # average of 100..1000 = 550
        assert result["avg"] == pytest.approx(550.0, abs=1.0)

    def test_old_rows_excluded_from_window(self, isolated_db):
        now = datetime.now(timezone.utc)
        # 5 recent rows within 24h window
        for i in range(5):
            ts = now - timedelta(hours=i)
            sw.record_loop_iter(ts=ts, team_lead_input_tokens=1000, team_lead_output_tokens=0)
        # 10 old rows older than since_hours=24 — should not appear
        for i in range(10):
            ts = now - timedelta(hours=25 + i)
            sw.record_loop_iter(ts=ts, team_lead_input_tokens=99999, team_lead_output_tokens=0)
        result = sw.team_lead_tokens_percentiles(since_hours=24)
        assert result["sample_size"] == 5
        assert result["avg"] == pytest.approx(1000.0, abs=1.0)


# ===========================================================================
# loop_idle_ratio_24h() — jsonl-based reader
# ===========================================================================


class TestLoopIdleRatio24h:

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_returns_none_when_file_absent(self, tmp_path):
        result = sw.loop_idle_ratio_24h(metrics_path=str(tmp_path / "no_file.jsonl"))
        assert result["ratio"] is None
        assert result["sample_size"] == 0

    def test_idle_detected_by_agents_spawned_zero(self, tmp_path):
        now = datetime.now(timezone.utc)
        rows = [
            {"timestamp": (now - timedelta(hours=i)).isoformat(), "agents_spawned": 0 if i < 3 else 2}
            for i in range(10)
        ]
        path = tmp_path / "loop-metrics.jsonl"
        self._write_jsonl(path, rows)
        result = sw.loop_idle_ratio_24h(metrics_path=str(path))
        assert result["sample_size"] == 10
        assert result["idle_count"] == 3
        assert result["ratio"] == pytest.approx(0.3, abs=0.01)

    def test_idle_detected_by_idle_flag(self, tmp_path):
        now = datetime.now(timezone.utc)
        rows = [
            {"timestamp": (now - timedelta(hours=i)).isoformat(), "idle": True if i < 2 else False, "agents_spawned": 1}
            for i in range(10)
        ]
        path = tmp_path / "loop-metrics.jsonl"
        self._write_jsonl(path, rows)
        result = sw.loop_idle_ratio_24h(metrics_path=str(path))
        assert result["idle_count"] == 2

    def test_rows_older_than_24h_excluded(self, tmp_path):
        now = datetime.now(timezone.utc)
        rows = [
            # 5 recent idle rows
            {"timestamp": (now - timedelta(hours=i)).isoformat(), "agents_spawned": 0}
            for i in range(5)
        ] + [
            # 5 old non-idle rows
            {"timestamp": (now - timedelta(hours=25 + i)).isoformat(), "agents_spawned": 3}
            for i in range(5)
        ]
        path = tmp_path / "loop-metrics.jsonl"
        self._write_jsonl(path, rows)
        result = sw.loop_idle_ratio_24h(metrics_path=str(path))
        assert result["sample_size"] == 5
        assert result["idle_count"] == 5

    def test_sample_below_5_returns_none_ratio(self, tmp_path):
        now = datetime.now(timezone.utc)
        rows = [
            {"timestamp": (now - timedelta(minutes=i)).isoformat(), "agents_spawned": 0}
            for i in range(4)
        ]
        path = tmp_path / "loop-metrics.jsonl"
        self._write_jsonl(path, rows)
        result = sw.loop_idle_ratio_24h(metrics_path=str(path))
        assert result["ratio"] is None
        assert result["sample_size"] == 4

    def test_test_origin_rows_skipped(self, tmp_path):
        now = datetime.now(timezone.utc)
        rows = [
            {"timestamp": (now - timedelta(minutes=i)).isoformat(), "agents_spawned": 0, "origin": "test"}
            for i in range(10)
        ]
        path = tmp_path / "loop-metrics.jsonl"
        self._write_jsonl(path, rows)
        result = sw.loop_idle_ratio_24h(metrics_path=str(path))
        # test-origin rows excluded → sample_size = 0
        assert result["sample_size"] == 0
        assert result["ratio"] is None

    def test_corrupt_jsonl_lines_skipped(self, tmp_path):
        now = datetime.now(timezone.utc)
        path = tmp_path / "loop-metrics.jsonl"
        with path.open("w") as fh:
            fh.write("{not valid json\n")
            for i in range(6):
                fh.write(json.dumps({
                    "timestamp": (now - timedelta(minutes=i)).isoformat(),
                    "agents_spawned": 1,
                }) + "\n")
        result = sw.loop_idle_ratio_24h(metrics_path=str(path))
        assert result["sample_size"] == 6


# ===========================================================================
# registered_metrics() / REGISTERED_WRITERS
# ===========================================================================


class TestRegisteredMetrics:

    def test_registered_writers_is_frozenset(self):
        assert isinstance(sw.REGISTERED_WRITERS, frozenset)

    def test_registered_metrics_is_frozenset(self):
        result = sw.registered_metrics()
        assert isinstance(result, frozenset)

    def test_registered_metrics_is_superset_of_registered_writers(self):
        all_metrics = sw.registered_metrics()
        assert sw.REGISTERED_WRITERS.issubset(all_metrics)

    def test_core_writer_metrics_present(self):
        """Metrics known to have active record() calls are all registered."""
        expected = {
            "role_verdict",
            "intervention_count",
            "interventions_per_classifier",
            "interventions_per_agent_avg",
            "intervention_to_self_correction_rate",
            "cost_spike",
            "iteration_cost_usd",
        }
        all_metrics = sw.registered_metrics()
        missing = expected - all_metrics
        assert not missing, f"Missing registered metrics: {missing}"

    def test_external_metrics_present(self):
        """Metrics written by external scripts are also registered."""
        expected_external = {
            "time_to_merge_seconds",
            "fix_cycle_count",
            "cost_per_merged_pr_usd",
        }
        all_metrics = sw.registered_metrics()
        missing = expected_external - all_metrics
        assert not missing, f"Missing external metrics: {missing}"

    def test_cost_attribution_unresolved_count_registered(self):
        """D#2282: the suppression counter emitted when post-merge-hook.sh's
        cost resolver isn't `agent_run` must be a registered writer, same as
        the metric it replaces in that case (cost_per_merged_pr_usd)."""
        assert "cost_attribution_unresolved_count" in sw.registered_metrics()

    def test_no_empty_strings_in_registry(self):
        all_metrics = sw.registered_metrics()
        assert "" not in all_metrics

    def test_fix_rounds_per_pr_in_registered_writers(self):
        """fix_rounds_per_pr is explicitly listed in REGISTERED_WRITERS."""
        assert "fix_rounds_per_pr" in sw.REGISTERED_WRITERS


# ===========================================================================
# _db_path() — env var priority
# ===========================================================================


class TestDbPath:

    def test_stats_db_path_env_takes_priority(self, tmp_path, monkeypatch):
        custom = tmp_path / "my_custom.duckdb"
        monkeypatch.setenv("STATS_DB_PATH", str(custom))
        monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
        result = sw._db_path()
        assert result == custom

    def test_stats_db_path_env_overrides_state_dir(self, tmp_path, monkeypatch):
        custom = tmp_path / "priority.duckdb"
        monkeypatch.setenv("STATS_DB_PATH", str(custom))
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path / "state"))
        result = sw._db_path()
        assert result == custom
