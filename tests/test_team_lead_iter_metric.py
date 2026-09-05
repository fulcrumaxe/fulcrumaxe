"""Tests for PR-b: per-iteration Team Lead token metrics.

Covers:
  - Worktree sub-agent session dirs are NOT counted as Team Lead
  - Graceful degrade: JSONL read failure writes zeros to loop-metrics.jsonl
  - team_lead_tokens_per_iter = input + output (cache excluded)
  - record_loop_iter() creates the loop_metrics DuckDB table and populates it
  - Migration: loop_metrics table created fresh includes team_lead_tokens_per_iter column
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.subscription_usage import team_lead_usage, _TEAM_LEAD_PROJECT_DIR_NAME
from backend.stats_writer import record_loop_iter, _ensure_loop_metrics_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


def _entry(
    timestamp: datetime,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> str:
    """Build a single JSONL line in Claude Code transcript format."""
    return json.dumps({
        "timestamp": timestamp.isoformat(),
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            }
        },
    })


def _make_projects_dir(tmp_path: Path) -> Path:
    """Return a projects root under tmp_path."""
    d = tmp_path / "projects"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# Test: worktree sub-agent dirs are excluded
# ---------------------------------------------------------------------------

class TestWorktreeDirsExcluded:
    """team_lead_usage() must only read from the exact TL project dir."""

    def test_worktree_dir_tokens_not_counted(self, tmp_path: Path) -> None:
        projects_dir = _make_projects_dir(tmp_path)

        # Team Lead dir (exact match)
        tl_dir = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
        tl_dir.mkdir()

        # Worktree sub-agent dir (should be excluded)
        wt_dir = projects_dir / f"{_TEAM_LEAD_PROJECT_DIR_NAME}--claude-worktrees-agent-abc123"
        wt_dir.mkdir()

        ts = _NOW - timedelta(seconds=60)
        since = (_NOW - timedelta(seconds=300)).timestamp()
        until = _NOW.timestamp()

        # TL dir: 100 input, 50 output
        (tl_dir / "session1.jsonl").write_text(
            _entry(ts, input_tokens=100, output_tokens=50) + "\n"
        )

        # Worktree dir: 9999 input, 9999 output — must NOT be counted
        (wt_dir / "agent-session.jsonl").write_text(
            _entry(ts, input_tokens=9999, output_tokens=9999) + "\n"
        )

        result = team_lead_usage(since_ts=since, until_ts=until,
                                  _projects_dir_override=projects_dir)

        assert result["input"] == 100, (
            f"Expected 100 input tokens from TL dir only, got {result['input']}"
        )
        assert result["output"] == 50, (
            f"Expected 50 output tokens from TL dir only, got {result['output']}"
        )

    def test_multiple_worktree_dirs_excluded(self, tmp_path: Path) -> None:
        projects_dir = _make_projects_dir(tmp_path)

        tl_dir = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
        tl_dir.mkdir()

        # Several worktree dirs
        for suffix in ["--claude-worktrees-agent-1", "--claude-worktrees-agent-2"]:
            d = projects_dir / f"{_TEAM_LEAD_PROJECT_DIR_NAME}{suffix}"
            d.mkdir()
            ts = _NOW - timedelta(seconds=30)
            (d / "session.jsonl").write_text(
                _entry(ts, input_tokens=5000, output_tokens=5000) + "\n"
            )

        # TL dir is empty
        since = (_NOW - timedelta(seconds=300)).timestamp()
        until = _NOW.timestamp()
        result = team_lead_usage(since_ts=since, until_ts=until,
                                  _projects_dir_override=projects_dir)

        assert result["input"] == 0
        assert result["output"] == 0

    def test_tl_dir_missing_returns_zeros(self, tmp_path: Path) -> None:
        projects_dir = _make_projects_dir(tmp_path)
        # TL dir does not exist
        since = (_NOW - timedelta(seconds=300)).timestamp()
        until = _NOW.timestamp()
        result = team_lead_usage(since_ts=since, until_ts=until,
                                  _projects_dir_override=projects_dir)

        assert result["input"] == 0
        assert result["output"] == 0
        assert result["session_files"] == []


# ---------------------------------------------------------------------------
# Test: graceful degrade — JSONL read failure writes zeros
# ---------------------------------------------------------------------------

class TestGracefulDegrade:
    """When team_lead_usage() encounters unreadable files it returns zeros."""

    def test_unreadable_jsonl_returns_zeros(self, tmp_path: Path) -> None:
        projects_dir = _make_projects_dir(tmp_path)
        tl_dir = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
        tl_dir.mkdir()

        # Create a file then make it unreadable
        bad_file = tl_dir / "session.jsonl"
        bad_file.write_text("valid line would be here\n")
        # Simulate read failure by patching Path.read_text to raise
        original_read = Path.read_text

        def _raise_for_bad(*args, **kwargs):
            p = args[0] if args else None
            if p is not None and str(p) == str(bad_file):
                raise OSError("simulated permission denied")
            return original_read(*args, **kwargs)

        since = (_NOW - timedelta(seconds=300)).timestamp()
        until = _NOW.timestamp()

        with patch.object(Path, "read_text", _raise_for_bad):
            result = team_lead_usage(since_ts=since, until_ts=until,
                                      _projects_dir_override=projects_dir)

        # Should return zeros, not raise
        assert result["input"] == 0
        assert result["output"] == 0

    def test_malformed_jsonl_lines_skipped(self, tmp_path: Path) -> None:
        projects_dir = _make_projects_dir(tmp_path)
        tl_dir = projects_dir / _TEAM_LEAD_PROJECT_DIR_NAME
        tl_dir.mkdir()

        ts = _NOW - timedelta(seconds=60)
        lines = [
            "NOT JSON AT ALL",
            '{"no_timestamp": true}',
            _entry(ts, input_tokens=42, output_tokens=7),
            "",
            '{"timestamp": "bad-date", "usage": {"input_tokens": 999}}',
        ]
        (tl_dir / "session.jsonl").write_text("\n".join(lines) + "\n")

        since = (_NOW - timedelta(seconds=300)).timestamp()
        until = _NOW.timestamp()
        result = team_lead_usage(since_ts=since, until_ts=until,
                                  _projects_dir_override=projects_dir)

        assert result["input"] == 42
        assert result["output"] == 7


# ---------------------------------------------------------------------------
# Test: team_lead_tokens_per_iter = input + output (cache excluded)
# ---------------------------------------------------------------------------

class TestTokensPerIterCalculation:
    """record_loop_iter() computes tokens_per_iter = input + output only."""

    def test_tokens_per_iter_excludes_cache(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test_stats.duckdb"

        try:
            import duckdb
        except ImportError:
            pytest.skip("duckdb not installed")

        with patch("backend.stats_writer._db_path", return_value=db_path):
            record_loop_iter(
                duration_s=30.0,
                team_lead_input_tokens=1000,
                team_lead_output_tokens=500,
                team_lead_cache_read=9999,   # must NOT be included
                team_lead_cache_write=8888,  # must NOT be included
            )

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            row = conn.execute(
                "SELECT team_lead_tokens_per_iter, team_lead_input_tokens, "
                "team_lead_output_tokens, team_lead_cache_read, team_lead_cache_write "
                "FROM loop_metrics ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "Expected one row in loop_metrics"
        tokens_per_iter, inp, out, cr, cw = row
        assert tokens_per_iter == inp + out, (
            f"tokens_per_iter ({tokens_per_iter}) != input ({inp}) + output ({out})"
        )
        assert tokens_per_iter == 1500, (
            f"Expected 1500 (1000+500), got {tokens_per_iter}"
        )
        assert cr == 9999
        assert cw == 8888

    def test_tokens_per_iter_zero_when_no_tl_session(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test_stats2.duckdb"

        try:
            import duckdb
        except ImportError:
            pytest.skip("duckdb not installed")

        with patch("backend.stats_writer._db_path", return_value=db_path):
            record_loop_iter(
                duration_s=10.0,
                team_lead_input_tokens=0,
                team_lead_output_tokens=0,
            )

        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            row = conn.execute(
                "SELECT team_lead_tokens_per_iter FROM loop_metrics LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] == 0


# ---------------------------------------------------------------------------
# Test: loop_metrics table schema / migration
# ---------------------------------------------------------------------------

class TestLoopMetricsMigration:
    """DuckDB loop_metrics table is created with the expected schema."""

    def test_table_created_with_all_columns(self, tmp_path: Path) -> None:
        try:
            import duckdb
        except ImportError:
            pytest.skip("duckdb not installed")

        db_path = tmp_path / "migration_test.duckdb"
        conn = duckdb.connect(str(db_path))
        try:
            _ensure_loop_metrics_schema(conn)
            cols = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='loop_metrics' ORDER BY column_name"
            ).fetchall()
        finally:
            conn.close()

        col_names = {r[0] for r in cols}
        required = {
            "ts",
            "duration_s",
            "team_lead_input_tokens",
            "team_lead_output_tokens",
            "team_lead_cache_read",
            "team_lead_cache_write",
            "team_lead_tokens_per_iter",
        }
        missing = required - col_names
        assert not missing, f"Missing columns: {missing}"

    def test_migration_adds_column_to_existing_table(self, tmp_path: Path) -> None:
        """Simulate a pre-PR-b DB that lacks team_lead_tokens_per_iter."""
        try:
            import duckdb
        except ImportError:
            pytest.skip("duckdb not installed")

        db_path = tmp_path / "legacy_test.duckdb"
        conn = duckdb.connect(str(db_path))
        try:
            # Create table without the new column
            conn.execute("""
                CREATE TABLE loop_metrics (
                    ts          TIMESTAMP PRIMARY KEY,
                    duration_s  DOUBLE NOT NULL DEFAULT 0
                )
            """)
            # Run migration
            _ensure_loop_metrics_schema(conn)
            cols = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='loop_metrics'"
            ).fetchall()
        finally:
            conn.close()

        col_names = {r[0] for r in cols}
        assert "team_lead_tokens_per_iter" in col_names, (
            "Migration should add team_lead_tokens_per_iter to existing table"
        )
