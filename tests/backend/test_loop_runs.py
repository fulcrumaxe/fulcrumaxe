"""
Tests for backend/loop_runs.py.

Covers all six acceptance criteria:
1. finish() writes a complete JSON with required fields
2. tail prints last 10 rows, exits 0
3. tail --n 5 limits to 5 rows
4. tail --failures-only filters to non-zero exits
5. latest_failing_run_path() returns path with non-zero exit (used by watchdog)
6. tail on empty dir exits 0 and prints "no loop runs recorded yet"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make sure repo root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend import loop_runs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_file(runs_dir: Path, started_at: str, exit_code: int,
                   duration_s: int = 10,
                   stderr_lines: list[str] | None = None) -> Path:
    """Write a finished loop-run JSON into runs_dir."""
    filename = loop_runs._ts_to_filename(started_at)
    path = runs_dir / filename
    data = {
        "started_at": started_at,
        "finished_at": started_at,  # simplified for tests
        "exit_code": exit_code,
        "duration_s": duration_s,
        "last_stderr_lines": stderr_lines or [],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# AC1: start() writes a stub; finish() writes all required fields
# ---------------------------------------------------------------------------

def test_start_creates_stub(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    import io
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    rc = loop_runs.cmd_start(type("A", (), {})())
    assert rc == 0
    printed_path = captured.getvalue().strip()
    assert printed_path
    p = Path(printed_path)
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["started_at"] is not None
    assert data["exit_code"] is None
    assert data["finished_at"] is None


def test_finish_writes_required_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    # Create a stub first
    ts = "2026-05-15T10-00-00Z"
    filename = loop_runs._ts_to_filename("2026-05-15T10:00:00Z")
    stub_path = tmp_path / filename
    stub_path.write_text(json.dumps({
        "started_at": "2026-05-15T10:00:00Z",
        "finished_at": None,
        "exit_code": None,
        "duration_s": None,
        "last_stderr_lines": [],
    }))

    # Write a fake stderr file
    stderr_file = tmp_path / "stderr.txt"
    stderr_file.write_text("error line 1\nerror line 2\n")

    args = type("A", (), {
        "file": str(stub_path),
        "exit": 1,
        "stderr": str(stderr_file),
    })()

    rc = loop_runs.cmd_finish(args)
    assert rc == 0

    data = json.loads(stub_path.read_text())
    assert data["started_at"] == "2026-05-15T10:00:00Z"
    assert data["finished_at"] is not None
    assert data["exit_code"] == 1
    assert isinstance(data["duration_s"], int)
    assert "error line 1" in data["last_stderr_lines"]
    assert "error line 2" in data["last_stderr_lines"]


def test_finish_no_stderr_file(tmp_path):
    stub_path = tmp_path / "2026-05-15T10-00-00Z.json"
    stub_path.write_text(json.dumps({
        "started_at": "2026-05-15T10:00:00Z",
        "finished_at": None,
        "exit_code": None,
        "duration_s": None,
        "last_stderr_lines": [],
    }))

    args = type("A", (), {
        "file": str(stub_path),
        "exit": 0,
        "stderr": "",
    })()

    rc = loop_runs.cmd_finish(args)
    assert rc == 0
    data = json.loads(stub_path.read_text())
    assert data["exit_code"] == 0
    assert data["last_stderr_lines"] == []


# ---------------------------------------------------------------------------
# AC2: tail prints last 10 rows, exits 0
# ---------------------------------------------------------------------------

def test_tail_last_10(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    # Write 15 finished runs
    for i in range(15):
        ts = f"2026-05-15T{i:02d}:00:00Z"
        _make_run_file(tmp_path, ts, exit_code=0)

    args = type("A", (), {"n": 10, "failures_only": False})()
    rc = loop_runs.cmd_tail(args)
    assert rc == 0

    out = capsys.readouterr().out
    # Header + separator + 10 data rows
    lines = [l for l in out.splitlines() if l.strip()]
    # Count non-header lines (skip header and separator)
    data_lines = [l for l in lines if not l.startswith("timestamp") and not l.startswith("-")]
    assert len(data_lines) == 10


# ---------------------------------------------------------------------------
# AC3: tail --n 5 limits to 5 rows
# ---------------------------------------------------------------------------

def test_tail_n_5(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    for i in range(12):
        ts = f"2026-05-15T{i:02d}:00:00Z"
        _make_run_file(tmp_path, ts, exit_code=0)

    args = type("A", (), {"n": 5, "failures_only": False})()
    rc = loop_runs.cmd_tail(args)
    assert rc == 0

    out = capsys.readouterr().out
    data_lines = [l for l in out.splitlines()
                  if l.strip() and not l.startswith("timestamp") and not l.startswith("-")]
    assert len(data_lines) == 5


# ---------------------------------------------------------------------------
# AC4: tail --failures-only filters to non-zero exits
# ---------------------------------------------------------------------------

def test_tail_failures_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    _make_run_file(tmp_path, "2026-05-15T01:00:00Z", exit_code=0)
    _make_run_file(tmp_path, "2026-05-15T02:00:00Z", exit_code=1)
    _make_run_file(tmp_path, "2026-05-15T03:00:00Z", exit_code=0)
    _make_run_file(tmp_path, "2026-05-15T04:00:00Z", exit_code=2,
                   stderr_lines=["fatal error"])

    args = type("A", (), {"n": 10, "failures_only": True})()
    rc = loop_runs.cmd_tail(args)
    assert rc == 0

    out = capsys.readouterr().out
    data_lines = [l for l in out.splitlines()
                  if l.strip() and not l.startswith("timestamp") and not l.startswith("-")]
    assert len(data_lines) == 2
    # Both lines should have non-zero exit codes
    for line in data_lines:
        parts = line.split()
        assert parts[1] in ("1", "2")


# ---------------------------------------------------------------------------
# AC5: latest_failing_run_path returns path of most recent failure
# ---------------------------------------------------------------------------

def test_latest_failing_run_path(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    _make_run_file(tmp_path, "2026-05-15T01:00:00Z", exit_code=1)
    _make_run_file(tmp_path, "2026-05-15T02:00:00Z", exit_code=0)
    _make_run_file(tmp_path, "2026-05-15T03:00:00Z", exit_code=1)
    _make_run_file(tmp_path, "2026-05-15T04:00:00Z", exit_code=0)

    result = loop_runs.latest_failing_run_path(repo_root=None)
    assert result is not None
    # Should be the 03:00:00 failure (alphabetically last non-zero)
    assert "03-00-00" in result
    assert result.endswith(".json")


def test_latest_failing_run_path_no_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    _make_run_file(tmp_path, "2026-05-15T01:00:00Z", exit_code=0)

    result = loop_runs.latest_failing_run_path(repo_root=None)
    assert result is None


# ---------------------------------------------------------------------------
# AC6: empty dir exits 0 and prints friendly message
# ---------------------------------------------------------------------------

def test_tail_empty_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)

    args = type("A", (), {"n": 10, "failures_only": False})()
    rc = loop_runs.cmd_tail(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "no loop runs recorded yet" in out


def test_tail_failures_only_empty_dir(tmp_path, monkeypatch, capsys):
    """failures-only on a dir with runs but no failures should also exit 0."""
    monkeypatch.setattr(loop_runs, "_runs_dir",
                        lambda repo_root=None: tmp_path)
    _make_run_file(tmp_path, "2026-05-15T01:00:00Z", exit_code=0)

    args = type("A", (), {"n": 10, "failures_only": True})()
    rc = loop_runs.cmd_tail(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "no" in out.lower()
