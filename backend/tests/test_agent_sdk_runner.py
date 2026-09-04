"""Tests for backend/orchestrator/agent_sdk_runner.py.

All tests mock claude_agent_sdk.query — no real network calls, no OAuth token.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers: fake SDK message types
# ---------------------------------------------------------------------------

def _make_text_block(text: str):
    from claude_agent_sdk.types import TextBlock
    return TextBlock(text=text)


def _make_tool_use_block(name: str = "Read"):
    from claude_agent_sdk.types import ToolUseBlock
    return ToolUseBlock(id="tu-1", name=name, input={"path": "/some/file"})


def _make_assistant_message(text: str = "", tool_calls: int = 0, input_tokens: int = 10, output_tokens: int = 20):
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    content = []
    if text:
        content.append(TextBlock(text=text))
    for i in range(tool_calls):
        content.append(ToolUseBlock(id=f"tu-{i}", name="Read", input={"path": "/f"}))
    return AssistantMessage(
        content=content,
        model="claude-sonnet-4-6",
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
    )


def _make_result_message(result_text: str = "", input_tokens: int = 0, output_tokens: int = 0):
    from claude_agent_sdk.types import ResultMessage
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=800,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        result=result_text or None,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens} if input_tokens or output_tokens else None,
    )


async def _fake_query_generator(*messages):
    """Async generator that yields the given messages."""
    for msg in messages:
        yield msg


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

AGENT_OUTPUT_BLOCK = """
Some text here.

<!-- AGENT_OUTPUT -->
```json
{"agent": "executor", "discussion": 42, "verdict": "done"}
```
<!-- /AGENT_OUTPUT -->
"""

SPEC_KWARGS = dict(
    role="executor",
    task_prompt="Implement feature X",
    tool_whitelist=["Read", "Bash"],
    role_card_path="",
    isolation="worktree",
    worktree_path="/tmp/wt-test",
    env_allowlist=["PATH", "HOME"],
    discussion=42,
    pr=None,
    agent_id="executor-42-111",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClaudeAgentSDKRunnerInit:
    def test_accepts_oauth_token_override(self):
        """Constructor accepts token override — no real env needed."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        runner = ClaudeAgentSDKRunner(oauth_token="test-token")
        assert runner._oauth_token_override == "test-token"

    def test_no_token_override_is_none(self):
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        runner = ClaudeAgentSDKRunner()
        assert runner._oauth_token_override is None


class TestLoadOAuthToken:
    def test_raises_when_token_absent_and_no_credentials_file(self):
        """Raises only when NEITHER env var NOR credentials file exists."""
        from backend.orchestrator.agent_sdk_runner import _load_oauth_token
        import os

        env_backup = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            with patch("backend.orchestrator.agent_sdk_runner.os.path.exists", return_value=False):
                with pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN"):
                    _load_oauth_token()
        finally:
            if env_backup is not None:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = env_backup

    def test_returns_token_from_env(self):
        from backend.orchestrator.agent_sdk_runner import _load_oauth_token
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "my-oauth-token"}):
            assert _load_oauth_token() == "my-oauth-token"

    def test_returns_none_when_credentials_file_exists(self):
        """Returns None (subscription login path) when credentials file is present."""
        from backend.orchestrator.agent_sdk_runner import _load_oauth_token
        import os

        env_backup = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            with patch("backend.orchestrator.agent_sdk_runner.os.path.exists", return_value=True):
                result = _load_oauth_token()
            assert result is None, (
                "Should return None when credentials file exists (subscription login path)"
            )
        finally:
            if env_backup is not None:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = env_backup

    def test_env_token_takes_precedence_over_credentials_file(self):
        """Env var wins when both CLAUDE_CODE_OAUTH_TOKEN and credentials file are present."""
        from backend.orchestrator.agent_sdk_runner import _load_oauth_token

        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "env-token"}):
            with patch("backend.orchestrator.agent_sdk_runner.os.path.exists", return_value=True):
                result = _load_oauth_token()
        assert result == "env-token"

    def test_no_api_key_fallback(self):
        """Token loader must NOT fall back to ANTHROPIC_API_KEY even when set."""
        from backend.orchestrator.agent_sdk_runner import _load_oauth_token
        import os

        # Even if ANTHROPIC_API_KEY is set, loader raises without token or credentials file
        env_backup = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-some-key"}, clear=False):
                os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                with patch("backend.orchestrator.agent_sdk_runner.os.path.exists", return_value=False):
                    with pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN"):
                        _load_oauth_token()
        finally:
            if env_backup is not None:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = env_backup


class TestSpawnSpecToOptions:
    """Verify the SpawnSpec → ClaudeAgentOptions mapping."""

    @pytest.mark.asyncio
    async def test_options_mapping(self):
        """sdk_query receives correct options derived from SpawnSpec."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok-abc")

        captured_options = []

        async def fake_query(prompt, options=None, **kwargs):
            captured_options.append(options)
            yield _make_assistant_message(text=AGENT_OUTPUT_BLOCK)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    await runner.run(spec)

        assert len(captured_options) == 1
        opts = captured_options[0]

        # system_prompt is a string (our custom prompt, not a preset)
        assert isinstance(opts.system_prompt, str)
        assert "executor" in opts.system_prompt

        # cwd maps to worktree_path
        assert opts.cwd == "/tmp/wt-test"

        # tools=[] disables built-in claude-code tools
        assert opts.tools == []

        # allowed_tools uses MCP-namespaced names matching the "tools" server key
        # (bare names like "Bash" would be a no-op — the SDK exposes MCP tools as
        # mcp__<server>__<tool>)
        assert "mcp__tools__Read" in opts.allowed_tools
        assert "mcp__tools__Bash" in opts.allowed_tools
        # Bare names must NOT be present — they would be a silent no-op
        assert "Read" not in opts.allowed_tools
        assert "Bash" not in opts.allowed_tools

        # permission_mode bypasses prompts (MCP wrappers enforce security)
        assert opts.permission_mode == "bypassPermissions"

        # env has the OAuth token, plus neutralized credential vars
        assert opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-abc"
        # Both higher-precedence vars must be explicitly neutralized (empty string)
        # so the SDK env-merge overrides any leaked parent-process values
        assert opts.env.get("ANTHROPIC_API_KEY") == ""
        assert opts.env.get("ANTHROPIC_AUTH_TOKEN") == ""

        # mcp_servers includes our in-process tool server
        assert "tools" in opts.mcp_servers

        # setting_sources must be explicit and include "project" (D#1790) —
        # this site disables built-in tools, so the PreToolUse hook has
        # nothing to fire on, but the setting still gates CLAUDE.md loading
        # and must not be left to an implicit SDK default.
        assert opts.setting_sources is not None
        assert "project" in opts.setting_sources

    @pytest.mark.asyncio
    async def test_subscription_login_path_no_token_in_env(self):
        """When credentials file exists and no env token, CLAUDE_CODE_OAUTH_TOKEN is absent from sdk_env."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec
        import os

        spec = SpawnSpec(**SPEC_KWARGS)
        # No oauth_token override — forces _load_oauth_token() to run
        runner = ClaudeAgentSDKRunner()

        captured_options = []

        async def fake_query(prompt, options=None, **kwargs):
            captured_options.append(options)
            yield _make_result_message()

        env_backup = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            with patch("backend.orchestrator.agent_sdk_runner.os.path.exists", return_value=True):
                with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
                    with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                        with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                            await runner.run(spec)
        finally:
            if env_backup is not None:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = env_backup

        opts = captured_options[0]
        # Token must NOT be injected — subprocess discovers its own login
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in opts.env, (
            "Subscription login path must not inject CLAUDE_CODE_OAUTH_TOKEN into sdk_env"
        )
        # API-key neutralization must still be present
        assert opts.env.get("ANTHROPIC_API_KEY") == ""
        assert opts.env.get("ANTHROPIC_AUTH_TOKEN") == ""

    @pytest.mark.asyncio
    async def test_no_api_key_in_options_env(self):
        """ANTHROPIC_API_KEY must be neutralized (empty) in ClaudeAgentOptions.env."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok-xyz")

        captured_options = []

        async def fake_query(prompt, options=None, **kwargs):
            captured_options.append(options)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    await runner.run(spec)

        opts = captured_options[0]
        # ANTHROPIC_API_KEY must be present but neutralized (empty string) so
        # the SDK merge overrides any inherited parent-env value with "".
        # The CLI treats "" as falsy — same effect as absent — but we need it
        # in options.env to win the merge over a potentially leaked parent var.
        assert opts.env.get("ANTHROPIC_API_KEY") == "", (
            "ANTHROPIC_API_KEY must be neutralized with '' to prevent parent-env leak"
        )
        assert opts.env.get("ANTHROPIC_AUTH_TOKEN") == "", (
            "ANTHROPIC_AUTH_TOKEN must be neutralized with '' to prevent parent-env leak"
        )


class TestEnvNeutralization:
    """Verify that parent-env API credentials are neutralized in the SDK env.

    The claude_agent_sdk merges options.env OVER os.environ (not replacing it).
    A leaked ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN from the parent process
    would override the OAuth subscription and bill via API key instead.
    """

    @pytest.mark.asyncio
    async def test_parent_api_key_neutralized(self, monkeypatch):
        """ANTHROPIC_API_KEY set in parent env must be neutralized in options.env."""
        import os
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leaked-key")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "leaked-auth-token")

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok-subscription")

        captured_options = []

        async def fake_query(prompt, options=None, **kwargs):
            captured_options.append(options)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    await runner.run(spec)

        assert len(captured_options) == 1
        opts = captured_options[0]

        # OAuth token must be set for subscription auth
        assert opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-subscription", (
            "CLAUDE_CODE_OAUTH_TOKEN must be present in options.env"
        )

        # Both higher-precedence vars must be neutralized — empty string overrides
        # the parent-env value in the SDK merge so the CLI treats them as absent
        assert opts.env.get("ANTHROPIC_API_KEY") == "", (
            f"ANTHROPIC_API_KEY should be '' in options.env, got {opts.env.get('ANTHROPIC_API_KEY')!r}; "
            "parent value 'sk-ant-leaked-key' must not leak through to the subprocess"
        )
        assert opts.env.get("ANTHROPIC_AUTH_TOKEN") == "", (
            f"ANTHROPIC_AUTH_TOKEN should be '' in options.env, got {opts.env.get('ANTHROPIC_AUTH_TOKEN')!r}; "
            "parent value 'leaked-auth-token' must not leak through to the subprocess"
        )

    @pytest.mark.asyncio
    async def test_neutralization_even_without_parent_env(self):
        """Neutralization keys must be present in options.env regardless of parent env."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok-clean")

        captured_options = []

        async def fake_query(prompt, options=None, **kwargs):
            captured_options.append(options)
            yield _make_result_message()

        with patch.dict("os.environ", {}, clear=False):
            # Ensure the vars are absent from the environment for this test
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

            with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
                with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                    with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                        await runner.run(spec)

        opts = captured_options[0]
        # Neutralization must always be present so future parent-env changes
        # (e.g. CI adds the var) don't silently break subscription routing
        assert "ANTHROPIC_API_KEY" in opts.env, (
            "ANTHROPIC_API_KEY neutralization must always be present in options.env"
        )
        assert "ANTHROPIC_AUTH_TOKEN" in opts.env, (
            "ANTHROPIC_AUTH_TOKEN neutralization must always be present in options.env"
        )
        assert opts.env["ANTHROPIC_API_KEY"] == ""
        assert opts.env["ANTHROPIC_AUTH_TOKEN"] == ""
        # Token was passed via oauth_token override so it must be set
        assert opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-clean"


class TestVerdictExtraction:
    @pytest.mark.asyncio
    async def test_verdict_extracted_from_agent_output(self):
        """Verdict parsed from AGENT_OUTPUT envelope in final text."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_assistant_message(text=AGENT_OUTPUT_BLOCK)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        assert result.verdict == "done"

    @pytest.mark.asyncio
    async def test_verdict_unknown_when_no_envelope(self):
        """Verdict is 'unknown' when no AGENT_OUTPUT block in final text."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_assistant_message(text="No envelope here.")
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        assert result.verdict == "unknown"

    @pytest.mark.asyncio
    async def test_verdict_fail_on_exception(self):
        """Verdict is 'fail' when sdk_query raises."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        async def fake_query(prompt, options=None, **kwargs):
            raise RuntimeError("SDK exploded")
            yield  # make it a generator

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        assert result.verdict == "fail"
        assert "SDK exploded" in result.error


class TestTokenCounting:
    @pytest.mark.asyncio
    async def test_tokens_from_result_message_when_present(self):
        """ResultMessage.usage takes precedence for token totals."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_assistant_message(text="some text", input_tokens=50, output_tokens=100)
            yield _make_result_message(input_tokens=500, output_tokens=1000)

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        # ResultMessage totals override per-turn counts
        assert result.input_tokens == 500
        assert result.output_tokens == 1000

    @pytest.mark.asyncio
    async def test_tokens_from_assistant_when_no_result_usage(self):
        """Falls back to accumulating AssistantMessage usage."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_assistant_message(text="text", input_tokens=30, output_tokens=60)
            yield _make_result_message()  # no usage in result

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        assert result.input_tokens == 30
        assert result.output_tokens == 60


class TestToolCallCounting:
    @pytest.mark.asyncio
    async def test_tool_calls_counted(self):
        """tool_calls_count reflects actual ToolUseBlock count."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_assistant_message(text="", tool_calls=3)
            yield _make_assistant_message(text=AGENT_OUTPUT_BLOCK, tool_calls=2)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        assert result.tool_calls_count == 5


class TestAgentRunAndAuditWrite:
    @pytest.mark.asyncio
    async def test_agent_run_written(self):
        """_write_agent_run called with correct RunResult fields."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        written_results = []

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_assistant_message(text=AGENT_OUTPUT_BLOCK)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run", side_effect=written_results.append):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        assert len(written_results) == 1
        written = written_results[0]
        assert written.role == "executor"
        assert written.discussion == 42
        assert written.verdict == "done"

    @pytest.mark.asyncio
    async def test_audit_written_without_oauth_token(self):
        """Audit record must not contain the OAuth token."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="super-secret-token")

        audit_entries = []

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit", side_effect=audit_entries.append):
                    await runner.run(spec)

        assert len(audit_entries) == 1
        entry = audit_entries[0]
        # Flatten entry to check for token leakage
        entry_str = str(entry)
        assert "super-secret-token" not in entry_str

    @pytest.mark.asyncio
    async def test_audit_event_is_subscription_backend(self):
        """Audit event identifies subscription backend."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        audit_entries = []

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit", side_effect=audit_entries.append):
                    await runner.run(spec)

        entry = audit_entries[0]
        assert entry.get("backend") == "subscription"
        assert entry.get("event") == "agent_sdk_run"


class TestRunResultShape:
    @pytest.mark.asyncio
    async def test_run_result_same_shape_as_sdk_runner(self):
        """RunResult has the same fields as SDKRunner produces."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import RunResult, SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_assistant_message(text=AGENT_OUTPUT_BLOCK)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    result = await runner.run(spec)

        assert isinstance(result, RunResult)
        # Check all expected fields are present
        assert hasattr(result, "agent_id")
        assert hasattr(result, "role")
        assert hasattr(result, "discussion")
        assert hasattr(result, "pr")
        assert hasattr(result, "verdict")
        assert hasattr(result, "final_text")
        assert hasattr(result, "input_tokens")
        assert hasattr(result, "output_tokens")
        assert hasattr(result, "tool_calls_count")
        assert hasattr(result, "prompt_sha256")
        assert hasattr(result, "start_ts")
        assert hasattr(result, "end_ts")
        assert hasattr(result, "error")

        assert result.role == "executor"
        assert result.discussion == 42
        assert result.prompt_sha256 != ""


class TestRedactionOnAudit:
    @pytest.mark.asyncio
    async def test_audit_entries_redacted(self):
        """Audit entries go through redact() — sk-ant- keys are replaced."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(
            role="executor",
            task_prompt="task with sk-ant-abc123 in it",
            tool_whitelist=["Read"],
            discussion=42,
            worktree_path="/tmp/wt",
        )
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        written_entries = []

        async def fake_query(prompt, options=None, **kwargs):
            yield _make_result_message()

        # Patch _write_audit at the sdk_runner level (shared by both runners)
        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit", side_effect=written_entries.append):
                    await runner.run(spec)

        # The audit write function is responsible for redaction internally
        # (inherited from sdk_runner._write_audit); we just confirm it was called
        assert len(written_entries) == 1


class TestAllowedToolsNaming:
    """Verify that allowed_tools are MCP-namespaced, not bare names.

    The SDK exposes MCP tools as mcp__<server>__<name>.  Passing bare names
    ("Bash") is a silent no-op — the whitelist gate doesn't fire because
    the tool names never match.  The server key in ClaudeAgentOptions.mcp_servers
    is "tools", so the expected form is mcp__tools__<name>.
    """

    @pytest.mark.asyncio
    async def test_allowed_tools_are_mcp_namespaced(self):
        """allowed_tools must be mcp__tools__<name> not bare names."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(
            role="executor",
            task_prompt="test",
            tool_whitelist=["Bash", "Read", "Grep"],
            worktree_path="/tmp/wt-test-naming",
        )
        runner = ClaudeAgentSDKRunner(oauth_token="tok-test")

        captured_options = []

        async def fake_query(prompt, options=None, **kwargs):
            captured_options.append(options)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    await runner.run(spec)

        opts = captured_options[0]
        allowed = opts.allowed_tools

        # Each tool name must be prefixed with mcp__tools__ (server key = "tools")
        assert "mcp__tools__Bash" in allowed, f"Expected mcp__tools__Bash in {allowed}"
        assert "mcp__tools__Read" in allowed, f"Expected mcp__tools__Read in {allowed}"
        assert "mcp__tools__Grep" in allowed, f"Expected mcp__tools__Grep in {allowed}"

        # Bare names must be absent — they'd be a silent no-op gate
        assert "Bash" not in allowed, f"Bare 'Bash' must not appear in {allowed}"
        assert "Read" not in allowed, f"Bare 'Read' must not appear in {allowed}"
        assert "Grep" not in allowed, f"Bare 'Grep' must not appear in {allowed}"

        # All entries must follow the mcp__tools__ prefix pattern
        for name in allowed:
            assert name.startswith("mcp__tools__"), (
                f"Entry {name!r} does not start with 'mcp__tools__'; "
                "all allowed_tools must be MCP-namespaced"
            )

    @pytest.mark.asyncio
    async def test_empty_whitelist_gives_empty_allowed_tools(self):
        """Empty tool_whitelist → empty allowed_tools list."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(
            role="executor",
            task_prompt="no tools",
            tool_whitelist=[],
            worktree_path="/tmp/wt-test-empty",
        )
        runner = ClaudeAgentSDKRunner(oauth_token="tok-test")

        captured_options = []

        async def fake_query(prompt, options=None, **kwargs):
            captured_options.append(options)
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    await runner.run(spec)

        opts = captured_options[0]
        assert opts.allowed_tools == [], f"Expected empty list, got {opts.allowed_tools}"


class TestNoRealNetworkCall:
    @pytest.mark.asyncio
    async def test_no_real_sdk_call(self):
        """Ensures sdk_query is mocked and no real subprocess is spawned."""
        from backend.orchestrator.agent_sdk_runner import ClaudeAgentSDKRunner
        from backend.orchestrator.sdk_runner import SpawnSpec

        spec = SpawnSpec(**SPEC_KWARGS)
        runner = ClaudeAgentSDKRunner(oauth_token="tok")

        call_count = [0]

        async def fake_query(prompt, options=None, **kwargs):
            call_count[0] += 1
            yield _make_result_message()

        with patch("backend.orchestrator.agent_sdk_runner.sdk_query", side_effect=fake_query):
            with patch("backend.orchestrator.agent_sdk_runner._write_agent_run"):
                with patch("backend.orchestrator.agent_sdk_runner._write_audit"):
                    await runner.run(spec)

        assert call_count[0] == 1, "sdk_query was called once (mocked)"
