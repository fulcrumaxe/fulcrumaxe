#!/usr/bin/env python3
"""wrong_premise_guard.py — PostToolUse/PostToolUseFailure hook.

Detects when an agent has called the same tool with the same arguments and
received the same error N consecutive times in a single agent run. When the
threshold is reached, the hook appends a directive to the tool output so the
agent sees it in its next turn, breaking the retry loop.

Usage (command hook, invoked by Claude Code's hook engine):
  Input:  JSON context via stdin
  Output: JSON with hookSpecificOutput.stopBehavior and/or modified output
  State:  /tmp/wpg-{session_id}.json — per-run retry counters

Control plane gates:
  gates.wrong_premise_guard            — default true; when false, hook is a no-op
  policies.agents.wrong_premise_retry_limit — default 3
  policies.agents.wrong_premise_total_limit — default 15 (circuit breaker)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_RETRY_LIMIT = 3
DEFAULT_TOTAL_LIMIT = 15  # circuit breaker: total failures regardless of tool

# Excluded roles — browser-tester has legitimate poll-and-wait patterns;
# run-analyst is read-only. Guard fires for all other roles.
EXCLUDED_ROLES = {"browser-tester", "run-analyst"}

# Volatile patterns to strip from error text before hashing so that
# timestamps, UUIDs, and request IDs don't prevent deduplication.
_VOLATILE_PATTERNS = [
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),  # UUID
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),  # ISO datetime
    re.compile(r"\breq(?:uest)?[-_]?id[:\s=]+\S+", re.I),  # request-id: ...
    re.compile(r"\btrace[-_]?id[:\s=]+\S+", re.I),           # trace-id: ...
    re.compile(r"\b\d{10,}\b"),                               # unix timestamps / large integers
]

DIRECTIVE_TEMPLATE = (
    "\n\n[wrong-premise-guard] You have called {tool_name} with the same arguments "
    "and received the same error {limit} times. Do not retry this call. Either change "
    "your approach, change the arguments, or emit AGENT_OUTPUT with verdict "
    "needs-fix/fail and describe the blocker."
)

CIRCUIT_BREAKER_DIRECTIVE = (
    "\n\n[wrong-premise-guard] You have accumulated {total} total tool failures in this "
    "run. Step back and rethink your approach entirely. Do not continue retrying with "
    "variations of the same strategy. Emit AGENT_OUTPUT with verdict needs-fix/fail and "
    "describe what you have tried and why you are blocked."
)


# ---------------------------------------------------------------------------
# Tool name extraction
# ---------------------------------------------------------------------------

def _extract_tool_name(context: dict[str, Any]) -> str:
    """Extract tool name from all known hook context variants.

    The Claude Code harness surfaces the tool name in different fields
    depending on the tool type:
      - Standard tools (Bash, Edit, Read, etc.): context["tool_name"]
      - MCP tools: context["tool"]["name"] or context["server_name"] + "." + context["tool_name"]
      - Nested tool wrapper: context["tool"]["name"]
      - Fallback: synthesize from tool_input content
    """
    # Primary: direct tool_name field
    name = context.get("tool_name") or ""
    if name:
        return name.strip()

    # MCP nested: context.tool.name
    tool_obj = context.get("tool")
    if isinstance(tool_obj, dict):
        nested = tool_obj.get("name") or ""
        if nested:
            # Optionally prefix with server name for MCP tools
            server = context.get("server_name") or ""
            if server:
                return f"{server}.{nested}".strip()
            return nested.strip()

    # tool_use_id often encodes the tool name as a prefix (e.g. "toolu_bash_...")
    tool_use_id = context.get("tool_use_id") or ""
    if tool_use_id:
        parts = tool_use_id.split("_")
        if len(parts) >= 2 and parts[0] in ("toolu", "tool"):
            candidate = parts[1]
            if candidate and candidate not in ("", "unknown"):
                return candidate

    # Synthesize from tool_input to avoid collapsing all unknowns into one bucket
    tool_input = context.get("tool_input") or {}
    return _synthetic_tool_name(tool_input)


def _synthetic_tool_name(tool_input: dict[str, Any]) -> str:
    """Derive a synthetic bucket name from tool_input when tool_name is absent.

    Returns a short string that distinguishes Bash commands from file edits
    from other operations, so different unknown-tool failures don't collide.
    """
    if not tool_input:
        return "unknown"

    # Bash-style: has "command" key
    command = tool_input.get("command") or ""
    if command:
        # Use the first word (the binary name) as the bucket
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word:
            return f"bash:{first_word[:32]}"

    # Edit-style: has "file_path" key
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if file_path:
        # Use just the filename so renames don't fragment the bucket
        name = Path(str(file_path)).name[:32]
        return f"edit:{name}"

    # Generic: hash the first key-value pair as a discriminator
    first_key = next(iter(tool_input), "")
    first_val = str(tool_input.get(first_key, ""))[:32]
    return f"input:{first_key}:{first_val}"


# ---------------------------------------------------------------------------
# Fuzzy arg normalisation
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_SLASH_RE = re.compile(r"/+\Z")


def _normalize_string_value(s: str) -> str:
    """Normalize a single string value for fuzzy matching."""
    # Collapse internal whitespace
    s = _WHITESPACE_RE.sub(" ", s).strip()
    # Remove trailing slashes
    s = _TRAILING_SLASH_RE.sub("", s)
    # Normalize quotes: replace single quotes with double quotes
    s = s.replace("'", '"')
    # Strip volatile patterns
    for pat in _VOLATILE_PATTERNS:
        s = pat.sub("", s)
    return s


def _normalize_value(v: Any) -> Any:
    """Recursively normalize a value for fuzzy arg matching."""
    if isinstance(v, str):
        return _normalize_string_value(v)
    if isinstance(v, dict):
        return {k: _normalize_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_normalize_value(item) for item in v]
    return v


def _fuzzy_normalize_args(tool_input: dict[str, Any]) -> str:
    """Normalise tool args so near-identical retries map to the same key.

    Strips:
      - extra whitespace
      - trailing slashes on paths
      - quote style differences (single vs double)
      - volatile tokens already handled by _VOLATILE_PATTERNS
    """
    normalized = _normalize_value(tool_input)
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_error(text: str) -> str:
    """Strip volatile fields and keep only the first line for hashing."""
    first_line = (text or "").split("\n")[0].strip()
    for pat in _VOLATILE_PATTERNS:
        first_line = pat.sub("", first_line)
    return first_line.strip()


def _make_key(tool_name: str, tool_input: dict[str, Any], error_text: str) -> str:
    """Return a stable hash key for a (tool, args, error) triple.

    Uses fuzzy-normalised args so near-identical retries (same tool, slightly
    varied whitespace/quotes, same error class) collapse to the same key.
    """
    args_part = _fuzzy_normalize_args(tool_input)
    error_part = _normalize_error(error_text)
    raw = f"{tool_name}\x00{args_part}\x00{error_part}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _state_path(session_id: str) -> Path:
    """Return the path to the per-session state file."""
    return Path(tempfile.gettempdir()) / f"wpg-{session_id}.json"


def _load_state(session_id: str) -> dict[str, Any]:
    p = _state_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"counts": {}, "triggered": [], "total_failures": 0, "circuit_tripped": False}


def _save_state(session_id: str, state: dict[str, Any]) -> None:
    p = _state_path(session_id)
    try:
        p.write_text(json.dumps(state))
    except Exception:
        pass


def _get_control_plane_value(key: str, default: Any) -> Any:
    """Read a value from the control plane; fall back to default on any error."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, str(repo_root / "control_plane.py"), "get", key],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            val = result.stdout.strip().strip('"')
            return val
    except Exception:
        pass
    return default


def _emit_team_log(agent_id: str, tool_name: str, limit: int) -> None:
    """Best-effort team-log emit — never fatal."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        scripts = repo_root / "scripts" / "rotate-team-log.sh"
        if scripts.exists():
            msg = (
                f"wrong-premise-guard: agent={agent_id} "
                f"tool={tool_name} blocked after {limit} identical failures"
            )
            subprocess.run(
                ["bash", str(scripts), "comment", msg],
                capture_output=True,
                timeout=10,
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main hook logic
# ---------------------------------------------------------------------------

def run(context: dict[str, Any]) -> dict[str, Any]:
    """Process a PostToolUseFailure event and return a hook output dict."""

    # --- Gate check ---
    guard_enabled = _get_control_plane_value("gates.wrong_premise_guard", "true")
    if str(guard_enabled).lower() in ("false", "0", "no"):
        return {}

    # --- Extract context fields ---
    session_id: str = context.get("session_id") or "unknown"
    tool_input: dict[str, Any] = context.get("tool_input") or {}
    tool_output: str = str(context.get("tool_output") or "")

    # Normalize tool name from all known field variants
    tool_name: str = _extract_tool_name(context)

    # Skip for excluded roles (role may be embedded in session_id or agent tags)
    agent_role: str = context.get("agent_role") or ""
    if agent_role in EXCLUDED_ROLES:
        return {}

    # Only act on errors (hook is also registered for PostToolUse as a no-op path)
    is_error: bool = bool(context.get("is_error", True))
    if not is_error:
        return {}

    # --- Retry limit ---
    try:
        limit = int(_get_control_plane_value(
            "policies.agents.wrong_premise_retry_limit", DEFAULT_RETRY_LIMIT
        ))
    except (TypeError, ValueError):
        limit = DEFAULT_RETRY_LIMIT

    # --- Total failure circuit-breaker limit ---
    try:
        total_limit = int(_get_control_plane_value(
            "policies.agents.wrong_premise_total_limit", DEFAULT_TOTAL_LIMIT
        ))
    except (TypeError, ValueError):
        total_limit = DEFAULT_TOTAL_LIMIT

    # --- Build per-key dedup key ---
    key = _make_key(tool_name, tool_input, tool_output)

    # --- Load state ---
    state = _load_state(session_id)
    counts: dict[str, int] = state.setdefault("counts", {})
    triggered: list[str] = state.setdefault("triggered", [])
    total_failures: int = state.get("total_failures", 0)
    circuit_tripped: bool = state.get("circuit_tripped", False)

    # Increment counters
    counts[key] = counts.get(key, 0) + 1
    total_failures += 1
    state["total_failures"] = total_failures
    state["circuit_tripped"] = circuit_tripped
    _save_state(session_id, state)

    # --- Circuit breaker: total failures across all tools ---
    if total_failures >= total_limit and not circuit_tripped:
        state["circuit_tripped"] = True
        _save_state(session_id, state)
        _emit_team_log(session_id, tool_name, total_limit)
        directive = CIRCUIT_BREAKER_DIRECTIVE.format(total=total_failures)
        return {
            "hookSpecificOutput": {
                "directive": directive,
                "tool_name": tool_name,
                "total_failures": total_failures,
                "limit": total_limit,
                "session_id": session_id,
                "stopBehavior": "block",
                "circuit_breaker": True,
            }
        }

    # If circuit already tripped, no further output (avoid flooding)
    if circuit_tripped:
        return {}

    # --- Per-key dedup check ---
    # If already triggered for this key in this run, no second injection
    if key in triggered:
        return {}

    if counts[key] < limit:
        return {}

    # --- Threshold reached: inject directive ---
    triggered.append(key)
    state["triggered"] = triggered
    _save_state(session_id, state)

    _emit_team_log(session_id, tool_name, limit)

    directive = DIRECTIVE_TEMPLATE.format(tool_name=tool_name, limit=limit)

    # Return the directive as a hookSpecificOutput so Claude Code can append it
    # to the tool result content. We also set stopBehavior for forward
    # compatibility with richer hook processing.
    return {
        "hookSpecificOutput": {
            "directive": directive,
            "tool_name": tool_name,
            "retry_count": counts[key],
            "limit": limit,
            "session_id": session_id,
            "stopBehavior": "block",
        }
    }


# ---------------------------------------------------------------------------
# Entry point (invoked as a command hook by Claude Code's hook engine)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        raw = sys.stdin.read()
        ctx = json.loads(raw) if raw.strip() else {}
    except Exception:
        ctx = {}

    output = run(ctx)
    # Print JSON to stdout — Claude Code reads this as HookDecision
    print(json.dumps(output))
