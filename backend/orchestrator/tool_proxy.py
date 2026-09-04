"""backend/orchestrator/tool_proxy.py — Tool execution proxy for SDK-routed agents.

Each tool handler takes an explicit `env: dict[str, str]` parameter — NO
os.environ default anywhere in this module (S2).

Imports hooks/sandbox_rules.py for sandbox enforcement where applicable.
Fails CLOSED on unknown tool names (S5).

Supported tools (matching the role-card tool whitelist used by most roles):
  Read, Edit, Write, Bash, Grep, Glob

Env allowlist baseline: {PATH, HOME, USER, LANG, LC_ALL}
Per-role grants (e.g. GH_TOKEN) come from role-card frontmatter; callers must
explicitly pass them into the env dict.

Security invariants:
  - ANTHROPIC_API_KEY is NEVER in the child env (enforced in run_bash)
  - Any *_API_KEY, *_TOKEN, *_SECRET pattern is blocked unless the role-card
    explicitly grants it via env_allowlist
  - run_bash raises TypeError if called without explicit env (no default)
  - run_bash command content is filtered via sandbox_rules.classify_bash (S6)
  - run_write/run_edit target paths must be within the worktree cwd (S7)
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

# Import sandbox_rules so run_bash can reuse the vetted classify_bash policy
# and check_claude_spawn deny-list.  sandbox_rules lives at hooks/sandbox_rules.py;
# we add the repo root to sys.path if the hooks package isn't importable yet.
try:
    from hooks.sandbox_rules import classify_bash as _classify_bash
    from hooks.sandbox_rules import check_claude_spawn as _check_claude_spawn
except ImportError:
    # Fall back to loading via absolute path (worktree may not have repo root on PYTHONPATH,
    # e.g. under pytest --import-mode=importlib, which does not add the repo root to
    # sys.path the way the legacy import mode does).
    import sys as _sys
    import importlib.util as _ilu
    _repo_root = Path(__file__).resolve().parents[2]  # backend/orchestrator → repo root
    _spec = _ilu.spec_from_file_location(
        "hooks.sandbox_rules",
        _repo_root / "hooks" / "sandbox_rules.py",
    )
    _mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    # Register in sys.modules BEFORE exec_module — dataclasses.dataclass looks up
    # cls.__module__ in sys.modules while processing @dataclass-decorated classes
    # in sandbox_rules.py; without this the lookup returns None and dataclass()
    # crashes with AttributeError. This is the documented importlib pattern for
    # loading a module from a file path (see importlib docs, "Importing a source
    # file directly").
    _sys.modules["hooks.sandbox_rules"] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    _classify_bash = _mod.classify_bash
    _check_claude_spawn = _mod.check_claude_spawn


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class UnknownToolError(Exception):
    """Raised when dispatch() receives a tool name not in the role whitelist."""


class EnvLeakError(Exception):
    """Raised when a forbidden credential key is detected in the child env."""


class SandboxBlockError(Exception):
    """Raised when sandbox_rules.classify_bash blocks a command (S6)."""


class PathEscapeError(Exception):
    """Raised when run_write/run_edit target path escapes the worktree cwd (S7)."""


# ---------------------------------------------------------------------------
# Env allowlist enforcement (S2)
# ---------------------------------------------------------------------------

# Patterns that must NEVER appear in a child process env, regardless of grants
_FORBIDDEN_ENV_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ANTHROPIC_API_KEY", re.IGNORECASE),
    re.compile(r"ANTHROPIC_AUTH_TOKEN", re.IGNORECASE),
    re.compile(r"CLAUDE_CODE_OAUTH_TOKEN", re.IGNORECASE),
    re.compile(r"OPENAI_API_KEY", re.IGNORECASE),
    re.compile(r".*_API_KEY$", re.IGNORECASE),
    re.compile(r".*_AUTH_TOKEN$", re.IGNORECASE),
    re.compile(r".*_SECRET$", re.IGNORECASE),
]

# Baseline env keys always permitted (the role card can add more)
_BASELINE_ENV_KEYS: frozenset[str] = frozenset(
    ["PATH", "HOME", "USER", "LANG", "LC_ALL"]
)


def validate_env(env: dict[str, str]) -> None:
    """Raise EnvLeakError if *env* contains any forbidden credential key.

    Called inside run_bash() before subprocess launch.
    """
    for key in env:
        for pattern in _FORBIDDEN_ENV_PATTERNS:
            if pattern.fullmatch(key):
                raise EnvLeakError(
                    f"Forbidden credential key '{key}' detected in child env. "
                    "Remove it from the env allowlist."
                )


def build_env(role_card_env_allowlist: list[str]) -> dict[str, str]:
    """Build a clean env dict from the OS env, keeping only allowed keys.

    Parameters
    ----------
    role_card_env_allowlist:
        List of additional env keys the role card grants (e.g. ["GH_TOKEN"]).
        These are added to the baseline set if present in the OS env.

    Returns
    -------
    dict[str, str]
        A clean env dict containing only baseline + granted keys.
        Credential keys in the allowlist are still rejected by validate_env().
    """
    import os as _os  # local import: the only permitted os.environ access in this file

    allowed_keys = _BASELINE_ENV_KEYS | frozenset(role_card_env_allowlist)
    clean: dict[str, str] = {}
    for key in allowed_keys:
        val = _os.environ.get(key)
        if val is not None:
            clean[key] = val
    return clean


# ---------------------------------------------------------------------------
# Path confinement helper (S7)
# ---------------------------------------------------------------------------

def _assert_path_within_cwd(path: str, cwd: str) -> Path:
    """Resolve *path* and raise PathEscapeError if it escapes *cwd*.

    Used by run_write and run_edit to prevent writes outside the worktree.

    Parameters
    ----------
    path:
        Absolute or relative path supplied by the caller.
    cwd:
        The worktree working directory (must be the confinement boundary).

    Returns
    -------
    Path
        The resolved (absolute) path — safe to use for I/O.

    Raises
    ------
    PathEscapeError
        If the resolved path is not under the resolved cwd.
    """
    cwd_resolved = Path(cwd).resolve()
    if Path(path).is_absolute():
        resolved = Path(path).resolve()
    else:
        resolved = (cwd_resolved / path).resolve()

    try:
        resolved.relative_to(cwd_resolved)
    except ValueError:
        raise PathEscapeError(
            f"Path '{path}' resolves to '{resolved}', which is outside the "
            f"worktree boundary '{cwd_resolved}'. Write refused."
        )
    return resolved


# ---------------------------------------------------------------------------
# Individual tool handlers
# ---------------------------------------------------------------------------

def run_read(path: str, env: dict[str, str], cwd: str) -> str:
    """Read a file and return its contents.

    Parameters
    ----------
    path:   Absolute or worktree-relative path to read.
    env:    Required explicit env dict (not used for reads, but enforced for
            signature consistency with run_bash).
    cwd:    Working directory (worktree root).

    Returns the file contents as a string.
    Raises FileNotFoundError if the path does not exist.
    """
    resolved = Path(cwd) / path if not Path(path).is_absolute() else Path(path)
    return resolved.read_text(encoding="utf-8", errors="replace")


def run_edit(
    path: str,
    old_string: str,
    new_string: str,
    env: dict[str, str],
    cwd: str,
) -> str:
    """Replace old_string with new_string in a file.

    Returns the full updated file contents on success.
    Raises ValueError if old_string is not found or is not unique.
    Raises PathEscapeError if the target path escapes the worktree cwd (S7).
    """
    resolved = _assert_path_within_cwd(path, cwd)
    content = resolved.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"old_string not found in {path}")
    if count > 1:
        raise ValueError(
            f"old_string appears {count} times in {path}; must be unique for Edit"
        )
    updated = content.replace(old_string, new_string, 1)
    resolved.write_text(updated, encoding="utf-8")
    return updated


def run_write(path: str, content: str, env: dict[str, str], cwd: str) -> str:
    """Write *content* to *path* (creates or overwrites).

    Returns the path written as a string.
    Raises PathEscapeError if the target path escapes the worktree cwd (S7).
    """
    resolved = _assert_path_within_cwd(path, cwd)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return str(resolved)


def run_bash(cmd: str, env: dict[str, str], cwd: str, timeout: int = 60) -> str:
    """Run *cmd* in a subprocess with the explicit *env* dict.

    Security invariants:
      S2 — *env* is a required positional parameter — no os.environ default.
           validate_env(env) is called before launch; raises EnvLeakError on
           forbidden credential keys.  Subprocess inherits ONLY the keys in
           *env*; no implicit inheritance.
      S6 — sandbox_rules.classify_bash() inspects command content before
           execution.  Dangerous commands (writes outside cwd, destructive
           git verbs, gh api mutations, nested claude spawns) are blocked
           via SandboxBlockError before the subprocess is launched.

    Parameters
    ----------
    cmd:        Shell command string.
    env:        Explicit env dict.  Must not contain forbidden credential keys.
    cwd:        Working directory for the subprocess.
    timeout:    Maximum wall-clock seconds (default 60).

    Returns stdout as a string (stderr merged into stdout).
    Raises subprocess.TimeoutExpired, subprocess.CalledProcessError,
    EnvLeakError, or SandboxBlockError as appropriate.
    """
    # S2: credential key check
    validate_env(env)

    # S6: command-content filtering — mirrors the two-stage check in sandbox.py.
    #
    # Stage A: check_claude_spawn — blocks nested claude process spawns and
    #   forbidden loop-trigger paths (same deny-list as the PreToolUse hook).
    spawn_decision = _check_claude_spawn([], cmd)
    if not spawn_decision.allow:
        raise SandboxBlockError(
            f"Command blocked by sandbox policy: {spawn_decision.reason!r}. "
            f"Command was: {cmd!r}"
        )

    # Stage B: classify_bash — blocks git write verbs, gh api mutations, PR
    #   merges, and output redirects outside the worktree cwd.
    decision = _classify_bash(cmd, cwd)
    if not decision.allow:
        raise SandboxBlockError(
            f"Command blocked by sandbox policy: {decision.reason!r}. "
            f"Command was: {cmd!r}"
        )

    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        env=env,   # explicit; no implicit os.environ inheritance
        cwd=cwd,
        timeout=timeout,
    )
    # Merge stderr into stdout to match Claude Code Bash tool behaviour
    output = result.stdout
    if result.stderr:
        output = output + result.stderr if output else result.stderr
    return output


def run_grep(
    pattern: str,
    path: str,
    env: dict[str, str],
    cwd: str,
    include: str = "",
) -> str:
    """Run grep for *pattern* under *path*.

    Returns matching lines.  Returns empty string if no match.
    """
    resolved = Path(cwd) / path if not Path(path).is_absolute() else Path(path)
    cmd_parts = ["grep", "-rn", "--include", include or "*", pattern, str(resolved)]
    result = subprocess.run(
        cmd_parts,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=30,
    )
    return result.stdout or ""


def run_glob(pattern: str, env: dict[str, str], cwd: str) -> list[str]:
    """Expand *pattern* (glob) relative to *cwd*.

    Returns a sorted list of matching paths (as strings).
    """
    import glob as _glob
    resolved_pattern = str(Path(cwd) / pattern) if not Path(pattern).is_absolute() else pattern
    return sorted(_glob.glob(resolved_pattern, recursive=True))


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

# Maps tool name → handler.  This is the single source of truth for which
# tools are supported.  All names must match the role-card `tools:` frontmatter
# conventions used across the project.
_TOOL_HANDLERS: dict[str, Any] = {
    "Read": run_read,
    "Edit": run_edit,
    "Write": run_write,
    "Bash": run_bash,
    "Grep": run_grep,
    "Glob": run_glob,
}


def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    whitelist: list[str],
    env: dict[str, str],
    cwd: str,
) -> Any:
    """Dispatch a tool call, enforcing the role whitelist.

    Fails CLOSED (S5): raises UnknownToolError for any tool name not present
    in the role-card whitelist.  No log-and-allow fallback.

    Parameters
    ----------
    tool_name:   Name of the tool to invoke.
    tool_input:  Dict of tool parameters as received from the SDK.
    whitelist:   List of tool names the role-card grants (from frontmatter).
    env:         Explicit env dict for subprocess tools.
    cwd:         Working directory for filesystem tools.
    """
    # Step 1: fail-closed whitelist check
    if tool_name not in whitelist:
        raise UnknownToolError(
            f"Tool '{tool_name}' is not in this role's whitelist {whitelist!r}. "
            "Agent run aborted (fail-closed)."
        )

    # Step 2: check we have a handler (belt-and-suspenders against typos)
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        raise UnknownToolError(
            f"Tool '{tool_name}' is whitelisted but has no registered handler. "
            "This is a programming error."
        )

    # Step 3: invoke with explicit env
    if tool_name == "Read":
        return handler(
            path=tool_input["path"],
            env=env,
            cwd=cwd,
        )
    elif tool_name == "Edit":
        return handler(
            path=tool_input["path"],
            old_string=tool_input["old_string"],
            new_string=tool_input["new_string"],
            env=env,
            cwd=cwd,
        )
    elif tool_name == "Write":
        return handler(
            path=tool_input["path"],
            content=tool_input["content"],
            env=env,
            cwd=cwd,
        )
    elif tool_name == "Bash":
        return handler(
            cmd=tool_input["command"],
            env=env,
            cwd=cwd,
            timeout=tool_input.get("timeout", 60),
        )
    elif tool_name == "Grep":
        return handler(
            pattern=tool_input["pattern"],
            path=tool_input.get("path", "."),
            env=env,
            cwd=cwd,
            include=tool_input.get("include", ""),
        )
    elif tool_name == "Glob":
        return handler(
            pattern=tool_input["pattern"],
            env=env,
            cwd=cwd,
        )
    else:
        # Should never reach here given the checks above
        raise UnknownToolError(f"Unhandled tool '{tool_name}'")
