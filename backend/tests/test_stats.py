"""Tests for backend/stats_writer.py and backend/stats_reader.py.

Covers:
  - record() writes rows to DuckDB
  - record() deduplicates on (ts, metric, tags)
  - summary returns last value per metric
  - series returns time-ordered rows filtered by --since
  - distribution returns P50/P90/P99
  - distribution with --tag filter
  - stats_reader.py summary CLI subcommand (subprocess)
  - stats_reader.py series CLI subcommand (subprocess)
  - stats_reader.py distribution CLI subcommand (subprocess)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Lazy import so test collection doesn't fail if duckdb is absent
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_writer(db_path: Path):
    """Return a record() callable scoped to a temp DB."""
    import importlib
    import backend.stats_writer as sw_mod

    # Monkeypatch _db_path for this call by using the module-level env var
    os.environ["STATS_DB_PATH"] = str(db_path)
    import importlib
    importlib.reload(sw_mod)
    return sw_mod.record


def record_to(db_path: Path, metric, value, unit, tags=None, source=None, ts=None):
    os.environ["STATS_DB_PATH"] = str(db_path)
    import importlib
    import backend.stats_writer as sw_mod
    importlib.reload(sw_mod)
    sw_mod.record(metric=metric, value=value, unit=unit, tags=tags, source=source, ts=ts)


def run_reader(db_path: Path, *args) -> dict | list:
    env = os.environ.copy()
    env["STATS_DB_PATH"] = str(db_path)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "stats_reader.py"), *args],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, f"stats_reader.py failed: {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# stats_writer tests
# ---------------------------------------------------------------------------

class TestStatsWriter:

    def test_record_creates_row(self, tmp_path):
        db = tmp_path / "test.duckdb"
        record_to(db, "latency", 1.5, "seconds", {"env": "test"})
        conn = duckdb.connect(str(db))
        rows = conn.execute("SELECT metric, value, unit FROM metric_event").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "latency"
        assert rows[0][1] == 1.5
        assert rows[0][2] == "seconds"

    def test_record_no_tags(self, tmp_path):
        db = tmp_path / "test.duckdb"
        record_to(db, "count_metric", 42, "count")
        conn = duckdb.connect(str(db))
        rows = conn.execute("SELECT value FROM metric_event WHERE metric='count_metric'").fetchall()
        conn.close()
        assert rows[0][0] == 42

    def test_record_deduplication(self, tmp_path):
        """Same (ts, metric, tags) twice — second is silently ignored."""
        db = tmp_path / "test.duckdb"
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        record_to(db, "dupe_metric", 1.0, "count", {"x": "a"}, ts=ts)
        record_to(db, "dupe_metric", 99.0, "count", {"x": "a"}, ts=ts)  # should be ignored
        conn = duckdb.connect(str(db))
        rows = conn.execute("SELECT value FROM metric_event WHERE metric='dupe_metric'").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == 1.0  # first value kept

    def test_record_multiple_metrics(self, tmp_path):
        db = tmp_path / "test.duckdb"
        record_to(db, "m1", 10.0, "count")
        record_to(db, "m2", 20.0, "usd")
        record_to(db, "m3", 0.5, "ratio")
        conn = duckdb.connect(str(db))
        rows = conn.execute("SELECT metric FROM metric_event ORDER BY metric").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["m1", "m2", "m3"]


# ---------------------------------------------------------------------------
# stats_reader: summary
# ---------------------------------------------------------------------------

class TestStatsReaderSummary:

    def test_summary_returns_last_value(self, tmp_path):
        db = tmp_path / "test.duckdb"
        t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        record_to(db, "my_metric", 5.0, "count", ts=t1)
        record_to(db, "my_metric", 7.0, "count", ts=t2)
        result = run_reader(db, "summary")
        assert isinstance(result, list)
        my = next((r for r in result if r["metric"] == "my_metric"), None)
        assert my is not None
        assert my["value"] == 7.0

    def test_summary_multiple_metrics(self, tmp_path):
        db = tmp_path / "test.duckdb"
        record_to(db, "alpha", 1.0, "count")
        record_to(db, "beta", 2.0, "usd")
        result = run_reader(db, "summary")
        names = {r["metric"] for r in result}
        assert "alpha" in names
        assert "beta" in names

    def test_summary_empty_db_returns_empty_list(self, tmp_path):
        """Database exists but has no rows — summary returns []."""
        db = tmp_path / "test.duckdb"
        # Create the schema without inserting rows
        conn = duckdb.connect(str(db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metric_event (
                ts TIMESTAMP, metric TEXT, tags JSON,
                value DOUBLE, unit TEXT, source TEXT,
                PRIMARY KEY (ts, metric, tags)
            )
        """)
        conn.close()
        result = run_reader(db, "summary")
        assert result == []


# ---------------------------------------------------------------------------
# stats_reader: series
# ---------------------------------------------------------------------------

class TestStatsReaderSeries:

    def test_series_returns_all_rows(self, tmp_path):
        db = tmp_path / "test.duckdb"
        for i in range(5):
            ts = datetime(2026, 1, 1, i, 0, 0, tzinfo=timezone.utc)
            record_to(db, "latency", float(i * 10), "ms", ts=ts)
        result = run_reader(db, "series", "latency")
        assert result["metric"] == "latency"
        assert len(result["rows"]) == 5

    def test_series_since_filter(self, tmp_path):
        db = tmp_path / "test.duckdb"
        old = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        # "recent" = 1 hour ago — guaranteed to be within 7d window
        new = datetime.now(timezone.utc) - timedelta(hours=1)
        record_to(db, "gauge", 1.0, "count", ts=old)
        record_to(db, "gauge", 2.0, "count", ts=new)
        result = run_reader(db, "series", "gauge", "--since", "7d")
        # Only the recent row should appear (old is from 2020)
        assert len(result["rows"]) == 1
        assert result["rows"][0]["value"] == 2.0

    def test_series_unknown_metric_returns_empty(self, tmp_path):
        db = tmp_path / "test.duckdb"
        record_to(db, "other_metric", 1.0, "count")
        result = run_reader(db, "series", "nonexistent_metric")
        assert result["rows"] == []


# ---------------------------------------------------------------------------
# stats_reader: distribution
# ---------------------------------------------------------------------------

class TestStatsReaderDistribution:

    def test_distribution_percentiles(self, tmp_path):
        db = tmp_path / "test.duckdb"
        # 100 values 1..100
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(1, 101):
            ts = base + timedelta(seconds=i)
            record_to(db, "duration", float(i), "seconds", ts=ts)
        result = run_reader(db, "distribution", "duration")
        assert result["n"] == 100
        assert result["p50"] is not None
        assert result["p90"] is not None
        assert result["p99"] is not None
        # P50 should be near 50, P90 near 90
        assert 45 <= result["p50"] <= 55
        assert 85 <= result["p90"] <= 95

    def test_distribution_with_tag_filter(self, tmp_path):
        db = tmp_path / "test.duckdb"
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            ts = base + timedelta(seconds=i)
            record_to(db, "ttm", float(i * 100), "seconds", tags={"tag": "Bug"}, ts=ts)
        for i in range(10):
            ts = base + timedelta(seconds=100 + i)
            record_to(db, "ttm", float(i * 1000), "seconds", tags={"tag": "Feature"}, ts=ts)
        result = run_reader(db, "distribution", "ttm", "--tag", "tag=Bug")
        assert result["n"] == 10
        assert result["tag_filter"] == "tag=Bug"
        # Bug values are 0..900 so P99 should be well below Feature values
        assert result["p99"] < 5000

    def test_distribution_no_data(self, tmp_path):
        db = tmp_path / "test.duckdb"
        record_to(db, "other", 1.0, "count")
        result = run_reader(db, "distribution", "missing_metric")
        assert result["n"] == 0


# ---------------------------------------------------------------------------
# _correct_unit — unit tests
# ---------------------------------------------------------------------------

class TestCorrectUnit:

    def test_orphan_worktree_rate_ratio_corrected_to_count(self):
        import importlib
        import backend.stats_reader as sr_mod
        importlib.reload(sr_mod)
        assert sr_mod._correct_unit("orphan_worktree_rate", "ratio") == "count"

    def test_orphan_worktree_rate_count_unchanged(self):
        import importlib
        import backend.stats_reader as sr_mod
        importlib.reload(sr_mod)
        assert sr_mod._correct_unit("orphan_worktree_rate", "count") == "count"

    def test_other_ratio_metrics_unchanged(self):
        import importlib
        import backend.stats_reader as sr_mod
        importlib.reload(sr_mod)
        assert sr_mod._correct_unit("scan_to_spawn_ratio", "ratio") == "ratio"
        assert sr_mod._correct_unit("acceptance_criteria_pass_rate", "ratio") == "ratio"

    def test_unknown_metric_unknown_unit_unchanged(self):
        import importlib
        import backend.stats_reader as sr_mod
        importlib.reload(sr_mod)
        assert sr_mod._correct_unit("some_new_metric", "usd") == "usd"


# ---------------------------------------------------------------------------
# summary() unit correction — integration tests
# ---------------------------------------------------------------------------

class TestSummaryUnitCorrection:

    def test_summary_corrects_orphan_ratio_to_count(self, tmp_path):
        """summary() must return unit='count' even when the DB row has unit='ratio'."""
        db = tmp_path / "test.duckdb"
        record_to(db, "orphan_worktree_rate", 21600.0, "ratio")
        import importlib
        import backend.stats_reader as sr_mod
        importlib.reload(sr_mod)
        rows = sr_mod.summary()
        orphan = next((r for r in rows if r["name"] == "orphan_worktree_rate"), None)
        assert orphan is not None, "orphan_worktree_rate not found in summary()"
        assert orphan["unit"] == "count", (
            f"Expected unit='count', got {orphan['unit']!r}. "
            "MetricSparkline would have displayed a percentage instead of a plain integer."
        )

    def test_summary_leaves_genuine_ratio_metrics_unchanged(self, tmp_path):
        """Metrics with a legitimate ratio unit must not be changed."""
        db = tmp_path / "test.duckdb"
        record_to(db, "scan_to_spawn_ratio", 0.75, "ratio")
        import importlib
        import backend.stats_reader as sr_mod
        importlib.reload(sr_mod)
        rows = sr_mod.summary()
        metric = next((r for r in rows if r["name"] == "scan_to_spawn_ratio"), None)
        assert metric is not None
        assert metric["unit"] == "ratio"
