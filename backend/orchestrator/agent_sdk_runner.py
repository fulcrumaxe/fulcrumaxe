"""backend/orchestrator/agent_sdk_runner.py — Subscription backend for the orchestrator.

Drives claude_agent_sdk.query() to execute an agent defined by a spawn spec.
Uses the Claude Pro/Max subscription — either via CLAUDE_CODE_OAUTH_TOKEN env var,
or via the stored login in ~/.claude/.credentials.json when the `claude` CLI is
already authenticated.

Security invariants:
  S1-sub: No ANTHROPIC_API_KEY is loaded or required. When an OAuth token is
      available it is passed to ClaudeAgentOptions.env so it reaches the claude
      CLI subprocess.  Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are
      explicitly neutralized (set to "") in sdk_env so they cannot leak from
      the parent process and trigger API-key billing regardless of which auth
      path is taken.  The CLI's auth checks are truthiness-based so empty string
      is treated the same as absent.
  S3: Discussion bodies, PR diffs, issue bodies, fetched URL content, and
      search results are wrapped in <untrusted>...</untrusted> delimiters.
  S4: Each run writes one row to agent_run and one line to audit.jsonl.
  S6: OAuth token never written to logs or audit trail.
  Tool security: All tools are exposed via MCP wrappers (mcp_tools.py) that
      reuse tool_proxy's fail-closed whitelist + credential blocking.

Design choice vs Implementation Notes:
  The Implementation Notes suggested passing `ClaudeAgentOptions.env` with the
  OAuth token. This is correct. The SDK drives the claude CLI subprocess and the
  CLI reads CLAUDE_CODE_OAUTH_TOKEN from its environment for subscription auth.
  We do NOT hand-roll a messages loop — the SDK's built-in CLI tool loop handles
  tool calls through the MCP server we register. This is simpler and avoids
  re-implementing what the SDK already does.

Usage::

    import asyncio
    from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner, SpawnSpec

    spec = SpawnSpec(
        role="code-reviewer",
        role_card_path=".claude/agents/code-reviewer.md",
        task_prompt="Review PR #42",
        tool_whitelist=["Read", "Bash", "Grep"],
        isolation="worktree",
        worktree_path="/path/to/wt",
        env_allowlist=["PATH", "HOME", "GH_TOKEN"],
    )
    runner = ClaudeAgentSDKRunner()
    result = asyncio.run(runner.run(spec))
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency guard
# ---------------------------------------------------------------------------

try:
    from claude_agent_sdk import query as sdk_query  # type: ignore[import]
    from claude_agent_sdk.types import (  # type: ignore[import]
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
    )
except ImportError as _err:
    raise ImportError(
        "The 'claude-agent-sdk' package is required for the subscription backend. "
        "Install it with: pip install claude-agent-sdk"
    ) from _err

# Re-use types and helpers from the API-key backend — same RunResult shape,
# same audit/agent_run write, same verdict extraction, same redaction.
from backend.orchestrator.sdk_runner import (
    RunResult,
    SpawnSpec,
    _SYSTEM_PROMPT_TEMPLATE,
    _extract_verdict,
    _load_role_card,
    _now_iso,
    _prompt_sha256,
    _write_agent_run,
    _write_audit,
    build_user_message,
)
from backend.orchestrator.tool_proxy import build_env
from backend.orchestrator.mcp_tools import build_mcp_server
from backend.orchestrator.redact import redact


# ---------------------------------------------------------------------------
# OAuth token loader (S1-sub)
# ---------------------------------------------------------------------------

_CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")


def detect_sdk_credential(credentials_path: Optional[str] = None) -> Optional[str]:
    """Detect which SDK credential kind is available, without reading the value.

    Returns the KIND of credential present, in precedence order:
      - "oauth_token"  if CLAUDE_CODE_OAUTH_TOKEN is set in the environment
      - "api_key"      if ANTHROPIC_API_KEY is set in the environment
      - "login"        if ~/.claude/.credentials.json exists (claude CLI stored login)
      - None           if no credential is available

    Security invariants:
      - The credential VALUE is never read or logged — only presence is detected.
      - The login file is checked for existence only (os.path.exists), not parsed.
      - ANTHROPIC_API_KEY is checked second; its presence does NOT suppress the
        login fallback.  The caller decides how to route based on the returned kind.

    Precedence rationale:
      1. CLAUDE_CODE_OAUTH_TOKEN: explicit env-var subscription token — highest
         priority, mirrors what the CLI does.
      2. ANTHROPIC_API_KEY: explicit env-var API key — API-key billing.
      3. ~/.claude/.credentials.json: stored claude CLI login — subscription billing
         (the normal case on subscription-login machines where neither env var is set).

    Parameters
    ----------
    credentials_path:
        Override the credentials file path (for testing). When None, uses the
        module-level _CREDENTIALS_FILE path (~/.claude/.credentials.json).

    Returns
    -------
    str | None
        One of "oauth_token", "api_key", "login", or None.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "oauth_token"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key"
    creds_file = credentials_path if credentials_path is not None else _CREDENTIALS_FILE
    if os.path.exists(creds_file):
        return "login"
    return None


def _load_oauth_token() -> Optional[str]:
    """Load the OAuth token for subscription auth, with two fallback paths.

    Returns
    -------
    str
        The token from CLAUDE_CODE_OAUTH_TOKEN (env-var path).
    None
        When CLAUDE_CODE_OAUTH_TOKEN is unset but the claude CLI is already
        logged in via ~/.claude/.credentials.json.  The caller should proceed
        WITHOUT setting CLAUDE_CODE_OAUTH_TOKEN in sdk_env — the claude
        subprocess will pick up its own stored login automatically.

    Raises
    ------
    RuntimeError
        When neither CLAUDE_CODE_OAUTH_TOKEN is set NOR the credentials file
        exists.  This is the real-run guard: we must not silently fall through
        to API-key billing (ANTHROPIC_API_KEY is still neutralized below).

    Notes
    -----
    Do NOT fall back to ANTHROPIC_API_KEY. That would silently switch from
    subscription auth to API-key billing without any warning.
    The env-leak neutralization (ANTHROPIC_API_KEY="" / ANTHROPIC_AUTH_TOKEN="")
    is applied in sdk_env construction regardless of which path is taken.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token

    # Subscription login stored on disk (e.g. `claude` CLI already authenticated)
    if os.path.exists(_CREDENTIALS_FILE):
        logger.debug(
            "CLAUDE_CODE_OAUTH_TOKEN not set; using stored login at %s",
            _CREDENTIALS_FILE,
        )
        return None  # caller will omit CLAUDE_CODE_OAUTH_TOKEN from sdk_env

    raise RuntimeError(
        "CLAUDE_CODE_OAUTH_TOKEN is not set and no stored login found at "
        f"{_CREDENTIALS_FILE}. "
        "Either set CLAUDE_CODE_OAUTH_TOKEN in your environment or log in with "
        "`claude` to use subscription auth. "
        "Do NOT set ANTHROPIC_API_KEY — it would override the subscription and "
        "use API-key billing instead."
    )


# ---------------------------------------------------------------------------
# ClaudeAgentSDKRunner
# ---------------------------------------------------------------------------

class ClaudeAgentSDKRunner:
    """Runs a single agent spawn via the Claude Agent SDK (subscription auth).

    Interchangeable with SDKRunner — both accept a SpawnSpec and return RunResult.
    The subscription backend differs in three ways:
      1. Auth via CLAUDE_CODE_OAUTH_TOKEN (no API key).
      2. The claude CLI drives its own tool loop; tools are exposed via MCP.
      3. Token counts come from ResultMessage.usage (not per-turn response.usage).
    """

    def __init__(self, oauth_token: Optional[str] = None) -> None:
        """Load OAuth token (or accept override for tests).

        Parameters
        ----------
        oauth_token:
            Override for testing. In production, the token is loaded from
            CLAUDE_CODE_OAUTH_TOKEN at run() call time (not __init__) so that
            tests that mock os.environ don't need to set it during construction.
        """
        self._oauth_token_override = oauth_token

    async def run(self, spec: SpawnSpec, auto_routed: Optional[bool] = None) -> RunResult:
        """Execute an agent spawn according to *spec* via subscription auth.

        Parameters
        ----------
        spec:
            The spawn specification for this agent run.
        auto_routed:
            True  — run was routed via SDK_AUTO_ROUTE gate (auto).
            False — explicit --sdk-lane opt-in.
            None  — CC run or pre-D#1364 row.
            Threaded in from dispatch so the value is written to the DB row
            BEFORE _write_agent_run is called.

        Returns
        -------
        RunResult
            Same shape as SDKRunner.run() — verdict, token counts, audit fields.
        """
        import time as _time

        start_ts = _now_iso()
        start_wall = _time.monotonic()

        agent_id = spec.agent_id or (
            f"{spec.role}-{spec.discussion or 'nod'}-"
            f"{int(datetime.now(timezone.utc).timestamp())}"
        )

        # Load OAuth token (S1-sub).
        # _oauth_token_override=None means "use env / stored login"; distinguish
        # the test-override case (explicit string) from the no-override case (None).
        oauth_token: Optional[str]
        if self._oauth_token_override is not None:
            # Explicit override (e.g. in tests) — use as-is.
            oauth_token = self._oauth_token_override
        else:
            # Returns a token string OR None (stored login path).
            oauth_token = _load_oauth_token()

        # Build clean env for tool proxy handlers (S2)
        tool_env = build_env(spec.env_allowlist)
        cwd = spec.worktree_path or "."

        # Load role card instructions
        role_card_instructions = _load_role_card(spec.role_card_path)

        # Build system prompt and user message (S3)
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            role=spec.role,
            role_card_instructions=role_card_instructions,
        )
        user_message = build_user_message(spec)
        prompt_sha = _prompt_sha256(system_prompt + user_message)

        # Build MCP server — our tool_proxy handlers exposed as in-process MCP
        mcp_server = build_mcp_server(
            whitelist=spec.tool_whitelist,
            env=tool_env,
            cwd=cwd,
        )

        # SpawnSpec → ClaudeAgentOptions mapping:
        #   system_prompt  ← role card + SYSTEM_PROMPT_TEMPLATE
        #   cwd            ← spec.worktree_path
        #   tools          ← [] (disable built-in claude-code tools; MCP handles everything)
        #   allowed_tools  ← MCP-namespaced tool names (mcp__tools__<name>) matching the
        #                    "tools" server key; bare names ("Bash") would be a no-op because
        #                    the SDK exposes MCP tools as mcp__<server>__<tool>, not bare names
        #   mcp_servers    ← {"tools": mcp_server} — our in-process MCP
        #   permission_mode← "bypassPermissions" (MCP wrappers enforce security)
        #   env            ← {CLAUDE_CODE_OAUTH_TOKEN: ...} — subscription auth
        #   model          ← from spec (optional, falls back to CLI default)
        #   max_turns      ← from spec (optional)
        #
        # GOTCHA (env-leak): claude_agent_sdk merges options.env OVER os.environ,
        # not replacing it.  A parent-process ANTHROPIC_API_KEY or
        # ANTHROPIC_AUTH_TOKEN would survive the merge and, since the CLI checks
        # them with truthiness (if(process.env.ANTHROPIC_API_KEY)), would take
        # precedence over CLAUDE_CODE_OAUTH_TOKEN and bill via API key instead.
        #
        # Fix: explicitly set both to "" in sdk_env so the SDK merge overrides
        # any parent value with an empty string.  The CLI's auth checks are
        # truthiness-based (!!process.env.ANTHROPIC_API_KEY, confirmed in CLI
        # binary strings), so "" is treated identically to "not set".
        #
        # When oauth_token is None the claude subprocess uses its own stored
        # login (~/.claude/.credentials.json, key claudeAiOauth).  We still
        # neutralize ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN to prevent a
        # stray parent-process API key from overriding the subscription path.
        sdk_env: dict[str, str] = {
            # Neutralize higher-precedence credentials that may be set in the
            # parent environment.  Empty string is falsy in JavaScript so the
            # CLI skips both keys and falls through to the subscription auth.
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }
        if oauth_token is not None:
            # Pass the token explicitly so the claude subprocess uses it.
            sdk_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
        # When oauth_token is None: omit CLAUDE_CODE_OAUTH_TOKEN entirely so
        # the claude subprocess discovers its own stored login.  The env-leak
        # neutralization above still applies.
        # Note: the token itself is never written to audit/logs; only prompt_sha256 is.

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            cwd=cwd,
            tools=[],  # disable built-in tools; MCP server handles everything
            allowed_tools=[f"mcp__tools__{name}" for name in spec.tool_whitelist],  # MCP-namespaced: mcp__<server>__<tool>
            mcp_servers={"tools": mcp_server},
            permission_mode="bypassPermissions",
            env=sdk_env,
            model=getattr(spec, "model", None) or None,
            max_turns=getattr(spec, "max_turns", None) or None,
            # Explicit on purpose (D#1790) — same reasoning as sdk_lane.py's
            # build_options: "project" loads .claude/settings.json (the
            # PreToolUse sandbox hook) and is required for CLAUDE.md loading.
            # This site disables built-in tools (tools=[] above) so the
            # PreToolUse Bash/Edit/Write/Agent hook has nothing to fire on
            # here — this runner's containment is tool_proxy's in-process
            # checks (see test_sdk_lane_sandbox_boundary.py). Still set
            # explicitly rather than left to an implicit SDK default, for
            # CLAUDE.md loading and so both ClaudeAgentOptions construction
            # sites stay consistent. "local" omitted: .claude/settings.local.json
            # is untracked (repo-local, gitignored) and unreviewable — the CLI
            # default this replaces did load local scope; excluding it here is
            # a deliberate narrowing, not an oversight, so don't "fix" this
            # back to including "local" without addressing the untracked-and-
            # unreviewable problem first.
            setting_sources=["user", "project"],
        )

        total_input_tokens = 0
        total_output_tokens = 0
        tool_calls_count = 0
        final_text = ""
        verdict = "unknown"
        error: Optional[str] = None

        try:
            async for message in sdk_query(prompt=user_message, options=options):
                if isinstance(message, AssistantMessage):
                    # Collect text from content blocks
                    text_parts = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                    if text_parts:
                        final_text = "\n".join(text_parts)

                    # Count tool uses (ToolUseBlock in content)
                    from claude_agent_sdk.types import ToolUseBlock  # type: ignore[import]
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls_count += 1

                    # Token usage per turn (may be None on some turns)
                    if message.usage:
                        total_input_tokens += message.usage.get("input_tokens", 0)
                        total_output_tokens += message.usage.get("output_tokens", 0)

                elif isinstance(message, ResultMessage):
                    # ResultMessage carries final cumulative usage
                    if message.usage:
                        # Prefer ResultMessage.usage totals when available
                        # (avoids double-counting with per-turn AssistantMessage.usage)
                        total_input_tokens = message.usage.get("input_tokens", total_input_tokens)
                        total_output_tokens = message.usage.get("output_tokens", total_output_tokens)
                    # result field holds the final text if any
                    if message.result:
                        final_text = message.result

            verdict = _extract_verdict(final_text)

        except Exception as e:  # noqa: BLE001
            error = str(e)
            verdict = "fail"
            final_text = f"[Agent SDK runner error: {e}]"
            logger.error("ClaudeAgentSDKRunner error for %s: %s", agent_id, e, exc_info=True)

        end_ts = _now_iso()

        result = RunResult(
            agent_id=agent_id,
            role=spec.role,
            discussion=spec.discussion,
            pr=spec.pr,
            verdict=verdict,
            final_text=final_text,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            tool_calls_count=tool_calls_count,
            prompt_sha256=prompt_sha,
            start_ts=start_ts,
            end_ts=end_ts,
            error=error,
            routed_via="sdk",
            auto_routed=auto_routed,  # set BEFORE _write_agent_run so the DB row is not NULL
        )

        # Write agent_run row (AC5)
        _write_agent_run(result)

        # Write audit line (S4, S6 — token never written)
        _write_audit({
            "event": "agent_sdk_run",
            "backend": "subscription",
            "agent_id": agent_id,
            "role": spec.role,
            "discussion": spec.discussion,
            "pr": spec.pr,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "verdict": verdict,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "prompt_sha256": prompt_sha,
            "tool_calls_count": tool_calls_count,
            "error": error,
            # oauth_token is intentionally ABSENT from this record (S6)
        })

        return result
