"""Real end-to-end smoke test for ClaudeAgentSDKRunner using the subscription login.

This test actually invokes the claude subprocess via claude_agent_sdk.query().

**Opt-in required:** The real smoke test is SKIPPED by default even when
credentials are present (~/.claude/.credentials.json or CLAUDE_CODE_OAUTH_TOKEN).
This prevents routine `pytest backend/tests/` runs from spending subscription
tokens or spawning nested claude processes on every authenticated machine.

To run the real smoke test, set the explicit opt-in flag:

    RUN_SDK_SMOKE=1 pytest backend/tests/test_agent_sdk_runner_smoke.py -v -s

The test will be skipped unless BOTH conditions are true:
  1. RUN_SDK_SMOKE=1 is set in the environment
  2. Either CLAUDE_CODE_OAUTH_TOKEN is set OR ~/.claude/.credentials.json exists

Expected outcome (with opt-in + subscription login present):
  - Returns a RunResult with verdict != "unknown" (agent replied)
  - No exception raised
  - Token counts > 0
  - CLAUDE_CODE_OAUTH_TOKEN is NOT needed in the environment

If the nested claude subprocess is blocked by the worktree sandbox the test
catches that and emits a clear BLOCKED message instead of failing cryptically.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

_HAS_TOKEN = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
_HAS_CREDS = os.path.exists(os.path.expanduser("~/.claude/.credentials.json"))
_CAN_RUN = bool(os.environ.get("RUN_SDK_SMOKE")) and (_HAS_TOKEN or _HAS_CREDS)

_SMOKE_SKIP_REASON = (
    "Real run requires RUN_SDK_SMOKE=1 env var PLUS either "
    "CLAUDE_CODE_OAUTH_TOKEN or ~/.claude/.credentials.json (stored login)."
)


# ---------------------------------------------------------------------------
# Minimal SpawnSpec for the smoke run
# ---------------------------------------------------------------------------

_TRIVIAL_PROMPT = textwrap.dedent("""\
    You are a minimal test agent.
    Your ONLY task: reply with exactly this AGENT_OUTPUT envelope and nothing else.

    <!-- AGENT_OUTPUT -->
    ```json
    {"agent": "smoke-test", "verdict": "done", "discussion": null, "pr": null}
    ```
    <!-- /AGENT_OUTPUT -->

    Do not use any tools. Do not add any other text.
""")


@pytest.mark.skipif(not _CAN_RUN, reason=_SMOKE_SKIP_REASON)
@pytest.mark.asyncio
async def test_real_run_on_subscription_login():
    """Real end-to-end run through ClaudeAgentSDKRunner using the subscription login.

    This test:
    - Creates a ClaudeAgentSDKRunner with NO oauth_token override
    - Runs a trivial SpawnSpec (no tools, no side effects)
    - Asserts the RunResult has a valid verdict and token counts
    - Captures and prints the full result for review

    If the nested claude subprocess is blocked by the sandbox, the error
    is caught and re-raised with a clear BLOCKED prefix.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
    from backend.orchestrator.sdk_runner import SpawnSpec

    with tempfile.TemporaryDirectory(prefix="smoke-wt-") as tmp_wt:
        spec = SpawnSpec(
            role="smoke-test",
            task_prompt=_TRIVIAL_PROMPT,
            tool_whitelist=[],  # no tools — pure text response
            role_card_path="",
            isolation="none",
            worktree_path=tmp_wt,
            env_allowlist=["PATH", "HOME"],
            discussion=None,
            pr=None,
            agent_id="smoke-test-real-001",
        )

        runner = ClaudeAgentSDKRunner()  # no oauth_token override

        try:
            result = await asyncio.wait_for(runner.run(spec), timeout=120)
        except asyncio.TimeoutError:
            pytest.fail("ClaudeAgentSDKRunner.run() timed out after 120s")
        except Exception as exc:
            err_str = str(exc)
            if "sandbox" in err_str.lower() or "blocked" in err_str.lower():
                pytest.fail(
                    f"BLOCKED: nested claude subprocess blocked by worktree sandbox.\n"
                    f"This means ClaudeAgentSDKRunner can only run from the control-plane "
                    f"context, not from inside a sandboxed sub-agent worktree.\n"
                    f"Evidence: {err_str}"
                )
            raise

    # Print the full result so the Team Lead can paste it into the PR body
    print("\n" + "=" * 60)
    print("REAL RUN RESULT:")
    print(f"  verdict:       {result.verdict}")
    print(f"  input_tokens:  {result.input_tokens}")
    print(f"  output_tokens: {result.output_tokens}")
    print(f"  tool_calls:    {result.tool_calls_count}")
    print(f"  error:         {result.error!r}")
    print(f"  final_text snippet:")
    snippet = (result.final_text or "")[:400]
    print(textwrap.indent(snippet, "    "))
    print("=" * 60 + "\n")

    # Assertions
    assert result.verdict != "fail" or result.error is None, (
        f"Run failed with error: {result.error}"
    )
    # The agent should produce SOME output
    assert result.final_text, "final_text must not be empty"
    # Token counts should be populated (subscription login returns usage)
    assert result.input_tokens > 0 or result.output_tokens > 0, (
        "Token counts must be > 0 for a real run"
    )
    # verdict must be a known value
    assert result.verdict in ("done", "pass", "fail", "skip", "needs-fix", "unknown"), (
        f"Unexpected verdict: {result.verdict!r}"
    )


# ---------------------------------------------------------------------------
# Tool-exercising smoke test — permanent regression guard for MCP tool path
# ---------------------------------------------------------------------------

_TOOL_PROMPT = textwrap.dedent("""\
    You are a minimal test agent.
    Your ONLY task is to use the Bash tool to run the command:
      echo SMOKE_TOOL_OK
    Then report the output in your response AND emit this AGENT_OUTPUT envelope:

    <!-- AGENT_OUTPUT -->
    ```json
    {"agent": "smoke-tool-test", "verdict": "done", "discussion": null, "pr": null}
    ```
    <!-- /AGENT_OUTPUT -->

    Do NOT skip the tool call. The test verifies tool_calls > 0.
""")


@pytest.mark.skipif(not _CAN_RUN, reason=_SMOKE_SKIP_REASON)
@pytest.mark.asyncio
async def test_tool_exercising_smoke():
    """Real end-to-end run that FORCES a tool call through the MCP path.

    This test is the permanent regression guard for the allowed_tools naming fix.
    It verifies that:
      1. tool_calls_count > 0 (the Bash tool was actually invoked via MCP)
      2. "SMOKE_TOOL_OK" appears in the agent's final text (tool result flowed back)

    The existing smoke test uses tool_whitelist=[] and tool_calls=0, so it does NOT
    cover the MCP tool path at all.  This test covers that gap.

    If nested claude is blocked by the sandbox, emits a clear BLOCKED message.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
    from backend.orchestrator.sdk_runner import SpawnSpec

    with tempfile.TemporaryDirectory(prefix="smoke-tool-wt-") as tmp_wt:
        spec = SpawnSpec(
            role="smoke-tool-test",
            task_prompt=_TOOL_PROMPT,
            tool_whitelist=["Bash"],
            role_card_path="",
            isolation="none",
            worktree_path=tmp_wt,
            env_allowlist=["PATH", "HOME"],
            discussion=None,
            pr=None,
            agent_id="smoke-tool-test-001",
        )

        runner = ClaudeAgentSDKRunner()  # no oauth_token override

        try:
            result = await asyncio.wait_for(runner.run(spec), timeout=180)
        except asyncio.TimeoutError:
            pytest.fail("ClaudeAgentSDKRunner.run() (tool smoke) timed out after 180s")
        except Exception as exc:
            err_str = str(exc)
            if "sandbox" in err_str.lower() or "blocked" in err_str.lower():
                pytest.fail(
                    f"BLOCKED: nested claude subprocess blocked by worktree sandbox.\n"
                    f"This smoke test must run from the control-plane context.\n"
                    f"Evidence: {err_str}"
                )
            raise

    # Print full result for Team Lead to paste into PR body
    print("\n" + "=" * 60)
    print("TOOL SMOKE RESULT:")
    print(f"  verdict:       {result.verdict}")
    print(f"  tool_calls:    {result.tool_calls_count}")
    print(f"  input_tokens:  {result.input_tokens}")
    print(f"  output_tokens: {result.output_tokens}")
    print(f"  error:         {result.error!r}")
    print(f"  final_text snippet:")
    snippet = (result.final_text or "")[:600]
    print(textwrap.indent(snippet, "    "))
    print("=" * 60 + "\n")

    # Core assertions: the MCP tool path must have actually fired
    assert result.tool_calls_count > 0, (
        f"Expected tool_calls_count > 0 but got {result.tool_calls_count}. "
        "The Bash tool was not invoked — MCP tool path may be broken."
    )
    assert "SMOKE_TOOL_OK" in (result.final_text or ""), (
        f"Expected 'SMOKE_TOOL_OK' in final_text but it was absent.\n"
        f"final_text: {(result.final_text or '')[:400]!r}\n"
        "The tool output did not flow back to the agent."
    )
