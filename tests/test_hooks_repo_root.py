"""tests/test_hooks_repo_root.py

Unit tests for hooks/repo_root.py: derive_repo_root_from, _is_real_git_dir,
and is_main_repo_root_confident.

Why this file exists
---------------------
The 463-test suite was green without a single reference to these three
names anywhere under tests/. That is not evidence the confidence branch
works — tests/conftest.py pins SANDBOX_MAIN_REPO_ROOT, which only feeds
resolve_main_repo_root() (the env-aware function). derive_main_repo_root(),
derive_repo_root_from(), and is_main_repo_root_confident() ignore the
environment by design (see the module docstring in hooks/repo_root.py) and
are derived once, at import time, from THIS repo's real .git — which is
always a genuine main checkout or a genuine linked worktree in any
environment these tests run in. The unconfident branch — a missing,
corrupted, or decoy .git — never executed once in the whole suite.

Every test below calls derive_repo_root_from() directly against a
synthetic tree built under tmp_path, bypassing the module import entirely,
so the confidence branch actually runs.

Run with:
    python3 -m pytest tests/test_hooks_repo_root.py -v

Do not run the full backend/tests/ suite from a worktree (D#1864) — this
file is self-contained and safe to run on its own.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.repo_root import (
    _is_real_git_dir,
    derive_repo_root_from,
)

try:
    from hooks.repo_root import is_main_repo_root_confident  # noqa: F401
except ImportError:  # pragma: no cover - only true on the pre-fix commit
    is_main_repo_root_confident = None


def _make_hooks_dir(root: Path) -> Path:
    """Create <root>/hooks/ and return the path a real caller would pass in
    (a file inside it) — derive_repo_root_from only ever looks at
    Path(module_file).resolve().parent.parent.
    """
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir / "repo_root.py"


# ---------------------------------------------------------------------------
# _is_real_git_dir
# ---------------------------------------------------------------------------


class TestIsRealGitDir:
    def test_missing_path_is_not_real(self, tmp_path: Path) -> None:
        assert _is_real_git_dir(tmp_path / "does-not-exist") is False

    def test_directory_without_head_is_not_real(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert _is_real_git_dir(git_dir) is False

    def test_directory_with_head_file_is_real(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        assert _is_real_git_dir(git_dir) is True

    def test_file_is_never_real_regardless_of_name(self, tmp_path: Path) -> None:
        # A worktree's un-corrected .git is a *file*, not a directory — that
        # alone must fail this check, independent of the gitdir: parsing
        # that happens one level up in derive_repo_root_from.
        git_file = tmp_path / ".git"
        git_file.write_text("gitdir: /somewhere/.git/worktrees/id\n")
        assert _is_real_git_dir(git_file) is False


# ---------------------------------------------------------------------------
# derive_repo_root_from — benign ambiguous/degraded states
#
# These are the states the fix in this PR round actually addresses: a
# stale or missing .git left behind by an interrupted `git worktree add`,
# or a moved main checkout. None of these require deliberate adversarial
# construction — they can all happen by accident.
# ---------------------------------------------------------------------------


class TestDeriveRepoRootFromBenignStates:
    def test_git_missing_entirely(self, tmp_path: Path) -> None:
        candidate_file = _make_hooks_dir(tmp_path)
        root, confident = derive_repo_root_from(candidate_file)
        assert confident is False
        assert root == tmp_path

    def test_git_empty_file(self, tmp_path: Path) -> None:
        candidate_file = _make_hooks_dir(tmp_path)
        (tmp_path / ".git").write_text("")
        root, confident = derive_repo_root_from(candidate_file)
        assert confident is False
        assert root == tmp_path

    def test_git_containing_garbage(self, tmp_path: Path) -> None:
        candidate_file = _make_hooks_dir(tmp_path)
        # No line starts with "gitdir:" — the parse loop runs to
        # completion without ever hitting the inner `break`.
        (tmp_path / ".git").write_text("this is not git content\njust noise\n")
        root, confident = derive_repo_root_from(candidate_file)
        assert confident is False
        assert root == tmp_path

    def test_git_unreadable(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("running as root — permission bits do not block reads")
        candidate_file = _make_hooks_dir(tmp_path)
        git_file = tmp_path / ".git"
        git_file.write_text("gitdir: /some/where/.git/worktrees/id\n")
        git_file.chmod(0o000)
        try:
            root, confident = derive_repo_root_from(candidate_file)
        finally:
            git_file.chmod(0o644)
        assert confident is False
        assert root == tmp_path

    def test_git_present_but_marker_less(self, tmp_path: Path) -> None:
        # Real shape of a submodule's .git file: has a "gitdir:" prefix but
        # never contains the worktree marker. Exercises the `break` path
        # (distinct code path from the no-gitdir-line-at-all garbage case).
        candidate_file = _make_hooks_dir(tmp_path)
        (tmp_path / ".git").write_text("gitdir: ../.git/modules/some-submodule\n")
        root, confident = derive_repo_root_from(candidate_file)
        assert confident is False
        assert root == tmp_path

    def test_gitdir_marker_target_does_not_exist(self, tmp_path: Path) -> None:
        candidate_file = _make_hooks_dir(tmp_path)
        bogus_main = tmp_path / "nonexistent-main-checkout"
        (tmp_path / ".git").write_text(
            f"gitdir: {bogus_main}/.git/worktrees/some-id\n"
        )
        root, confident = derive_repo_root_from(candidate_file)
        assert confident is False
        assert root == tmp_path


# ---------------------------------------------------------------------------
# derive_repo_root_from — genuine states, confident
# ---------------------------------------------------------------------------


class TestDeriveRepoRootFromGenuineStates:
    def test_genuine_main_checkout_is_confident(self, tmp_path: Path) -> None:
        candidate_file = _make_hooks_dir(tmp_path)
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

        root, confident = derive_repo_root_from(candidate_file)

        assert confident is True
        # This is what makes classify_cwd's team_lead branch trustworthy —
        # the root is the checkout itself.
        assert root == tmp_path

    def test_genuine_linked_worktree_resolves_to_main_not_worktree(
        self, tmp_path: Path
    ) -> None:
        main_repo = tmp_path / "main-repo"
        main_git = main_repo / ".git"
        main_git.mkdir(parents=True)
        (main_git / "HEAD").write_text("ref: refs/heads/main\n")

        worktree = main_repo / ".claude" / "worktrees" / "wt1"
        candidate_file = _make_hooks_dir(worktree)
        (worktree / ".git").write_text(f"gitdir: {main_git}/worktrees/wt1\n")

        root, confident = derive_repo_root_from(candidate_file)

        assert confident is True
        # Contained: the worktree resolves back to the MAIN root, never to
        # its own path — this is what stops a worktree from being promoted
        # to "team_lead" in sandbox_rules.classify_cwd.
        assert root == main_repo
        assert root != worktree


# ---------------------------------------------------------------------------
# Adversarial decoy states — S1 and S2 (D#1890 / PR #1894 round 5)
#
# Both require an agent to deliberately construct filesystem evidence
# inside a tree it already has write access to. The Team Lead's ruling
# (see hooks/repo_root.py module docstring, "Residual — accepted out of
# scope") is that hooks/ is a guardrail against accidental writes, not a
# defense against deliberate adversarial probing, and no observed agent
# behaviour has warranted the stronger model. These are recorded as
# strict xfail, not fixed and not silently dropped: they must stay
# failing exactly this way, so a future change that starts passing them
# (or that breaks for an unrelated reason) is visible either way.
# ---------------------------------------------------------------------------


class TestAdversarialDecoysAcceptedOutOfScope:
    @pytest.mark.xfail(
        strict=True,
        reason="S1, accepted out of scope — see hooks/repo_root.py module "
        "docstring 'Residual' section and D#1890 PR #1894 round 5 adjudication",
    )
    def test_decoy_git_directory_with_hand_written_head(self, tmp_path: Path) -> None:
        """A worktree's `.git` *file* replaced by a directory containing a
        hand-written HEAD satisfies _is_real_git_dir, so derive_repo_root_from
        reports confident=True with the worktree's own path as "root" — the
        exact worktree-promoted-to-team_lead shape the confidence check exists
        to catch, reconstructed deliberately instead of by accident.
        """
        worktree = tmp_path / "worktree"
        candidate_file = _make_hooks_dir(worktree)
        decoy_git = worktree / ".git"
        decoy_git.mkdir()
        (decoy_git / "HEAD").write_text("ref: refs/heads/main\n")

        root, confident = derive_repo_root_from(candidate_file)

        # Secure expectation that does NOT hold today.
        assert not confident

    @pytest.mark.xfail(
        strict=True,
        reason="S2, accepted out of scope — see hooks/repo_root.py module "
        "docstring 'Residual' section and D#1890 PR #1894 round 5 adjudication",
    )
    def test_decoy_gitdir_marker_points_at_forged_main_root(
        self, tmp_path: Path
    ) -> None:
        """The `gitdir:` marker's target is trusted once it passes
        _is_real_git_dir, with no check that it is the REAL main checkout
        rather than a second decoy the same agent just planted next to it.
        """
        forged_main = tmp_path / "forged-main"
        forged_git = forged_main / ".git"
        forged_git.mkdir(parents=True)
        (forged_git / "HEAD").write_text("ref: refs/heads/main\n")

        worktree = tmp_path / "worktree"
        candidate_file = _make_hooks_dir(worktree)
        (worktree / ".git").write_text(
            f"gitdir: {forged_git}/worktrees/decoy-id\n"
        )

        root, confident = derive_repo_root_from(candidate_file)

        # Secure expectation that does NOT hold today: the resolver cannot
        # tell a forged main root from a real one.
        assert not confident or root != forged_main
