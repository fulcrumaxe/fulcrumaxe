"""Tests for backend/stats/batch_attempt.py — D#2524 PR-b.

Covers:
    mark_batch_attempt()      — writes a stats_batch_attempt row
    find_incomplete_batches() — the detector (acceptance items 8, 9)

Isolation: STATS_DB_PATH env var is monkeypatched to a tmp_path file, same
pattern as backend/tests/test_stats_writer.py. The real
~/.autonomous-forever-state/stats.duckdb is NEVER touched.

Run with:
    python3 -m pytest backend/tests/test_stats_batch_attempt.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    import duckdb as _duckdb_mod
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect every stats_writer / batch_attempt call to a fresh temp DuckDB."""
    db_file = tmp_path / "test_stats.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(db_file))
    monkeypatch.delenv("AUTONOMOUS_TEAM_STATE_DIR", raising=False)
    yield db_file


import backend.stats_writer as sw  # noqa: E402
import backend.stats.batch_attempt as ba  # noqa: E402


def _write_metric_rows(pr: str, metrics: list[str]) -> None:
    """Simulate a (possibly partial) merge batch landing in metric_event,
    tagged the same way scripts/post-merge-hook.sh tags every row: {"pr": pr}.
    """
    rows = [{"metric": m, "value": 1.0, "unit": "count", "tags": {"pr": pr}} for m in metrics]
    sw.record_many(rows)


# ===========================================================================
# mark_batch_attempt()
# ===========================================================================


class TestMarkBatchAttempt:

    def test_writes_pr_and_expected_metrics(self, isolated_db):
        ba.mark_batch_attempt("77", ["a", "b", "c"], source="post-merge-hook")
        conn = _duckdb_mod.connect(str(isolated_db))
        try:
            rows = conn.execute(
                "SELECT pr, expected_metrics, source FROM stats_batch_attempt"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "77"
        assert json.loads(rows[0][1]) == ["a", "b", "c"]
        assert rows[0][2] == "post-merge-hook"

    def test_multiple_attempts_for_different_prs_coexist(self, isolated_db):
        ba.mark_batch_attempt("1", ["x"])
        ba.mark_batch_attempt("2", ["y"])
        conn = _duckdb_mod.connect(str(isolated_db))
        try:
            rows = conn.execute("SELECT pr FROM stats_batch_attempt ORDER BY pr").fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == ["1", "2"]


# ===========================================================================
# find_incomplete_batches() — acceptance items 8, 9
# ===========================================================================


class TestFindIncompleteBatches:

    def test_no_db_file_returns_empty(self, tmp_path, monkeypatch):
        # A path that has never been written to at all.
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "never_created.duckdb"))
        assert ba.find_incomplete_batches() == []

    def test_no_attempt_table_returns_empty(self, isolated_db):
        # DB exists (metric_event written) but stats_batch_attempt was never
        # created — pre-PR-b state. Nothing is attributable.
        sw.record("sentinel", 1.0, "count")
        assert ba.find_incomplete_batches() == []

    def test_complete_batch_reports_nothing(self, isolated_db):
        """Item 8: a complete batch is reported as nothing."""
        expected = ["time_to_merge_seconds", "fix_cycle_count", "pr_file_conflict_score"]
        ba.mark_batch_attempt("42", expected)
        _write_metric_rows("42", expected)
        assert ba.find_incomplete_batches() == []

    def test_partial_prefix_batch_reported_naming_pr(self, isolated_db):
        """Item 8: a proper-prefix batch is reported, naming the PR."""
        expected = ["time_to_merge_seconds", "fix_cycle_count", "pr_file_conflict_score"]
        ba.mark_batch_attempt("42", expected)
        _write_metric_rows("42", expected[:1])  # only the first metric landed

        results = ba.find_incomplete_batches()
        assert len(results) == 1
        assert results[0].pr == "42"
        assert results[0].status == "lost_partial"
        assert results[0].written == ["time_to_merge_seconds"]
        assert results[0].missing == ["fix_cycle_count", "pr_file_conflict_score"]

    def test_zero_row_batch_with_marker_is_lost_zero(self, isolated_db):
        """Item 9: a zero-row batch WITH an attempt marker is reported as lost,
        not silently skipped."""
        expected = ["time_to_merge_seconds", "fix_cycle_count"]
        ba.mark_batch_attempt("99", expected)
        # No metric_event rows written for PR 99 at all.
        sw.record("unrelated_metric", 1.0, "count", tags={"pr": "1"})

        results = ba.find_incomplete_batches()
        assert len(results) == 1
        assert results[0].pr == "99"
        assert results[0].status == "lost_zero"
        assert results[0].written == []
        assert results[0].missing == sorted(expected)

    def test_zero_row_batch_without_marker_is_not_reported(self, isolated_db):
        """Item 9: a zero-row batch with NO attempt marker (hook never ran,
        or pre-PR-b data) cannot be attributed and must not be reported."""
        # A marker exists for PR 1 only; PR 100 has nothing at all — no
        # marker, no metric_event rows. That is the "hook never ran"
        # ambiguity this Discussion documents, and it must stay unreported.
        ba.mark_batch_attempt("1", ["m1"])
        _write_metric_rows("1", ["m1"])

        results = ba.find_incomplete_batches()
        assert [r.pr for r in results] == []

    def test_filters_by_pr(self, isolated_db):
        ba.mark_batch_attempt("10", ["a", "b"])
        _write_metric_rows("10", ["a"])  # incomplete
        ba.mark_batch_attempt("20", ["c", "d"])
        _write_metric_rows("20", ["c"])  # also incomplete

        results = ba.find_incomplete_batches(prs=["10"])
        assert [r.pr for r in results] == ["10"]

    def test_as_dict_shape(self, isolated_db):
        ba.mark_batch_attempt("5", ["only_metric"])
        results = ba.find_incomplete_batches()
        assert results[0].as_dict() == {
            "pr": "5",
            "attempted_at": results[0].attempted_at,
            "status": "lost_zero",
            "expected": ["only_metric"],
            "written": [],
            "missing": ["only_metric"],
        }
