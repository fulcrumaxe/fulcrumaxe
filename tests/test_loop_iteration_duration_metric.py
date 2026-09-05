"""Test that loop_iteration_duration_seconds round-trips through stats_writer/reader.

Covers:
  - append-loop-metrics.sh emits the metric to stats.duckdb
  - stats_reader summary returns the stored value
  - empty-table case returns empty list (not error)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")

REPO_ROOT = Path(__file__).resolve().parent.parent
METRIC_NAME = "loop_iteration_duration_seconds"


def _record(db_path: Path, value: float, source: str = "loop") -> None:
    """Write one loop_iteration_duration_seconds row via stats_writer."""
    env = os.environ.copy()
    env["STATS_DB_PATH"] = str(db_path)
    result = subprocess.run(
        [
            sys.executable, "-c",
            f"from backend.stats_writer import record; "
            f"record('{METRIC_NAME}', {value}, 'seconds', source='{source}')"
        ],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"stats_writer failed: {result.stderr}"


def _read_summary(db_path: Path) -> list:
    """Run stats_reader summary and return parsed JSON."""
    env = os.environ.copy()
    env["STATS_DB_PATH"] = str(db_path)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "stats_reader.py"), "summary"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"stats_reader failed: {result.stderr}"
    return json.loads(result.stdout)


class TestLoopIterationDurationMetric:

    def test_record_and_read_back(self, tmp_path):
        """Write one row, verify it shows up in summary with correct value and unit."""
        db = tmp_path / "stats.duckdb"
        _record(db, 330.0)

        rows = _read_summary(db)
        entry = next((r for r in rows if r["metric"] == METRIC_NAME), None)
        assert entry is not None, f"{METRIC_NAME} not found in summary"
        assert entry["value"] == 330.0
        assert entry["unit"] == "seconds"

    def test_summary_returns_latest_value(self, tmp_path):
        """Two writes — summary returns the more recent value."""
        import importlib
        import backend.stats_writer as sw_mod

        os.environ["STATS_DB_PATH"] = str(tmp_path / "stats.duckdb")
        importlib.reload(sw_mod)

        t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        sw_mod.record(METRIC_NAME, 120.0, "seconds", source="loop", ts=t1)
        sw_mod.record(METRIC_NAME, 450.0, "seconds", source="loop", ts=t2)

        rows = _read_summary(tmp_path / "stats.duckdb")
        entry = next((r for r in rows if r["metric"] == METRIC_NAME), None)
        assert entry is not None
        assert entry["value"] == 450.0  # latest

    def test_empty_table_returns_empty_list(self, tmp_path):
        """Database exists with schema but zero rows — summary returns []."""
        db = tmp_path / "empty.duckdb"
        conn = duckdb.connect(str(db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metric_event (
                ts      TIMESTAMP NOT NULL,
                metric  TEXT      NOT NULL,
                tags    JSON,
                value   DOUBLE    NOT NULL,
                unit    TEXT      NOT NULL,
                source  TEXT,
                PRIMARY KEY (ts, metric, tags)
            )
        """)
        conn.close()

        env = os.environ.copy()
        env["STATS_DB_PATH"] = str(db)
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "backend" / "stats_reader.py"), "summary"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data == []

    def test_append_loop_metrics_emits_to_duckdb(self, tmp_path):
        """append-loop-metrics.sh with a known duration writes the metric to stats.duckdb."""
        db = tmp_path / "stats.duckdb"
        metrics_file = tmp_path / "loop-metrics.jsonl"
        env = os.environ.copy()
        env["STATS_DB_PATH"] = str(db)
        env["METRICS_FILE"] = str(metrics_file)

        result = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts" / "append-loop-metrics.sh"),
                "--iter-start-iso", "2026-01-01T10:00:00Z",
                "--iter-end-iso",   "2026-01-01T10:05:30Z",
                "--duration-seconds", "330",
                "--agents-spawned", "2",
                "--prs-merged", "1",
                "--discussions-scanned", "3",
                "--prs-scanned", "2",
            ],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"Script failed: {result.stderr}"

        # Verify the row landed in stats.duckdb
        conn = duckdb.connect(str(db))
        rows = conn.execute(
            "SELECT value, unit, source FROM metric_event WHERE metric = ?",
            [METRIC_NAME]
        ).fetchall()
        conn.close()

        assert len(rows) == 1, f"Expected 1 row, got: {rows}"
        assert rows[0][0] == 330.0
        assert rows[0][1] == "seconds"
        assert rows[0][2] == "loop"
