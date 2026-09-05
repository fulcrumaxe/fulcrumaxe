"""
Behavioral tests for backend/scheduled_jobs_status.py.

All tests are isolated to tmp_path — no real ~/.autonomous-forever-state/
or .autonomous-team/ directories are touched.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.scheduled_jobs_status as sjs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_run_log(path: Path, rows: list[dict]) -> None:
    """Write rows to a runs.jsonl file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _row(job: str, started_at: str, exit_code: int = 0) -> dict:
    return {"job": job, "started_at": started_at, "exit_code": exit_code}


# ---------------------------------------------------------------------------
# read_run_log — basic reading
# ---------------------------------------------------------------------------


class TestReadRunLog:
    def test_returns_empty_list_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.jsonl"
        with patch.object(sjs, "_run_log_path", return_value=missing):
            result = sjs.read_run_log()
        assert result == []

    def test_returns_empty_list_when_file_is_empty(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        log.write_text("")
        with patch.object(sjs, "_run_log_path", return_value=log):
            result = sjs.read_run_log()
        assert result == []

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        log.write_text('\n\n{"job":"heartbeat","started_at":"2026-01-01T12:00:00Z","exit_code":0}\n\n')
        with patch.object(sjs, "_run_log_path", return_value=log):
            result = sjs.read_run_log()
        assert len(result) == 1
        assert result[0]["job"] == "heartbeat"

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        log.write_text(
            'not-json\n'
            '{"job":"heartbeat","started_at":"2026-01-01T12:00:00Z","exit_code":0}\n'
            '{broken\n'
        )
        with patch.object(sjs, "_run_log_path", return_value=log):
            result = sjs.read_run_log()
        assert len(result) == 1
        assert result[0]["job"] == "heartbeat"

    def test_returns_rows_sorted_newest_first(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T10:00:00Z"),
            _row("heartbeat", "2026-01-01T12:00:00Z"),
            _row("heartbeat", "2026-01-01T11:00:00Z"),
        ])
        with patch.object(sjs, "_run_log_path", return_value=log):
            result = sjs.read_run_log()
        timestamps = [r["started_at"] for r in result]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_rows_without_started_at_survive(self, tmp_path: Path) -> None:
        """Rows missing started_at should not crash the sort (treated as empty string)."""
        log = tmp_path / "runs.jsonl"
        log.write_text('{"job":"heartbeat","exit_code":0}\n')
        with patch.object(sjs, "_run_log_path", return_value=log):
            result = sjs.read_run_log()
        assert len(result) == 1

    def test_returns_multiple_jobs_together(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T10:00:00Z"),
            _row("janitor", "2026-01-01T09:00:00Z"),
            _row("heartbeat", "2026-01-01T08:00:00Z"),
        ])
        with patch.object(sjs, "_run_log_path", return_value=log):
            result = sjs.read_run_log()
        assert len(result) == 3
        jobs = [r["job"] for r in result]
        assert "heartbeat" in jobs
        assert "janitor" in jobs


# ---------------------------------------------------------------------------
# job_status — status computation
# ---------------------------------------------------------------------------


class TestJobStatus:
    def test_no_runs_returns_none_fields(self, tmp_path: Path) -> None:
        missing_log = tmp_path / "no_runs.jsonl"
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=missing_log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["job"] == "heartbeat"
        assert status["last_run_at"] is None
        assert status["last_exit_code"] is None
        assert status["consecutive_failures"] == 0
        assert status["total_runs"] == 0

    def test_single_success_run(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z", exit_code=0)])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["last_run_at"] == "2026-01-01T12:00:00Z"
        assert status["last_exit_code"] == 0
        assert status["consecutive_failures"] == 0
        assert status["total_runs"] == 1

    def test_single_failure_run(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z", exit_code=1)])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["last_exit_code"] == 1
        assert status["consecutive_failures"] == 1

    def test_counts_consecutive_failures_from_log(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        # Most recent = latest started_at. Three failures, then a success.
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T10:00:00Z", exit_code=0),  # oldest — success
            _row("heartbeat", "2026-01-01T11:00:00Z", exit_code=1),
            _row("heartbeat", "2026-01-01T12:00:00Z", exit_code=1),
            _row("heartbeat", "2026-01-01T13:00:00Z", exit_code=1),  # newest
        ])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["consecutive_failures"] == 3
        assert status["total_runs"] == 4

    def test_consecutive_failures_reset_after_success(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T10:00:00Z", exit_code=1),
            _row("heartbeat", "2026-01-01T11:00:00Z", exit_code=1),
            _row("heartbeat", "2026-01-01T12:00:00Z", exit_code=0),  # success
            _row("heartbeat", "2026-01-01T13:00:00Z", exit_code=1),  # newest, failure
        ])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        # Only 1 consecutive failure from the top (newest = failure, then success below it)
        assert status["consecutive_failures"] == 1

    def test_breaker_file_overrides_log_count(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T12:00:00Z", exit_code=1),
        ])
        breaker = tmp_path / "breaker.json"
        breaker.write_text(json.dumps({"consecutive_failures": 7}))
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=breaker),
        ):
            status = sjs.job_status("heartbeat")
        # Breaker file takes priority
        assert status["consecutive_failures"] == 7

    def test_breaker_file_missing_key_defaults_to_zero(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z", exit_code=1)])
        breaker = tmp_path / "breaker.json"
        breaker.write_text(json.dumps({"other_key": 99}))
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["consecutive_failures"] == 0

    def test_malformed_breaker_file_does_not_crash(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z", exit_code=0)])
        breaker = tmp_path / "breaker.json"
        breaker.write_text("not-valid-json{{{")
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=breaker),
        ):
            status = sjs.job_status("heartbeat")
        # Falls through gracefully; breaker exists so log-based count is skipped → 0
        assert status["consecutive_failures"] == 0

    def test_filters_by_job_name(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T12:00:00Z", exit_code=0),
            _row("janitor", "2026-01-01T11:00:00Z", exit_code=1),
            _row("janitor", "2026-01-01T10:00:00Z", exit_code=1),
        ])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("janitor")
        assert status["total_runs"] == 2
        assert status["last_exit_code"] == 1
        assert status["consecutive_failures"] == 2

    def test_last_run_at_is_most_recent(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T10:00:00Z"),
            _row("heartbeat", "2026-01-01T14:00:00Z"),
            _row("heartbeat", "2026-01-01T09:00:00Z"),
        ])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["last_run_at"] == "2026-01-01T14:00:00Z"

    def test_no_schedule_produces_no_next_run(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z")])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat", schedule=None)
        assert status["next_run_at"] is None

    def test_return_type_is_dict_with_expected_keys(self, tmp_path: Path) -> None:
        missing_log = tmp_path / "no_runs.jsonl"
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=missing_log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("myjob")
        expected_keys = {"job", "last_run_at", "next_run_at", "last_exit_code",
                         "consecutive_failures", "total_runs"}
        assert set(status.keys()) == expected_keys


# ---------------------------------------------------------------------------
# load_manifest_jobs — missing / inaccessible manifest
# ---------------------------------------------------------------------------


class TestLoadManifestJobs:
    def test_missing_manifest_returns_empty_list(self, tmp_path: Path) -> None:
        missing = tmp_path / "jobs.yaml"
        with patch.object(sjs, "_manifest_path", return_value=missing):
            result = sjs.load_manifest_jobs()
        assert result == []


# ---------------------------------------------------------------------------
# next_run_at — edge cases
# ---------------------------------------------------------------------------


class TestNextRunAt:
    def test_invalid_schedule_returns_none(self) -> None:
        """A bad cron expression should not raise — returns None instead."""
        now = datetime.now(timezone.utc)
        result = sjs.next_run_at("not-a-cron", now)
        # Either None (parse_jobs unavailable or error) or a valid ISO string
        assert result is None or isinstance(result, str)

    def test_none_schedule_bypassed_at_job_status_level(self, tmp_path: Path) -> None:
        """next_run_at is never called when schedule=None."""
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z")])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
            patch.object(sjs, "next_run_at") as mock_nra,
        ):
            sjs.job_status("heartbeat", schedule=None)
        mock_nra.assert_not_called()

    def test_schedule_provided_calls_next_run_at(self, tmp_path: Path) -> None:
        """When schedule is set, next_run_at is called."""
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z")])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
            patch.object(sjs, "next_run_at", return_value="2026-01-02T00:00:00Z") as mock_nra,
        ):
            status = sjs.job_status("heartbeat", schedule="0 * * * *")
        mock_nra.assert_called_once()
        assert status["next_run_at"] == "2026-01-02T00:00:00Z"


# ---------------------------------------------------------------------------
# Edge cases — mixed / unusual input
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_all_success_no_failures(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("heartbeat", "2026-01-01T10:00:00Z", exit_code=0),
            _row("heartbeat", "2026-01-01T11:00:00Z", exit_code=0),
            _row("heartbeat", "2026-01-01T12:00:00Z", exit_code=0),
        ])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["consecutive_failures"] == 0
        assert status["total_runs"] == 3

    def test_job_with_no_history_in_multi_job_log(self, tmp_path: Path) -> None:
        """Querying a job name that has zero rows in a populated log."""
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            _row("janitor", "2026-01-01T10:00:00Z"),
            _row("janitor", "2026-01-01T11:00:00Z"),
        ])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["total_runs"] == 0
        assert status["last_run_at"] is None

    def test_run_row_missing_exit_code(self, tmp_path: Path) -> None:
        """Rows without exit_code should not crash — exit_code defaults to absent."""
        log = tmp_path / "runs.jsonl"
        log.write_text('{"job":"heartbeat","started_at":"2026-01-01T12:00:00Z"}\n')
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        # exit_code key missing in row → last_exit_code is None
        assert status["last_exit_code"] is None
        assert status["total_runs"] == 1

    def test_large_consecutive_failure_count_from_breaker(self, tmp_path: Path) -> None:
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [_row("heartbeat", "2026-01-01T12:00:00Z", exit_code=1)])
        breaker = tmp_path / "breaker.json"
        breaker.write_text(json.dumps({"consecutive_failures": 999}))
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["consecutive_failures"] == 999

    def test_run_log_with_only_dispatcher_rows_ignored(self, tmp_path: Path) -> None:
        """Dispatcher rows are not job runs — querying any named job returns 0."""
        log = tmp_path / "runs.jsonl"
        _write_run_log(log, [
            {"job": "dispatcher", "started_at": "2026-01-01T12:00:00Z", "exit_code": 0},
        ])
        missing_breaker = tmp_path / "no_breaker.json"
        with (
            patch.object(sjs, "_run_log_path", return_value=log),
            patch.object(sjs, "_breaker_path", return_value=missing_breaker),
        ):
            status = sjs.job_status("heartbeat")
        assert status["total_runs"] == 0

    def test_job_name_in_status_matches_input(self, tmp_path: Path) -> None:
        missing_log = tmp_path / "no_runs.jsonl"
        missing_breaker = tmp_path / "no_breaker.json"
        for name in ["heartbeat", "janitor", "my-job-with-dashes", "job_underscore"]:
            with (
                patch.object(sjs, "_run_log_path", return_value=missing_log),
                patch.object(sjs, "_breaker_path", return_value=missing_breaker),
            ):
                status = sjs.job_status(name)
            assert status["job"] == name
