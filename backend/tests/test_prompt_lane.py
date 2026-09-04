"""
Tests for backend/prompt_lane/sdk_lane.py

Run with:
    python -m pytest backend/tests/test_prompt_lane.py -v

These are the event-mapper tests for the Claude Agent SDK prompt lane —
the direct replacement for the ~480 lines of AgentEvent mapper tests that
used to live in backend/tests/test_server.py (_emit_agent_event /
_agent_event_to_dict / _install_multiagent_proxy), now testing
sdk_lane.run_prompt's mapping from real claude_agent_sdk message types
onto the same wire protocol.

No real ClaudeSDKClient / subprocess is ever started — ClaudeSDKClient is
monkeypatched with a fake that yields a canned message sequence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TaskUsage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import backend.prompt_lane.sdk_lane as sdk_lane


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stream_event(*, session_id="sess-1", event, parent_tool_use_id=None) -> StreamEvent:
    return StreamEvent(uuid="u1", session_id=session_id, event=event, parent_tool_use_id=parent_tool_use_id)


def _task_usage() -> TaskUsage:
    return TaskUsage(total_tokens=0, tool_uses=0, duration_ms=0)


class _FakeSDKClient:
    """Stand-in for claude_agent_sdk.ClaudeSDKClient.

    Replays a canned message sequence from receive_response() and records
    connect/query/disconnect calls so tests can assert lifecycle behaviour.
    """

    #: Set by _install_fake_client per-test — the sequence to replay.
    messages: list = []
    #: Set by _install_fake_client per-test — raised mid-stream if not None.
    raise_after: Exception | None = None

    def __init__(self, options=None):
        self.options = options
        self.connect = AsyncMock(side_effect=self._connect)
        self.query = AsyncMock(side_effect=self._query)
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.connected = False
        self.disconnected = False
        self.queried_with = None

    async def _connect(self):
        self.connected = True

    async def _query(self, prompt):
        self.queried_with = prompt

    async def _disconnect(self):
        self.disconnected = True

    async def receive_response(self):
        for msg in type(self).messages:
            yield msg
        if type(self).raise_after is not None:
            raise type(self).raise_after


def _install_fake_client(monkeypatch, messages, raise_after=None):
    """Monkeypatch sdk_lane.ClaudeSDKClient with a fake yielding *messages*."""
    _FakeSDKClient.messages = messages
    _FakeSDKClient.raise_after = raise_after
    monkeypatch.setattr(sdk_lane, "ClaudeSDKClient", _FakeSDKClient)
    return _FakeSDKClient


# ---------------------------------------------------------------------------
# has_credential / build_options — credential precedence
# ---------------------------------------------------------------------------


def test_has_credential_true_when_detected(monkeypatch):
    monkeypatch.setattr(sdk_lane, "detect_sdk_credential", lambda: "api_key")
    assert sdk_lane.has_credential() is True


def test_has_credential_false_when_none(monkeypatch):
    monkeypatch.setattr(sdk_lane, "detect_sdk_credential", lambda: None)
    assert sdk_lane.has_credential() is False


def test_build_options_oauth_token_sets_env_and_neutralizes_api_key(monkeypatch):
    """OAuth path passes CLAUDE_CODE_OAUTH_TOKEN and zeroes a stray API key —
    same S1-sub precedence guard as agent_sdk_runner.py."""
    monkeypatch.setattr(sdk_lane, "detect_sdk_credential", lambda: "oauth_token")
    monkeypatch.setattr(sdk_lane, "_load_oauth_token", lambda: "tok-123")

    options = sdk_lane.build_options()

    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-123"
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == ""


def test_build_options_api_key_leaves_env_untouched(monkeypatch):
    """api_key / login / no credential: no env override — the claude CLI
    subprocess inherits ANTHROPIC_API_KEY from the parent process as-is."""
    monkeypatch.setattr(sdk_lane, "detect_sdk_credential", lambda: "api_key")

    options = sdk_lane.build_options()

    assert options.env == {}


def test_build_options_resume_and_model(monkeypatch):
    monkeypatch.setattr(sdk_lane, "detect_sdk_credential", lambda: None)
    monkeypatch.setenv("AF_MODEL", "claude-sonnet-4-5")

    options = sdk_lane.build_options(resume="sess-existing")

    assert options.resume == "sess-existing"
    assert options.model == "claude-sonnet-4-5"
    assert options.permission_mode == "bypassPermissions"
    assert options.include_partial_messages is True


def test_build_options_no_af_model_leaves_model_none(monkeypatch):
    monkeypatch.setattr(sdk_lane, "detect_sdk_credential", lambda: None)
    monkeypatch.delenv("AF_MODEL", raising=False)

    options = sdk_lane.build_options()

    assert options.model is None


def test_build_options_setting_sources_includes_project(monkeypatch):
    """D#1790: setting_sources must be explicit and include "project" —
    that's what loads .claude/settings.json (the PreToolUse sandbox hook)
    and is required for CLAUDE.md loading. Must not be left to the SDK's
    implicit default (None), and must not regress to a list that drops
    "project" — either would silently disable the hook and CLAUDE.md."""
    monkeypatch.setattr(sdk_lane, "detect_sdk_credential", lambda: None)

    options = sdk_lane.build_options()

    assert options.setting_sources is not None
    assert "project" in options.setting_sources


# ---------------------------------------------------------------------------
# _map_stream_event
# ---------------------------------------------------------------------------


def test_map_stream_event_text_delta():
    evt = _stream_event(event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}})
    result = sdk_lane._map_stream_event(evt)
    assert result == [{"type": "content", "content": "hi"}]


def test_map_stream_event_thinking_delta():
    evt = _stream_event(event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    result = sdk_lane._map_stream_event(evt)
    assert result == [{"type": "thinking", "content": "hmm"}]


def test_map_stream_event_other_delta_type_ignored():
    evt = _stream_event(event={"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}})
    assert sdk_lane._map_stream_event(evt) == []


def test_map_stream_event_non_delta_event_type_ignored():
    evt = _stream_event(event={"type": "message_start"})
    assert sdk_lane._map_stream_event(evt) == []


def test_map_stream_event_empty_event_dict():
    evt = _stream_event(event={})
    assert sdk_lane._map_stream_event(evt) == []


# ---------------------------------------------------------------------------
# _stringify_tool_result
# ---------------------------------------------------------------------------


def test_stringify_tool_result_none():
    assert sdk_lane._stringify_tool_result(None) == ""


def test_stringify_tool_result_string_passthrough():
    assert sdk_lane._stringify_tool_result("plain output") == "plain output"


def test_stringify_tool_result_list_of_text_blocks():
    content = [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]
    assert sdk_lane._stringify_tool_result(content) == "line one\nline two"


def test_stringify_tool_result_ignores_non_text_blocks():
    content = [{"type": "image", "source": {}}, {"type": "text", "text": "kept"}]
    assert sdk_lane._stringify_tool_result(content) == "kept"


# ---------------------------------------------------------------------------
# run_prompt — full mapping, one message type at a time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_prompt_maps_stream_event_content_delta(monkeypatch):
    _install_fake_client(monkeypatch, [
        _stream_event(event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                      session_id="sess-1", stop_reason=None, total_cost_usd=None, usage={"input_tokens": 1, "output_tokens": 1},
                      result="ok", structured_output=None, model_usage=None, permission_denials=None,
                      deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None),
    ])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    assert events[0] == {"type": "content", "content": "hi"}
    assert events[-1] == {"type": "done", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_run_prompt_maps_tool_use_and_tool_result(monkeypatch):
    assistant = AssistantMessage(
        content=[ToolUseBlock(id="call-1", name="Bash", input={"cmd": "ls"})],
        model="claude-x", parent_tool_use_id=None, error=None, usage=None,
        message_id="m1", stop_reason=None, session_id="sess-1", uuid=None,
    )
    user = UserMessage(
        content=[ToolResultBlock(tool_use_id="call-1", content="file.txt", is_error=False)],
        uuid=None, parent_tool_use_id=None, tool_use_result=None,
    )
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sess-1", stop_reason=None, total_cost_usd=None, usage={"input_tokens": 1, "output_tokens": 1},
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [assistant, user, result])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    tool_use = next(e for e in events if e["type"] == "tool_use")
    assert tool_use == {"type": "tool_use", "tool": "Bash", "call_id": "call-1", "input": {"cmd": "ls"}}

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result == {"type": "tool_result", "call_id": "call-1", "result": "file.txt", "is_error": False}


@pytest.mark.asyncio
async def test_run_prompt_assistant_error_field_emits_error_event(monkeypatch):
    assistant = AssistantMessage(
        content=[], model="claude-x", parent_tool_use_id=None, error="rate_limit",
        usage=None, message_id="m1", stop_reason=None, session_id="sess-1", uuid=None,
    )
    result = ResultMessage(subtype="error", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
                            session_id="sess-1", stop_reason="error", total_cost_usd=None, usage=None,
                            result="rate limited", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [assistant, result])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 2  # one from AssistantMessage.error, one from ResultMessage.is_error
    assert error_events[0]["error"] == "rate_limit"
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_run_prompt_wraps_subagent_events_by_parent_tool_use_id(monkeypatch):
    """Messages carrying parent_tool_use_id belong to a subagent's own stream
    (spawned via the Task tool) and must be wrapped as agent_event, not leaked
    into the top-level stream indistinguishable from agent-0's own output —
    this is what the deleted _install_multiagent_proxy monkey-patch used to do.

    task_id and parent_tool_use_id are deliberately DISTINCT values here
    (task-1 vs task-1-tool-use — the Task tool_use block's own call_id) —
    against the real SDK these are two different, uncorrelated identifiers.
    agent_spawn/agent_exit announce agent_id=task_id (the only id the TUI's
    appendToAgent recognizes); a subagent's own messages carry
    parent_tool_use_id=<the Task call_id> instead. If run_prompt wrapped
    child events under the raw parent_tool_use_id without resolving it back
    to task_id via TaskStartedMessage.tool_use_id, the TUI would silently
    drop them as belonging to an agent_id it never saw an agent_spawn for —
    a same-value test can't catch that; this one can."""
    started = TaskStartedMessage(subtype="task_started", data={}, task_id="task-1", description="review PR",
                                  uuid="u1", session_id="sess-1", tool_use_id="task-1-tool-use", task_type="code-reviewer")
    sub_stream_text = _stream_event(
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "sub says hi"}},
        parent_tool_use_id="task-1-tool-use",
    )
    sub_stream_thinking = _stream_event(
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "sub thinking"}},
        parent_tool_use_id="task-1-tool-use",
    )
    sub_assistant = AssistantMessage(
        content=[ToolUseBlock(id="sub-call-1", name="Read", input={"file": "x.py"})],
        model="claude-x", parent_tool_use_id="task-1-tool-use", error=None, usage=None,
        message_id="m-sub", stop_reason=None, session_id="sess-1", uuid=None,
    )
    sub_user = UserMessage(
        content=[ToolResultBlock(tool_use_id="sub-call-1", content="contents of x.py", is_error=False)],
        uuid=None, parent_tool_use_id="task-1-tool-use", tool_use_result=None,
    )
    # Top-level (agent-0) events with no parent_tool_use_id must still be unwrapped.
    top_stream_text = _stream_event(event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "top says hi"}})
    top_assistant = AssistantMessage(
        content=[ToolUseBlock(id="top-call-1", name="Bash", input={"cmd": "ls"})],
        model="claude-x", parent_tool_use_id=None, error=None, usage=None,
        message_id="m-top", stop_reason=None, session_id="sess-1", uuid=None,
    )
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sess-1", stop_reason=None, total_cost_usd=None, usage={"input_tokens": 1, "output_tokens": 1},
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [
        started, sub_stream_thinking, sub_stream_text, sub_assistant, sub_user,
        top_stream_text, top_assistant, result,
    ])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    # Nothing subagent-origin leaks into the top level: no top-level tool_use/
    # tool_result/content/thinking event carries the subagent's own call_id or text.
    top_level_types = {e["type"] for e in events}
    assert "tool_result" not in top_level_types  # only the subagent produced a tool_result
    top_level_tool_uses = [e for e in events if e["type"] == "tool_use"]
    assert len(top_level_tool_uses) == 1
    assert top_level_tool_uses[0]["call_id"] == "top-call-1"
    top_level_content = [e for e in events if e["type"] == "content"]
    assert len(top_level_content) == 1
    assert top_level_content[0]["content"] == "top says hi"
    assert not [e for e in events if e["type"] == "thinking"]  # the only thinking was the subagent's

    # Every subagent event arrives wrapped as agent_event, tagged with task_id
    # ("task-1") — NOT the raw parent_tool_use_id ("task-1-tool-use") the
    # messages actually carried. This is the id agent_spawn announced, and the
    # only one the TUI will render content under.
    agent_events = [e for e in events if e["type"] == "agent_event"]
    assert len(agent_events) == 4  # thinking, content, tool_use, tool_result
    assert all(e["agent_id"] == "task-1" for e in agent_events)
    assert not any(e["agent_id"] == "task-1-tool-use" for e in agent_events)

    spawn = next(e for e in events if e["type"] == "agent_spawn")
    assert spawn["agent_id"] == "task-1"

    inner_types = {e["inner"]["type"] for e in agent_events}
    assert inner_types == {"thinking", "content", "tool_use", "tool_result"}

    inner_tool_use = next(e["inner"] for e in agent_events if e["inner"]["type"] == "tool_use")
    assert inner_tool_use == {"type": "tool_use", "tool": "Read", "call_id": "sub-call-1", "input": {"file": "x.py"}}

    inner_tool_result = next(e["inner"] for e in agent_events if e["inner"]["type"] == "tool_result")
    assert inner_tool_result == {"type": "tool_result", "call_id": "sub-call-1", "result": "contents of x.py", "is_error": False}

    inner_content = next(e["inner"] for e in agent_events if e["inner"]["type"] == "content")
    assert inner_content == {"type": "content", "content": "sub says hi"}

    inner_thinking = next(e["inner"] for e in agent_events if e["inner"]["type"] == "thinking")
    assert inner_thinking == {"type": "thinking", "content": "sub thinking"}


@pytest.mark.asyncio
async def test_run_prompt_subagent_lifecycle_started_progress_notification(monkeypatch):
    started = TaskStartedMessage(subtype="task_started", data={}, task_id="task-1", description="review PR",
                                  uuid="u1", session_id="sess-1", tool_use_id=None, task_type="code-reviewer")
    progress = TaskProgressMessage(subtype="task_progress", data={}, task_id="task-1", description="review PR",
                                    usage=_task_usage(), uuid="u2", session_id="sess-1", tool_use_id=None,
                                    last_tool_name="Read")
    notification = TaskNotificationMessage(subtype="task_notification", data={}, task_id="task-1", status="completed",
                                            output_file="", summary="done", uuid="u3", session_id="sess-1",
                                            tool_use_id=None, usage=None)
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sess-1", stop_reason=None, total_cost_usd=None, usage={"input_tokens": 1, "output_tokens": 1},
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [started, progress, notification, result])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    spawn = next(e for e in events if e["type"] == "agent_spawn")
    assert spawn == {"type": "agent_spawn", "agent_id": "task-1", "agent_name": "code-reviewer", "parent_id": "agent-0"}

    agent_event = next(e for e in events if e["type"] == "agent_event")
    assert agent_event["agent_id"] == "task-1"
    assert agent_event["inner"]["type"] == "tool_use"
    assert agent_event["inner"]["tool"] == "Read"

    exit_evt = next(e for e in events if e["type"] == "agent_exit")
    assert exit_evt == {"type": "agent_exit", "agent_id": "task-1", "exit_code": 0}


@pytest.mark.asyncio
async def test_run_prompt_subagent_failed_notification_exit_code_1(monkeypatch):
    started = TaskStartedMessage(subtype="task_started", data={}, task_id="task-2", description="broken task",
                                  uuid="u1", session_id="sess-1", tool_use_id=None, task_type=None)
    notification = TaskNotificationMessage(subtype="task_notification", data={}, task_id="task-2", status="failed",
                                            output_file="", summary="oops", uuid="u3", session_id="sess-1",
                                            tool_use_id=None, usage=None)
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sess-1", stop_reason=None, total_cost_usd=None, usage=None,
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [started, notification, result])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    exit_evt = next(e for e in events if e["type"] == "agent_exit")
    assert exit_evt["exit_code"] == 1
    # agent_name falls back to description when task_type is absent.
    spawn = next(e for e in events if e["type"] == "agent_spawn")
    assert spawn["agent_name"] == "broken task"


@pytest.mark.asyncio
async def test_run_prompt_task_updated_terminal_status_emits_exit_once(monkeypatch):
    """A TaskUpdatedMessage with a terminal status closes out a task that never
    got an explicit TaskNotificationMessage — and only once."""
    started = TaskStartedMessage(subtype="task_started", data={}, task_id="task-3", description="lint",
                                  uuid="u1", session_id="sess-1", tool_use_id=None, task_type="quality-sweep")
    updated = TaskUpdatedMessage(subtype="task_updated", data={}, task_id="task-3", patch={"status": "completed"},
                                  status="completed", session_id="sess-1", uuid="u4")
    # A second terminal update for the same task must not emit a second agent_exit.
    updated_again = TaskUpdatedMessage(subtype="task_updated", data={}, task_id="task-3", patch={"status": "completed"},
                                        status="completed", session_id="sess-1", uuid="u5")
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sess-1", stop_reason=None, total_cost_usd=None, usage=None,
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [started, updated, updated_again, result])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    exit_events = [e for e in events if e["type"] == "agent_exit"]
    assert len(exit_events) == 1
    assert exit_events[0]["exit_code"] == 0


@pytest.mark.asyncio
async def test_run_prompt_notification_for_unknown_task_id_ignored(monkeypatch):
    """A TaskNotificationMessage for a task_id that was never opened (e.g. lost
    the TaskStartedMessage) does not emit a spurious agent_exit."""
    notification = TaskNotificationMessage(subtype="task_notification", data={}, task_id="ghost-task", status="completed",
                                            output_file="", summary="?", uuid="u3", session_id="sess-1",
                                            tool_use_id=None, usage=None)
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sess-1", stop_reason=None, total_cost_usd=None, usage=None,
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [notification, result])

    events = [e async for e in sdk_lane.run_prompt("hello")]

    assert not [e for e in events if e["type"] == "agent_exit"]


# ---------------------------------------------------------------------------
# run_prompt — session id resolution and client lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_prompt_resolves_session_id_from_result_when_none_given(monkeypatch):
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sdk-assigned-id", stop_reason=None, total_cost_usd=None, usage=None,
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [result])

    events = [e async for e in sdk_lane.run_prompt("hello", session_id=None)]

    assert events[-1] == {"type": "done", "session_id": "sdk-assigned-id"}


@pytest.mark.asyncio
async def test_run_prompt_connects_queries_and_disconnects(monkeypatch):
    result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="sess-1", stop_reason=None, total_cost_usd=None, usage=None,
                            result="ok", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [result])

    created: list[_FakeSDKClient] = []
    orig_init = _FakeSDKClient.__init__

    def _tracking_init(self, options=None):
        orig_init(self, options)
        created.append(self)

    monkeypatch.setattr(_FakeSDKClient, "__init__", _tracking_init)

    [e async for e in sdk_lane.run_prompt("do the thing", session_id="existing")]

    assert len(created) == 1
    instance = created[0]
    assert instance.connected is True
    assert instance.queried_with == "do the thing"
    assert instance.disconnected is True
    assert instance.options.resume == "existing"


@pytest.mark.asyncio
async def test_run_prompt_disconnects_even_when_lane_raises(monkeypatch):
    """If receive_response() raises mid-stream, disconnect() still runs and the
    exception propagates to the caller (backend/server.py's _handle_request
    catches it and emits an error event)."""
    _install_fake_client(
        monkeypatch,
        [StreamEvent(uuid="u1", session_id="sess-1", event={"type": "message_start"}, parent_tool_use_id=None)],
        raise_after=RuntimeError("subprocess died"),
    )

    disconnected = {}
    orig_init = _FakeSDKClient.__init__

    def _tracking_init(self, options=None):
        orig_init(self, options)
        disconnected["instance"] = self

    monkeypatch.setattr(_FakeSDKClient, "__init__", _tracking_init)

    with pytest.raises(RuntimeError, match="subprocess died"):
        async for _ in sdk_lane.run_prompt("hello"):
            pass

    assert disconnected["instance"].disconnected is True


# ---------------------------------------------------------------------------
# run_prompt — wire protocol type-value invariant (Spec item 6)
# ---------------------------------------------------------------------------

_ALLOWED_WIRE_TYPES = {
    "ready", "thinking", "content", "tool_use", "tool_result", "usage",
    "done", "error", "agent_spawn", "agent_event", "agent_exit",
}


@pytest.mark.asyncio
async def test_run_prompt_only_emits_allowed_wire_types(monkeypatch):
    """Every event type run_prompt can yield across a realistic mixed sequence
    is one of the 11 wire-protocol type values — no new type is introduced."""
    assistant = AssistantMessage(
        content=[ToolUseBlock(id="call-1", name="Bash", input={"cmd": "ls"})],
        model="claude-x", parent_tool_use_id=None, error="rate_limit", usage=None,
        message_id="m1", stop_reason=None, session_id="sess-1", uuid=None,
    )
    user = UserMessage(
        content=[ToolResultBlock(tool_use_id="call-1", content="file.txt", is_error=False)],
        uuid=None, parent_tool_use_id=None, tool_use_result=None,
    )
    started = TaskStartedMessage(subtype="task_started", data={}, task_id="task-1", description="review",
                                  uuid="u1", session_id="sess-1", tool_use_id=None, task_type="code-reviewer")
    progress = TaskProgressMessage(subtype="task_progress", data={}, task_id="task-1", description="review",
                                    usage=_task_usage(), uuid="u2", session_id="sess-1", tool_use_id=None,
                                    last_tool_name="Read")
    notification = TaskNotificationMessage(subtype="task_notification", data={}, task_id="task-1", status="completed",
                                            output_file="", summary="done", uuid="u3", session_id="sess-1",
                                            tool_use_id=None, usage=None)
    stream_text = _stream_event(event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}})
    stream_thinking = _stream_event(event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    result = ResultMessage(subtype="error", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
                            session_id="sess-1", stop_reason="error", total_cost_usd=None, usage={"input_tokens": 1, "output_tokens": 1},
                            result="failed", structured_output=None, model_usage=None, permission_denials=None,
                            deferred_tool_use=None, errors=None, api_error_status=None, uuid=None, terminal_reason=None)

    _install_fake_client(monkeypatch, [
        stream_thinking, stream_text, started, progress, assistant, user, notification, result,
    ])

    events = [e async for e in sdk_lane.run_prompt("hello")]
    types_emitted = {e["type"] for e in events}

    assert types_emitted <= _ALLOWED_WIRE_TYPES
    # Sanity: this fixture really did exercise most of the non-"ready" types
    # ("ready" is emitted by backend/server.py's _main, not by run_prompt).
    assert {"thinking", "content", "tool_use", "tool_result", "usage", "done",
            "error", "agent_spawn", "agent_event", "agent_exit"} <= types_emitted
