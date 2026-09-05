"""
Unit tests for loop.timeline and loop.iteration_detail JSON-RPC methods in server.py.

Tests cover:
  - Happy path: both methods return expected data
  - Malformed JSONL line is skipped silently
  - Missing log file returns log: null
  - limit clamping (max 500)
  - Large log truncation with marker
  - Invalid params raise -32602
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Allow imports from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _make_metrics_file(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a loop-metrics.jsonl fixture and return its path."""
    at_dir = tmp_path / ".autonomous-team"
    at_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = at_dir / "loop-metrics.jsonl"
    with metrics_path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return metrics_path


def _make_log_file(tmp_path: Path, timestamp: str, content: str) -> Path:
    """Write a loop-runs log fixture and return its path.

    timestamp is ISO8601 e.g. "2026-04-11T01:41:20Z"
    """
    import re
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z?", timestamp)
    assert m, f"Bad timestamp: {timestamp}"
    filename = f"{m.group(1)}{m.group(2)}{m.group(3)}T{m.group(4)}{m.group(5)}{m.group(6)}Z.log"
    log_dir = tmp_path / ".autonomous-team" / "loop-runs" / "autonomous-forever"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / filename
    log_path.write_text(content)
    return log_path


_SAMPLE_ROWS = [
    {
        "timestamp": "2026-04-11T01:41:20Z",
        "duration_seconds": 61,
        "agents_spawned": 0,
        "prs_merged": 0,
        "discussions_scanned": 2,
        "prs_scanned": 1,
        "idle": False,
        "error": None,
    },
    {
        "timestamp": "2026-04-11T01:51:02Z",
        "duration_seconds": 44,
        "agents_spawned": 1,
        "prs_merged": 0,
        "discussions_scanned": 2,
        "prs_scanned": 0,
        "idle": False,
        "error": None,
    },
    {
        "timestamp": "2026-04-11T02:01:15Z",
        "duration_seconds": 30,
        "agents_spawned": 0,
        "prs_merged": 1,
        "discussions_scanned": 1,
        "prs_scanned": 2,
        "idle": True,
        "error": None,
    },
]


class TestLoopTimelineRpc(unittest.TestCase):
    """Tests for loop.timeline RPC method."""

    def _handler(self):
        from backend.server import _RPC_METHODS
        return _RPC_METHODS["loop.timeline"]

    def test_registered(self):
        from backend.server import _RPC_METHODS
        self.assertIn("loop.timeline", _RPC_METHODS)

    def test_happy_path(self, tmp_path=None):
        import tempfile
        if tmp_path is None:
            tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 100})

        self.assertEqual(len(result), 3)
        # Ordered oldest → newest
        self.assertEqual(result[0]["timestamp"], "2026-04-11T01:41:20Z")
        self.assertEqual(result[2]["timestamp"], "2026-04-11T02:01:15Z")

        # All 8 fields present
        for row in result:
            for field in ("timestamp", "duration_seconds", "agents_spawned", "prs_merged",
                          "discussions_scanned", "prs_scanned", "idle", "error"):
                self.assertIn(field, row, f"missing field {field!r}")

    def test_malformed_line_skipped(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        at_dir = tmp_path / ".autonomous-team"
        at_dir.mkdir(parents=True, exist_ok=True)
        metrics_path = at_dir / "loop-metrics.jsonl"
        with metrics_path.open("w") as fh:
            fh.write(json.dumps(_SAMPLE_ROWS[0]) + "\n")
            fh.write("NOT VALID JSON {{{\n")  # malformed
            fh.write(json.dumps(_SAMPLE_ROWS[1]) + "\n")

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 100})

        # Surrounding valid rows present; bad line skipped
        self.assertEqual(len(result), 2)
        timestamps = [r["timestamp"] for r in result]
        self.assertIn("2026-04-11T01:41:20Z", timestamps)
        self.assertIn("2026-04-11T01:51:02Z", timestamps)

    def test_limit_clamped_at_500(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        # Write 600 rows
        rows = [
            {
                "timestamp": f"2026-05-{str(i).zfill(2)}T00:00:00Z" if i <= 31 else f"2026-06-{str(i - 31).zfill(2)}T00:00:00Z",
                "duration_seconds": i,
                "agents_spawned": 0,
                "prs_merged": 0,
                "discussions_scanned": 0,
                "prs_scanned": 0,
                "idle": False,
                "error": None,
            }
            for i in range(1, 601)
        ]
        _make_metrics_file(tmp_path, rows)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 999})  # over max

        self.assertEqual(len(result), 500)

    def test_limit_returns_most_recent(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 2})

        self.assertEqual(len(result), 2)
        # Should be the last 2 rows (deque maxlen keeps tail)
        timestamps = [r["timestamp"] for r in result]
        self.assertIn("2026-04-11T01:51:02Z", timestamps)
        self.assertIn("2026-04-11T02:01:15Z", timestamps)

    def test_missing_file_returns_empty(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        # No metrics file created

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 100})

        self.assertEqual(result, [])

    def test_idle_flag_preserved(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 100})

        idle_flags = [r["idle"] for r in result]
        self.assertEqual(idle_flags, [False, False, True])

    def test_test_origin_rows_excluded_by_default(self):
        """Rows with origin=='test' are filtered out unless include_test=true."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        rows = [
            {**_SAMPLE_ROWS[0], "origin": "cron"},
            {**_SAMPLE_ROWS[1], "origin": "test"},   # should be filtered
            {**_SAMPLE_ROWS[2], "origin": "interactive"},
        ]
        _make_metrics_file(tmp_path, rows)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result_default = self._handler()({"limit": 100})
            result_include = self._handler()({"limit": 100, "include_test": True})

        # Default: test row excluded
        self.assertEqual(len(result_default), 2)
        timestamps = [r["timestamp"] for r in result_default]
        self.assertIn("2026-04-11T01:41:20Z", timestamps)
        self.assertIn("2026-04-11T02:01:15Z", timestamps)
        self.assertNotIn("2026-04-11T01:51:02Z", timestamps)

        # With include_test: all 3 rows returned
        self.assertEqual(len(result_include), 3)

    def test_missing_origin_treated_as_cron(self):
        """Rows missing the 'origin' field are treated as 'cron' (back-compat)."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        # _SAMPLE_ROWS have no 'origin' field
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 100})

        # All 3 rows should be returned (treated as cron)
        self.assertEqual(len(result), 3)

    def test_legacy_ts_field_back_compat(self):
        """Rows using the old 'ts' field name are read correctly."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        # Old rows use 'ts' instead of 'timestamp' and 'duration_s' instead of 'duration_seconds'
        rows = [
            {"ts": "2026-04-11T01:41:20Z", "duration_s": 61, "agents_spawned": 0,
             "prs_merged": 0, "discussions_scanned": 0, "prs_scanned": 0},
        ]
        _make_metrics_file(tmp_path, rows)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 100})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timestamp"], "2026-04-11T01:41:20Z")
        self.assertEqual(result[0]["duration_seconds"], 61)

    def test_epoch_duration_sanitised_to_zero(self):
        """Historic rows that stored a Unix epoch as duration_seconds are clamped to 0.

        A real 60-second iteration passes through unchanged.
        A value like 1_775_912_081 (April 2026 epoch ~5.6 billion seconds = 178 years)
        must be zeroed — it is clearly not a real loop duration.
        """
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        rows = [
            {
                "timestamp": "2026-04-11T12:54:41Z",
                "duration_seconds": 1_775_912_081,  # Unix epoch stored as duration — corrupt
                "agents_spawned": 0,
                "prs_merged": 0,
            },
            {
                "timestamp": "2026-05-15T14:47:42Z",
                "duration_s": 1_778_856_462,  # Same problem via duration_s field
                "agents_spawned": 0,
                "prs_merged": 0,
            },
            {
                "timestamp": "2026-05-18T20:00:00Z",
                "duration_s": 60,  # Legitimate 60-second iteration
                "agents_spawned": 1,
                "prs_merged": 0,
            },
        ]
        _make_metrics_file(tmp_path, rows)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"limit": 100})

        self.assertEqual(len(result), 3)
        # Corrupted epoch values must be zeroed
        self.assertEqual(result[0]["duration_seconds"], 0,
                         "duration_seconds=1_775_912_081 should be clamped to 0")
        self.assertEqual(result[1]["duration_seconds"], 0,
                         "duration_s=1_778_856_462 should be clamped to 0")
        # Legitimate 60s duration passes through unchanged
        self.assertEqual(result[2]["duration_seconds"], 60,
                         "duration_s=60 should be preserved as-is")


class TestLoopIterationDetailRpc(unittest.TestCase):
    """Tests for loop.iteration_detail RPC method."""

    def _handler(self):
        from backend.server import _RPC_METHODS
        return _RPC_METHODS["loop.iteration_detail"]

    def test_registered(self):
        from backend.server import _RPC_METHODS
        self.assertIn("loop.iteration_detail", _RPC_METHODS)

    def test_happy_path_with_log(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)
        ts = "2026-04-11T01:41:20Z"
        log_content = "This is the loop run log content.\nLine 2.\n"
        _make_log_file(tmp_path, ts, log_content)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertEqual(result["timestamp"], ts)
        self.assertEqual(result["metrics"]["duration_seconds"], 61)
        self.assertEqual(result["log"], log_content)
        self.assertIsNotNone(result["log_path"])

    def test_missing_log_file_returns_null(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)
        ts = "2026-04-11T01:51:02Z"
        # No log file created for this timestamp

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertEqual(result["timestamp"], ts)
        self.assertIsNone(result["log"])
        self.assertIsNone(result["log_path"])
        # Metrics row still returned
        self.assertEqual(result["metrics"]["agents_spawned"], 1)

    def test_large_log_truncated(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)
        ts = "2026-04-11T01:41:20Z"
        # Write > 64 KB log
        big_content = "X" * (65 * 1024)
        _make_log_file(tmp_path, ts, big_content)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertIsNotNone(result["log"])
        self.assertIn("[truncated:", result["log"])
        self.assertIn("bytes]", result["log"])
        # Content should be <= 64 KB + marker overhead
        self.assertLessEqual(len(result["log"]), 65 * 1024 + 200)

    def test_missing_timestamp_raises_32602(self):
        import backend.server as srv
        with self.assertRaises(Exception) as ctx:
            self._handler()({"timestamp": ""})
        exc = ctx.exception
        self.assertEqual(getattr(exc, "rpc_code", None), -32602)

    def test_bad_timestamp_format_raises_32602(self):
        import backend.server as srv
        with self.assertRaises(Exception) as ctx:
            self._handler()({"timestamp": "not-a-timestamp"})
        exc = ctx.exception
        self.assertEqual(getattr(exc, "rpc_code", None), -32602)

    def test_no_matching_metrics_row(self):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)
        ts = "2026-04-11T03:00:00Z"  # Not in sample rows

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertEqual(result["timestamp"], ts)
        self.assertEqual(result["metrics"], {})
        self.assertIsNone(result["log"])


class TestLoopIterationDetailRunId(unittest.TestCase):
    """Tests for Bug 3 — log file resolved via run_id, not timestamp."""

    def _handler(self):
        from backend.server import _RPC_METHODS
        return _RPC_METHODS["loop.iteration_detail"]

    def test_run_id_log_found(self):
        """Row has run_id; log file is named after run_id, not the timestamp."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        run_id = "20260510T053731Z"
        ts = "2026-05-10T05:55:22Z"
        row = {
            "timestamp": ts,
            "iteration": 0,
            "duration_seconds": 1071,
            "actions": 1,
            "exit_code": 0,
            "trigger": "dashboard",
            "run_id": run_id,
        }
        _make_metrics_file(tmp_path, [row])
        log_dir = tmp_path / ".autonomous-team" / "loop-runs" / "autonomous-forever"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{run_id}.log"
        log_file.write_text("log content for run_id test")

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertIsNotNone(result["log"])
        self.assertIn("log content for run_id test", result["log"])
        self.assertTrue(result["log_path"].endswith(f"{run_id}.log"))

    def test_run_id_suffix_variant(self):
        """Row has run_id; log file has a suffix like <run_id>-1.log."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        run_id = "20260510T053731Z"
        ts = "2026-05-10T05:55:22Z"
        row = {
            "timestamp": ts,
            "duration_seconds": 50,
            "run_id": run_id,
        }
        _make_metrics_file(tmp_path, [row])
        log_dir = tmp_path / ".autonomous-team" / "loop-runs" / "autonomous-forever"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Only the suffixed variant exists
        (log_dir / f"{run_id}-1.log").write_text("suffixed log content")

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertIsNotNone(result["log"])
        self.assertIn("suffixed log content", result["log"])

    def test_legacy_no_run_id_falls_back_to_timestamp(self):
        """Legacy row without run_id — matcher falls back to timestamp-derived filename."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        ts = "2026-04-11T01:41:20Z"
        row = {
            "timestamp": ts,
            "duration_seconds": 61,
        }
        _make_metrics_file(tmp_path, [row])
        # Create log file named after timestamp
        log_content = "legacy log content"
        _make_log_file(tmp_path, ts, log_content)

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertIsNotNone(result["log"])
        self.assertIn("legacy log content", result["log"])

    def test_counters_normalised_to_zero_when_row_found(self):
        """Row found but missing counter fields — backend returns 0, not absent."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        ts = "2026-05-10T05:55:22Z"
        row = {
            "timestamp": ts,
            "iteration": 0,
            "duration_seconds": 1071,
            "actions": 1,
            "exit_code": 0,
            "trigger": "dashboard",
            "run_id": "20260510T053731Z",
        }
        _make_metrics_file(tmp_path, [row])

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        metrics = result["metrics"]
        self.assertEqual(metrics.get("agents_spawned"), 0)
        self.assertEqual(metrics.get("prs_merged"), 0)
        self.assertEqual(metrics.get("discussions_scanned"), 0)
        self.assertEqual(metrics.get("prs_scanned"), 0)

    def test_counters_absent_when_row_missing(self):
        """Row absent entirely — metrics is empty dict, no counter defaults injected."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        _make_metrics_file(tmp_path, _SAMPLE_ROWS)
        ts = "2026-04-11T03:00:00Z"  # Not in sample rows

        import backend.server as srv
        with patch.object(srv, "_REPO_ROOT", tmp_path):
            result = self._handler()({"timestamp": ts})

        self.assertEqual(result["metrics"], {})


if __name__ == "__main__":
    unittest.main()
