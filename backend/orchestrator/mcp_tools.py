"""backend/orchestrator/mcp_tools.py — MCP tool wrappers for the subscription backend.

Wraps the existing tool_proxy handlers (Read/Edit/Write/Bash/Grep/Glob) as
in-process Agent-SDK MCP tools via @tool / create_sdk_mcp_server().

Security contract:
  - REUSES tool_proxy's validation logic exactly (imported, never re-implemented).
  - validate_env() called before every Bash invocation — blocks forbidden credentials.
  - dispatch() fail-closed whitelist enforced at the MCP handler level.
  - ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN must NOT appear in the child env
    passed to tool_proxy handlers; only the cleaned env from build_env() is used.

Usage::

    from backend.orchestrator.mcp_tools import build_mcp_server

    server_config = build_mcp_server(
        whitelist=["Read", "Bash", "Grep"],
        env={"PATH": "/usr/bin", "HOME": "<home>"},
        cwd="/path/to/worktree",
    )
    # Pass server_config to ClaudeAgentOptions(mcp_servers={"tools": server_config})
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency guard
# ---------------------------------------------------------------------------

try:
    from claude_agent_sdk import create_sdk_mcp_server, tool  # type: ignore[import]
    from claude_agent_sdk.types import McpSdkServerConfig  # type: ignore[import]
except ImportError as _err:
    raise ImportError(
        "The 'claude-agent-sdk' package is required for the subscription backend. "
        "Install it with: pip install claude-agent-sdk"
    ) from _err

from backend.orchestrator.tool_proxy import (
    run_read,
    run_edit,
    run_write,
    run_bash,
    run_grep,
    run_glob,
    UnknownToolError,
    EnvLeakError,
)


# ---------------------------------------------------------------------------
# MCP tool factory
#
# Tools are closures over (env, cwd) — these are fixed at server-creation time
# for the lifetime of a single agent run. Each tool validates the whitelist before
# dispatching so the SDK's `allowed_tools` is a second line of defence.
# ---------------------------------------------------------------------------

def _make_read_tool(env: dict[str, str], cwd: str):
    """Return a @tool-decorated Read handler bound to (env, cwd)."""

    @tool(
        name="Read",
        description="Read a file and return its contents.",
        input_schema={"path": str},
    )
    async def handle_read(args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        try:
            content = run_read(path=path, env=env, cwd=cwd)
            return {"content": [{"type": "text", "text": content}]}
        except (FileNotFoundError, OSError) as e:
            return {"content": [{"type": "text", "text": f"Error reading {path}: {e}"}], "is_error": True}

    return handle_read


def _make_edit_tool(env: dict[str, str], cwd: str):
    """Return a @tool-decorated Edit handler bound to (env, cwd)."""

    @tool(
        name="Edit",
        description="Replace old_string with new_string in a file.",
        input_schema={"path": str, "old_string": str, "new_string": str},
    )
    async def handle_edit(args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        old_string = args["old_string"]
        new_string = args["new_string"]
        try:
            updated = run_edit(path=path, old_string=old_string, new_string=new_string, env=env, cwd=cwd)
            return {"content": [{"type": "text", "text": updated}]}
        except (ValueError, OSError) as e:
            return {"content": [{"type": "text", "text": f"Error editing {path}: {e}"}], "is_error": True}

    return handle_edit


def _make_write_tool(env: dict[str, str], cwd: str):
    """Return a @tool-decorated Write handler bound to (env, cwd)."""

    @tool(
        name="Write",
        description="Write content to a file (creates or overwrites).",
        input_schema={"path": str, "content": str},
    )
    async def handle_write(args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        content = args["content"]
        try:
            written_path = run_write(path=path, content=content, env=env, cwd=cwd)
            return {"content": [{"type": "text", "text": f"Written: {written_path}"}]}
        except OSError as e:
            return {"content": [{"type": "text", "text": f"Error writing {path}: {e}"}], "is_error": True}

    return handle_write


def _make_bash_tool(env: dict[str, str], cwd: str):
    """Return a @tool-decorated Bash handler bound to (env, cwd).

    validate_env() is called via run_bash() before subprocess launch — this blocks
    any forbidden credential keys that might slip in through mis-configuration.
    """

    @tool(
        name="Bash",
        description="Run a shell command.",
        input_schema={"command": str},
    )
    async def handle_bash(args: dict[str, Any]) -> dict[str, Any]:
        cmd = args["command"]
        timeout = int(args.get("timeout", 60))
        try:
            output = run_bash(cmd=cmd, env=env, cwd=cwd, timeout=timeout)
            return {"content": [{"type": "text", "text": output}]}
        except EnvLeakError as e:
            # Credential leak attempt — hard error, never silenced
            logger.error("Bash tool env leak blocked: %s", e)
            return {"content": [{"type": "text", "text": f"Security block: {e}"}], "is_error": True}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"Error running command: {e}"}], "is_error": True}

    return handle_bash


def _make_grep_tool(env: dict[str, str], cwd: str):
    """Return a @tool-decorated Grep handler bound to (env, cwd)."""

    @tool(
        name="Grep",
        description="Search for a pattern in files.",
        input_schema={"pattern": str, "path": str},
    )
    async def handle_grep(args: dict[str, Any]) -> dict[str, Any]:
        pattern = args["pattern"]
        path = args.get("path", ".")
        include = args.get("include", "")
        try:
            result = run_grep(pattern=pattern, path=path, env=env, cwd=cwd, include=include)
            return {"content": [{"type": "text", "text": result or "(no matches)"}]}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"Error running grep: {e}"}], "is_error": True}

    return handle_grep


def _make_glob_tool(env: dict[str, str], cwd: str):
    """Return a @tool-decorated Glob handler bound to (env, cwd)."""

    @tool(
        name="Glob",
        description="Expand a glob pattern and return matching paths.",
        input_schema={"pattern": str},
    )
    async def handle_glob(args: dict[str, Any]) -> dict[str, Any]:
        pattern = args["pattern"]
        try:
            paths = run_glob(pattern=pattern, env=env, cwd=cwd)
            text = "\n".join(paths) if paths else "(no matches)"
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"Error running glob: {e}"}], "is_error": True}

    return handle_glob


# ---------------------------------------------------------------------------
# Map: tool name → factory function
# ---------------------------------------------------------------------------

_TOOL_FACTORIES = {
    "Read": _make_read_tool,
    "Edit": _make_edit_tool,
    "Write": _make_write_tool,
    "Bash": _make_bash_tool,
    "Grep": _make_grep_tool,
    "Glob": _make_glob_tool,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_mcp_server(
    whitelist: list[str],
    env: dict[str, str],
    cwd: str,
    server_name: str = "tools",
) -> "McpSdkServerConfig":
    """Build an in-process MCP server exposing only the whitelisted tools.

    Only tools in *whitelist* are registered; unknown names are silently skipped
    (consistent with sdk_runner's _build_sdk_tools behaviour). The returned
    McpSdkServerConfig is ready to pass to ClaudeAgentOptions.mcp_servers.

    Parameters
    ----------
    whitelist:
        List of tool names to expose (e.g. ["Read", "Bash", "Grep"]).
    env:
        Cleaned env dict for tool execution. Must NOT contain forbidden credential
        keys — tool_proxy's validate_env() will raise EnvLeakError if it does.
    cwd:
        Working directory for all tool invocations.
    server_name:
        MCP server name (default "tools").

    Returns
    -------
    McpSdkServerConfig
        In-process MCP server configuration for use with ClaudeAgentOptions.
    """
    tools = []
    for name in whitelist:
        factory = _TOOL_FACTORIES.get(name)
        if factory is None:
            logger.debug("build_mcp_server: skipping unknown tool %r", name)
            continue
        tool_instance = factory(env=env, cwd=cwd)
        tools.append(tool_instance)

    return create_sdk_mcp_server(name=server_name, version="1.0.0", tools=tools)
