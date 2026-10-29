"""Tests for Muse-lane spawn accounting + the lane-aware loop runner.

- muse_lane.run_prompt records every `muse exec` spawn with
  claude_spawn_tracker (source="muse_lane") and aborts before Popen when the
  breaker trips — the same contract backend/api.py honors for `claude -p`.
- backend/api._start_loop_run execs `muse exec` (headless flags mirroring
  muse_lane.build_command) when AF_LANE=muse, else keeps `claude -p`.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import backend.prompt_lane.muse_lane as muse_lane  # noqa: E402
from backend.claude_spawn_tracker import SpawnBlocked  # noqa: E402
from backend.spawn_guard import AcquireResult, AcquireStatus  # noqa: E402


def _ok_guard():
    guard = MagicMock()
    guard.acquire.return_value = AcquireResult(status=AcquireStatus.OK, source="t")
    guard.release.return_value = None
    return guard


def _drained_popen():
    proc = MagicMock()
    proc.pid = 4242
    proc.stdout.readline.side_effect = ["hello\n", ""]
    proc.wait.return_value = 0
    proc.returncode = 0
    return proc


def _wait_done(run, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run.get("status") != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("loop-run worker did not finish")


@pytest.mark.asyncio
async def test_lane_records_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")

    async def _lines():
        yield b'{"stream": {"kind": "session", "id": "s"}, "payload_type": "run.terminal.completed", "payload": {}}\n'

    async def _fake(*argv, **kwargs):
        proc = MagicMock()
        proc.stdout = _lines()

        async def _read():
            return b""

        proc.stderr.read = _read

        async def _wait():
            return 0

        proc.wait = _wait
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    with patch("backend.claude_spawn_tracker.record") as mock_record:
        events = [evt async for evt in muse_lane.run_prompt("hi")]
    assert events[-1]["type"] == "done"
    mock_record.assert_called_once_with(source="muse_lane")


@pytest.mark.asyncio
async def test_lane_spawn_blocked_aborts_before_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    started = []

    async def _fake(*argv, **kwargs):
        started.append(argv)
        raise AssertionError("must not spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    with patch("backend.claude_spawn_tracker.record", side_effect=SpawnBlocked("tripped")):
        with pytest.raises(SpawnBlocked):
            async for _evt in muse_lane.run_prompt("hi"):
                pass
    assert started == []


def test_loop_runner_uses_muse_when_lane_muse(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LANE", "muse")
    import backend.api as api_mod
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    proc = _drained_popen()
    with (
        patch.object(api_mod, "_spawn_guard", _ok_guard()),
        patch("subprocess.Popen", return_value=proc) as mock_popen,
        patch("backend.claude_spawn_tracker.record"),
        patch("shutil.which", return_value="/custom/muse"),
    ):
        run = api_mod._start_loop_run("do the loop", source="loop_run_test")
        _wait_done(run)
    worker_calls = [c[0][0] for c in mock_popen.call_args_list if "/custom/muse" in c[0][0]]
    assert len(worker_calls) == 1
    argv = worker_calls[0]
    assert argv[0] == "/custom/muse"
    assert argv[1] == "exec"
    assert "--prompt-file" in argv
    assert "--approval-mode" in argv and "--disable-sandbox" in argv
    prompt_path = argv[argv.index("--prompt-file") + 1]
    assert Path(prompt_path).read_text() == "do the loop"
    assert run["status"] == "done"


def test_loop_runner_keeps_claude_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AF_LANE", raising=False)
    import backend.api as api_mod
    proc = _drained_popen()
    with (
        patch.object(api_mod, "_spawn_guard", _ok_guard()),
        patch("subprocess.Popen", return_value=proc) as mock_popen,
        patch("backend.claude_spawn_tracker.record"),
        patch("shutil.which", return_value="/bin/claude"),
    ):
        run = api_mod._start_loop_run("do the loop", source="loop_run_test")
        _wait_done(run)
    worker_calls = [c[0][0] for c in mock_popen.call_args_list if "do the loop" in c[0][0]]
    assert len(worker_calls) == 1
    argv = worker_calls[0]
    assert argv[:3] == ["claude", "-p", "do the loop"]
