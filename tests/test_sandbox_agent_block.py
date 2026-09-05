"""tests/test_sandbox_agent_block.py

Unit tests for the Agent() spawn block and gh api mutation block in
hooks/sandbox_rules.py and hooks/sandbox.py.

Run with:
    python3 -m pytest tests/test_sandbox_agent_block.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.sandbox_rules import (
    classify_agent_spawn,
    classify_bash,
    classify_cwd,
)
from testsupport.fixture_paths import FIXTURE_MAIN_REPO

_MAIN_REPO = FIXTURE_MAIN_REPO
_WT_BASE = f"{_MAIN_REPO}/.claude/worktrees"
_WT_CWD = f"{_WT_BASE}/test-agent-123"
_TL_CWD = _MAIN_REPO
_UNTRUSTED_CWD = "/tmp/random"


# ---------------------------------------------------------------------------
# classify_agent_spawn — unit tests
# ---------------------------------------------------------------------------


class TestClassifyAgentSpawn:
    def test_team_lead_cwd_allows_spawn(self) -> None:
        d = classify_agent_spawn(_TL_CWD, {"prompt": "do work"})
        assert d.allow is True

    def test_worktree_cwd_blocks_spawn(self) -> None:
        d = classify_agent_spawn(_WT_CWD, {"prompt": "do work"})
        assert d.allow is False
        assert "agent_spawn_in_worktree" in d.reason

    def test_untrusted_cwd_blocks_spawn(self) -> None:
        d = classify_agent_spawn(_UNTRUSTED_CWD, {"prompt": "do work"})
        assert d.allow is False
        assert "agent_spawn_in_untrusted_cwd" in d.reason

    def test_worktree_subdir_also_blocked(self) -> None:
        d = classify_agent_spawn(f"{_WT_CWD}/backend", {"task_prompt": "..."})
        assert d.allow is False
        assert "agent_spawn_in_worktree" in d.reason


# ---------------------------------------------------------------------------
# classify_bash — gh api mutation checks
# ---------------------------------------------------------------------------


class TestClassifyBashGhApiMutation:
    """classify_bash is called when CWD is a worktree; we test the mutation path."""

    def test_gh_api_post_blocked(self) -> None:
        d = classify_bash("gh api repos/owner/repo/issues -X POST -f title=test", _WT_CWD)
        assert d.allow is False
        assert "sandbox_block_gh_api_mutation" in d.reason

    def test_gh_api_patch_blocked(self) -> None:
        d = classify_bash("gh api repos/owner/repo/issues/1 -X PATCH -f state=closed", _WT_CWD)
        assert d.allow is False
        assert "sandbox_block_gh_api_mutation" in d.reason

    def test_gh_api_put_blocked(self) -> None:
        d = classify_bash("gh api repos/owner/repo/pulls/5/merge -X PUT", _WT_CWD)
        assert d.allow is False

    def test_gh_api_delete_blocked(self) -> None:
        d = classify_bash("gh api repos/owner/repo/issues/1 -X DELETE", _WT_CWD)
        assert d.allow is False
        assert "sandbox_block_gh_api_mutation" in d.reason

    def test_gh_api_method_flag_blocked(self) -> None:
        d = classify_bash("gh api repos/owner/repo/issues --method POST -f title=x", _WT_CWD)
        assert d.allow is False

    def test_gh_issue_close_blocked(self) -> None:
        d = classify_bash("gh issue close 42", _WT_CWD)
        assert d.allow is False

    def test_gh_pr_review_request_changes_blocked(self) -> None:
        d = classify_bash("gh pr review 7 --request-changes -b 'needs work'", _WT_CWD)
        assert d.allow is False

    def test_gh_api_get_allowed(self) -> None:
        # Plain gh api (GET) is read-only — must NOT be blocked
        d = classify_bash("gh api repos/owner/repo/issues/1", _WT_CWD)
        assert d.allow is True

    def test_gh_pr_list_allowed(self) -> None:
        d = classify_bash("gh pr list --repo owner/repo --state open", _WT_CWD)
        assert d.allow is True

    def test_gh_pr_create_allowed(self) -> None:
        # gh pr create is the executor workflow and must remain allowed
        d = classify_bash("gh pr create --title 'my PR' --body 'details'", _WT_CWD)
        assert d.allow is True


# ---------------------------------------------------------------------------
# sandbox.py integration — invoke the hook script directly via subprocess
# ---------------------------------------------------------------------------


def _run_hook(tool_name: str, tool_input: dict, cwd: str) -> tuple[int, str, str]:
    """Run hooks/sandbox.py with the given payload, return (exit_code, stdout, stderr)."""
    hook = str(_REPO / "hooks" / "sandbox.py")
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd})
    result = subprocess.run(
        [sys.executable, hook],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


class TestSandboxHookAgent:
    def test_agent_blocked_from_worktree(self) -> None:
        code, _, stderr = _run_hook("Agent", {"prompt": "spawn me"}, _WT_CWD)
        assert code == 2
        assert "agent_spawn_in_worktree" in stderr

    def test_agent_blocked_from_untrusted(self) -> None:
        code, _, stderr = _run_hook("Agent", {"prompt": "spawn me"}, _UNTRUSTED_CWD)
        assert code == 2
        assert "agent_spawn_in_untrusted_cwd" in stderr

    def test_agent_allowed_from_team_lead(self) -> None:
        code, _, stderr = _run_hook("Agent", {"prompt": "spawn me"}, _TL_CWD)
        assert code == 0

    def test_gh_api_mutation_blocked_from_worktree(self) -> None:
        code, _, stderr = _run_hook(
            "Bash",
            {"command": "gh api repos/owner/repo/issues -X POST -f title=x"},
            _WT_CWD,
        )
        assert code == 2
        assert "sandbox_block_gh_api_mutation" in stderr

    def test_gh_api_get_allowed_from_worktree(self) -> None:
        code, _, _ = _run_hook(
            "Bash",
            {"command": "gh api repos/owner/repo/issues/1"},
            _WT_CWD,
        )
        assert code == 0
