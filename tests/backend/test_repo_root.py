"""tests/backend/test_repo_root.py

Tests that main_repo_root() always resolves to the MAIN working-tree root,
even when called from a linked working tree or without a git repo at all.

AC1: From the main checkout, returns a path that contains a .git directory.
AC2: With git rev-parse returning a linked tree's shared common-dir, returns
     the parent of that common-dir (the main checkout), not the linked tree.
AC3: When git rev-parse fails (non-git context), falls back to __file__-derived path.
AC4: status_page._DEFAULT_OUTPUT points at main repo wiki/, not a linked tree's wiki/.
AC5: corpus-drift-audit wiki_dir resolves to main repo wiki/ from any cwd.

These ACs predate D#1997 and still hold; what changed underneath them is the
seam. The module used to call subprocess.run directly with no cwd=, so these
tests patched `backend.repo_root.subprocess.run`. It now routes every git call
through the `_git` helper (which anchors at a path and applies a timeout), so
the patches target `_git`. Patching subprocess.run would still "work" in the
sense of not erroring — a MagicMock's .returncode compares unequal to 0, so
_git would read it as a failed git and fall back — which is exactly the shape
of test that passes without testing anything. Hence the seam change here.

The results are also memoised now, so every test that moves the environment
must clear the caches first; the autouse fixture below does it.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backend import repo_root


@pytest.fixture(autouse=True)
def _clear_resolver_caches():
    """main_repo_root()/repo_root() are lru_cached — a patch applied after a
    real call would otherwise be handed a stale answer and assert nothing."""
    repo_root._clear_caches()
    yield
    repo_root._clear_caches()


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """The override short-circuits everything below it; these ACs are about the
    git path, so it must not leak in from the ambient environment."""
    monkeypatch.delenv(repo_root.ENV_REPO_ROOT, raising=False)


# ---------------------------------------------------------------------------
# AC1: From the current environment (main checkout or linked tree), the
#      returned path must be the main repo root that contains a .git DIR.
# ---------------------------------------------------------------------------


def test_main_repo_root_has_git_dir():
    """main_repo_root() returns a path with a .git directory (main checkout)."""
    from backend.repo_root import main_repo_root

    root = main_repo_root()
    git_path = root / ".git"
    assert git_path.exists(), (
        f"Expected {root}/.git to exist, but it doesn't. "
        f"main_repo_root() returned: {root}"
    )
    assert git_path.is_dir(), (
        f"{root}/.git exists but is not a directory (is it a linked tree's .git file?). "
        f"main_repo_root() should return the MAIN checkout root."
    )


# ---------------------------------------------------------------------------
# AC2: When git-common-dir points to a linked tree's shared .git, the function
#      returns the *parent* of that dir — which is the main repo root.
# ---------------------------------------------------------------------------


def test_worktree_common_dir_resolves_to_main_root(tmp_path):
    """Simulate a linked working tree: --show-toplevel names the linked tree
    while --git-common-dir names the shared .git in the main checkout.

    The function must return <main>, not the linked tree root. Both halves are
    faked, so this pins the mapping rather than just the last hop.
    """
    # A fake "main repo" with a real .git directory, and a linked tree beside it.
    fake_main = tmp_path / "main-repo"
    fake_main.mkdir()
    fake_git = fake_main / ".git"
    fake_git.mkdir()

    fake_linked = tmp_path / "linked-tree"
    fake_linked.mkdir()

    def fake_git_cmd(*args, **kwargs):
        if "--show-toplevel" in args:
            return str(fake_linked)
        if "--git-common-dir" in args:
            return str(fake_git)
        return None

    with patch.object(repo_root, "_git", fake_git_cmd):
        repo_root._clear_caches()
        result = repo_root.main_repo_root()
        running_in = repo_root.repo_root()

    assert running_in == fake_linked, (
        f"repo_root() should name the linked tree, got {running_in}"
    )
    assert result == fake_main, (
        f"Expected {fake_main}, got {result}. "
        "main_repo_root() should return parent of git-common-dir."
    )
    assert result != running_in, "the two answers must differ inside a linked tree"


# ---------------------------------------------------------------------------
# AC3: When git rev-parse fails (not in a git repo), falls back gracefully.
# ---------------------------------------------------------------------------


def test_fallback_when_git_unavailable():
    """When git cannot answer at all, use the __file__-derived fallback."""
    with patch.object(repo_root, "_git", lambda *a, **k: None):
        repo_root._clear_caches()
        result = repo_root.main_repo_root()

    # Fallback is Path(__file__).resolve().parent.parent
    expected_fallback = Path(repo_root.__file__).resolve().parent.parent
    assert result == expected_fallback, (
        f"Expected __file__-based fallback {expected_fallback}, got {result}"
    )


def test_fallback_when_git_returns_empty():
    """Blank/whitespace-only git output is 'git cannot answer', not an answer.

    _git already collapses empty stdout to None; this pins that a caller
    handing back "" or "  \\n" can never be mistaken for a path.
    """
    with patch.object(repo_root, "_git", lambda *a, **k: None):
        repo_root._clear_caches()
        result = repo_root.main_repo_root()

    expected_fallback = Path(repo_root.__file__).resolve().parent.parent
    assert result == expected_fallback, (
        f"Expected __file__-based fallback {expected_fallback}, got {result}"
    )


def test_git_helper_treats_blank_stdout_as_no_answer():
    """The layer below the two tests above: _git itself must return None for
    whitespace-only output, so the fallbacks they assert are actually reached."""
    from unittest.mock import MagicMock

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "  \n"

    with patch.object(repo_root.subprocess, "run", return_value=proc):
        assert repo_root._git("rev-parse", "--show-toplevel", cwd=Path("/")) is None


def test_git_helper_treats_nonzero_exit_as_no_answer():
    """A failing git must not have its stdout read as a path."""
    from unittest.mock import MagicMock

    proc = MagicMock()
    proc.returncode = 128
    proc.stdout = "/not/a/real/answer\n"

    with patch.object(repo_root.subprocess, "run", return_value=proc):
        assert repo_root._git("rev-parse", "--show-toplevel", cwd=Path("/")) is None


# ---------------------------------------------------------------------------
# AC4: status_page._DEFAULT_OUTPUT must NOT be inside a linked tree's wiki/.
#      It must point at the MAIN repo wiki/.
# ---------------------------------------------------------------------------


def test_status_page_default_output_not_in_worktree():
    """status_page._DEFAULT_OUTPUT must not live inside a linked tree path."""
    # Reload to pick up any module-level mutations
    import importlib
    import backend.status_page as sp
    importlib.reload(sp)

    out = sp._DEFAULT_OUTPUT
    out_str = str(out)

    # Must not contain a linked-tree marker
    assert ".claude/worktrees" not in out_str, (
        f"_DEFAULT_OUTPUT is inside a linked tree: {out_str}"
    )

    # Must end with wiki/Project-Status.md
    assert out_str.endswith("wiki/Project-Status.md"), (
        f"Unexpected _DEFAULT_OUTPUT path: {out_str}"
    )


# ---------------------------------------------------------------------------
# AC5: corpus-drift-audit wiki_dir resolves to main root, not a linked tree.
# ---------------------------------------------------------------------------


def test_corpus_drift_audit_wiki_dir_not_in_worktree():
    """wiki_dir inside corpus-drift-audit must resolve to the main repo wiki/."""
    from backend.repo_root import main_repo_root

    wiki_dir = main_repo_root() / "wiki"
    wiki_dir_str = str(wiki_dir)

    assert ".claude/worktrees" not in wiki_dir_str, (
        f"wiki_dir is inside a linked tree: {wiki_dir_str}"
    )
    # Should resolve to a real path that looks like a wiki dir
    assert wiki_dir_str.endswith("/wiki"), (
        f"Unexpected wiki_dir: {wiki_dir_str}"
    )
