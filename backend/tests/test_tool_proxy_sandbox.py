"""backend/tests/test_tool_proxy_sandbox.py

Tests for the sandbox hardening in tool_proxy.py:
  S6 — run_bash blocks dangerous commands via sandbox_rules.classify_bash()
       and check_claude_spawn()
  S7 — run_write / run_edit reject paths that escape the worktree cwd

All dangerous-command checks assert the block happens BEFORE execution —
no real destructive operation is run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable regardless of where pytest is launched from.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.orchestrator.tool_proxy import (
    EnvLeakError,
    PathEscapeError,
    SandboxBlockError,
    run_bash,
    run_edit,
    run_write,
)
from testsupport.fixture_paths import FIXTURE_MAIN_REPO

# A clean env that passes validate_env()
_CLEAN_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}

# A fake worktree cwd that classify_bash can use for path-confinement checks.
# Must be under a recognised worktree prefix so _worktree_root_from_cwd returns
# the right boundary (not the /tmp/ exception path) — see
# testsupport/fixture_paths.py for why the synthetic root deliberately does
# not live under /tmp/.
_FAKE_WORKTREE_CWD = f"{FIXTURE_MAIN_REPO}/.claude/worktrees/test-sandbox-abc123"


# ---------------------------------------------------------------------------
# S6 — run_bash: dangerous command blocking via classify_bash
# ---------------------------------------------------------------------------

class TestRunBashSandboxBlocking:
    """run_bash must block dangerous commands BEFORE launching a subprocess."""

    def test_blocks_write_outside_cwd_via_redirect(self, tmp_path):
        """Output redirect to a non-/tmp absolute path outside the worktree is blocked.

        classify_bash exempts /tmp/ as ephemeral — so we use a path under the
        repo root (which is clearly outside _FAKE_WORKTREE_CWD) to test the block.
        The command never executes: SandboxBlockError is raised before subprocess.run.
        """
        # Target is under the main repo root but not in the worktree — clearly outside.
        outside_path = f"{FIXTURE_MAIN_REPO}/PROBE_MUST_NOT_EXIST.txt"
        cmd = f"echo evil > {outside_path}"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)
        # File must not have been created
        assert not Path(outside_path).exists()

    def test_blocks_destructive_git_checkout(self, tmp_path):
        """git checkout is an always-blocked verb regardless of target."""
        cmd = "git checkout main"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)

    def test_blocks_destructive_git_reset(self, tmp_path):
        """git reset --hard is blocked."""
        cmd = "git reset --hard HEAD"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)

    def test_blocks_git_rm(self, tmp_path):
        """git rm violates archive protocol — blocked."""
        cmd = "git rm somefile.py"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)

    def test_blocks_gh_pr_merge(self, tmp_path):
        """gh pr merge is blocked — sub-agents may not merge."""
        cmd = "gh pr merge 42 --squash"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)

    def test_blocks_gh_api_mutation_post(self, tmp_path):
        """gh api -X POST mutation is blocked."""
        cmd = "gh api repos/owner/repo/issues -X POST -f title=test"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)

    def test_blocks_claude_spawn(self, tmp_path):
        """Nested claude spawn is blocked via check_claude_spawn (stage A)."""
        cmd = "claude -p 'do something'"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)

    def test_blocks_claude_spawn_via_forbidden_fragment(self, tmp_path):
        """spawn-agent.sh path fragment is blocked."""
        cmd = "bash scripts/spawn-agent.sh --role executor"
        with pytest.raises(SandboxBlockError, match="sandbox policy"):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)

    def test_allows_benign_in_cwd_command(self, tmp_path):
        """A benign in-worktree command is allowed and executes normally."""
        # Write a marker file inside tmp_path, then read it back
        marker = tmp_path / "marker.txt"
        marker.write_text("hello")
        result = run_bash(f"cat {marker}", _CLEAN_ENV, str(tmp_path))
        assert "hello" in result

    def test_allows_git_status(self, tmp_path):
        """git status is a read-only verb — always allowed."""
        result = run_bash("git status 2>&1 || true", _CLEAN_ENV, str(tmp_path))
        # No SandboxBlockError — command ran (exit code doesn't matter here)
        assert isinstance(result, str)

    def test_allows_echo(self, tmp_path):
        """Plain echo with no redirect is allowed."""
        result = run_bash("echo test_output", _CLEAN_ENV, str(tmp_path))
        assert "test_output" in result

    def test_env_leak_still_blocked_before_sandbox_check(self, tmp_path):
        """EnvLeakError is raised (S2) even for an otherwise-allowed command."""
        dirty_env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-bad"}
        with pytest.raises(EnvLeakError):
            run_bash("echo hi", dirty_env, str(tmp_path))

    def test_block_happens_before_execution(self):
        """Verifies no side effect: a write-outside-worktree redirect doesn't create the file.

        Uses a path under the repo root — outside the fake worktree boundary —
        that does not exist. A successful block means the file is never created.
        """
        outside_path = f"{FIXTURE_MAIN_REPO}/PROBE_BLOCK_TEST_MUST_NOT_EXIST.txt"
        cmd = f"echo danger > {outside_path}"
        with pytest.raises(SandboxBlockError):
            run_bash(cmd, _CLEAN_ENV, _FAKE_WORKTREE_CWD)
        assert not Path(outside_path).exists(), "Blocked command must not have executed"


# ---------------------------------------------------------------------------
# S7 — run_write: path confinement
# ---------------------------------------------------------------------------

class TestRunWritePathConfinement:
    """run_write must reject absolute paths outside cwd and ../ escapes."""

    def test_allows_relative_path_within_cwd(self, tmp_path):
        """Relative path that stays within cwd is allowed."""
        result = run_write("subdir/file.txt", "content", _CLEAN_ENV, str(tmp_path))
        assert (tmp_path / "subdir" / "file.txt").read_text() == "content"

    def test_allows_absolute_path_within_cwd(self, tmp_path):
        """Absolute path inside cwd is allowed."""
        target = str(tmp_path / "inside.txt")
        result = run_write(target, "ok", _CLEAN_ENV, str(tmp_path))
        assert Path(target).read_text() == "ok"

    def test_blocks_absolute_path_outside_cwd(self, tmp_path):
        """Absolute path outside worktree boundary is rejected."""
        import tempfile
        with tempfile.TemporaryDirectory() as other_dir:
            outside = str(Path(other_dir) / "evil.txt")
            with pytest.raises(PathEscapeError, match="outside the worktree boundary"):
                run_write(outside, "evil", _CLEAN_ENV, str(tmp_path))
            # File must not have been created
            assert not Path(outside).exists()

    def test_blocks_dotdot_escape(self, tmp_path):
        """../escape path is rejected after realpath resolution."""
        # Create a sibling directory to escape into
        import tempfile
        with tempfile.TemporaryDirectory() as parent:
            worktree = Path(parent) / "worktree"
            worktree.mkdir()
            sibling = Path(parent) / "sibling"
            sibling.mkdir()
            escape_path = "../sibling/evil.txt"
            with pytest.raises(PathEscapeError, match="outside the worktree boundary"):
                run_write(escape_path, "evil", _CLEAN_ENV, str(worktree))
            assert not (sibling / "evil.txt").exists()


# ---------------------------------------------------------------------------
# S7 — run_edit: path confinement
# ---------------------------------------------------------------------------

class TestRunEditPathConfinement:
    """run_edit must reject absolute paths outside cwd and ../ escapes."""

    def test_allows_edit_within_cwd(self, tmp_path):
        """Edit on a file inside cwd is allowed."""
        f = tmp_path / "editme.txt"
        f.write_text("old content")
        result = run_edit(str(f), "old content", "new content", _CLEAN_ENV, str(tmp_path))
        assert "new content" in result

    def test_blocks_absolute_path_outside_cwd(self, tmp_path):
        """Absolute path outside worktree boundary is rejected before any I/O."""
        import tempfile
        with tempfile.TemporaryDirectory() as other_dir:
            outside = str(Path(other_dir) / "outside.txt")
            # Don't even create the file — the check must fire before reading
            with pytest.raises(PathEscapeError, match="outside the worktree boundary"):
                run_edit(outside, "old", "new", _CLEAN_ENV, str(tmp_path))

    def test_blocks_dotdot_escape(self, tmp_path):
        """../escape path is rejected after realpath resolution."""
        import tempfile
        with tempfile.TemporaryDirectory() as parent:
            worktree = Path(parent) / "worktree"
            worktree.mkdir()
            sibling = Path(parent) / "sibling"
            sibling.mkdir()
            victim = sibling / "victim.txt"
            victim.write_text("do not touch")
            escape_path = "../sibling/victim.txt"
            with pytest.raises(PathEscapeError, match="outside the worktree boundary"):
                run_edit(escape_path, "do not touch", "corrupted", _CLEAN_ENV, str(worktree))
            # Original file must be unchanged
            assert victim.read_text() == "do not touch"
