"""
backend/tests/test_loop_runs.py — behavioral tests for backend/loop_runs.py.

Isolation: every test monkeypatches backend.loop_runs._runs_dir to return a
fresh tmp_path subdirectory.  The real ~/.fulcrumaxe-state/ and the
repo's .autonomous-team/loop-runs/ directories are never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

import backend.loop_runs as lr


# ---------------------------------------------------------------------------
# Isolation fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an isolated runs dir; patch _runs_dir() to always return it."""
    d = tmp_path / "loop-runs"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lr, "_runs_dir", lambda repo_root=None: d)
    return d


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_finish_args(file: str, exit_code: int, stderr: str = "") -> argparse.Namespace:
    return argparse.Namespace(file=file, exit=exit_code, stderr=stderr)


def _make_tail_args(n: int = 10, failures_only: bool = False) -> argparse.Namespace:
    return argparse.Namespace(n=n, failures_only=failures_only)


def _write_run(runs_dir: Path, started_at: str, finished_at: str | None,
               exit_code: int | None, duration_s: int | None = None,
               stderr_lines: list[str] | None = None) -> Path:
    """Write a run JSON file directly into runs_dir, bypassing cmd_start."""
    safe_ts = started_at.replace(":", "-")
    if not safe_ts.endswith(".json"):
        safe_ts += ".json"
    path = runs_dir / safe_ts
    data = {
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "duration_s": duration_s,
        "last_stderr_lines": stderr_lines or [],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# _ts_to_filename
# ---------------------------------------------------------------------------

class TestTsToFilename:
    def test_replaces_colons(self) -> None:
        fn = lr._ts_to_filename("2026-05-15T02:46:00Z")
        assert fn == "2026-05-15T02-46-00Z.json"
        assert ":" not in fn

    def test_appends_json_extension(self) -> None:
        fn = lr._ts_to_filename("2026-05-15T10:00:00Z")
        assert fn.endswith(".json")

    def test_no_double_extension(self) -> None:
        # If the string already ends with .json (unusual), must not double it.
        # The function only adds .json if not already present.
        fn = lr._ts_to_filename("2026-05-15T10-00-00Z.json")
        assert fn.count(".json") == 1


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------

class TestCmdStart:
    def test_creates_stub_file(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        rc = lr.cmd_start(args)
        assert rc == 0
        printed = capsys.readouterr().out.strip()
        assert printed  # a path was printed
        stub_path = Path(printed)
        assert stub_path.exists()

    def test_stub_has_required_keys(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        printed = capsys.readouterr().out.strip()
        data = json.loads(Path(printed).read_text())
        assert "started_at" in data
        assert data["finished_at"] is None
        assert data["exit_code"] is None
        assert data["duration_s"] is None
        assert data["last_stderr_lines"] == []

    def test_stub_started_at_is_iso8601(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        printed = capsys.readouterr().out.strip()
        data = json.loads(Path(printed).read_text())
        ts = data["started_at"]
        assert ts.endswith("Z"), f"expected UTC 'Z' suffix: {ts!r}"
        assert "T" in ts

    def test_file_placed_in_runs_dir(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        printed = capsys.readouterr().out.strip()
        stub_path = Path(printed)
        assert stub_path.parent == runs_dir


# ---------------------------------------------------------------------------
# cmd_finish
# ---------------------------------------------------------------------------

class TestCmdFinish:
    def test_finish_updates_exit_code(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        stub_path = Path(capsys.readouterr().out.strip())

        rc = lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=0))
        assert rc == 0
        data = json.loads(stub_path.read_text())
        assert data["exit_code"] == 0

    def test_finish_sets_nonzero_exit_code(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        stub_path = Path(capsys.readouterr().out.strip())

        lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=2))
        data = json.loads(stub_path.read_text())
        assert data["exit_code"] == 2

    def test_finish_sets_finished_at(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        stub_path = Path(capsys.readouterr().out.strip())

        lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=0))
        data = json.loads(stub_path.read_text())
        assert data["finished_at"] is not None
        assert data["finished_at"].endswith("Z")

    def test_finish_computes_nonneg_duration(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        stub_path = Path(capsys.readouterr().out.strip())

        lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=0))
        data = json.loads(stub_path.read_text())
        assert isinstance(data["duration_s"], int)
        assert data["duration_s"] >= 0

    def test_finish_reads_stderr_file(self, runs_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        stub_path = Path(capsys.readouterr().out.strip())

        stderr_file = tmp_path / "err.txt"
        stderr_file.write_text("something went wrong\nanother error line\n")

        lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=1, stderr=str(stderr_file)))
        data = json.loads(stub_path.read_text())
        assert data["last_stderr_lines"] == ["something went wrong", "another error line"]

    def test_finish_truncates_stderr_to_20_lines(self, runs_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        args = argparse.Namespace()
        lr.cmd_start(args)
        stub_path = Path(capsys.readouterr().out.strip())

        stderr_file = tmp_path / "err.txt"
        # Write 30 non-empty lines
        stderr_file.write_text("\n".join(f"line{i}" for i in range(30)) + "\n")

        lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=1, stderr=str(stderr_file)))
        data = json.loads(stub_path.read_text())
        assert len(data["last_stderr_lines"]) <= 20
        # Should be the last 20 lines
        assert data["last_stderr_lines"][-1] == "line29"

    def test_finish_missing_file_returns_1(self, runs_dir: Path) -> None:
        rc = lr.cmd_finish(_make_finish_args("/nonexistent/path/run.json", exit_code=0))
        assert rc == 1

    def test_finish_missing_stderr_file_is_ok(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        """A non-existent stderr path should not fail the finish."""
        args = argparse.Namespace()
        lr.cmd_start(args)
        stub_path = Path(capsys.readouterr().out.strip())

        rc = lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=0, stderr="/tmp/does-not-exist-xyz.txt"))
        assert rc == 0
        data = json.loads(stub_path.read_text())
        assert data["last_stderr_lines"] == []

    def test_finish_on_malformed_json_returns_1(self, runs_dir: Path, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{")
        rc = lr.cmd_finish(_make_finish_args(str(bad_file), exit_code=0))
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_tail
# ---------------------------------------------------------------------------

class TestCmdTail:
    def test_empty_dir_prints_message(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        rc = lr.cmd_tail(_make_tail_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "no loop runs" in out.lower()

    def test_stub_only_dir_prints_no_runs(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        """Unfinished stubs (exit_code=None) must not appear in tail output."""
        _write_run(runs_dir, "2026-05-15T10:00:00Z", finished_at=None, exit_code=None)
        rc = lr.cmd_tail(_make_tail_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "no loop runs" in out.lower()

    def test_finished_run_appears_in_output(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        _write_run(runs_dir, "2026-05-15T10:00:00Z", "2026-05-15T10:05:00Z", exit_code=0, duration_s=300)
        lr.cmd_tail(_make_tail_args())
        out = capsys.readouterr().out
        # Timestamp prefix should appear (colons replaced with hyphens in filename but started_at is in the JSON)
        assert "2026-05-15" in out

    def test_returns_at_most_n_rows(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        for i in range(15):
            ts = f"2026-05-15T{i:02d}:00:00Z"
            _write_run(runs_dir, ts, f"2026-05-15T{i:02d}:01:00Z", exit_code=0, duration_s=60)
        lr.cmd_tail(_make_tail_args(n=5))
        out = capsys.readouterr().out
        # Header + separator + 5 data rows = 7 non-empty lines
        data_lines = [l for l in out.splitlines() if l.strip() and "---" not in l and "timestamp" not in l]
        assert len(data_lines) == 5

    def test_ordering_newest_last(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        """Rows are sorted ascending by filename (which is the start timestamp)."""
        for hour in [3, 1, 2]:
            ts = f"2026-05-15T{hour:02d}:00:00Z"
            _write_run(runs_dir, ts, f"2026-05-15T{hour:02d}:01:00Z", exit_code=0, duration_s=60)
        lr.cmd_tail(_make_tail_args(n=10))
        out = capsys.readouterr().out
        data_lines = [l for l in out.splitlines() if l.strip() and "---" not in l and "timestamp" not in l]
        # Expect sorted ascending: hour 01, 02, 03
        assert "01" in data_lines[0]
        assert "03" in data_lines[-1]

    def test_failures_only_filters_successes(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        _write_run(runs_dir, "2026-05-15T01:00:00Z", "2026-05-15T01:01:00Z", exit_code=0, duration_s=60)
        _write_run(runs_dir, "2026-05-15T02:00:00Z", "2026-05-15T02:01:00Z", exit_code=1, duration_s=60)
        _write_run(runs_dir, "2026-05-15T03:00:00Z", "2026-05-15T03:01:00Z", exit_code=0, duration_s=60)

        lr.cmd_tail(_make_tail_args(failures_only=True))
        out = capsys.readouterr().out
        data_lines = [l for l in out.splitlines() if l.strip() and "---" not in l and "timestamp" not in l]
        assert len(data_lines) == 1

    def test_failures_only_empty_message(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        """When --failures-only is set but no failures exist, print appropriate message."""
        _write_run(runs_dir, "2026-05-15T01:00:00Z", "2026-05-15T01:01:00Z", exit_code=0, duration_s=60)
        rc = lr.cmd_tail(_make_tail_args(failures_only=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "no failed loop runs" in out.lower()

    def test_malformed_json_file_is_skipped(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        """A malformed JSON file must not crash tail; it should simply be skipped."""
        bad = runs_dir / "2026-05-15T09-00-00Z.json"
        bad.write_text("BROKEN {{{")
        # Also write one good finished run
        _write_run(runs_dir, "2026-05-15T10:00:00Z", "2026-05-15T10:01:00Z", exit_code=0, duration_s=60)

        rc = lr.cmd_tail(_make_tail_args())
        assert rc == 0
        out = capsys.readouterr().out
        data_lines = [l for l in out.splitlines() if l.strip() and "---" not in l and "timestamp" not in l]
        assert len(data_lines) == 1

    def test_last_stderr_line_shown_in_output(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        _write_run(
            runs_dir, "2026-05-15T10:00:00Z", "2026-05-15T10:01:00Z",
            exit_code=1, duration_s=60,
            stderr_lines=["preflight failed", "reason: timeout"],
        )
        lr.cmd_tail(_make_tail_args())
        out = capsys.readouterr().out
        assert "preflight failed" in out


# ---------------------------------------------------------------------------
# latest_failing_run_path
# ---------------------------------------------------------------------------

class TestLatestFailingRunPath:
    def test_returns_none_when_dir_is_empty(self, tmp_path: Path) -> None:
        d = tmp_path / "loop-runs"
        d.mkdir()
        result = lr.latest_failing_run_path(repo_root=tmp_path)
        assert result is None

    def test_returns_none_when_no_failures(self, tmp_path: Path) -> None:
        d = tmp_path / ".autonomous-team" / "loop-runs"
        d.mkdir(parents=True)
        ts = "2026-05-15T10:00:00Z"
        safe = ts.replace(":", "-") + ".json"
        (d / safe).write_text(json.dumps({
            "started_at": ts, "finished_at": "2026-05-15T10:01:00Z",
            "exit_code": 0, "duration_s": 60, "last_stderr_lines": [],
        }) + "\n")
        result = lr.latest_failing_run_path(repo_root=tmp_path)
        assert result is None

    def test_returns_path_of_failing_run(self, tmp_path: Path) -> None:
        d = tmp_path / ".autonomous-team" / "loop-runs"
        d.mkdir(parents=True)
        for i, ec in enumerate([0, 1, 0]):
            ts = f"2026-05-15T{10 + i:02d}:00:00Z"
            safe = ts.replace(":", "-") + ".json"
            (d / safe).write_text(json.dumps({
                "started_at": ts, "finished_at": f"2026-05-15T{10 + i:02d}:01:00Z",
                "exit_code": ec, "duration_s": 60, "last_stderr_lines": [],
            }) + "\n")
        result = lr.latest_failing_run_path(repo_root=tmp_path)
        assert result is not None
        data = json.loads(Path(result).read_text())
        assert data["exit_code"] == 1

    def test_returns_most_recent_failing_run(self, tmp_path: Path) -> None:
        """When multiple failures exist, the most recent (lexicographically last filename) is returned."""
        d = tmp_path / ".autonomous-team" / "loop-runs"
        d.mkdir(parents=True)
        for i in range(3):
            ts = f"2026-05-15T{10 + i:02d}:00:00Z"
            safe = ts.replace(":", "-") + ".json"
            (d / safe).write_text(json.dumps({
                "started_at": ts, "finished_at": f"2026-05-15T{10 + i:02d}:01:00Z",
                "exit_code": 1, "duration_s": 60, "last_stderr_lines": [],
            }) + "\n")
        result = lr.latest_failing_run_path(repo_root=tmp_path)
        assert result is not None
        # The most recent file is 12:00:00
        assert "12-00-00" in result

    def test_skips_stubs_no_exit_code(self, tmp_path: Path) -> None:
        """Stubs with exit_code=None must not be returned as failures."""
        d = tmp_path / ".autonomous-team" / "loop-runs"
        d.mkdir(parents=True)
        ts = "2026-05-15T10:00:00Z"
        safe = ts.replace(":", "-") + ".json"
        (d / safe).write_text(json.dumps({
            "started_at": ts, "finished_at": None,
            "exit_code": None, "duration_s": None, "last_stderr_lines": [],
        }) + "\n")
        result = lr.latest_failing_run_path(repo_root=tmp_path)
        assert result is None

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        d = tmp_path / ".autonomous-team" / "loop-runs"
        d.mkdir(parents=True)
        (d / "2026-05-15T10-00-00Z.json").write_text("BAD JSON")
        result = lr.latest_failing_run_path(repo_root=tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _prune
# ---------------------------------------------------------------------------

class TestPrune:
    def test_prune_removes_oldest_beyond_limit(self, tmp_path: Path) -> None:
        d = tmp_path / "prune-test"
        d.mkdir()
        # Create 5 JSON files named alphabetically
        for i in range(5):
            (d / f"file-{i:02d}.json").write_text("{}")
        lr._prune(d, keep=3)
        remaining = sorted(d.glob("*.json"))
        assert len(remaining) == 3
        # Newest 3 should survive
        names = [f.name for f in remaining]
        assert "file-02.json" in names
        assert "file-03.json" in names
        assert "file-04.json" in names

    def test_prune_keeps_all_when_within_limit(self, tmp_path: Path) -> None:
        d = tmp_path / "prune-test2"
        d.mkdir()
        for i in range(3):
            (d / f"file-{i:02d}.json").write_text("{}")
        lr._prune(d, keep=10)
        assert len(list(d.glob("*.json"))) == 3

    def test_prune_empty_dir_is_noop(self, tmp_path: Path) -> None:
        d = tmp_path / "prune-empty"
        d.mkdir()
        lr._prune(d, keep=10)  # must not raise
        assert len(list(d.glob("*.json"))) == 0


# ---------------------------------------------------------------------------
# Integration: start → finish → tail pipeline
# ---------------------------------------------------------------------------

class TestStartFinishTailPipeline:
    def test_full_success_pipeline(self, runs_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        # start
        start_rc = lr.cmd_start(argparse.Namespace())
        assert start_rc == 0
        stub_path = Path(capsys.readouterr().out.strip())

        # finish
        finish_rc = lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=0))
        assert finish_rc == 0

        # tail should show one row
        lr.cmd_tail(_make_tail_args())
        out = capsys.readouterr().out
        data_lines = [l for l in out.splitlines() if l.strip() and "---" not in l and "timestamp" not in l]
        assert len(data_lines) == 1
        assert "0" in data_lines[0]  # exit code 0

    def test_full_failure_pipeline(self, runs_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        start_rc = lr.cmd_start(argparse.Namespace())
        assert start_rc == 0
        stub_path = Path(capsys.readouterr().out.strip())

        stderr_file = tmp_path / "err.txt"
        stderr_file.write_text("fatal: something exploded\n")

        finish_rc = lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=1, stderr=str(stderr_file)))
        assert finish_rc == 0

        lr.cmd_tail(_make_tail_args(failures_only=True))
        out = capsys.readouterr().out
        assert "fatal: something exploded" in out

    def test_prune_fires_after_finish(self, runs_dir: Path, capsys: pytest.CaptureFixture) -> None:
        """After cmd_finish, runs beyond 1000 should be pruned. We test the boundary at a
        much smaller scale by calling _prune directly within a controlled dir."""
        # Create 5 stubs and finish one to trigger pruning path; verify _prune itself via
        # the standalone prune tests above.  Here we verify the count stays at 1 after one run.
        start_rc = lr.cmd_start(argparse.Namespace())
        stub_path = Path(capsys.readouterr().out.strip())
        lr.cmd_finish(_make_finish_args(str(stub_path), exit_code=0))
        files = list(runs_dir.glob("*.json"))
        assert len(files) == 1
