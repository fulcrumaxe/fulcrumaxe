"""Unit tests for scan_to_spawn_ratio computation.

Tests:
  - compute_ratio helper: empty file, all-idle, all-active, mixed, 24h window cutoff
  - stats_reader.scan_to_spawn: empty DB, populated DB, window_iterations cap
  - stats_writer.record: round-trip via DuckDB
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# Make sure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_metrics_file(tmp_path: Path, rows: list[dict]) -> Path:
    """Write synthetic loop-metrics.jsonl rows and return the path."""
    f = tmp_path / "loop-metrics.jsonl"
    with f.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return f


def _iso(hours_ago: float = 0) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_ratio_from_file(metrics_path: Path, window_hours: int = 24):
    """Pure Python helper that mirrors the shell script logic."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    total = 0
    scan_no_spawn = 0
    with metrics_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts_str = row.get("timestamp") or row.get("ts")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < cutoff:
                continue
            total += 1
            spawned = row.get("agents_spawned", 0) or 0
            if spawned == 0:
                scan_no_spawn += 1
    if total == 0:
        return None
    return round(scan_no_spawn / total, 4)


# ---------------------------------------------------------------------------
# compute_ratio_from_file tests
# ---------------------------------------------------------------------------

class TestComputeRatio:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "loop-metrics.jsonl"
        f.write_text("")
        result = compute_ratio_from_file(f)
        assert result is None

    def test_all_idle(self, tmp_path):
        rows = [
            {"timestamp": _iso(1), "agents_spawned": 0},
            {"timestamp": _iso(2), "agents_spawned": 0},
            {"timestamp": _iso(3), "agents_spawned": 0},
        ]
        f = _make_metrics_file(tmp_path, rows)
        result = compute_ratio_from_file(f)
        assert result == 1.0

    def test_all_active(self, tmp_path):
        rows = [
            {"timestamp": _iso(1), "agents_spawned": 3},
            {"timestamp": _iso(2), "agents_spawned": 1},
        ]
        f = _make_metrics_file(tmp_path, rows)
        result = compute_ratio_from_file(f)
        assert result == 0.0

    def test_mixed(self, tmp_path):
        rows = [
            {"timestamp": _iso(1), "agents_spawned": 0},
            {"timestamp": _iso(2), "agents_spawned": 2},
            {"timestamp": _iso(3), "agents_spawned": 0},
            {"timestamp": _iso(4), "agents_spawned": 1},
        ]
        f = _make_metrics_file(tmp_path, rows)
        result = compute_ratio_from_file(f)
        # 2 out of 4 had no spawns
        assert result == 0.5

    def test_window_cutoff_excludes_old_rows(self, tmp_path):
        rows = [
            # within 24h: 1 idle
            {"timestamp": _iso(1), "agents_spawned": 0},
            # older than 24h: 3 active — should be excluded
            {"timestamp": _iso(25), "agents_spawned": 5},
            {"timestamp": _iso(30), "agents_spawned": 2},
            {"timestamp": _iso(48), "agents_spawned": 1},
        ]
        f = _make_metrics_file(tmp_path, rows)
        result = compute_ratio_from_file(f)
        # Only 1 row in window, and it's idle → ratio = 1.0
        assert result == 1.0

    def test_skips_rows_without_timestamp(self, tmp_path):
        rows = [
            {"agents_spawned": 0},          # no timestamp → skipped
            {"timestamp": _iso(1), "agents_spawned": 0},
        ]
        f = _make_metrics_file(tmp_path, rows)
        result = compute_ratio_from_file(f)
        assert result == 1.0

    def test_skips_malformed_json(self, tmp_path):
        f = tmp_path / "loop-metrics.jsonl"
        f.write_text(
            'not valid json\n'
            + json.dumps({"timestamp": _iso(1), "agents_spawned": 0}) + "\n"
        )
        result = compute_ratio_from_file(f)
        assert result == 1.0

    def test_only_old_rows_returns_none(self, tmp_path):
        rows = [
            {"timestamp": _iso(25), "agents_spawned": 0},
            {"timestamp": _iso(30), "agents_spawned": 0},
        ]
        f = _make_metrics_file(tmp_path, rows)
        result = compute_ratio_from_file(f)
        assert result is None


# ---------------------------------------------------------------------------
# stats_reader.scan_to_spawn tests
# ---------------------------------------------------------------------------

class TestScanToSpawnReader:
    def test_empty_db(self, tmp_path):
        """scan_to_spawn returns empty result when DB has no rows."""
        import importlib
        import backend.stats_reader as sr

        db = tmp_path / "stats.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        try:
            # DB does not exist yet
            result = sr.scan_to_spawn(window_iterations=10)
            assert result == {"points": [], "mean": None, "n": 0}
        finally:
            os.environ.pop("STATS_DB_PATH", None)

    def test_populated_db(self, tmp_path):
        """scan_to_spawn returns correct rolling mean from DB."""
        import backend.stats_writer as sw
        import backend.stats_reader as sr

        db = tmp_path / "stats.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        try:
            # Write 3 rows
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            sw.record("scan_to_spawn_ratio", 0.5, "ratio",
                      tags={"window_hours": "24"}, source="test",
                      ts=now - timedelta(minutes=30))
            sw.record("scan_to_spawn_ratio", 0.0, "ratio",
                      tags={"window_hours": "24"}, source="test",
                      ts=now - timedelta(minutes=20))
            sw.record("scan_to_spawn_ratio", 1.0, "ratio",
                      tags={"window_hours": "24"}, source="test",
                      ts=now - timedelta(minutes=10))

            result = sr.scan_to_spawn(window_iterations=10)
            assert result["n"] == 3
            assert len(result["points"]) == 3
            assert result["mean"] == pytest.approx(0.5, abs=1e-4)
            # Points are in chronological order
            values = [p["value"] for p in result["points"]]
            assert values == [0.5, 0.0, 1.0]
        finally:
            os.environ.pop("STATS_DB_PATH", None)

    def test_window_iterations_cap(self, tmp_path):
        """scan_to_spawn respects window_iterations limit."""
        import backend.stats_writer as sw
        import backend.stats_reader as sr

        db = tmp_path / "stats.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            # Write 5 rows
            for i in range(5):
                sw.record("scan_to_spawn_ratio", float(i) / 4, "ratio",
                          tags={"window_hours": "24"}, source="test",
                          ts=now - timedelta(minutes=50 - i * 10))

            result = sr.scan_to_spawn(window_iterations=3)
            assert result["n"] == 3
        finally:
            os.environ.pop("STATS_DB_PATH", None)


# ---------------------------------------------------------------------------
# stats_writer round-trip test
# ---------------------------------------------------------------------------

class TestStatsWriterRoundTrip:
    def test_record_and_read_back(self, tmp_path):
        """record() writes a row that stats_reader.series() can retrieve."""
        import backend.stats_writer as sw
        import backend.stats_reader as sr

        db = tmp_path / "stats.duckdb"
        os.environ["STATS_DB_PATH"] = str(db)
        try:
            sw.record("scan_to_spawn_ratio", 0.75, "ratio",
                      tags={"window_hours": "24"}, source="unit-test")
            points = sr.series("scan_to_spawn_ratio", since_hours=1)
            assert len(points) == 1
            assert points[0]["value"] == pytest.approx(0.75)
        finally:
            os.environ.pop("STATS_DB_PATH", None)
