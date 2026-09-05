"""tests/test_dial_sandbox_integration.py

Verify that the sandbox hook refuses writes to dial-registry.json,
dial-directive-allowlist.json, and audit.jsonl from non-Team-Lead
worktree path patterns.

Run with:
    pytest tests/test_dial_sandbox_integration.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.sandbox_rules import (
    Decision,
    classify_bash,
    classify_path_write,
    _is_dial_protected_path,
)
from testsupport.fixture_paths import FIXTURE_HOME, FIXTURE_MAIN_REPO

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MAIN_REPO = FIXTURE_MAIN_REPO
_WT_CLAUDE = f"{_MAIN_REPO}/.claude/worktrees/abc123"
_WT_CLAUDE_SUBDIR = f"{_MAIN_REPO}/.claude/worktrees/abc123/src"
_TEAM_LEAD_CWD = _MAIN_REPO

# State dir paths that sub-agents must never write
_STATE_DIR = f"{FIXTURE_HOME}/.autonomous-forever-state"
_DIAL_REGISTRY = f"{_STATE_DIR}/dial-registry.json"
_DIAL_ALLOWLIST = f"{_STATE_DIR}/dial-directive-allowlist.json"
_AUDIT_JSONL = f"{_STATE_DIR}/audit.jsonl"


# ---------------------------------------------------------------------------
# _is_dial_protected_path — unit tests for the name-matching helper
# ---------------------------------------------------------------------------

class TestIsDialProtectedPath:
    """Unit tests for the _is_dial_protected_path helper."""

    @pytest.mark.parametrize("path", [
        _DIAL_REGISTRY,
        _DIAL_ALLOWLIST,
        _AUDIT_JSONL,
        "/tmp/dial-registry.json",
        "/some/other/dir/audit.jsonl",
        "dial-registry.json",           # relative
        "dial-directive-allowlist.json", # relative
        "audit.jsonl",                   # relative
    ])
    def test_protected_paths_detected(self, path: str) -> None:
        assert _is_dial_protected_path(path) is True, f"Expected {path!r} to be protected"

    @pytest.mark.parametrize("path", [
        "/tmp/foo.json",
        f"{FIXTURE_MAIN_REPO}/.autonomous-team/audit.jsonl.bak",
        "notaudit.jsonl",
        "dial-registry.json.bak",
        "src/backend.py",
    ])
    def test_non_protected_paths_pass(self, path: str) -> None:
        assert _is_dial_protected_path(path) is False, f"Expected {path!r} to be unprotected"


# ---------------------------------------------------------------------------
# classify_path_write — Edit/Write tool blocking from worktree
# ---------------------------------------------------------------------------

class TestClassifyPathWriteDialBlocking:
    """classify_path_write must block writes to dial-protected files from worktrees."""

    @pytest.mark.parametrize("protected_path", [
        _DIAL_REGISTRY,
        _DIAL_ALLOWLIST,
        _AUDIT_JSONL,
    ])
    def test_blocks_absolute_dial_registry_write_from_worktree(
        self, protected_path: str
    ) -> None:
        d = classify_path_write(protected_path, _WT_CLAUDE)
        assert not d.allow, f"Expected write to {protected_path!r} to be blocked"
        assert "dial-registry write blocked" in d.reason

    @pytest.mark.parametrize("protected_path", [
        _DIAL_REGISTRY,
        _DIAL_ALLOWLIST,
        _AUDIT_JSONL,
    ])
    def test_blocks_dial_registry_write_from_worktree_subdir(
        self, protected_path: str
    ) -> None:
        d = classify_path_write(protected_path, _WT_CLAUDE_SUBDIR)
        assert not d.allow
        assert "dial-registry write blocked" in d.reason

    @pytest.mark.parametrize("relative_path", [
        "dial-registry.json",
        "dial-directive-allowlist.json",
        "audit.jsonl",
    ])
    def test_blocks_relative_dial_registry_path(self, relative_path: str) -> None:
        d = classify_path_write(relative_path, _WT_CLAUDE)
        assert not d.allow
        assert "dial-registry write blocked" in d.reason

    def test_allows_normal_worktree_write(self) -> None:
        # Normal file inside the worktree should still be allowed
        normal_path = f"{_WT_CLAUDE}/src/app.py"
        d = classify_path_write(normal_path, _WT_CLAUDE)
        assert d.allow

    def test_allows_dial_registry_from_team_lead_worktree_check(self) -> None:
        # classify_path_write is only called when in a worktree (sandbox.py gates on is_worktree).
        # The Team Lead is NOT in a worktree, so classify_path_write is never called for TL.
        # This test documents the expected behavior: the function always blocks dial files
        # regardless of CWD since is_worktree() gates the call in sandbox.py.
        # When called with team-lead CWD (which shouldn't happen in practice), it still blocks.
        # This is acceptable — Team Lead uses Python API directly, not Edit/Write tool on audit.jsonl.
        d = classify_path_write(_AUDIT_JSONL, _TEAM_LEAD_CWD)
        assert not d.allow  # Always blocked at this layer; TL gate is in sandbox.py


# ---------------------------------------------------------------------------
# classify_bash — output-redirect blocking from worktree
# ---------------------------------------------------------------------------

class TestClassifyBashDialRedirectBlocking:
    """classify_bash must block output redirects targeting dial-protected files."""

    @pytest.mark.parametrize("command", [
        f"echo '{{}}' > {_DIAL_REGISTRY}",
        f"python3 -c 'import json; print(json.dumps([]))' > {_DIAL_ALLOWLIST}",
        f"echo '{{\"kind\":\"dial_change\"}}' >> {_AUDIT_JSONL}",
        f"tee {_DIAL_REGISTRY}",
    ])
    def test_blocks_bash_redirect_to_dial_files(self, command: str) -> None:
        d = classify_bash(command, _WT_CLAUDE)
        assert not d.allow, f"Expected command to be blocked: {command!r}"

    def test_allows_redirect_to_worktree_json(self) -> None:
        # A JSON file inside the worktree should be fine
        cmd = f"echo '{{}}' > {_WT_CLAUDE}/src/config.json"
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow

    def test_allows_reading_dial_registry(self) -> None:
        # Reading (cat, python3 load) should never be blocked by these rules.
        # Note: classify_bash doesn't check reads — only writes via redirects/write-commands.
        cmd = f"python3 -c \"import json; print(open('{_DIAL_REGISTRY}').read())\""
        d = classify_bash(cmd, _WT_CLAUDE)
        assert d.allow


# ---------------------------------------------------------------------------
# Regression: existing sandbox rules not broken by new dial-protection logic
# ---------------------------------------------------------------------------

class TestSandboxRulesUnchanged:
    """Existing sandbox rules continue to work after adding dial-protection."""

    def test_git_checkout_still_blocked(self) -> None:
        from hooks.sandbox_rules import classify_bash
        d = classify_bash("git checkout main", _WT_CLAUDE)
        assert not d.allow

    def test_git_commit_still_allowed(self) -> None:
        from hooks.sandbox_rules import classify_bash
        d = classify_bash("git commit -m 'test'", _WT_CLAUDE)
        assert d.allow

    def test_git_rm_still_blocked(self) -> None:
        from hooks.sandbox_rules import classify_git_rm
        d = classify_git_rm("git rm src/foo.py")
        assert not d.allow

    def test_normal_write_outside_worktree_blocked(self) -> None:
        # Non-dial absolute path outside worktree is still blocked
        d = classify_path_write(f"{_MAIN_REPO}/scripts/something.sh", _WT_CLAUDE)
        assert not d.allow
        assert "file_path outside worktree" in d.reason

    def test_normal_write_inside_worktree_allowed(self) -> None:
        d = classify_path_write(f"{_WT_CLAUDE}/backend/myfile.py", _WT_CLAUDE)
        assert d.allow
