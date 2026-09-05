"""tests/test_classify_cwd.py

Unit tests for hooks/sandbox_rules.py::classify_cwd and related helpers.

Run with:
    python3 -m pytest tests/test_classify_cwd.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.sandbox_rules import (
    classify_cwd,
    is_foreign_self_governed,
    is_team_lead,
    is_worktree,
)
from testsupport.fixture_paths import FIXTURE_HOME, FIXTURE_MAIN_REPO

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_MAIN_REPO = FIXTURE_MAIN_REPO
_WT_BASE = f"{_MAIN_REPO}/.claude/worktrees"


class TestClassifyCwd:
    # -- team_lead paths --

    def test_main_repo_root_is_team_lead(self) -> None:
        assert classify_cwd(_MAIN_REPO) == "team_lead"

    def test_main_repo_subdir_is_team_lead(self) -> None:
        assert classify_cwd(f"{_MAIN_REPO}/backend") == "team_lead"

    def test_main_repo_deep_subdir_is_team_lead(self) -> None:
        assert classify_cwd(f"{_MAIN_REPO}/scripts/lib/helpers") == "team_lead"

    def test_trailing_slash_is_team_lead(self) -> None:
        # Trailing slash must normalise correctly
        assert classify_cwd(f"{_MAIN_REPO}/") == "team_lead"

    # -- worktree paths --

    def test_claude_worktree_root_is_worktree(self) -> None:
        assert classify_cwd(f"{_WT_BASE}/abc123") == "worktree"

    def test_claude_worktree_subdir_is_worktree(self) -> None:
        assert classify_cwd(f"{_WT_BASE}/abc123/src") == "worktree"

    def test_tmp_wt_is_worktree(self) -> None:
        assert classify_cwd("/tmp/wt-someagent") == "worktree"

    def test_tmp_wt_subdir_is_worktree(self) -> None:
        assert classify_cwd("/tmp/wt-someagent/backend") == "worktree"

    # -- untrusted paths --

    def test_slash_tmp_random_is_untrusted(self) -> None:
        assert classify_cwd("/tmp/random") == "untrusted"

    def test_slash_tmp_is_untrusted(self) -> None:
        assert classify_cwd("/tmp") == "untrusted"

    def test_home_agent_parent_is_untrusted(self) -> None:
        # the fixture home is the *parent* of the repo, not inside it
        assert classify_cwd(FIXTURE_HOME) == "untrusted"

    def test_entirely_different_path_is_untrusted(self) -> None:
        assert classify_cwd("/var/log") == "untrusted"

    def test_root_is_untrusted(self) -> None:
        assert classify_cwd("/") == "untrusted"

    # -- dot-dot normalisation --

    def test_dot_dot_escaping_worktree_is_untrusted(self) -> None:
        # A path that uses .. to escape the worktree should resolve to the
        # parent directory and NOT be classified as worktree.
        escaped = f"{_WT_BASE}/abc123/../../.."
        # resolve() will canonicalise this back to the fixture main repo,
        # which is team_lead, not worktree — the important thing is that it's
        # NOT classified as worktree despite starting with the worktree prefix.
        result = classify_cwd(escaped)
        # After resolving, this should end up as team_lead (or untrusted), NOT worktree.
        assert result in ("team_lead", "untrusted")

    def test_dot_dot_within_worktree_stays_worktree(self) -> None:
        # src/../lib inside the worktree still resolves to the worktree
        path = f"{_WT_BASE}/abc123/src/../lib"
        assert classify_cwd(path) == "worktree"

    # -- is_team_lead helper --

    def test_is_team_lead_true_for_main_repo(self) -> None:
        assert is_team_lead(_MAIN_REPO) is True

    def test_is_team_lead_false_for_worktree(self) -> None:
        assert is_team_lead(f"{_WT_BASE}/abc123") is False

    def test_is_team_lead_false_for_untrusted(self) -> None:
        assert is_team_lead("/tmp/random") is False

    # -- back-compat: is_worktree still works --

    def test_is_worktree_returns_id_for_worktree(self) -> None:
        result = is_worktree(f"{_WT_BASE}/abc123/src")
        assert result == "abc123"

    def test_is_worktree_returns_none_for_team_lead(self) -> None:
        assert is_worktree(_MAIN_REPO) is None

    def test_is_worktree_returns_none_for_untrusted(self) -> None:
        assert is_worktree("/tmp/random") is None


class TestForeignSelfGoverned:
    """is_foreign_self_governed — defer to sibling autonomous teams (e.g. lafk-demo)."""

    @staticmethod
    def _make_team(root: Path) -> None:
        (root / ".git").mkdir(parents=True, exist_ok=True)
        (root / ".autonomous-team").mkdir(parents=True, exist_ok=True)

    def test_foreign_git_repo_with_marker_is_foreign(self, tmp_path: Path) -> None:
        self._make_team(tmp_path)
        assert is_foreign_self_governed(str(tmp_path)) is True

    def test_subdir_of_foreign_team_is_foreign(self, tmp_path: Path) -> None:
        self._make_team(tmp_path)
        sub = tmp_path / "dashboard" / "src"
        sub.mkdir(parents=True)
        assert is_foreign_self_governed(str(sub)) is True

    def test_git_repo_without_marker_is_not_foreign(self, tmp_path: Path) -> None:
        # A git repo with no .autonomous-team/ is just a scratch repo → stays untrusted.
        (tmp_path / ".git").mkdir()
        assert is_foreign_self_governed(str(tmp_path)) is False

    def test_marker_without_git_is_not_foreign(self, tmp_path: Path) -> None:
        (tmp_path / ".autonomous-team").mkdir()
        assert is_foreign_self_governed(str(tmp_path)) is False

    def test_plain_tmp_dir_is_not_foreign(self) -> None:
        assert is_foreign_self_governed("/tmp/random") is False

    def test_main_repo_is_not_foreign(self) -> None:
        # Our own repo carries .autonomous-team/ but must NEVER be treated as foreign.
        assert is_foreign_self_governed(_MAIN_REPO) is False

    def test_main_repo_worktree_is_not_foreign(self) -> None:
        # Sub-agent worktrees live under the main repo → must stay sandboxed, not deferred.
        assert is_foreign_self_governed(f"{_WT_BASE}/abc123") is False

    def test_foreign_team_does_not_match_main_subtree(self, tmp_path: Path) -> None:
        # Defer applies; classify_cwd still reports untrusted (defer is handled in
        # sandbox.py before tier routing, not by reclassifying the tier).
        self._make_team(tmp_path)
        assert classify_cwd(str(tmp_path)) == "untrusted"
        assert is_foreign_self_governed(str(tmp_path)) is True
