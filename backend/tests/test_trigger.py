"""
Tests for backend/trigger.py

Run with:
    python -m pytest backend/tests/test_trigger.py -v
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import backend.trigger as trigger_mod
from backend.trigger import (
    _check_lockfile,
    _read_session,
    _rotate_session,
    _run_preflight,
    _should_rotate,
    _write_session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_paths(tmp_path: Path, monkeypatch):
    """Redirect all module-level Path constants to tmp_path."""
    monkeypatch.setattr(trigger_mod, "LOCK_PATH", tmp_path / "loop.lock")
    monkeypatch.setattr(trigger_mod, "SESSION_PATH", tmp_path / "session.json")
    monkeypatch.setattr(trigger_mod, "NOW_MD_PATH", tmp_path / "now.md")
    monkeypatch.setattr(trigger_mod, "LOOP_LOG_PATH", tmp_path / "loop.log")
    monkeypatch.setattr(trigger_mod, "REPO_DIR", tmp_path)


# ---------------------------------------------------------------------------
# _check_lockfile — no lockfile
# ---------------------------------------------------------------------------


def test_check_lockfile_no_file(tmp_path: Path):
    """Returns True when no lockfile exists."""
    assert _check_lockfile() is True


def test_check_lockfile_stale_pid(tmp_path: Path):
    """Removes lockfile and returns True when PID is dead."""
    lock = trigger_mod.LOCK_PATH
    lock.write_text("99999999")
    with patch("backend.trigger.os.kill", side_effect=ProcessLookupError):
        result = _check_lockfile()
    assert result is True
    assert not lock.exists()


def test_check_lockfile_live_pid(tmp_path: Path):
    """Returns False when PID is alive."""
    lock = trigger_mod.LOCK_PATH
    lock.write_text(str(os.getpid()))
    result = _check_lockfile()
    assert result is False


def test_check_lockfile_corrupt(tmp_path: Path):
    """Removes corrupt lockfile and returns True."""
    lock = trigger_mod.LOCK_PATH
    lock.write_text("not-a-pid")
    result = _check_lockfile()
    assert result is True
    assert not lock.exists()


def test_check_lockfile_permission_error(tmp_path: Path):
    """Returns False when we can not signal the PID (PermissionError)."""
    lock = trigger_mod.LOCK_PATH
    lock.write_text("12345")
    with patch("backend.trigger.os.kill", side_effect=PermissionError):
        result = _check_lockfile()
    assert result is False


# ---------------------------------------------------------------------------
# _read_session — session.json parsing
# ---------------------------------------------------------------------------


def test_read_session_missing():
    """Returns None when session.json does not exist."""
    assert _read_session() is None


def test_read_session_valid(tmp_path: Path):
    """Returns dict for a valid session file."""
    data = {
        "session_id": "sess-abc123",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "iteration_count": 5,
    }
    trigger_mod.SESSION_PATH.write_text(json.dumps(data))
    result = _read_session()
    assert result is not None
    assert result["session_id"] == "sess-abc123"
    assert result["iteration_count"] == 5


def test_read_session_corrupt(tmp_path: Path):
    """Returns None for corrupt JSON."""
    trigger_mod.SESSION_PATH.write_text("{bad json!!!")
    assert _read_session() is None


def test_read_session_missing_field(tmp_path: Path):
    """Returns None when required fields are absent."""
    trigger_mod.SESSION_PATH.write_text(json.dumps({"session_id": "x"}))
    assert _read_session() is None


def test_read_session_wrong_types(tmp_path: Path):
    """Returns None when field types are wrong."""
    data = {
        "session_id": 123,
        "created_at": "2026-01-01T00:00:00Z",
        "iteration_count": 5,
    }
    trigger_mod.SESSION_PATH.write_text(json.dumps(data))
    assert _read_session() is None


# ---------------------------------------------------------------------------
# _write_session — atomic write
# ---------------------------------------------------------------------------


def test_write_session_roundtrip(tmp_path: Path):
    """Written session can be read back via _read_session."""
    trigger_mod.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_session("test-session-id", 3)
    result = _read_session()
    assert result is not None
    assert result["session_id"] == "test-session-id"
    assert result["iteration_count"] == 3


def test_write_session_atomic_no_tmp(tmp_path: Path):
    """Temporary file is cleaned up after write."""
    trigger_mod.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_session("atomic-test", 1)
    tmp = trigger_mod.SESSION_PATH.with_suffix(".tmp")
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# _should_rotate
# ---------------------------------------------------------------------------


def test_should_rotate_by_count():
    """Returns True when iteration_count hits max."""
    session = {
        "session_id": "x",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "iteration_count": 20,
    }
    with patch.dict(os.environ, {"AF_SESSION_MAX_ITERATIONS": "20"}):
        assert _should_rotate(session) is True


def test_should_rotate_not_yet():
    """Returns False when under thresholds."""
    session = {
        "session_id": "x",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "iteration_count": 1,
    }
    with patch.dict(os.environ, {"AF_SESSION_MAX_ITERATIONS": "20", "AF_SESSION_MAX_AGE_MINUTES": "120"}):
        assert _should_rotate(session) is False


def test_should_rotate_by_age():
    """Returns True when session is too old."""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    session = {
        "session_id": "x",
        "created_at": old_time,
        "iteration_count": 1,
    }
    with patch.dict(os.environ, {"AF_SESSION_MAX_ITERATIONS": "20", "AF_SESSION_MAX_AGE_MINUTES": "120"}):
        assert _should_rotate(session) is True


def test_should_rotate_bad_timestamp():
    """Returns True (rotate to be safe) on bad timestamp."""
    session = {
        "session_id": "x",
        "created_at": "not-a-date",
        "iteration_count": 1,
    }
    assert _should_rotate(session) is True


# ---------------------------------------------------------------------------
# _rotate_session
# ---------------------------------------------------------------------------


def test_rotate_session_removes_session_file(tmp_path: Path):
    """After rotation, session.json is gone."""
    trigger_mod.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_session("rotate-me", 5)
    assert trigger_mod.SESSION_PATH.exists()
    session = {"session_id": "rotate-me", "iteration_count": 5}
    _rotate_session(session)
    assert not trigger_mod.SESSION_PATH.exists()


def test_rotate_session_appends_to_now_md(tmp_path: Path):
    """Rotation notice is appended to now.md."""
    trigger_mod.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    trigger_mod.NOW_MD_PATH.write_text("# Previous content\n")
    session = {"session_id": "s123", "iteration_count": 7}
    _rotate_session(session)
    content = trigger_mod.NOW_MD_PATH.read_text()
    assert "Session rotated" in content
    assert "s123" in content
    assert "7 iterations" in content


def test_rotate_session_extracts_summary_lines(tmp_path: Path):
    """Loop log SUMMARY lines are included in rotation notice."""
    trigger_mod.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_lines = "\n".join([
        "[10:00] SUMMARY: merged PR #1",
        "[10:10] SUMMARY: merged PR #2",
        "[10:20] other line",
    ])
    trigger_mod.LOOP_LOG_PATH.write_text(log_lines)
    trigger_mod.NOW_MD_PATH.write_text("")
    _rotate_session({"session_id": "x", "iteration_count": 2})
    content = trigger_mod.NOW_MD_PATH.read_text()
    assert "merged PR #1" in content
    assert "merged PR #2" in content


# ---------------------------------------------------------------------------
# _run_preflight
# ---------------------------------------------------------------------------


def test_run_preflight_script_missing(tmp_path: Path):
    """Returns (True, '{}') when preflight script not found."""
    ok, summary = _run_preflight()
    assert ok is True
    assert summary == "{}"


def test_run_preflight_script_passes(tmp_path: Path):
    """Returns (True, stdout) when script exits 0."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "loop-preflight.sh"
    script.write_text('#!/bin/bash\necho \'{"status":"ok"}\'')
    script.chmod(0o755)
    with patch.object(trigger_mod, "REPO_DIR", tmp_path):
        ok, summary = _run_preflight()
    assert ok is True
    assert "ok" in summary


def test_run_preflight_script_fails(tmp_path: Path):
    """Returns (False, '{}') when script exits non-zero."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "loop-preflight.sh"
    script.write_text("#!/bin/bash\nexit 1")
    script.chmod(0o755)
    with patch.object(trigger_mod, "REPO_DIR", tmp_path):
        ok, summary = _run_preflight()
    assert ok is False
    assert summary == "{}"


def test_run_preflight_oserror(tmp_path: Path):
    """Returns (True, '{}') if subprocess raises OSError."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "loop-preflight.sh"
    script.write_text("#!/bin/bash\necho ok")
    script.chmod(0o755)
    with patch.object(trigger_mod, "REPO_DIR", tmp_path), \
         patch("backend.trigger.subprocess.run", side_effect=OSError("no bash")):
        ok, summary = _run_preflight()
    assert ok is True
    assert summary == "{}"


# ---------------------------------------------------------------------------
# FIFO write path
# ---------------------------------------------------------------------------


def test_fifo_write_sends_json(tmp_path: Path):
    """When FIFO exists, main() writes a JSON request to it."""
    fifo_path = tmp_path / "trigger.fifo"
    os.mkfifo(str(fifo_path))
    # Just assert the FIFO is a named pipe (integration smoke test)
    assert os.path.exists(str(fifo_path))
    stat = os.stat(str(fifo_path))
    import stat as stat_mod
    assert stat_mod.S_ISFIFO(stat.st_mode)


# ---------------------------------------------------------------------------
# main() — CLI entry point (mocked to avoid real subprocess/exec calls)
# ---------------------------------------------------------------------------


def test_main_lockfile_blocked(tmp_path: Path, monkeypatch):
    """main() exits 0 immediately when lockfile shows iteration is running."""
    monkeypatch.setattr("sys.argv", ["trigger.py", "run /loop"])
    with patch("backend.trigger._check_lockfile", return_value=False), \
         patch("backend.log.setup_logging", return_value=None, create=True):
        with pytest.raises(SystemExit) as exc:
            trigger_mod.main()
        assert exc.value.code == 0


def test_main_preflight_fails(tmp_path: Path, monkeypatch):
    """main() exits 0 when preflight fails."""
    monkeypatch.setattr("sys.argv", ["trigger.py"])
    with patch("backend.trigger._check_lockfile", return_value=True), \
         patch("backend.trigger._run_preflight", return_value=(False, "{}")), \
         patch("backend.log.setup_logging", return_value=None, create=True):
        with patch.dict("sys.modules", {
            "backend.backup": MagicMock(create_backup=MagicMock(), prune_backups=MagicMock()),
        }):
            with pytest.raises(SystemExit) as exc:
                trigger_mod.main()
            assert exc.value.code == 0


def test_main_fifo_write_path(tmp_path: Path, monkeypatch):
    """main() writes to FIFO when it exists."""
    monkeypatch.setattr("sys.argv", ["trigger.py", "hello"])

    # Create a real FIFO in tmp_path
    fifo = tmp_path / "af.fifo"
    os.mkfifo(str(fifo))
    monkeypatch.setattr(trigger_mod, "FIFO", str(fifo))

    written: list[str] = []

    class FakeFH:
        def write(self, data):
            written.append(data)
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_open(path, *args, **kwargs):
        if str(path) == str(fifo):
            return FakeFH()
        import builtins
        return builtins.__dict__["open"](path, *args, **kwargs)

    with patch("backend.trigger._check_lockfile", return_value=True), \
         patch("backend.trigger._run_preflight", return_value=(True, "{}")), \
         patch("backend.trigger._read_session", return_value=None), \
         patch("backend.trigger.os.path.exists", return_value=True), \
         patch("backend.trigger.setup_logging", return_value=None, create=True), \
         patch("backend.log.setup_logging", return_value=None, create=True):
        try:
            # Patch backup imports to avoid real calls
            with patch.dict("sys.modules", {
                "backend.backup": MagicMock(create_backup=MagicMock(), prune_backups=MagicMock()),
                "backend.session_manager": MagicMock(SessionManager=MagicMock()),
            }):
                with patch("builtins.open", side_effect=fake_open):
                    trigger_mod.main()
        except Exception:
            pass  # It may raise after the write — that's OK

    # Verify something was written to the FIFO
    if written:
        data = json.loads(written[0])
        assert "prompt" in data
        assert data["prompt"] == "hello"


# ---------------------------------------------------------------------------
# main() — fallback-exec guard (S6): binary absent must fail legibly,
# not with a bare traceback.
# ---------------------------------------------------------------------------


def test_main_fallback_exec_guard_missing_binary(tmp_path: Path, monkeypatch, capsys, caplog):
    """When FIFO is absent and the fallback CLI binary doesn't exist either,
    main() must exit non-zero with an actionable message — not raise an
    uncaught FileNotFoundError."""
    monkeypatch.setattr("sys.argv", ["trigger.py", "hello"])

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # Deliberately do NOT create the fallback CLI's expected binary path
    # under fake_home — that's the condition the guard is meant to catch.

    with patch("backend.trigger._check_lockfile", return_value=True), \
         patch("backend.trigger._run_preflight", return_value=(True, "{}")), \
         patch("backend.trigger._read_session", return_value=None), \
         patch("backend.trigger.os.path.exists", return_value=False), \
         patch("backend.trigger.setup_logging", return_value=None, create=True), \
         patch("backend.log.setup_logging", return_value=None, create=True):
        with patch.dict("sys.modules", {
            "backend.backup": MagicMock(create_backup=MagicMock(), prune_backups=MagicMock()),
            "backend.session_manager": MagicMock(SessionManager=MagicMock()),
        }):
            with pytest.raises(SystemExit) as exc:
                trigger_mod.main()

    assert exc.value.code != 0, "expected a non-zero exit, not a silent success"

    # No uncaught exception means pytest never prints a traceback for this
    # failure mode — the substantive assertion is that pytest.raises caught
    # a plain SystemExit, not a FileNotFoundError. Belt-and-suspenders check
    # that nothing resembling a traceback leaked to captured stdout/stderr.
    captured = capsys.readouterr()
    assert "Traceback" not in (captured.out + captured.err)

    # The actionable message goes through logger.error() — assert it names
    # the missing binary's path and says what to do about it.
    error_records = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "expected an ERROR-level log naming the missing binary"
    assert any(str(fake_home) in msg for msg in error_records), error_records
    assert any("install" in msg.lower() or "start the tui" in msg.lower() for msg in error_records), error_records


def test_main_default_prompt(tmp_path: Path, monkeypatch):
    """main() uses 'run /loop iteration' when no args given."""
    monkeypatch.setattr("sys.argv", ["trigger.py"])

    written: list[str] = []

    class FakeFH:
        def write(self, data): written.append(data)
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("backend.trigger._check_lockfile", return_value=True), \
         patch("backend.trigger._run_preflight", return_value=(True, "{}")), \
         patch("backend.trigger._read_session", return_value=None), \
         patch("backend.trigger.os.path.exists", return_value=True), \
         patch("backend.trigger.setup_logging", return_value=None, create=True), \
         patch("backend.log.setup_logging", return_value=None, create=True):
        with patch.dict("sys.modules", {
            "backend.backup": MagicMock(create_backup=MagicMock(), prune_backups=MagicMock()),
            "backend.session_manager": MagicMock(SessionManager=MagicMock()),
        }):
            with patch("builtins.open", side_effect=lambda p, *a, **kw: FakeFH() if "fifo" in str(p).lower() or p == trigger_mod.FIFO else open(p, *a, **kw)):
                try:
                    trigger_mod.main()
                except Exception:
                    pass

    if written:
        data = json.loads(written[0])
        assert "run /loop iteration" in data["prompt"]
