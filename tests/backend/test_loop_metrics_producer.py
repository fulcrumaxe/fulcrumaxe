"""Tests for loop-metrics producer sanity check and migration.

Covers:
  - append-loop-metrics.sh rejects duration_s > 86400 (epoch values) with exit code 2
  - append-loop-metrics.sh accepts realistic durations (< 86400)
  - migrate-loop-metrics.py rewrites corrupt rows and preserves clean rows
  - migrate-loop-metrics.py dry-run leaves the file unchanged
  - canonical field name is duration_s (not duration_seconds)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APPEND_SCRIPT = REPO_ROOT / "scripts" / "append-loop-metrics.sh"
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate-loop-metrics.py"

_UNIX_EPOCH_VALUE = 1_778_856_462  # May 2026 epoch -- the exact corrupt value seen in prod


def _run_append(extra_args: list[str], env_override: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["AF_MCP_TEST_ORIGIN"] = "0"  # don't trigger the E2E-test guard
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["bash", str(APPEND_SCRIPT)] + extra_args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


class TestProducerSanityCheck:
    """append-loop-metrics.sh must reject epoch-valued duration_s."""

    def test_realistic_duration_accepted(self, tmp_path):
        """A realistic duration (330s) must produce a valid row."""
        metrics_file = tmp_path / "loop-metrics.jsonl"
        result = _run_append(
            [
                "--iter-start-iso", "2026-01-01T10:00:00Z",
                "--iter-end-iso",   "2026-01-01T10:05:30Z",
                "--duration-seconds", "330",
                "--agents-spawned", "0",
                "--prs-merged", "0",
                "--discussions-scanned", "0",
                "--prs-scanned", "0",
            ],
            {"METRICS_FILE": str(metrics_file)},
        )
        assert result.returncode == 0, f"Expected success, got: {result.stderr}"
        assert metrics_file.exists(), "No row was written"
        row = json.loads(metrics_file.read_text().strip())
        assert row["duration_s"] == 330

    def test_epoch_duration_rejected(self, tmp_path):
        """duration_s equal to a Unix epoch must be rejected (exit 2, no row written)."""
        metrics_file = tmp_path / "loop-metrics.jsonl"
        result = _run_append(
            [
                "--iter-start-iso", "2026-05-15T14:47:00Z",
                "--iter-end-iso",   "2026-05-15T14:47:42Z",
                "--duration-seconds", str(_UNIX_EPOCH_VALUE),
                "--agents-spawned", "0",
                "--prs-merged", "0",
                "--discussions-scanned", "0",
                "--prs-scanned", "0",
            ],
            {"METRICS_FILE": str(metrics_file)},
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for epoch duration, got {result.returncode}. "
            f"stderr: {result.stderr}"
        )
        # File must not exist or be empty -- no corrupt row written
        assert not metrics_file.exists() or metrics_file.stat().st_size == 0, (
            "Corrupt row was written despite sanity check"
        )

    def test_exactly_86400_seconds_accepted(self, tmp_path):
        """The boundary: 86400s (exactly 24h) is the max allowed duration."""
        metrics_file = tmp_path / "loop-metrics.jsonl"
        result = _run_append(
            [
                "--iter-start-iso", "2026-01-01T00:00:00Z",
                "--iter-end-iso",   "2026-01-02T00:00:00Z",
                "--duration-seconds", "86400",
                "--agents-spawned", "0",
                "--prs-merged", "0",
                "--discussions-scanned", "0",
                "--prs-scanned", "0",
            ],
            {"METRICS_FILE": str(metrics_file)},
        )
        assert result.returncode == 0, f"86400s should be accepted: {result.stderr}"

    def test_86401_seconds_rejected(self, tmp_path):
        """One second over the limit must be rejected."""
        metrics_file = tmp_path / "loop-metrics.jsonl"
        result = _run_append(
            [
                "--iter-start-iso", "2026-01-01T00:00:00Z",
                "--iter-end-iso",   "2026-01-02T00:00:01Z",
                "--duration-seconds", "86401",
                "--agents-spawned", "0",
                "--prs-merged", "0",
                "--discussions-scanned", "0",
                "--prs-scanned", "0",
            ],
            {"METRICS_FILE": str(metrics_file)},
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for duration > 86400, got {result.returncode}"
        )

    def test_canonical_field_name_is_duration_s(self, tmp_path):
        """The written row must use 'duration_s', not 'duration_seconds'."""
        metrics_file = tmp_path / "loop-metrics.jsonl"
        _run_append(
            [
                "--iter-start-iso", "2026-01-01T10:00:00Z",
                "--iter-end-iso",   "2026-01-01T10:05:00Z",
                "--duration-seconds", "300",
                "--agents-spawned", "0",
                "--prs-merged", "0",
                "--discussions-scanned", "0",
                "--prs-scanned", "0",
            ],
            {"METRICS_FILE": str(metrics_file)},
        )
        row = json.loads(metrics_file.read_text().strip())
        assert "duration_s" in row, "Expected canonical 'duration_s' field"
        assert "duration_seconds" not in row, (
            "Old field name 'duration_seconds' should not be present in new rows"
        )


class TestMigrationScript:
    """migrate-loop-metrics.py must fix historic corrupt rows."""

    def _make_metrics_file(self, tmp_path: Path, rows: list[dict]) -> Path:
        f = tmp_path / "loop-metrics.jsonl"
        with f.open("w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return f

    def test_corrupt_rows_are_zeroed(self, tmp_path):
        """Corrupt rows get duration_s=0 and corrupt=true."""
        f = self._make_metrics_file(tmp_path, [
            {"timestamp": "2026-04-11T12:54:41Z", "duration_seconds": _UNIX_EPOCH_VALUE},
            {"timestamp": "2026-05-15T14:47:42Z", "duration_s": _UNIX_EPOCH_VALUE, "origin": "interactive"},
            {"timestamp": "2026-05-18T22:26:23Z", "duration_s": 272, "origin": "cron"},
        ])
        result = subprocess.run(
            [sys.executable, str(MIGRATE_SCRIPT), "--metrics-file", str(f)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"Migration failed: {result.stderr}"

        rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        assert len(rows) == 3

        # First row was corrupt (duration_seconds field)
        assert rows[0]["duration_s"] == 0
        assert rows[0]["corrupt"] is True
        assert "duration_seconds" not in rows[0]

        # Second row was corrupt (duration_s field, interactive)
        assert rows[1]["duration_s"] == 0
        assert rows[1]["corrupt"] is True
        assert rows[1]["origin"] == "interactive"

        # Third row was clean -- must be untouched
        assert rows[2]["duration_s"] == 272
        assert "corrupt" not in rows[2]

    def test_dry_run_leaves_file_unchanged(self, tmp_path):
        """--dry-run must not modify the file."""
        original_rows = [
            {"timestamp": "2026-04-11T12:54:41Z", "duration_seconds": _UNIX_EPOCH_VALUE},
            {"timestamp": "2026-05-18T22:26:23Z", "duration_s": 272},
        ]
        f = self._make_metrics_file(tmp_path, original_rows)
        original_content = f.read_text()

        subprocess.run(
            [sys.executable, str(MIGRATE_SCRIPT), "--dry-run", "--metrics-file", str(f)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert f.read_text() == original_content, "dry-run modified the file"

    def test_no_corrupt_rows_is_noop(self, tmp_path):
        """When there are no corrupt rows, the file is untouched."""
        rows = [
            {"timestamp": "2026-05-18T22:26:23Z", "duration_s": 272},
            {"timestamp": "2026-05-18T22:29:55Z", "duration_s": 198},
        ]
        f = self._make_metrics_file(tmp_path, rows)
        original_content = f.read_text()

        subprocess.run(
            [sys.executable, str(MIGRATE_SCRIPT), "--metrics-file", str(f)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert f.read_text() == original_content, "migration changed clean rows"

    def test_malformed_json_lines_preserved(self, tmp_path):
        """Lines that aren't valid JSON are left alone (not lost)."""
        f = tmp_path / "loop-metrics.jsonl"
        f.write_text(
            '{"timestamp":"2026-04-11T12:54:41Z","duration_s":272}\n'
            'not valid json\n'
            '{"timestamp":"2026-05-18T22:26:23Z","duration_s":300}\n'
        )
        subprocess.run(
            [sys.executable, str(MIGRATE_SCRIPT), "--metrics-file", str(f)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        lines = f.read_text().splitlines()
        assert len(lines) == 3
        assert lines[1] == "not valid json"
