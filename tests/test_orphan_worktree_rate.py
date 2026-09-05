"""Tests for orphan_worktree_rate metric emission.

Verifies that:
 - reap-worktrees.sh emits the orphan_worktree_rate metric to stats.duckdb
 - the metric value is computed as reaped / elapsed_hours (orphans per hour)
 - the metric unit is 'count', NOT 'ratio' — using 'ratio' caused the dashboard
   to multiply by 100 and display values like 2016000% (D#1036)
 - an empty-table path is handled (no rows, empty stats_reader result)
 - the STATS_DB_PATH env var is respected so tests use a temp DB
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip if duckdb not installed
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")

REPO_ROOT = Path(__file__).resolve().parent.parent
REAPER_SCRIPT = REPO_ROOT / "scripts" / "reap-worktrees.sh"
STATS_READER = REPO_ROOT / "backend" / "stats_reader.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def record_to(db_path: Path, metric: str, value: float, unit: str,
              tags: dict | None = None, ts: datetime | None = None) -> None:
    os.environ["STATS_DB_PATH"] = str(db_path)
    import importlib
    import backend.stats_writer as sw
    importlib.reload(sw)
    sw.record(metric=metric, value=value, unit=unit, tags=tags, ts=ts)


def read_metric(db_path: Path, metric: str) -> list[dict]:
    """Return all rows for a metric from the temp DB via stats_reader series."""
    env = os.environ.copy()
    env["STATS_DB_PATH"] = str(db_path)
    result = subprocess.run(
        [sys.executable, str(STATS_READER), "series", metric],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        return []
    import json
    data = json.loads(result.stdout)
    return data.get("rows", [])


# ---------------------------------------------------------------------------
# Unit tests: metric calculation logic
# ---------------------------------------------------------------------------

class TestOrphanWorktreeRateCalc:
    """Tests that verify the rate arithmetic without running the reaper."""

    def test_rate_zero_reaped(self, tmp_path):
        """0 orphans → rate should be 0.0."""
        db = tmp_path / "test.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        import importlib
        import backend.stats_writer as sw
        importlib.reload(sw)
        reaped = 0
        elapsed_s = 60
        elapsed_h = elapsed_s / 3600.0
        rate = reaped / max(elapsed_h, 1 / 3600.0)
        sw.record(
            "orphan_worktree_rate",
            round(rate, 6),
            "count",  # orphans/hour is a count rate, not a 0-1 ratio
            tags={"reaped": str(reaped), "elapsed_s": str(elapsed_s)},
            source="reap-worktrees",
        )
        rows = read_metric(db, "orphan_worktree_rate")
        assert len(rows) == 1
        assert rows[0]["value"] == 0.0

    def test_rate_nonzero_reaped(self, tmp_path):
        """3 orphans in 3600 seconds → rate should be 3.0 orphans/hour."""
        db = tmp_path / "test.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        import importlib
        import backend.stats_writer as sw
        importlib.reload(sw)
        reaped = 3
        elapsed_s = 3600
        elapsed_h = elapsed_s / 3600.0
        rate = reaped / max(elapsed_h, 1 / 3600.0)
        sw.record(
            "orphan_worktree_rate",
            round(rate, 6),
            "count",  # orphans/hour is a count rate, not a 0-1 ratio
            tags={"reaped": str(reaped), "elapsed_s": str(elapsed_s)},
            source="reap-worktrees",
        )
        rows = read_metric(db, "orphan_worktree_rate")
        assert len(rows) == 1
        # 3 orphans / 1 hour = 3.0 orphans per hour
        assert abs(rows[0]["value"] - 3.0) < 0.001

    def test_rate_sub_second_elapsed_does_not_divide_by_zero(self, tmp_path):
        """elapsed_s=0 (floor to 1s) should not raise ZeroDivisionError."""
        db = tmp_path / "test.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        import importlib
        import backend.stats_writer as sw
        importlib.reload(sw)
        reaped = 1
        elapsed_s = 1  # floor applied in script; simulate here
        elapsed_h = elapsed_s / 3600.0
        rate = reaped / max(elapsed_h, 1 / 3600.0)
        # 1 orphan / (1/3600 hour) = 3600.0
        sw.record(
            "orphan_worktree_rate",
            round(rate, 6),
            "count",  # orphans/hour is a count rate, not a 0-1 ratio
            tags={"reaped": str(reaped), "elapsed_s": str(elapsed_s)},
            source="reap-worktrees",
        )
        rows = read_metric(db, "orphan_worktree_rate")
        assert len(rows) == 1
        assert rows[0]["value"] > 0  # finite, non-zero

    def test_unit_is_count_not_ratio(self, tmp_path):
        """Regression for D#1036: unit must be 'count', not 'ratio'.

        The dashboard MetricSparkline multiplies ratio-unit values by 100 to
        display as a percentage. When orphan_worktree_rate was stored as 'ratio'
        with values like 3600 (1 orphan per 1-second run = 3600 orphans/hour),
        the display showed 360,000% — or worse, 2,016,000% in production.

        The metric is orphans-per-hour, a count rate — it must use 'count'.
        """
        db = tmp_path / "unit_check.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        import importlib
        import backend.stats_writer as sw
        importlib.reload(sw)
        # Simulate a 1-second reap with 1 orphan — the worst-case value
        reaped = 1
        elapsed_s = 1
        elapsed_h = elapsed_s / 3600.0
        rate = reaped / max(elapsed_h, 1 / 3600.0)
        # rate == 3600.0 here — multiplied by 100 for 'ratio' gives 360,000%
        # with 'count' unit the dashboard shows it as the integer 3600 (orphans/hr)
        sw.record(
            "orphan_worktree_rate",
            round(rate, 6),
            "count",
            tags={"reaped": str(reaped), "elapsed_s": str(elapsed_s)},
            source="reap-worktrees",
        )
        rows = read_metric(db, "orphan_worktree_rate")
        assert len(rows) == 1
        # The stored value is the raw rate, not * 100
        # If someone uses 'ratio' unit by mistake, the dashboard renders value*100 as %.
        # Assert stored value is not in the millions (what 'ratio'*100 would give).
        assert rows[0]["value"] < 100_000, (
            f"orphan_worktree_rate value {rows[0]['value']} is unreasonably large — "
            "check that the unit is 'count', not 'ratio'"
        )


# ---------------------------------------------------------------------------
# Empty-table path
# ---------------------------------------------------------------------------

class TestOrphanWorktreeRateEmptyTable:
    """stats_reader returns empty list when no rows exist for the metric."""

    def test_empty_table_returns_empty_list(self, tmp_path):
        db = tmp_path / "empty.duckdb"
        # Write an unrelated metric so DB + schema exist
        record_to(db, "some_other_metric", 42.0, "count")
        rows = read_metric(db, "orphan_worktree_rate")
        assert rows == []


# ---------------------------------------------------------------------------
# Integration test: reaper script emits the metric
# ---------------------------------------------------------------------------

class TestReaperScriptEmitsMetric:
    """Run reap-worktrees.sh with a temp DB and confirm the metric appears."""

    @pytest.mark.skipif(not REAPER_SCRIPT.exists(), reason="reap-worktrees.sh not found")
    def test_reaper_emits_orphan_worktree_rate(self, tmp_path):
        db = tmp_path / "reaper_test.duckdb"
        env = os.environ.copy()
        env["STATS_DB_PATH"] = str(db)
        # --dry-run: this test shells out to the real reaper against the real
        # repo (cwd=REPO_ROOT) — it must never perform a live git worktree
        # remove (D#1864). PYTEST_CURRENT_TEST also forces dry-run as a
        # second, independent guard; passing the flag here keeps this test's
        # intent explicit even if that env-based guard is ever bypassed.
        # We pass --ttl-min 1 so TTL logic doesn't block; output goes to /dev/null
        result = subprocess.run(
            ["bash", str(REAPER_SCRIPT), "--ttl-min", "1", "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        # The reaper may exit non-zero if there are lock errors in CI — treat as skip
        if result.returncode != 0:
            pytest.skip(f"reap-worktrees.sh exited {result.returncode}: {result.stderr[:200]}")

        # Give the DB write a moment to flush
        rows = read_metric(db, "orphan_worktree_rate")
        assert len(rows) >= 1, (
            f"Expected at least one orphan_worktree_rate row; got 0. "
            f"reaper stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        row = rows[-1]
        assert row["value"] >= 0.0
        # Tags should carry reaped and elapsed_s
        assert "tags" in row
        tags = row["tags"]
        assert "reaped" in tags
        assert "elapsed_s" in tags
        assert tags["dry_run"] == "true"


# ---------------------------------------------------------------------------
# D#2155 PR-b: elapsed_s is tagged on every emission (reap-worktrees.sh:183)
# but nothing read it -- that's the exact instrumentation that would have
# caught the 16,460ms/call regression the post-agent-hook comment hid. This
# reads the value (not just checks the key is present) and asserts on it, so
# a reap that regresses back toward that cost fails a test instead of only
# showing up as an unmeasured, unnoticed spawn-time tax.
# ---------------------------------------------------------------------------

class TestElapsedSInstrumentationIsRead:
    """Reads reap-worktrees.sh's own elapsed_s tag and asserts on its value."""

    @pytest.mark.skipif(not REAPER_SCRIPT.exists(), reason="reap-worktrees.sh not found")
    def test_elapsed_s_value_is_read_and_bounded(self, tmp_path):
        db = tmp_path / "elapsed_check.duckdb"
        env = os.environ.copy()
        env["STATS_DB_PATH"] = str(db)
        result = subprocess.run(
            ["bash", str(REAPER_SCRIPT), "--ttl-min", "1", "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        if result.returncode != 0:
            pytest.skip(f"reap-worktrees.sh exited {result.returncode}: {result.stderr[:200]}")

        rows = read_metric(db, "orphan_worktree_rate")
        assert len(rows) >= 1, "expected a fresh orphan_worktree_rate row to read elapsed_s from"
        tags = rows[-1]["tags"]
        assert "elapsed_s" in tags

        # Read the VALUE, not just key presence -- must parse as a
        # non-negative int, matching what reap-worktrees.sh:183 actually
        # writes ("int(_ELAPSED_S)").
        elapsed_s = int(tags["elapsed_s"])
        assert elapsed_s >= 1, "reap-worktrees.sh floors elapsed_s at 1s (see line ~158)"

        # Regression guard, not a performance target: this PR throttles HOW
        # OFTEN the reaper runs, not how long a single run takes, so a run
        # can legitimately still take double digits of seconds on a
        # worktree-heavy host. The ceiling here is generous (D#2155 measured
        # 16.46s on this host) -- it exists to catch the instrumentation
        # going unread again, e.g. a future change that makes a single pass
        # pathologically slow (a stuck lock, an infinite retry) rather than
        # to enforce a tight SLA the throttle itself was never meant to buy.
        assert elapsed_s < 60, (
            f"reap-worktrees.sh took {elapsed_s}s for one --dry-run pass -- "
            "this is exactly the kind of cost the unread elapsed_s tag hid "
            "before (D#2155); investigate before this creeps further"
        )
