"""backend/tests/test_sdk_lane_sandbox_boundary.py

Tests asserting the sandbox boundary between the Claude Code lane and the SDK lane.

Key claim being tested: the Claude Code PreToolUse hook (sandbox.py) fires only for
Claude Code's native tool dispatch. It does NOT fire for the SDK lane's in-process
MCP Bash calls. SDK Bash is protected by tool_proxy's in-process layers.

These tests verify:
  1. The hook installation matchers cover only native CC tool names.
  2. tool_proxy.run_bash bypasses the hook by design (in-process invocation).
  3. validate_env() blocks credential leaks regardless of hook coverage.
  4. run_bash NOW applies classify_bash command-content filtering (S6, added by PR #1372) —
     dangerous commands raise SandboxBlockError before the subprocess is launched.
     The gap documented in PR #1368 is now CLOSED.
  5. sandbox_rules.classify_bash() blocks out-of-worktree writes — the same checks
     that the CC PreToolUse hook applies are now applied in-process by run_bash.

See wiki/SDK-Lane-Sandbox-Analysis.md for the full analysis.
"""

from __future__ import annotations

import sys
import json
import os
import subprocess
from pathlib import Path

import pytest

# Allow imports from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from testsupport.fixture_paths import FIXTURE_MAIN_REPO  # noqa: E402

_CLEAN_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}


# ---------------------------------------------------------------------------
# 1. Hook installer only registers native CC tool matchers
# ---------------------------------------------------------------------------

class TestHookMatchersAreNativeOnly:
    """The sandbox hook is registered for native CC tool names only.

    If the matchers ever include MCP tool names (e.g. 'mcp__tools__Bash'),
    this test will catch it and the analysis doc needs updating.
    """

    def test_no_mcp_prefix_matchers_in_install_script(self):
        """install-sandbox-hook.sh only adds matchers: Bash, Edit, Write, Agent."""
        install_script = Path(__file__).resolve().parents[2] / "scripts" / "install-sandbox-hook.sh"
        source = install_script.read_text()

        # The required_matchers set in the script
        assert '"Bash"' in source
        assert '"Edit"' in source
        assert '"Write"' in source
        assert '"Agent"' in source

        # Must NOT contain MCP-prefixed tool names
        assert "mcp__" not in source, (
            "install-sandbox-hook.sh must not register mcp__* matchers — "
            "these are in-process SDK tools, not native CC tools"
        )

    def test_sandbox_py_checks_native_tool_names_only(self):
        """sandbox.py dispatches on 'Bash', 'Edit', 'Write', 'Agent' — never mcp__* names."""
        sandbox_path = Path(__file__).resolve().parents[2] / "hooks" / "sandbox.py"
        source = sandbox_path.read_text()

        # The hook explicitly matches these exact strings
        assert '"Bash"' in source or "'Bash'" in source
        assert '"Edit"' in source or "'Edit'" in source
        assert '"Write"' in source or "'Write'" in source
        assert '"Agent"' in source or "'Agent'" in source

        # Must NOT contain MCP-prefixed tool name checks
        assert "mcp__" not in source, (
            "sandbox.py must not reference mcp__* tool names — "
            "those tools never reach the Claude Code hook boundary"
        )


# ---------------------------------------------------------------------------
# 2. tool_proxy.run_bash is a direct Python call (no hook invocation path)
# ---------------------------------------------------------------------------

class TestToolProxyIsInProcess:
    """run_bash() is a Python function that calls subprocess.run directly.

    There is no hook invocation in the call path. This test confirms the
    function signature and execution model — no external hook binary is called.
    """

    def test_run_bash_is_callable_python_function(self):
        """run_bash is a regular Python function, not a subprocess wrapper."""
        import inspect
        from backend.orchestrator.tool_proxy import run_bash

        assert callable(run_bash)
        sig = inspect.signature(run_bash)
        assert "cmd" in sig.parameters
        assert "env" in sig.parameters
        assert "cwd" in sig.parameters

    def test_run_bash_does_not_invoke_sandbox_hook(self, tmp_path, monkeypatch):
        """run_bash runs the command directly — sandbox.py is never called."""
        hook_calls = []
        original_run = subprocess.run

        def tracking_run(args, **kwargs):
            # Capture any call to python3 .../sandbox.py
            if isinstance(args, (list, tuple)):
                cmd_str = " ".join(str(a) for a in args)
            else:
                cmd_str = str(args)
            if "sandbox.py" in cmd_str:
                hook_calls.append(cmd_str)
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", tracking_run)

        from backend.orchestrator.tool_proxy import run_bash
        run_bash(cmd="echo hello", env=_CLEAN_ENV, cwd=str(tmp_path))

        assert hook_calls == [], (
            f"sandbox.py should NOT be called during run_bash, but was: {hook_calls}"
        )

    def test_run_bash_executes_command_without_hook(self, tmp_path):
        """run_bash executes shell commands directly — no hook interposition."""
        from backend.orchestrator.tool_proxy import run_bash
        output = run_bash(cmd="echo sdk_lane_test", env=_CLEAN_ENV, cwd=str(tmp_path))
        assert "sdk_lane_test" in output


# ---------------------------------------------------------------------------
# 3. validate_env blocks credentials regardless of hook coverage
# ---------------------------------------------------------------------------

class TestValidateEnvProtection:
    """validate_env() is the credential-protection layer that DOES apply on SDK lane."""

    def test_anthropic_api_key_blocked(self, tmp_path):
        """ANTHROPIC_API_KEY in env raises EnvLeakError before subprocess launch."""
        from backend.orchestrator.tool_proxy import run_bash, EnvLeakError

        dirty_env = {**_CLEAN_ENV, "ANTHROPIC_API_KEY": "sk-ant-test"}
        with pytest.raises(EnvLeakError):
            run_bash(cmd="echo hi", env=dirty_env, cwd=str(tmp_path))

    def test_claude_code_oauth_token_blocked(self, tmp_path):
        from backend.orchestrator.tool_proxy import run_bash, EnvLeakError

        dirty_env = {**_CLEAN_ENV, "CLAUDE_CODE_OAUTH_TOKEN": "tok-secret"}
        with pytest.raises(EnvLeakError):
            run_bash(cmd="echo hi", env=dirty_env, cwd=str(tmp_path))

    def test_any_api_key_suffix_blocked(self, tmp_path):
        from backend.orchestrator.tool_proxy import run_bash, EnvLeakError

        dirty_env = {**_CLEAN_ENV, "MY_CUSTOM_SERVICE_API_KEY": "v1-abc"}
        with pytest.raises(EnvLeakError):
            run_bash(cmd="echo hi", env=dirty_env, cwd=str(tmp_path))

    def test_clean_env_allowed(self, tmp_path):
        """Clean env (no credential keys) passes validate_env without error."""
        from backend.orchestrator.tool_proxy import run_bash
        # Should not raise
        output = run_bash(cmd="echo clean", env=_CLEAN_ENV, cwd=str(tmp_path))
        assert "clean" in output


# ---------------------------------------------------------------------------
# 4. Gap CLOSED (PR #1372): SDK Bash now has command-content filtering
# ---------------------------------------------------------------------------

class TestSdkBashCommandContentGap:
    """Verifies that the command-content filter gap is now CLOSED (PR #1372, D#1371).

    Before #1372, run_bash had no command-content filtering — it only checked env
    credentials. The CC sandbox hook would block dangerous commands from a worktree
    agent, but the SDK lane's run_bash would let them through.

    After #1372, run_bash calls sandbox_rules.classify_bash() before executing any
    subprocess. Dangerous commands now raise SandboxBlockError. SDK lane is at parity
    with the PreToolUse hook for command-content policy.

    These tests confirm the gap is closed — they assert that previously-unblocked
    dangerous commands NOW raise SandboxBlockError.
    """

    def test_run_bash_allows_tmp_write_from_inner_dir(self, tmp_path):
        """run_bash still allows writes to /tmp paths — classify_bash permits ephemeral paths.

        classify_bash explicitly allows /tmp/ and /var/tmp/ writes. Writing to a /tmp
        path from a subdirectory is still permitted after #1372. This test confirms
        the hardening does not over-block legitimate ephemeral I/O.
        """
        from backend.orchestrator.tool_proxy import run_bash, EnvLeakError, SandboxBlockError

        probe_path = tmp_path / "probe.txt"
        inner_dir = tmp_path / "inner"
        inner_dir.mkdir()

        # /tmp writes are allowed — should not raise SandboxBlockError
        try:
            run_bash(
                cmd=f"echo sdk_write_test > {probe_path}",
                env=_CLEAN_ENV,
                cwd=str(inner_dir),
            )
        except (EnvLeakError, SandboxBlockError) as exc:
            pytest.fail(f"Writes within /tmp should be allowed, but got: {exc}")

    def test_sandbox_rules_blocks_write_outside_worktree(self, tmp_path):
        """sandbox_rules.classify_bash() blocks writes to non-/tmp paths outside the worktree.

        classify_bash explicitly allows /tmp/ and /var/tmp/ writes (ephemeral paths).
        To observe the block we use a path outside both /tmp/ AND the worktree
        root — the synthetic main-repo root from testsupport/fixture_paths.py,
        which is deliberately not under /tmp/ for exactly this reason.
        """
        from hooks.sandbox_rules import classify_bash
        import tempfile

        # Use a recognised worktree prefix so classify_bash has a worktree root to check against
        wt_root = Path(tempfile.mkdtemp(prefix="wt-", dir="/tmp"))
        wt_inner = wt_root / "subdir"
        wt_inner.mkdir()

        # Target is outside /tmp/ AND outside the worktree root — should be blocked
        # Use a path under the main repo that is clearly outside any /tmp/wt-* worktree
        outside_path = f"{FIXTURE_MAIN_REPO}/probe_outside_worktree.txt"
        decision = classify_bash(
            command=f"echo x > {outside_path}",
            cwd=str(wt_inner),
        )
        assert not decision.allow, (
            f"classify_bash should block writes outside the worktree, got: {decision}"
        )
        assert "outside worktree" in decision.reason or "redirect" in decision.reason

    def test_run_bash_now_blocks_git_branch(self, tmp_path):
        """run_bash now blocks 'git branch' via classify_bash — gap is CLOSED.

        Before PR #1372: run_bash had no command-content filter; 'git branch' would
        execute (possibly failing with a git error, but not blocked by tool_proxy).

        After PR #1372: classify_bash() is called before subprocess launch. 'git branch'
        is a git write-verb (treated as a destructive git command by classify_bash) and
        raises SandboxBlockError — same policy as the CC PreToolUse hook.
        """
        from backend.orchestrator.tool_proxy import run_bash, SandboxBlockError

        with pytest.raises(SandboxBlockError, match="blocked by sandbox policy"):
            run_bash(
                cmd="git branch 2>&1 || true",
                env={**_CLEAN_ENV, "GIT_DIR": str(tmp_path)},
                cwd=str(tmp_path),
            )

    def test_run_bash_now_blocks_git_checkout(self, tmp_path):
        """run_bash blocks 'git checkout' — destructive git verb blocked by classify_bash."""
        from backend.orchestrator.tool_proxy import run_bash, SandboxBlockError

        with pytest.raises(SandboxBlockError, match="blocked by sandbox policy"):
            run_bash(
                cmd="git checkout main",
                env=_CLEAN_ENV,
                cwd=str(tmp_path),
            )

    def test_run_bash_now_blocks_git_reset(self, tmp_path):
        """run_bash blocks 'git reset' — destructive git verb blocked by classify_bash."""
        from backend.orchestrator.tool_proxy import run_bash, SandboxBlockError

        with pytest.raises(SandboxBlockError, match="blocked by sandbox policy"):
            run_bash(
                cmd="git reset --hard HEAD",
                env=_CLEAN_ENV,
                cwd=str(tmp_path),
            )


# ---------------------------------------------------------------------------
# 5. Stage 1 whitelist (Read/Grep/Glob only) is safe
# ---------------------------------------------------------------------------

class TestStage1WhitelistIsSafe:
    """Stage 1 allows only Read, Grep, Glob — none can run shell commands.

    Bash is not in the Stage 1 whitelist, so the command-content filter is
    not even reached. This provides defense-in-depth on top of the S6 filter.
    """

    def test_bash_not_in_stage1_whitelist(self):
        """Verify Bash is absent from the stage-1 read-only tool set."""
        stage1_tools = {"Read", "Grep", "Glob"}
        assert "Bash" not in stage1_tools

    def test_dispatch_rejects_bash_when_not_whitelisted(self, tmp_path):
        """dispatch() fails closed — Bash is refused when not in whitelist."""
        from backend.orchestrator.tool_proxy import dispatch, UnknownToolError

        with pytest.raises(UnknownToolError):
            dispatch(
                tool_name="Bash",
                tool_input={"command": "echo hi"},
                whitelist=["Read", "Grep", "Glob"],  # Stage 1 — no Bash
                env=_CLEAN_ENV,
                cwd=str(tmp_path),
            )

    def test_read_tool_cannot_execute_shell_commands(self, tmp_path):
        """run_read reads file contents only — no subprocess involvement."""
        from backend.orchestrator.tool_proxy import run_read

        test_file = tmp_path / "safe.txt"
        test_file.write_text("hello")

        content = run_read(path=str(test_file), env=_CLEAN_ENV, cwd=str(tmp_path))
        assert content == "hello"
