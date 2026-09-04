"""backend/prompt_lane/sdk_lane.py — Claude Agent SDK streaming lane.

Owns the ClaudeSDKClient session lifecycle for one prompt-lane request and
maps SDK messages onto the wire protocol backend/server.py has always
streamed over stdout (see that module's docstring for the protocol). This
replaces the previous prompt lane's Agent / SessionService / MessageService
stack and the ``_install_multiagent_proxy`` monkey-patch that used to shadow
AgentTool.forward_subagent_events.

One ClaudeSDKClient is created per inbound request (connect -> query ->
receive_response -> disconnect) rather than one long-lived client shared
across requests. That mirrors the old shape — one Agent instance,
many concurrent ``agent.run()`` calls dispatched as asyncio tasks — without
needing a shared mutable session store: conversation continuity comes from
``ClaudeAgentOptions.resume`` (the SDK's own session store), not from an
in-process object shared between concurrent tasks.

Credential detection is intentionally NOT re-derived here — it is imported
from backend/orchestrator/agent_sdk_runner.py, which already implements the
precedence rules (CLAUDE_CODE_OAUTH_TOKEN > ANTHROPIC_API_KEY > stored CLI
login) and the env-neutralization gotcha (options.env merges OVER
os.environ, so a stray ANTHROPIC_API_KEY needs to be zeroed out when the
OAuth token path is taken).

Subagent events are routed by ``parent_tool_use_id``: AssistantMessage,
UserMessage, and StreamEvent all carry it, set to the Task tool_use block's
own call_id. That id is NOT the same as TaskStartedMessage.task_id — the id
agent_spawn/agent_exit use as ``agent_id``, and the only id the TUI has
ever announced an agent under (tui/src/index.tsx's appendToAgent drops any
event whose agent_id it never saw an agent_spawn for). The two only meet
through TaskStartedMessage.tool_use_id, a third field carrying the Task
tool_use's call_id alongside its own task_id — so run_prompt records
tool_use_id -> task_id when a task starts and looks child events'
parent_tool_use_id up through that map before wrapping them as
``{"type": "agent_event", "agent_id": task_id, "inner": evt}``. This
replaces what ``_install_multiagent_proxy`` used to do by wrapping
AgentTool's generator. Without the id correlation, subagent tool_use/
tool_result/content/thinking would either leak into agent-0's own output,
or (naively wrapped under the wrong id) render as an empty stub in the TUI.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from backend.orchestrator.agent_sdk_runner import _load_oauth_token, detect_sdk_credential

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Task statuses that close out a subagent's lifecycle (-> agent_exit).
_TERMINAL_TASK_STATUSES = {"completed", "failed", "killed", "stopped"}


def has_credential() -> bool:
    """True if any Claude Agent SDK credential is available.

    One of: CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or a stored
    ``claude`` CLI login. Used by backend/server.py's startup check —
    server.py never inspects credential kinds itself.
    """
    return detect_sdk_credential() is not None


def build_options(*, resume: str | None = None) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for one prompt-lane request.

    resume: an existing SDK session ID to continue, or None to start a new
    session (the SDK assigns a fresh session ID, surfaced back to the caller
    via ResultMessage.session_id).
    """
    sdk_env: dict[str, str] = {}
    if detect_sdk_credential() == "oauth_token":
        token = _load_oauth_token()
        if token:
            sdk_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        # Neutralize a stray ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN so it
        # can't silently outrank the OAuth token in the SDK's
        # options.env-over-os.environ merge (same guard as
        # agent_sdk_runner.py's S1-sub).
        sdk_env["ANTHROPIC_API_KEY"] = ""
        sdk_env["ANTHROPIC_AUTH_TOKEN"] = ""
    # api_key / login / no credential: leave env untouched. The claude CLI
    # subprocess inherits ANTHROPIC_API_KEY from the parent process as-is,
    # or falls back to its own stored login.

    return ClaudeAgentOptions(
        env=sdk_env,
        include_partial_messages=True,
        resume=resume,
        model=os.environ.get("AF_MODEL") or None,
        cwd=str(_REPO_ROOT),
        # No human is present to approve tool use in this lane — it is
        # driven by stdin/FIFO from a cron-triggered loop, same as the
        # previous prompt lane's builtin-tools lane it replaces.
        permission_mode="bypassPermissions",
        # Explicit on purpose (D#1790): "project" loads .claude/settings.json,
        # which is where the PreToolUse sandbox hook is registered, AND is
        # required to load CLAUDE.md files (SDK docstring: 'Must include
        # "project" to load CLAUDE.md files'). Leaving this unset means the
        # SDK's implicit default decides both — currently None (load
        # everything) at 0.2.126, but that's an implicit default, not a
        # contract, and a future SDK version could change it with no
        # reviewable diff on our side. Do NOT delete this as "redundant" with
        # the current default; that's exactly the failure mode it closes.
        # "local" is deliberately omitted: .claude/settings.local.json is
        # untracked (repo-local, gitignored) and unreviewable — anyone can
        # drop one in without it ever appearing in a diff. The CLI default
        # this replaces did load local scope; excluding it here is an
        # intentional narrowing, not an oversight, so don't "fix" this back
        # to including "local" without addressing the untracked-and-
        # unreviewable problem first.
        setting_sources=["user", "project"],
    )


async def run_prompt(prompt: str, session_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
    """Run one prompt through the Claude Agent SDK.

    Yields wire-protocol event dicts (without the "id" field — the caller
    adds that before writing to stdout). Always ends with exactly one
    ``done`` event carrying the resolved session_id, unless the SDK itself
    raises — in that case the exception propagates and the caller is
    responsible for emitting an ``error`` event.
    """
    options = build_options(resume=session_id)
    resolved_session_id = session_id
    open_task_ids: set[str] = set()
    # Task tool_use call_id -> task_id, recorded from TaskStartedMessage.
    # parent_tool_use_id on a subagent's own messages carries the call_id, not
    # the task_id agent_spawn announced it under — see module docstring.
    task_ids_by_tool_use_id: dict[str, str] = {}

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
        await client.query(prompt)

        async for message in client.receive_response():
            if isinstance(message, StreamEvent):
                resolved_session_id = message.session_id or resolved_session_id
                for evt in _map_stream_event(message):
                    if message.parent_tool_use_id:
                        agent_id = _resolve_agent_id(task_ids_by_tool_use_id, message.parent_tool_use_id)
                        yield {
                            "type": "agent_event",
                            "agent_id": agent_id,
                            "inner": evt,
                        }
                    else:
                        yield evt

            elif isinstance(message, AssistantMessage):
                resolved_session_id = message.session_id or resolved_session_id
                if message.error:
                    error_evt = {"type": "error", "error": str(message.error)}
                    if message.parent_tool_use_id:
                        agent_id = _resolve_agent_id(task_ids_by_tool_use_id, message.parent_tool_use_id)
                        yield {
                            "type": "agent_event",
                            "agent_id": agent_id,
                            "inner": error_evt,
                        }
                    else:
                        yield error_evt
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_input = block.input if isinstance(block.input, dict) else {}
                        # Written out twice (wrapped/unwrapped) rather than built once
                        # and wrapped conditionally: tests/test_protocol.py's static
                        # contract test regex-scans for a literal
                        # `yield {"type": "tool_use", ...}` and can't see through a
                        # variable reference, so the unwrapped literal has to stay
                        # intact for the type to remain detectable.
                        if message.parent_tool_use_id:
                            agent_id = _resolve_agent_id(task_ids_by_tool_use_id, message.parent_tool_use_id)
                            tool_use_evt = {
                                "type": "tool_use",
                                "tool": block.name,
                                "call_id": block.id,
                                "input": tool_input,
                            }
                            yield {
                                "type": "agent_event",
                                "agent_id": agent_id,
                                "inner": tool_use_evt,
                            }
                        else:
                            yield {
                                "type": "tool_use",
                                "tool": block.name,
                                "call_id": block.id,
                                "input": tool_input,
                            }

            elif isinstance(message, UserMessage):
                blocks = message.content if isinstance(message.content, list) else []
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        result_str = _stringify_tool_result(block.content)
                        is_error = bool(block.is_error)
                        if message.parent_tool_use_id:
                            agent_id = _resolve_agent_id(task_ids_by_tool_use_id, message.parent_tool_use_id)
                            tool_result_evt = {
                                "type": "tool_result",
                                "call_id": block.tool_use_id,
                                "result": result_str,
                                "is_error": is_error,
                            }
                            yield {
                                "type": "agent_event",
                                "agent_id": agent_id,
                                "inner": tool_result_evt,
                            }
                        else:
                            yield {
                                "type": "tool_result",
                                "call_id": block.tool_use_id,
                                "result": result_str,
                                "is_error": is_error,
                            }

            elif isinstance(message, TaskStartedMessage):
                open_task_ids.add(message.task_id)
                if message.tool_use_id:
                    task_ids_by_tool_use_id[message.tool_use_id] = message.task_id
                yield {
                    "type": "agent_spawn",
                    "agent_id": message.task_id,
                    "agent_name": message.task_type or message.description,
                    "parent_id": "agent-0",
                }

            elif isinstance(message, TaskProgressMessage):
                if message.last_tool_name:
                    progress_evt = {
                        "type": "tool_use",
                        "tool": message.last_tool_name,
                        "call_id": None,
                        "input": {},
                    }
                    yield {
                        "type": "agent_event",
                        "agent_id": message.task_id,
                        "inner": progress_evt,
                    }

            elif isinstance(message, TaskNotificationMessage):
                if message.task_id in open_task_ids:
                    open_task_ids.discard(message.task_id)
                    yield {
                        "type": "agent_exit",
                        "agent_id": message.task_id,
                        "exit_code": 0 if message.status == "completed" else 1,
                    }

            elif isinstance(message, TaskUpdatedMessage):
                if message.task_id in open_task_ids and message.status in _TERMINAL_TASK_STATUSES:
                    open_task_ids.discard(message.task_id)
                    yield {
                        "type": "agent_exit",
                        "agent_id": message.task_id,
                        "exit_code": 0 if message.status == "completed" else 1,
                    }

            elif isinstance(message, ResultMessage):
                resolved_session_id = message.session_id or resolved_session_id
                raw_usage = message.usage or {}
                usage_payload = {
                    "input_tokens": raw_usage.get("input_tokens", 0),
                    "output_tokens": raw_usage.get("output_tokens", 0),
                }
                yield {
                    "type": "usage",
                    "usage": usage_payload,
                }
                if message.is_error:
                    yield {
                        "type": "error",
                        "error": message.result or message.stop_reason or "agent run failed",
                    }
                yield {"type": "done", "session_id": resolved_session_id}

    finally:
        await client.disconnect()


def _resolve_agent_id(task_ids_by_tool_use_id: dict[str, str], parent_tool_use_id: str) -> str:
    """Map a Task tool_use call_id to the task_id its agent_spawn announced.

    parent_tool_use_id (on a subagent's own AssistantMessage/UserMessage/
    StreamEvent) is the Task tool_use block's own call_id — a different,
    uncorrelated value from TaskStartedMessage.task_id, which is what
    agent_spawn/agent_exit use as agent_id and the only id the TUI has ever
    announced an agent under. Falls back to the raw call_id if no
    TaskStartedMessage has been seen for it yet (should not happen — a task
    always starts before its own content appears in the stream — but this
    is safer than raising).
    """
    return task_ids_by_tool_use_id.get(parent_tool_use_id, parent_tool_use_id)


def _map_stream_event(message: StreamEvent) -> list[dict[str, Any]]:
    """Map a raw Anthropic streaming event to token-level content/thinking deltas.

    ``message.event`` is the raw Messages-API streaming event dict
    (content_block_delta / delta.type text_delta|thinking_delta, etc.) —
    this is the token-level source that used to come from
    AgentEventType.CONTENT_DELTA / THINKING.
    """
    raw = message.event or {}
    if raw.get("type") != "content_block_delta":
        return []
    delta = raw.get("delta") or {}
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        return [{"type": "content", "content": delta.get("text", "")}]
    if delta_type == "thinking_delta":
        return [{"type": "thinking", "content": delta.get("thinking", "")}]
    return []


def _stringify_tool_result(content: str | list[dict[str, Any]] | None) -> str:
    """Flatten a ToolResultBlock's content into the plain string the wire protocol expects."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)
