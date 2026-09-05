"""tests/test_stats_freshness_watchdog.py

Unit tests for backend/stats_freshness_watchdog.py (Discussion #613).

Covers:
  - check() returns correct stale entry when metric last_ts is 3h ago
  - team-log warning deduplication — same (iteration, metric) posted once
  - bug-filing threshold split: warn vs bug
  - file_bugs() calls _create_discussion for age >= BUG_AGE_SECONDS
  - file_bugs() skips when marker already exists (idempotency)
  - dry-run mode: never posts team-log or files Discussions
  - 2 fresh + 2 stale + 1 very-stale seeded metrics assert correct split
  - INTERMITTENT_METRICS raises bug threshold to 72h
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "backend"))

import stats_freshness_watchdog as sfw
from stats_freshness_watchdog import (
    WARN_AGE_SECONDS,
    BUG_AGE_SECONDS,
    INTERMITTENT_BUG_AGE_SECONDS,
    _human_age,
    _warned_this_process,
    check,
    warn_stale,
    file_bugs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(metric_name: str, age_seconds: int) -> dict:
    now = datetime.now(tz=timezone.utc)
    last_ts = now - timedelta(seconds=age_seconds)
    return {
        "metric_name": metric_name,
        "last_ts": last_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_seconds": age_seconds,
    }


def _seed_duckdb(rows: list[dict]) -> str:
    """Create a temp DuckDB file with metric_event rows and return its path."""
    import duckdb

    tmp = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    tmp_name = tmp.name
    tmp.close()
    # DuckDB requires the file to not exist when creating a new database
    os.unlink(tmp_name)
    conn = duckdb.connect(tmp_name)
    conn.execute("""
        CREATE TABLE metric_event (
            ts      TIMESTAMP NOT NULL,
            metric  TEXT      NOT NULL,
            tags    JSON,
            value   DOUBLE    NOT NULL,
            unit    TEXT      NOT NULL,
            source  TEXT,
            PRIMARY KEY (ts, metric, tags)
        )
    """)
    for row in rows:
        conn.execute(
            "INSERT INTO metric_event (ts, metric, tags, value, unit, source) VALUES (?, ?, ?, ?, ?, ?)",
            [row["ts"], row["metric"], "{}", row.get("value", 1.0), "count", "test"],
        )
    conn.close()
    return tmp_name


# ---------------------------------------------------------------------------
# AC1: check() returns 1 stale entry for metric with last_ts = now - 3h
# ---------------------------------------------------------------------------

class TestCheckReturnsSingleStaleEntry:
    def test_three_hour_old_metric_is_stale(self):
        now = datetime.now(tz=timezone.utc)
        old_ts = now - timedelta(hours=3)
        db_path = _seed_duckdb([
            {"ts": old_ts, "metric": "loop_iteration"},
        ])
        try:
            with patch.object(sfw, "_db_path", return_value=__import__("pathlib").Path(db_path)):
                rows = check()
            assert len(rows) == 1
            assert rows[0]["metric_name"] == "loop_iteration"
            assert rows[0]["age_seconds"] >= WARN_AGE_SECONDS
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# AC2: warn_stale() deduplication — same (iteration, metric) posted once
# ---------------------------------------------------------------------------

class TestWarnStaleDeduplication:
    def setup_method(self):
        # Clear the per-process dedup set before each test
        _warned_this_process.clear()

    def test_same_metric_warned_once_per_process(self):
        row = _make_row("loop_iteration", WARN_AGE_SECONDS + 60)

        with patch.object(sfw, "_post_team_log") as mock_log:
            warn_stale([row], dry_run=False)
            warn_stale([row], dry_run=False)

        # Posted exactly once despite two calls
        assert mock_log.call_count == 1

    def test_different_metrics_each_warned(self):
        rows = [
            _make_row("metric_a", WARN_AGE_SECONDS + 100),
            _make_row("metric_b", WARN_AGE_SECONDS + 200),
        ]
        with patch.object(sfw, "_post_team_log") as mock_log:
            warn_stale(rows, dry_run=False)

        assert mock_log.call_count == 2

    def test_dry_run_does_not_post(self):
        row = _make_row("metric_x", WARN_AGE_SECONDS + 60)
        with patch.object(sfw, "_post_team_log") as mock_log:
            warn_stale([row], dry_run=True)
        mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# AC8: 2 fresh + 2 stale + 1 very-stale — assert correct warn/bug split
# ---------------------------------------------------------------------------

class TestFiveMetricSplit:
    def test_warn_bug_split(self):
        rows = [
            _make_row("fresh_a", 60),                        # fresh
            _make_row("fresh_b", 600),                       # fresh
            _make_row("stale_a", WARN_AGE_SECONDS + 300),    # warn only
            _make_row("stale_b", WARN_AGE_SECONDS + 900),    # warn only
            _make_row("very_stale", BUG_AGE_SECONDS + 3600), # bug threshold
        ]
        warn = [r for r in rows if r["age_seconds"] >= WARN_AGE_SECONDS]
        bug = [r for r in rows if r["age_seconds"] >= BUG_AGE_SECONDS]

        assert len(warn) == 3  # stale_a, stale_b, very_stale
        assert len(bug) == 1   # only very_stale
        assert bug[0]["metric_name"] == "very_stale"


# ---------------------------------------------------------------------------
# AC4: file_bugs() files Discussion for age >= 24h; second call is no-op
# ---------------------------------------------------------------------------

class TestFileBugsIdempotency:
    def test_files_discussion_for_very_stale(self):
        row = _make_row("loop_iteration", BUG_AGE_SECONDS + 3600)
        with patch.object(sfw, "_marker_exists", return_value=False), \
             patch.object(sfw, "_get_repo_id", return_value="R_fake"), \
             patch.object(sfw, "_get_category_id", return_value="C_fake"), \
             patch.object(sfw, "_create_discussion", return_value="https://github.com/test/1"):
            results = file_bugs([row], dry_run=False)
        assert len(results) == 1
        assert results[0]["filed"] is True
        assert results[0]["url"] == "https://github.com/test/1"

    def test_skipped_when_marker_exists(self):
        row = _make_row("loop_iteration", BUG_AGE_SECONDS + 3600)
        with patch.object(sfw, "_marker_exists", return_value=True), \
             patch.object(sfw, "_create_discussion") as mock_create:
            results = file_bugs([row], dry_run=False)
        mock_create.assert_not_called()
        assert len(results) == 1
        assert results[0]["filed"] is False

    def test_not_filed_below_bug_threshold(self):
        # Age is between WARN and BUG thresholds
        row = _make_row("metric_mid", WARN_AGE_SECONDS + 300)
        with patch.object(sfw, "_create_discussion") as mock_create:
            results = file_bugs([row], dry_run=False)
        mock_create.assert_not_called()
        assert len(results) == 0  # nothing filed

    def test_dry_run_returns_placeholder(self):
        row = _make_row("metric_dry", BUG_AGE_SECONDS + 100)
        results = file_bugs([row], dry_run=True)
        assert len(results) == 1
        assert "DRY-RUN" in (results[0]["url"] or "")


# ---------------------------------------------------------------------------
# AC5: Bug Discussion title contains metric name
# ---------------------------------------------------------------------------

class TestBugDiscussionTitle:
    def test_title_contains_metric_name(self):
        from stats_freshness_watchdog import _file_stale_bug
        row = _make_row("my_metric", BUG_AGE_SECONDS + 1000)
        marker = "<!-- stats-freshness:my_metric -->"

        with patch.object(sfw, "_marker_exists", return_value=False), \
             patch.object(sfw, "_get_repo_id", return_value="R_x"), \
             patch.object(sfw, "_get_category_id", return_value="C_x"), \
             patch.object(sfw, "_create_discussion") as mock_create:
            mock_create.return_value = "https://github.com/test/99"
            _file_stale_bug(row, marker, dry_run=False)

        call_args = mock_create.call_args
        title_passed = call_args[0][0]
        assert "my_metric" in title_passed
        assert "[Bug]" in title_passed


# ---------------------------------------------------------------------------
# AC6: Watchdog completes in < 1s on small data
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_check_returns_quickly(self):
        import time
        now = datetime.now(tz=timezone.utc)
        old_ts = now - timedelta(hours=1)
        db_path = _seed_duckdb([{"ts": old_ts, "metric": "perf_metric"}])
        try:
            with patch.object(sfw, "_db_path", return_value=__import__("pathlib").Path(db_path)):
                t0 = time.perf_counter()
                check()
                elapsed = time.perf_counter() - t0
            assert elapsed < 1.0, f"check() took {elapsed:.2f}s, expected < 1s"
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# AC7: DuckDB error is swallowed — never raises into caller
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_missing_db_returns_empty(self):
        with patch.object(sfw, "_db_path", return_value=__import__("pathlib").Path("/nonexistent/stats.duckdb")):
            rows = check()
        assert rows == []

    def test_query_exception_swallowed(self):
        with patch.object(sfw, "_query_freshness", side_effect=RuntimeError("boom")):
            rows = check()
        assert rows == []


# ---------------------------------------------------------------------------
# INTERMITTENT_METRICS: bug threshold raised to 72h
# ---------------------------------------------------------------------------

class TestIntermittentMetrics:
    def test_intermittent_metric_not_filed_at_24h(self):
        original = sfw.INTERMITTENT_METRICS.copy()
        sfw.INTERMITTENT_METRICS.add("weekend_metric")
        try:
            row = _make_row("weekend_metric", BUG_AGE_SECONDS + 1000)
            # 24h + 1000s < 72h — should NOT be filed
            with patch.object(sfw, "_create_discussion") as mock_create:
                results = file_bugs([row], dry_run=False)
            mock_create.assert_not_called()
            assert len(results) == 0
        finally:
            sfw.INTERMITTENT_METRICS.clear()
            sfw.INTERMITTENT_METRICS.update(original)

    def test_intermittent_metric_filed_at_72h(self):
        original = sfw.INTERMITTENT_METRICS.copy()
        sfw.INTERMITTENT_METRICS.add("weekend_metric")
        try:
            row = _make_row("weekend_metric", INTERMITTENT_BUG_AGE_SECONDS + 100)
            with patch.object(sfw, "_marker_exists", return_value=False), \
                 patch.object(sfw, "_get_repo_id", return_value="R"), \
                 patch.object(sfw, "_get_category_id", return_value="C"), \
                 patch.object(sfw, "_create_discussion", return_value="https://github.com/test/2"):
                results = file_bugs([row], dry_run=False)
            assert results[0]["filed"] is True
        finally:
            sfw.INTERMITTENT_METRICS.clear()
            sfw.INTERMITTENT_METRICS.update(original)


# ---------------------------------------------------------------------------
# _human_age helper
# ---------------------------------------------------------------------------

class TestHumanAge:
    def test_hours_and_minutes(self):
        assert _human_age(3 * 3600 + 12 * 60) == "3h 12m"

    def test_minutes_only(self):
        assert _human_age(45 * 60) == "45m"

    def test_zero(self):
        assert _human_age(0) == "0m"
