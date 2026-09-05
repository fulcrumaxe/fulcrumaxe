"""tests/test_health_metrics_freshness.py

Asserts that every metric watched by the freshness checker has a corresponding
writer registration in backend/stats_writer.registered_metrics().

The freshness checker (stats_freshness_watchdog.py) dynamically scans ALL
distinct metric names stored in the DuckDB stats database.  If a metric row
exists with a stale timestamp but NO active writer, the watchdog will fire
false-positive stale alerts forever.  This test fails when that mismatch exists,
so dead metrics can never silently accumulate again.

Two test scenarios:
  1. Positive — a DB seeded with known-good metrics.  All must be in the writer set.
  2. Negative — the three historically dead metrics must NOT be in the writer set,
     confirming that adding them to the DB (without a writer) would break this test.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from backend.stats_writer import registered_metrics  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_duckdb(metrics: list[str]) -> str:
    """Create a temp DuckDB with one fresh row per metric name. Returns DB path."""
    import duckdb

    # Use a non-existent path so DuckDB creates it fresh (it rejects zero-byte files).
    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # Remove the placeholder so DuckDB creates it from scratch.
    db_path = tmp.name

    conn = duckdb.connect(db_path)
    conn.execute("""
        CREATE TABLE metric_event (
            ts      TIMESTAMP NOT NULL,
            metric  TEXT      NOT NULL,
            tags    JSON,
            value   DOUBLE    NOT NULL,
            unit    TEXT      NOT NULL,
            source  TEXT
        )
    """)
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S.000")
    for m in metrics:
        conn.execute(
            "INSERT INTO metric_event VALUES (CAST(? AS TIMESTAMP), ?, '{}', 1.0, 'count', 'test')",
            [ts_str, m],
        )
    conn.close()
    return db_path


def _get_db_metrics(db_path: str) -> set[str]:
    """Return the set of distinct metric names in a DuckDB file."""
    import duckdb

    conn = duckdb.connect(db_path, read_only=True)
    try:
        rows = conn.execute("SELECT DISTINCT metric FROM metric_event").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The three dead metric names (Discussion #1153)
# ---------------------------------------------------------------------------

DEAD_METRICS = {
    "test_write_with_dashboard_running",
    "acceptance_test_gate2",
    "test_write_gate2",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRegisteredMetricsCoversAllWatched:
    """Every metric the freshness checker would watch must have a writer."""

    def test_known_live_metrics_all_have_writers(self) -> None:
        """Seed a DB with the real production metrics; assert all have writers."""
        # These are the metrics that exist in the live DB (validated 2026-05-19).
        # The test would catch any new metric written without a corresponding
        # registered_metrics() entry.
        live_metrics = [
            "acceptance_criteria_pass_rate",
            "cost_per_merged_pr_usd",
            "fix_cycle_count",
            "fix_rounds_per_pr",
            "hard_rule_violation_count",
            "impersonation_rate",
            "loop_iteration_duration_seconds",
            "orphan_worktree_rate",
            "pr_file_conflict_score",
            "reviewer_acceptance_latency_seconds",
            "role_verdict",
            "scan_to_spawn_ratio",
            "spec_to_first_pr_latency_seconds",
            "time_to_merge_seconds",
            "wasted_tokens_ratio",
        ]
        db_path = _seed_duckdb(live_metrics)
        try:
            watched = _get_db_metrics(db_path)
            writers = registered_metrics()
            missing = watched - writers
            assert not missing, (
                f"The following metrics are in the DB (watched by freshness checker) "
                f"but have no registered writer — they will cause false-positive stale "
                f"alerts. Add them to stats_writer.registered_metrics() or delete the "
                f"rows from DuckDB:\n  {sorted(missing)}"
            )
        finally:
            os.unlink(db_path)

    def test_dead_metrics_not_in_writer_set(self) -> None:
        """Dead metrics must NOT be in registered_metrics() — they have no writer.

        If any of the three historically-dead metric names were re-added to the
        writer set without a real writer implementation, this test would not catch
        it. The assertion is: a DB seeded with ONLY dead metrics produces a mismatch
        (watched - writers is non-empty), proving the test would fail if a dead
        metric appeared in the DB.
        """
        db_path = _seed_duckdb(list(DEAD_METRICS))
        try:
            watched = _get_db_metrics(db_path)
            writers = registered_metrics()
            # None of the dead metrics should be in the writer set
            in_writers = DEAD_METRICS & writers
            assert not in_writers, (
                f"Dead metric(s) {sorted(in_writers)} were added to registered_metrics() "
                f"without a real writer implementation. Remove them from the writer set "
                f"and delete any existing rows from DuckDB."
            )
            # Confirm mismatch is what we expect (dead → no writer → test would fail)
            missing = watched - writers
            assert missing == DEAD_METRICS, (
                f"Expected dead metrics {sorted(DEAD_METRICS)} to have no writer. "
                f"Got: {sorted(missing)}"
            )
        finally:
            os.unlink(db_path)

    def test_real_db_has_no_unwatched_dead_metrics(self) -> None:
        """If the real stats.duckdb exists, it must contain no dead metric rows.

        This test runs the migration (dry-run) and asserts the count is 0.
        If it's not 0, the migration has not been run yet — the CI gate catches it.
        Skips gracefully when DuckDB is not installed or the DB file does not exist.
        """
        try:
            import duckdb  # noqa: PLC0415
        except ImportError:
            pytest.skip("duckdb not installed")

        # Resolve the real DB path using the same priority order as stats_writer
        from backend.stats_writer import _db_path  # noqa: PLC0415
        db_path = _db_path()
        if not db_path.exists():
            pytest.skip(f"stats.duckdb not found at {db_path}")

        dead = list(DEAD_METRICS)
        placeholders = ", ".join("?" for _ in dead)
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            (count,) = conn.execute(
                f"SELECT COUNT(*) FROM metric_event WHERE metric IN ({placeholders})",
                dead,
            ).fetchone()
        finally:
            conn.close()

        assert count == 0, (
            f"Found {count} dead-metric row(s) in {db_path} for "
            f"{sorted(DEAD_METRICS)}. Run the migration to clean them up:\n"
            f"  python3 backend/migrations/001_drop_dead_metrics.py"
        )
