"""backend/prompt_lane/muse_lane.py — Muse Code CLI streaming lane.

Runs one prompt through ``muse exec --json`` as a subprocess and maps the
JSONL event stream onto the same wire protocol backend/server.py has always
streamed over stdout (see backend/prompt_lane/sdk_lane.py for the protocol).
Drop-in sibling of sdk_lane: same ``has_credential()`` / ``run_prompt()``
shape, so the caller picks a lane without changing its streaming logic —
see backend/prompt_lane/__init__.py's get_lane().

Event mapping (observed against muse 1.4.14 ``exec --json``):

- ``run.output.delta`` (payload.text)              -> ``content``
- ``task.lifecycle.proposed`` task_kind ``tool.*`` -> ``tool_use``
- ``tool.result``                               -> ``tool_result``
- ``run.usage`` (payload.models[].*_tokens)        -> ``usage``
- ``run.terminal.completed``                       -> ``done`` (session from stream.id)
- ``run.terminal.failed``                          -> ``error`` + ``done``

Subagent correlation rides on the subagent tool results, not the task
lifecycle: a ``subagent_spawn`` accept carries ``subagent_id`` /
``agent_path`` / ``task_ref``, and a ``subagent_wait`` ready carries the
child's ``summary``. ``run_prompt`` keeps the open-subagent set (mirroring
sdk_lane) and derives ``agent_spawn`` / ``agent_event`` / ``agent_exit``
frames from those results. The child's inner activity never streams to the
parent (no ``parent_task_id`` events exist), so the wait-ready summary is
synthesized as a single content ``agent_event`` — live child tool streaming
is the known fidelity gap versus the Claude lane.

Headless flags mirror sdk_lane's bypassPermissions posture — no human is
present to approve tool use in this lane: ``--approval-mode never``,
``--disable-sandbox``, ``--trust-workspace`` (trust is also what allows
agent delegation at all). All three are per-run CLI flags, never persisted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

NAME = "muse"

CREDENTIAL_HINT = "no Muse credential — run `muse login`, or set AF_MUSE_PROVIDER=echo for dry runs"


def should_use() -> bool:
    """True if the Muse lane should serve prompt-lane requests.

    AF_LANE=muse|claude forces a lane. Default (auto) keeps Claude when its
    binary resolves — no behavior change on Claude boxes — and falls to
    Muse when only ``muse`` is available.
    """
    choice = os.environ.get("AF_LANE", "auto").lower()
    if choice == "muse":
        return True
    if choice == "claude":
        return False
    claude_ok = bool(os.environ.get("CLAUDE_BIN") or shutil.which("claude"))
    if claude_ok:
        return False
    return resolve_binary() is not None


def resolve_binary() -> str | None:
    """Resolve the muse binary: $MUSE_BIN override, then PATH."""
    return os.environ.get("MUSE_BIN") or shutil.which("muse")


def has_credential() -> bool:
    """True if a headless muse run can authenticate.

    Requires a resolvable ``muse`` binary plus stored provider credentials
    (``~/.config/muse/auth.json`` from ``muse login``), unless the echo
    provider is explicitly selected via AF_MUSE_PROVIDER=echo (tests).
    Used by backend/server.py's startup check — server.py never inspects
    credential kinds itself.
    """
    if resolve_binary() is None:
        return False
    if os.environ.get("AF_MUSE_PROVIDER") == "echo":
        return True
    auth_path = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "muse" / "auth.json"
    return auth_path.exists()


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a role card into (frontmatter dict, body). No YAML dependency:
    the cards use flat `key: value` frontmatter only."""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            meta[key] = value
    return meta, text[end + 4 :].lstrip("\n")


def _card_description(meta: dict[str, str], body: str, name: str) -> str:
    """description is host-required: frontmatter, else first heading, else name."""
    if meta.get("description"):
        return meta["description"]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or f"{name} agent"
        if stripped and not stripped.startswith("#"):
            return stripped[:160]
    return f"{name} agent"


# Roles the loop actually spawns by type (mirrors the twin's snippet set).
# The full 26-card overlay (~170KB) exceeds the kernel's 128KB single-arg
# limit, so loop runs ship this subset (~55KB); rarer roles still work
# untyped with the role carried in the assembled prompt (the v1 path).
CORE_SPAWN_ROLES = [
    "executor",
    "code-reviewer",
    "project-manager",
    "security-reviewer",
    "acceptance-tester",
]

# Single-argv floor under the kernel's 128KB MAX_ARG_STRLEN. build_command
# refuses oversized overlays loudly instead of dying with E2BIG at exec.
_MAX_OVERLAY_BYTES = 100_000


def build_agents_overlay(
    roles: list[str] | None = None,
    agents_dir: str | None = None,
) -> str | None:
    """Compile role cards into a `muse exec --agents` overlay JSON string.

    Each card becomes ``{description, prompt}`` — exactly the fields the host
    accepts (extra keys risk agent_definition_invalid; verified live). The
    prompt is the card body with frontmatter stripped. `roles=None` compiles
    every ``*.md`` card; explicit roles must all exist (FileNotFoundError
    otherwise — a silently missing role would reject typed spawns at
    runtime). Returns None when the agents dir is absent or yields no cards.
    """
    directory = Path(agents_dir) if agents_dir else _REPO_ROOT / ".claude" / "agents"
    if not directory.is_dir():
        return None
    if roles is None:
        names = sorted(p.stem for p in directory.glob("*.md"))
    else:
        names = list(roles)
        missing = [n for n in names if not (directory / f"{n}.md").is_file()]
        if missing:
            raise FileNotFoundError(f"role card(s) not found in {directory}: {', '.join(missing)}")
    overlay: dict[str, dict[str, str]] = {}
    for name in names:
        try:
            text = (directory / f"{name}.md").read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _split_frontmatter(text)
        overlay[name] = {"description": _card_description(meta, body, name), "prompt": body}
    if not overlay:
        return None
    return json.dumps(overlay)


def build_command(
    *,
    prompt_file: str,
    session_id: str | None = None,
    workspace: str | None = None,
    agents_overlay: str | None = None,
) -> list[str]:
    """Build the ``muse exec`` argv for one prompt-lane request.

    session_id: an existing Muse session UUID to continue, or None to start
    a new session (Muse assigns one, surfaced via stream.id on every event).
    """
    binary = resolve_binary()
    if binary is None:
        raise RuntimeError("cannot resolve muse binary — checked $MUSE_BIN and PATH")
    cmd = [
        binary,
        "exec",
        "--json",
        "--prompt-file",
        prompt_file,
        "--workspace",
        workspace or str(_REPO_ROOT),
        # No human is present to approve tool use in this lane — it is
        # driven by stdin/FIFO from a cron-triggered loop, same as the
        # Claude lane's bypassPermissions it replaces.
        "--approval-mode",
        "never",
        "--disable-sandbox",
        "--trust-workspace",
    ]
    if session_id is not None:
        cmd += ["--session-id", session_id]
    provider = os.environ.get("AF_MUSE_PROVIDER")
    if provider:
        cmd += ["--provider", provider]
    model = os.environ.get("AF_MODEL") or None
    if model:
        cmd += ["--model", model]
    effort = os.environ.get("AF_REASONING_EFFORT")
    if effort:
        cmd += ["--reasoning-effort", effort]
    max_steps = os.environ.get("AF_MAX_MODEL_STEPS")
    if max_steps:
        cmd += ["--max-model-steps", max_steps]
    if agents_overlay:
        if len(agents_overlay.encode("utf-8")) > _MAX_OVERLAY_BYTES:
            raise ValueError(
                f"--agents overlay is {len(agents_overlay.encode('utf-8'))} bytes; "
                f"over the {_MAX_OVERLAY_BYTES}-byte single-arg budget — pass fewer roles"
            )
        cmd += ["--agents", agents_overlay]
    return cmd


async def run_prompt(prompt: str, session_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
    """Run one prompt through ``muse exec --json``.

    Yields wire-protocol event dicts (without the "id" field — the caller
    adds that before writing to stdout). Always ends with exactly one
    ``done`` event carrying the resolved session_id. If the subprocess
    exits without a terminal event, RuntimeError propagates (after any
    partial events already yielded) and the caller is responsible for
    emitting an ``error`` event — same contract as sdk_lane.
    """
    resolved_session_id = session_id
    saw_terminal = False
    # subagent_ids announced by agent_spawn and not yet closed by agent_exit.
    # Mirrors sdk_lane's open_task_ids: a wait-ready for an unknown id emits
    # nothing, and a second accept for a known id (subagent_send_message also
    # returns status "accepted") emits no duplicate spawn.
    open_subagents: set[str] = set()

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=True) as prompt_tmp:
        prompt_tmp.write(prompt)
        prompt_tmp.flush()
        cmd = build_command(prompt_file=prompt_tmp.name, session_id=session_id)
        # Spawn budget + circuit breaker, same contract as backend/api.py's
        # `claude -p` path: SpawnBlocked aborts before the subprocess exists
        # and propagates (the caller emits the error event, as with any
        # other run_prompt failure).
        try:
            from backend.claude_spawn_tracker import record as _record_spawn
        except ImportError:
            _record_spawn = None  # tracker not yet available (bootstrap)
        if _record_spawn is not None:
            _record_spawn(source="muse_lane")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_REPO_ROOT),
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("muse_lane: skipping non-JSON stdout line: %.120s", line)
                continue
            if not isinstance(envelope, dict):
                continue
            stream = envelope.get("stream") or {}
            if isinstance(stream, dict) and stream.get("kind") == "session" and stream.get("id"):
                resolved_session_id = stream["id"]
            for evt in _map_envelope(envelope):
                if evt["type"] == "done":
                    saw_terminal = True
                    evt["session_id"] = resolved_session_id
                yield evt
                if evt["type"] == "tool_result":
                    for frame in _subagent_frames(evt, open_subagents):
                        yield frame

        stderr_tail = (await proc.stderr.read()).decode("utf-8", "replace")[-2000:]
        returncode = await proc.wait()

    if not saw_terminal:
        raise RuntimeError(f"muse exec exited {returncode} without a terminal event. stderr: {stderr_tail.strip()[-500:]}")


def _agent_name_from_path(agent_path: str) -> str:
    """Derive the child's display name from its agent_path.

    Observed shape is ``<parent-path>/<task-name>/<generation>`` (e.g.
    ``main/pong responder/1``); the task name sits second from the end.
    Anything else yields "" — sdk_lane likewise allows an empty name.
    """
    segments = [seg for seg in agent_path.split("/") if seg]
    if len(segments) >= 2:
        return segments[-2]
    return ""


def _subagent_frames(tool_result_evt: dict[str, Any], open_subagents: set[str]) -> list[dict[str, Any]]:
    """Derive agent_spawn/agent_event/agent_exit frames from one tool_result.

    Only subagent tool results (spawn accept, wait ready, send-message accept)
    carry a JSON ``subagent_id``; every other tool result passes through with
    no frames. Frame shapes match sdk_lane exactly (agent_id, agent_name,
    parent_id "agent-0"; exit_code 0/1).
    """
    try:
        data = json.loads(tool_result_evt.get("result") or "")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    subagent_id = data.get("subagent_id")
    if not isinstance(subagent_id, str) or not subagent_id:
        return []
    status = data.get("status")
    if status == "accepted" and subagent_id not in open_subagents:
        open_subagents.add(subagent_id)
        agent_path = data.get("agent_path")
        return [
            {
                "type": "agent_spawn",
                "agent_id": subagent_id,
                "agent_name": _agent_name_from_path(agent_path) if isinstance(agent_path, str) else "",
                "parent_id": "agent-0",
            }
        ]
    if status == "ready" and subagent_id in open_subagents:
        open_subagents.discard(subagent_id)
        failed = bool(tool_result_evt.get("is_error")) or bool(data.get("errorKind"))
        frames: list[dict[str, Any]] = []
        summary = data.get("summary")
        if summary is not None:
            frames.append(
                {
                    "type": "agent_event",
                    "agent_id": subagent_id,
                    "inner": {"type": "content", "content": str(summary)},
                }
            )
        frames.append({"type": "agent_exit", "agent_id": subagent_id, "exit_code": 1 if failed else 0})
        return frames
    return []


def _map_envelope(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one ``muse exec --json`` envelope onto wire-protocol events."""
    payload_type = envelope.get("payload_type")
    payload = envelope.get("payload") or {}
    if not isinstance(payload, dict):
        return []

    if payload_type == "run.output.delta":
        text = payload.get("text", "")
        return [{"type": "content", "content": text}]

    if payload_type == "task.lifecycle.proposed":
        event = payload.get("event") or {}
        task_kind = event.get("task_kind", "") if isinstance(event, dict) else ""
        if isinstance(task_kind, str) and task_kind.startswith("tool."):
            return [{"type": "tool_use", "tool": task_kind[len("tool."):], "call_id": event.get("task_id"), "input": {}}]
        return []

    if payload_type == "tool.result":
        facts = payload.get("correlation_facts") or {}
        outcome = facts.get("outcome", "") if isinstance(facts, dict) else ""
        return [
            {
                "type": "tool_result",
                "call_id": payload.get("call_id"),
                "result": payload.get("text", ""),
                "is_error": outcome != "success",
            }
        ]

    if payload_type == "run.usage":
        input_tokens = 0
        output_tokens = 0
        models = payload.get("models") or []
        if isinstance(models, list):
            for model in models:
                if isinstance(model, dict):
                    input_tokens += int(model.get("input_tokens") or 0)
                    output_tokens += int(model.get("output_tokens") or 0)
        return [{"type": "usage", "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}]

    if payload_type == "run.terminal.completed":
        return [{"type": "done", "session_id": None}]

    if payload_type == "run.terminal.failed":
        return [
            {
                "type": "error",
                "error": payload.get("text") or payload.get("reason") or "muse run failed",
            },
            {"type": "done", "session_id": None},
        ]

    return []
