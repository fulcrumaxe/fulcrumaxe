"""
Tests for backend/prompt_lane/muse_lane.py

Run with:
    python -m pytest backend/tests/test_muse_lane.py -v

Mapper + lifecycle tests for the Muse Code CLI prompt lane. No real
``muse`` subprocess is ever started except in the echo-provider test,
which is skipped when the binary is missing (echo is free and
deterministic, so it is safe where the binary exists).
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

import backend.claude_spawn_tracker as spawn_tracker
import backend.prompt_lane.muse_lane as muse_lane
from backend.blackboard import Blackboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(payload_type: str, payload: dict, session_id: str = "sess-1") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "stream": {"kind": "session", "id": session_id},
            "payload_type": payload_type,
            "payload": payload,
        }
    )


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode() for line in lines]

    async def __aiter__(self):  # noqa: ANN204
        for line in self._lines:
            yield line


class _FakeStderr:
    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


def _install_fake_exec(monkeypatch: pytest.MonkeyPatch, lines: list[str], returncode: int = 0) -> dict:
    """Patch asyncio.create_subprocess_exec; returns captured argv dict."""
    captured: dict = {}

    async def _fake(*argv: str, **kwargs):  # noqa: ANN202, ANN003
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return _FakeProc(lines, returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    return captured


def _no_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUSE_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)


# ---------------------------------------------------------------------------
# should_use / resolve_binary / has_credential
# ---------------------------------------------------------------------------


def test_should_use_explicit_muse(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binaries(monkeypatch)
    monkeypatch.setenv("AF_LANE", "muse")
    assert muse_lane.should_use() is True


def test_should_use_explicit_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binaries(monkeypatch)
    monkeypatch.setenv("AF_LANE", "claude")
    assert muse_lane.should_use() is False


def test_should_use_auto_prefers_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AF_LANE", raising=False)
    monkeypatch.delenv("MUSE_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/bin/{cmd}")
    assert muse_lane.should_use() is False


def test_should_use_auto_falls_to_muse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AF_LANE", raising=False)
    monkeypatch.delenv("MUSE_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/bin/muse" if cmd == "muse" else None)
    assert muse_lane.should_use() is True


def test_should_use_auto_no_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binaries(monkeypatch)
    monkeypatch.delenv("AF_LANE", raising=False)
    assert muse_lane.should_use() is False


def test_resolve_binary_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    assert muse_lane.resolve_binary() == "/custom/muse"


def test_has_credential_false_without_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binaries(monkeypatch)
    monkeypatch.delenv("AF_MUSE_PROVIDER", raising=False)
    assert muse_lane.has_credential() is False


def test_has_credential_echo_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    monkeypatch.setenv("AF_MUSE_PROVIDER", "echo")
    assert muse_lane.has_credential() is True


def test_has_credential_auth_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    monkeypatch.delenv("AF_MUSE_PROVIDER", raising=False)
    auth_dir = tmp_path / "muse"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text("{}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert muse_lane.has_credential() is True


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------


def test_build_command_headless_flags(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    for var in ("AF_MODEL", "AF_REASONING_EFFORT", "AF_MAX_MODEL_STEPS", "AF_MUSE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    cmd = muse_lane.build_command(prompt_file="/tmp/p.md", workspace=str(tmp_path))
    assert cmd[:6] == ["/custom/muse", "exec", "--json", "--prompt-file", "/tmp/p.md", "--workspace"]
    assert "--approval-mode" in cmd and "never" in cmd
    assert "--disable-sandbox" in cmd
    assert "--trust-workspace" in cmd
    assert "--session-id" not in cmd


def test_build_command_session_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    monkeypatch.setenv("AF_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("AF_MAX_MODEL_STEPS", "25")
    monkeypatch.delenv("AF_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("AF_MUSE_PROVIDER", raising=False)
    cmd = muse_lane.build_command(prompt_file="/tmp/p.md", session_id="sess-9")
    assert "--session-id" in cmd and "sess-9" in cmd
    assert "--model" in cmd and "muse-spark-1.3" in cmd
    assert "--max-model-steps" in cmd and "25" in cmd


def test_build_command_no_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_binaries(monkeypatch)
    with pytest.raises(RuntimeError, match="muse binary"):
        muse_lane.build_command(prompt_file="/tmp/p.md")


# ---------------------------------------------------------------------------
# _map_envelope
# ---------------------------------------------------------------------------


def test_map_content_delta() -> None:
    [evt] = muse_lane._map_envelope(json.loads(_envelope("run.output.delta", {"text": "hello"})))
    assert evt == {"type": "content", "content": "hello"}


def test_map_tool_proposal() -> None:
    [evt] = muse_lane._map_envelope(
        json.loads(_envelope("task.lifecycle.proposed", {"event": {"task_kind": "tool.bash", "task_id": "t1"}}))
    )
    assert evt == {"type": "tool_use", "tool": "bash", "call_id": "t1", "input": {}}


def test_map_tool_proposal_ignores_non_tools() -> None:
    assert muse_lane._map_envelope(
        json.loads(_envelope("task.lifecycle.proposed", {"event": {"task_kind": "model.meta.response"}}))
    ) == []


def test_map_tool_result() -> None:
    [evt] = muse_lane._map_envelope(
        json.loads(
            _envelope(
                "tool.result",
                {"call_id": "c1", "text": "PROBE-OK", "correlation_facts": {"outcome": "success"}},
            )
        )
    )
    assert evt == {"type": "tool_result", "call_id": "c1", "result": "PROBE-OK", "is_error": False}


def test_map_tool_result_error() -> None:
    [evt] = muse_lane._map_envelope(
        json.loads(
            _envelope(
                "tool.result",
                {"call_id": "c2", "text": "boom", "correlation_facts": {"outcome": "failed"}},
            )
        )
    )
    assert evt["is_error"] is True


def test_map_usage_sums_models() -> None:
    [evt] = muse_lane._map_envelope(
        json.loads(
            _envelope(
                "run.usage",
                {"models": [{"input_tokens": 100, "output_tokens": 10}, {"input_tokens": 50, "output_tokens": 5}]},
            )
        )
    )
    assert evt == {"type": "usage", "usage": {"input_tokens": 150, "output_tokens": 15}}


def test_map_terminal_completed() -> None:
    [evt] = muse_lane._map_envelope(json.loads(_envelope("run.terminal.completed", {"text": "final"})))
    assert evt["type"] == "done"


def test_map_terminal_failed() -> None:
    evts = muse_lane._map_envelope(json.loads(_envelope("run.terminal.failed", {"text": "bad"})))
    assert [e["type"] for e in evts] == ["error", "done"]
    assert evts[0]["error"] == "bad"


def test_map_unknown_ignored() -> None:
    assert muse_lane._map_envelope(json.loads(_envelope("task.lifecycle.accepted", {}))) == []


# ---------------------------------------------------------------------------
# run_prompt with faked subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_prompt_full_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    for var in ("AF_MODEL", "AF_REASONING_EFFORT", "AF_MAX_MODEL_STEPS", "AF_MUSE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    lines = [
        _envelope("run.output.delta", {"text": "working"}, session_id="sess-7"),
        _envelope(
            "task.lifecycle.proposed", {"event": {"task_kind": "tool.bash", "task_id": "t1"}}, session_id="sess-7"
        ),
        _envelope(
            "tool.result",
            {"call_id": "c1", "text": "ok", "correlation_facts": {"outcome": "success"}},
            session_id="sess-7",
        ),
        _envelope("run.usage", {"models": [{"input_tokens": 10, "output_tokens": 2}]}, session_id="sess-7"),
        _envelope("run.terminal.completed", {"text": "final"}, session_id="sess-7"),
        "not json {{{",
    ]
    captured = _install_fake_exec(monkeypatch, lines)
    events = [evt async for evt in muse_lane.run_prompt("do it")]
    assert [e["type"] for e in events] == ["content", "tool_use", "tool_result", "usage", "done"]
    assert events[-1]["session_id"] == "sess-7"
    assert captured["argv"][0] == "/custom/muse"
    assert "--prompt-file" in captured["argv"]


@pytest.mark.asyncio
async def test_run_prompt_no_terminal_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    lines = [_envelope("run.output.delta", {"text": "partial"})]
    _install_fake_exec(monkeypatch, lines, returncode=1)
    events: list[dict] = []
    with pytest.raises(RuntimeError, match="without a terminal event"):
        async for evt in muse_lane.run_prompt("do it"):
            events.append(evt)
    assert [e["type"] for e in events] == ["content"]


@pytest.mark.asyncio
async def test_run_prompt_failed_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    lines = [_envelope("run.terminal.failed", {"text": "kaput"})]
    _install_fake_exec(monkeypatch, lines)
    events = [evt async for evt in muse_lane.run_prompt("do it")]
    assert [e["type"] for e in events] == ["error", "done"]
    assert events[0]["error"] == "kaput"


# ---------------------------------------------------------------------------
# Live echo-provider smoke (skipped without the binary)
# ---------------------------------------------------------------------------

muse_binary = shutil.which("muse")


@pytest.fixture(autouse=True)
def _scratch_state(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    # run_prompt records every spawn with claude_spawn_tracker (blackboard
    # writes); keep all of it in scratch so the state-paths pytest guard
    # never trips and real state is untouched.
    monkeypatch.setenv(
        "AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path_factory.mktemp("muse-lane-state"))
    )
    # claude_spawn_tracker holds a module-level Blackboard() singleton whose
    # root froze at import time (D#1810 pattern); in the full suite that root
    # points at another test's leaked dir with a tripped breaker, so every
    # run_prompt test fails with SpawnBlocked. Rebind after setenv so record()
    # honors this test's scratch dir. monkeypatch restores the original after.
    monkeypatch.setattr(spawn_tracker, "_bb", Blackboard())


@pytest.mark.asyncio
@pytest.mark.skipif(muse_binary is None, reason="muse binary not available")
async def test_run_prompt_echo_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_MUSE_PROVIDER", "echo")
    monkeypatch.setenv("AF_MAX_MODEL_STEPS", "3")
    events = [evt async for evt in muse_lane.run_prompt("Reply with exactly: ECHO-OK")]
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert events[-1]["session_id"]
    assert "content" in types


# ---------------------------------------------------------------------------
# Subagent frames (spawn-accept -> agent_spawn, wait-ready -> agent_exit)
# ---------------------------------------------------------------------------


def _tool_result_line(text: str, outcome: str = "success", call_id: str = "c1") -> str:
    return _envelope(
        "tool.result",
        {"call_id": call_id, "text": text, "correlation_facts": {"outcome": outcome}},
    )


_SPAWN_ACCEPT = json.dumps(
    {
        "status": "accepted",
        "subagent_id": "sub-1",
        "work_id": "work.v2.native_subagent.sha256.abc",
        "agent_path": "main/test child/1",
        "task_ref": "task/abc#0",
    }
)

_WAIT_READY = json.dumps(
    {
        "status": "ready",
        "subagent_id": "sub-1",
        "task_ref": "task/abc#5",
        "summary": "PONG",
        "workspace": {"placement": "shared"},
        "evidence_refs": [],
    }
)

_SEND_ACCEPT = json.dumps(
    {"status": "accepted", "subagent_id": "sub-1", "summary": "message queued through #1"}
)


def _close(lines: list[str]) -> list[str]:
    return [*lines, _envelope("run.terminal.completed", {"text": "final"})]


@pytest.mark.asyncio
async def test_run_prompt_spawn_accept_emits_agent_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    _install_fake_exec(monkeypatch, _close([_tool_result_line(_SPAWN_ACCEPT)]))
    events = [evt async for evt in muse_lane.run_prompt("spawn it")]
    assert [e["type"] for e in events] == ["tool_result", "agent_spawn", "done"]
    spawn = events[1]
    assert spawn["agent_id"] == "sub-1"
    assert spawn["agent_name"] == "test child"
    assert spawn["parent_id"] == "agent-0"


@pytest.mark.asyncio
async def test_run_prompt_wait_ready_emits_event_and_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    lines = _close([_tool_result_line(_SPAWN_ACCEPT), _tool_result_line(_WAIT_READY, call_id="c2")])
    _install_fake_exec(monkeypatch, lines)
    events = [evt async for evt in muse_lane.run_prompt("spawn and wait")]
    assert [e["type"] for e in events] == ["tool_result", "agent_spawn", "tool_result", "agent_event", "agent_exit", "done"]
    agent_evt = events[3]
    assert agent_evt["agent_id"] == "sub-1"
    assert agent_evt["inner"] == {"type": "content", "content": "PONG"}
    assert events[4] == {"type": "agent_exit", "agent_id": "sub-1", "exit_code": 0}


@pytest.mark.asyncio
async def test_run_prompt_send_message_no_duplicate_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    lines = _close([_tool_result_line(_SPAWN_ACCEPT), _tool_result_line(_SEND_ACCEPT, call_id="c2")])
    _install_fake_exec(monkeypatch, lines)
    events = [evt async for evt in muse_lane.run_prompt("spawn and follow up")]
    assert [e["type"] for e in events].count("agent_spawn") == 1


@pytest.mark.asyncio
async def test_run_prompt_wait_unknown_id_no_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    _install_fake_exec(monkeypatch, _close([_tool_result_line(_WAIT_READY)]))
    events = [evt async for evt in muse_lane.run_prompt("wait with no spawn")]
    assert "agent_exit" not in [e["type"] for e in events]


@pytest.mark.asyncio
async def test_run_prompt_wait_failed_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    failed = json.dumps({"status": "ready", "subagent_id": "sub-1", "summary": "", "errorKind": "timeout"})
    lines = _close([_tool_result_line(_SPAWN_ACCEPT), _tool_result_line(failed, outcome="failed", call_id="c2")])
    _install_fake_exec(monkeypatch, lines)
    events = [evt async for evt in muse_lane.run_prompt("spawn and fail")]
    assert events[-2] == {"type": "agent_exit", "agent_id": "sub-1", "exit_code": 1}


# ---------------------------------------------------------------------------
# Typed roles (--agents overlay compiled from .claude/agents cards)
# ---------------------------------------------------------------------------


def test_build_agents_overlay_from_cards(tmp_path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "executor.md").write_text(
        "---\ndescription: Executor role\nmodel: sonnet\n---\n\n# Executor\n\nDo the work.\n"
    )
    (agents / "reviewer.md").write_text("# Reviewer\n\nReview the work.\n")
    overlay = muse_lane.build_agents_overlay(roles=["executor", "reviewer"], agents_dir=str(agents))
    assert overlay is not None
    data = json.loads(overlay)
    assert data["executor"]["description"] == "Executor role"
    assert "Do the work." in data["executor"]["prompt"]
    assert "model: sonnet" not in data["executor"]["prompt"]
    assert data["reviewer"]["description"] != ""
    assert set(data["executor"].keys()) == {"description", "prompt"}


def test_build_agents_overlay_missing_dir() -> None:
    assert muse_lane.build_agents_overlay(roles=["executor"], agents_dir="/nonexistent") is None


def test_build_command_includes_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    cmd = muse_lane.build_command(prompt_file="/tmp/x.md", agents_overlay='{"executor": {"description": "x", "prompt": "y"}}')
    assert "--agents" in cmd
    assert cmd[cmd.index("--agents") + 1].startswith('{"executor"')


def test_build_command_omits_overlay_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    assert "--agents" not in muse_lane.build_command(prompt_file="/tmp/x.md")


def test_core_overlay_fits_arg_budget() -> None:
    overlay = muse_lane.build_agents_overlay(roles=muse_lane.CORE_SPAWN_ROLES)
    assert overlay is not None
    assert len(overlay.encode("utf-8")) <= 100_000
    assert set(json.loads(overlay).keys()) == set(muse_lane.CORE_SPAWN_ROLES)


def test_build_command_rejects_oversize_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSE_BIN", "/custom/muse")
    with pytest.raises(ValueError, match="single-arg budget"):
        muse_lane.build_command(prompt_file="/tmp/x.md", agents_overlay="x" * 100_001)
