"""tests/test_repo_root_resolver.py

Tests for the canonical checkout-path resolver (D#1997):
  - backend/repo_root.py            repo_root() / main_repo_root()
  - scripts/lib/repo-root-resolve.sh _resolve_repo_root / _resolve_main_repo_root

Every expectation here is computed *independently* of the module under test —
by running git directly, by walking this test file's own location, or by
building a synthetic repository whose layout the test itself chose — rather
than by asking the resolver what it thinks. A test that sources its expected
value from its subject can only ever assert that the subject equals itself,
which is the failure mode D#1984 catalogues and the reason this suite exists
at all: the resolver's whole job is to be right about a path, so a test that
takes the path from the resolver tests nothing.

The `Path(__file__).resolve().parents[1]` below is therefore deliberate, not a
second resolver: it is the independent measurement the resolver is checked
against.

Host-shape independence
-----------------------
The distinction this module exists to make — a linked working tree resolving
differently from the checkout it was branched from — used to be asserted only
when the host checkout happened to be a linked tree itself, and skipped into a
vacuous `root == main` branch otherwise. Every mutation of that distinction
survived on a plain clone, which is the shape CI runs. TestLinkedWorkingTree
below now builds its own repository and its own linked tree in a tmpdir, so
the assertion is the same one wherever the suite runs.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Independent anchor — see module docstring. tests/ sits directly under the
# checkout root, so one parent up is the tree this suite is running from.
_TESTS_DIR = Path(__file__).resolve().parent
_ANCHOR = _TESTS_DIR.parent

sys.path.insert(0, str(_ANCHOR))

from backend import repo_root as resolver  # noqa: E402

SHELL_RESOLVER = _ANCHOR / "scripts" / "lib" / "repo-root-resolve.sh"
_SHELL_RELPATH = "scripts/lib/repo-root-resolve.sh"


def _git(*args: str, cwd: Path) -> str | None:
    """Independent git query used to build expected values."""
    proc = subprocess.run(
        ("git", *args), cwd=str(cwd), capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _in_linked_worktree() -> bool:
    """True when this checkout is a linked working tree, decided independently
    of the resolver: a linked tree's own git dir differs from the shared one."""
    git_dir = _git("rev-parse", "--path-format=absolute", "--git-dir", cwd=_TESTS_DIR)
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=_TESTS_DIR)
    return bool(git_dir and common and Path(git_dir) != Path(common))


def _clean_env(**overrides: str) -> dict:
    """A child environment with every inherited git/resolver steering removed.

    The override and the GIT_* variables are dropped so a test's own setting is
    the only thing in play; anything a test wants is passed in explicitly.
    """
    env = os.environ.copy()
    env.pop(resolver.ENV_REPO_ROOT, None)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(var, None)
    env.update(overrides)
    return env


# Run the resolver that lives in *tree*, not the one this test imported, so a
# synthetic checkout can be exercised through its own copy of the module.
_PY_SNIPPET = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from backend.repo_root import repo_root, main_repo_root\n"
    "print(repo_root())\n"
    "print(main_repo_root())\n"
)


def _python_resolvers(env: dict, cwd: Path, tree: Path | None = None) -> tuple[str, str]:
    """(repo_root, main_repo_root) from *tree*'s Python module, run at *cwd*."""
    tree = tree or _ANCHOR
    proc = subprocess.run(
        [sys.executable, "-c", _PY_SNIPPET, str(tree)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 2, proc.stdout
    return lines[0], lines[1]


def _shell_resolvers(
    env: dict, cwd: Path, tree: Path | None = None, relative: bool = False
) -> tuple[str, str, int]:
    """(repo_root, main_repo_root, rc) from *tree*'s shell twin, run at *cwd*.

    With relative=True the file is sourced by a path relative to *cwd*, which
    is the spelling that used to leave the anchor dependent on where the caller
    later wandered to.
    """
    tree = tree or _ANCHOR
    src = _SHELL_RELPATH if relative else shlex.quote(str(Path(tree) / _SHELL_RELPATH))
    proc = subprocess.run(
        ["bash", "-c", f"source {src}; _resolve_repo_root; _resolve_main_repo_root"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    lines = proc.stdout.strip().splitlines()
    root = lines[0] if lines else ""
    main = lines[1] if len(lines) > 1 else ""
    return root, main, proc.returncode


@pytest.fixture(autouse=True)
def _clean_resolver_cache():
    """Memoised answers must not leak between tests that move the environment."""
    resolver._clear_caches()
    yield
    resolver._clear_caches()


# ---------------------------------------------------------------------------
# Python resolver — shape
# ---------------------------------------------------------------------------

class TestPythonResolverShape:
    def test_repo_root_is_absolute_existing_directory(self):
        root = resolver.repo_root()
        assert root.is_absolute(), root
        assert root.is_dir(), root

    def test_main_repo_root_is_absolute_existing_directory(self):
        main = resolver.main_repo_root()
        assert main.is_absolute(), main
        assert main.is_dir(), main

    def test_repo_root_contains_the_resolver_module(self):
        """Whatever it answers must actually be this tree, not a neighbour."""
        assert (resolver.repo_root() / "backend" / "repo_root.py").is_file()

    def test_main_repo_root_contains_a_git_directory(self):
        assert (resolver.main_repo_root() / ".git").exists()

    def test_results_are_memoised(self):
        """Both functions shell out; a caller in a loop must not pay for it."""
        assert resolver.repo_root() is resolver.repo_root()
        assert resolver.main_repo_root() is resolver.main_repo_root()


# ---------------------------------------------------------------------------
# Python resolver — correctness against independent git measurements
# ---------------------------------------------------------------------------

class TestPythonResolverCorrectness:
    def test_repo_root_matches_git_toplevel(self):
        expected = _git("rev-parse", "--show-toplevel", cwd=_TESTS_DIR)
        assert expected, "test requires a git work tree"
        assert resolver.repo_root() == Path(expected).resolve()

    def test_main_repo_root_matches_parent_of_shared_git_dir(self):
        common = _git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=_TESTS_DIR
        )
        assert common, "test requires a git work tree"
        assert resolver.main_repo_root() == Path(common).resolve().parent

    def test_worktree_and_main_differ_exactly_when_in_a_linked_worktree(self):
        """The host-shape-dependent version of the distinction.

        Kept because it is a genuine statement about the checkout the suite is
        running in, but it is NOT what pins the behaviour: on a plain clone it
        takes the `root == main` branch, which every mutant satisfies. The
        assertion that actually holds the line on every host is in
        TestLinkedWorkingTree below.
        """
        root = resolver.repo_root()
        main = resolver.main_repo_root()
        if _in_linked_worktree():
            assert root != main, (root, main)
            assert not (main / ".git").is_file(), "main checkout's .git is a directory"
        else:
            assert root == main, (root, main)


# ---------------------------------------------------------------------------
# The linked-tree distinction, asserted on any host
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def linked_tree(tmp_path_factory):
    """Build a real repository with a real linked working tree.

    Returns (repo, linked). Both paths are chosen by this fixture, so the
    expected values are independent of anything the resolver reports.

    This exists because asking "is the host a linked tree?" and skipping the
    assertion when it isn't means the assertion never runs in CI, which clones
    plainly. Building the shape locally costs one `git init` and makes the
    check say the same thing everywhere.
    """
    base = Path(tmp_path_factory.mktemp("repo_root_linked")).resolve()
    repo = base / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)

    shutil.copy2(_ANCHOR / "backend" / "repo_root.py", repo / "backend" / "repo_root.py")
    shutil.copy2(SHELL_RESOLVER, repo / _SHELL_RELPATH)

    # The fixture's own git calls are isolated from operator/system config:
    # commit signing, hook templates and init templates would otherwise be
    # inherited and can fail or hang a commit on someone else's machine. The
    # resolver runs below deliberately do NOT use this env — they should see
    # git exactly as a real caller would.
    env = _clean_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*args: str) -> None:
        proc = subprocess.run(
            ("git", *args), cwd=str(repo), env=env, capture_output=True, text=True
        )
        assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"

    git("init", "-q")
    git("config", "user.email", "resolver-test@example.invalid")
    git("config", "user.name", "resolver test")
    git("add", "-A")
    git("commit", "-q", "--no-gpg-sign", "-m", "resolver fixture")

    linked = base / "linked"
    git("worktree", "add", "-q", "--detach", str(linked), "HEAD")

    assert (linked / "backend" / "repo_root.py").is_file(), "fixture did not populate"
    assert (repo / ".git").is_dir(), "main checkout .git should be a directory"
    assert (linked / ".git").is_file(), "linked tree .git should be a file"
    return repo, linked


class TestLinkedWorkingTree:
    """The distinction the contamination classifier depends on.

    Every assertion runs identically on a plain clone and on a linked-tree
    host, because the shape under test is built here rather than inherited
    from wherever the suite happens to be checked out.
    """

    def test_python_repo_root_is_the_linked_tree(self, linked_tree):
        repo, linked = linked_tree
        env = _clean_env()
        root, _ = _python_resolvers(env, cwd=repo.parent, tree=linked)
        assert root == str(linked)

    def test_python_main_repo_root_is_the_branched_from_checkout(self, linked_tree):
        repo, linked = linked_tree
        env = _clean_env()
        _, main = _python_resolvers(env, cwd=repo.parent, tree=linked)
        assert main == str(repo)

    def test_python_answers_actually_differ(self, linked_tree):
        """The mutation this pins: main_repo_root() returning repo_root()."""
        repo, linked = linked_tree
        env = _clean_env()
        root, main = _python_resolvers(env, cwd=repo.parent, tree=linked)
        assert root != main, (root, main)
        assert str(repo) != str(linked), "fixture built a degenerate pair"

    def test_shell_repo_root_is_the_linked_tree(self, linked_tree):
        repo, linked = linked_tree
        root, _, rc = _shell_resolvers(_clean_env(), cwd=repo.parent, tree=linked)
        assert rc == 0
        assert root == str(linked)

    def test_shell_main_repo_root_is_the_branched_from_checkout(self, linked_tree):
        repo, linked = linked_tree
        _, main, rc = _shell_resolvers(_clean_env(), cwd=repo.parent, tree=linked)
        assert rc == 0
        assert main == str(repo)

    def test_python_and_shell_agree_inside_a_linked_tree(self, linked_tree):
        repo, linked = linked_tree
        env = _clean_env()
        py_root, py_main = _python_resolvers(env, cwd=repo.parent, tree=linked)
        sh_root, sh_main, rc = _shell_resolvers(env, cwd=repo.parent, tree=linked)
        assert rc == 0
        assert (py_root, py_main) == (sh_root, sh_main)

    def test_python_resolver_is_cwd_independent(self, linked_tree):
        """Run from inside a *different* repository; still answers about this tree.

        This is the defect the pre-D#1997 implementation had and the reason the
        anchoring changed: it ran `git rev-parse` with no cwd=, so standing in
        another repo made it report that repo's root while its docstring
        promised this one. The fixture's repo is a real, unrelated checkout, so
        a resolver that consults the process cwd answers with it and fails here.
        """
        repo, _ = linked_tree
        env = _clean_env()
        root, main = _python_resolvers(env, cwd=repo)
        assert root == str(resolver.repo_root()), f"followed the cwd into {root!r}"
        assert main == str(resolver.main_repo_root()), f"followed the cwd into {main!r}"


# ---------------------------------------------------------------------------
# Python resolver — overrides and fallbacks
# ---------------------------------------------------------------------------

class TestPythonResolverFallbacks:
    def test_env_override_wins_for_repo_root(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve()
            monkeypatch.setenv(resolver.ENV_REPO_ROOT, str(target))
            resolver._clear_caches()
            assert resolver.repo_root() == target

    def test_env_override_outside_git_falls_back_to_itself_for_main(self, monkeypatch):
        """An override pointing somewhere git knows nothing about must not
        silently hand back this repo's real root."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp).resolve()
            monkeypatch.setenv(resolver.ENV_REPO_ROOT, str(target))
            resolver._clear_caches()
            assert resolver.main_repo_root() == target

    def test_repo_root_falls_back_to_module_anchor_without_git(self, monkeypatch):
        monkeypatch.delenv(resolver.ENV_REPO_ROOT, raising=False)
        monkeypatch.setattr(resolver, "_git", lambda *a, **k: None)
        resolver._clear_caches()
        assert resolver.repo_root() == resolver._MODULE_ANCHOR

    def test_main_repo_root_falls_back_to_repo_root_without_git(self, monkeypatch):
        monkeypatch.delenv(resolver.ENV_REPO_ROOT, raising=False)
        monkeypatch.setattr(resolver, "_git", lambda *a, **k: None)
        resolver._clear_caches()
        assert resolver.main_repo_root() == resolver.repo_root()

    def test_main_repo_root_falls_back_when_shared_git_dir_is_not_dot_git(
        self, monkeypatch
    ):
        """Bare repo / --separate-git-dir layouts, where the parent of the
        shared git dir is some unrelated folder rather than a checkout.

        D#1984: the fixture directory MUST actually exist. main_repo_root()
        has two independent branches that each produce this same fallback —
        the ``.git``-name check, and ``parent.is_dir()`` — and a nonexistent
        common-dir path (the previous fixture: '/nonexistent/somewhere/...')
        satisfies the second branch regardless of the first, so deleting the
        name guard entirely left this assertion passing unchanged (verified:
        41 passed with the guard removed). Pointing the common dir at a real,
        existing directory that merely isn't named ``.git`` means only the
        name guard can produce the fallback; removing it instead makes
        main_repo_root() return that real directory, which is not repo_root(),
        and this assertion goes red.
        """
        monkeypatch.delenv(resolver.ENV_REPO_ROOT, raising=False)

        with tempfile.TemporaryDirectory() as tmp:
            # A real, existing parent — the leaf itself need not exist, only
            # `common_path.parent` is ever stat'd.
            shared_git_dir = Path(tmp).resolve() / "project.git"

            def fake_git(*args, **kwargs):
                if "--show-toplevel" in args:
                    return str(_ANCHOR)
                if "--git-common-dir" in args:
                    return str(shared_git_dir)
                return None

            monkeypatch.setattr(resolver, "_git", fake_git)
            resolver._clear_caches()
            assert resolver.main_repo_root() == resolver.repo_root()


# ---------------------------------------------------------------------------
# Shell resolver, and parity with the Python one
# ---------------------------------------------------------------------------

def _run_shell_resolver(env_override: dict | None = None) -> tuple[str, str, int]:
    """Source the shell resolver exactly as the Spec's acceptance check does."""
    env = _clean_env(**(env_override or {}))
    return _shell_resolvers(env, cwd=_ANCHOR, relative=True)


class TestShellResolver:
    def test_shell_resolver_file_exists(self):
        assert SHELL_RESOLVER.is_file(), SHELL_RESOLVER

    def test_shell_resolver_exits_zero_and_prints_two_absolute_paths(self):
        root, main, rc = _run_shell_resolver()
        assert rc == 0
        assert root.startswith("/"), root
        assert main.startswith("/"), main

    def test_shell_resolver_agrees_with_python_resolver(self):
        """Spec acceptance item 2: the shell twin prints the same two paths."""
        root, main, rc = _run_shell_resolver()
        assert rc == 0
        resolver._clear_caches()
        assert root == str(resolver.repo_root())
        assert main == str(resolver.main_repo_root())

    def test_shell_resolver_is_cwd_independent(self):
        """Anchored to its own file, so running it from another directory —
        including one inside a different repository — answers about this tree."""
        from_root, main_from_root, _ = _run_shell_resolver()
        with tempfile.TemporaryDirectory() as tmp:
            root, main, rc = _shell_resolvers(_clean_env(), cwd=Path(tmp))
        assert rc == 0
        assert root == from_root
        assert main == main_from_root

    def test_shell_main_repo_root_falls_back_when_shared_git_dir_is_not_dot_git(self):
        """Shell twin of TestPythonResolverFallbacks's same-named test (D#1984).

        A parity test (assert-shell-agrees-with-Python) cannot catch a
        mutation applied identically to both twins — that is exactly how the
        removed guard survived once already. This stubs the shell's own git
        wrapper directly, independent of the Python resolver, and — same fix
        as the Python test — points the common dir at a REAL, existing
        directory that merely isn't named ``.git``, so only the name guard
        (not a nonexistent-path fallback) can produce the result.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Unlike the Python fix, the shell resolver's `cd "$common/.."`
            # must actually be able to `cd` into `$common` first — bash's cd
            # resolves path components against the filesystem, unlike
            # Path.resolve()'s pure string normalisation — so the leaf itself,
            # not just its parent, has to exist here.
            shared_git_dir = Path(tmp).resolve() / "project.git"
            shared_git_dir.mkdir()
            script = (
                f"source {_SHELL_RELPATH}\n"
                "_repo_root_resolve__git() {\n"
                "  for a in \"$@\"; do\n"
                f"    [ \"$a\" = --show-toplevel ] && printf %s {shlex.quote(str(_ANCHOR))} && return 0\n"
                f"    [ \"$a\" = --git-common-dir ] && printf %s {shlex.quote(str(shared_git_dir))} && return 0\n"
                "  done\n"
                "  return 1\n"
                "}\n"
                "_resolve_repo_root\n"
                "_resolve_main_repo_root\n"
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                cwd=str(_ANCHOR),
                env=_clean_env(),
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr
            lines = proc.stdout.strip().splitlines()
            assert len(lines) == 2, proc.stdout
            root, main = lines
            assert main == root, (root, main)


# ---------------------------------------------------------------------------
# The env override must mean the same thing in both languages
# ---------------------------------------------------------------------------

class TestEnvOverrideNormalisation:
    """Python normalised the override and the shell echoed it raw.

    Every non-canonical spelling therefore diverged, and both override tests
    passed a pre-normalised `str(Path(tmp).resolve())`, so neither could fail
    on it. These pass the spellings a human actually types. The trailing slash
    is the one that bites in production: the contamination classifier
    prefix-matches write paths against this value, and `/repo/` is not the same
    prefix as `/repo`.
    """

    @staticmethod
    def _spellings(target: Path) -> dict[str, str]:
        return {
            "canonical": str(target),
            "trailing_slash": str(target) + "/",
            "dot_slash": str(target) + "/./",
            "dot_dot": f"{target}/../{target.name}",
            "tilde": f"~/{target.name}",
            "relative": target.name,
        }

    @pytest.mark.parametrize(
        "spelling",
        ["canonical", "trailing_slash", "dot_slash", "dot_dot", "tilde", "relative"],
    )
    def test_shell_normalises_the_override_like_python(self, spelling, tmp_path):
        # HOME is pointed at the tmpdir so the `~` spelling is exercised for
        # real without writing anything into the operator's actual home.
        home = tmp_path.resolve()
        target = home / "tree"
        target.mkdir()

        raw = self._spellings(target)[spelling]
        env = _clean_env(HOME=str(home), **{resolver.ENV_REPO_ROOT: raw})

        # cwd is HOME so the bare-relative spelling has something to resolve
        # against, in both languages.
        py_root, py_main = _python_resolvers(env, cwd=home)
        sh_root, sh_main, rc = _shell_resolvers(env, cwd=home)

        assert rc == 0
        assert sh_root == str(target), f"{spelling}: shell gave {sh_root!r}"
        assert sh_main == str(target), f"{spelling}: shell gave {sh_main!r}"
        assert (py_root, py_main) == (sh_root, sh_main), spelling

    def test_shell_override_is_absolute_even_when_the_path_does_not_exist(
        self, tmp_path
    ):
        """`cd` cannot normalise a path that isn't there, but the fallback must
        still never emit a relative path — that is the defect class this whole
        sweep exists to remove."""
        env = _clean_env(**{resolver.ENV_REPO_ROOT: "no/such/tree"})
        sh_root, sh_main, rc = _shell_resolvers(env, cwd=tmp_path)
        assert rc == 0
        assert sh_root.startswith("/"), sh_root
        assert sh_main.startswith("/"), sh_main


# ---------------------------------------------------------------------------
# The shell anchor must be fixed at source time, not at call time
# ---------------------------------------------------------------------------

class TestShellAnchorIsCapturedAtSourceTime:
    """`cd`-ing after a relative `source` used to change the answer.

    The anchor was computed inside the function, so a relative BASH_SOURCE[0]
    was re-resolved against wherever the caller stood when it *called*, not
    where it sourced. Measured on the old code: from a linked tree, sourcing
    relatively and then cd-ing to /tmp returned empty with rc=1, and cd-ing
    into a sibling checkout of the same project silently returned the sibling.
    Both contradicted the file header. The tests below pin each shape.
    """

    def test_relative_source_then_cd_elsewhere_keeps_the_answer(self):
        env = _clean_env()
        script = (
            f"source {_SHELL_RELPATH}\n"
            'before="$(_resolve_repo_root)"\n'
            "cd /\n"
            'after="$(_resolve_repo_root)"\n'
            'printf "%s\\n%s\\n" "$before" "$after"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            cwd=str(_ANCHOR),
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert len(lines) == 2, proc.stdout
        before, after = lines
        assert before == after, (before, after)
        assert after == str(resolver.repo_root())

    def test_relative_source_then_cd_into_another_checkout(self, linked_tree):
        """The silent-wrong-answer shape: no error, different repository.

        Sources from inside the linked tree by a relative path, then changes
        into the checkout it was branched from — a genuinely different working
        tree of the same project, which is exactly what made the old bug
        invisible.
        """
        repo, linked = linked_tree
        env = _clean_env()
        script = (
            f"source {_SHELL_RELPATH}\n"
            'before="$(_resolve_repo_root)"\n'
            f"cd {shlex.quote(str(repo))}\n"
            'after="$(_resolve_repo_root)"\n'
            'printf "%s\\n%s\\n" "$before" "$after"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            cwd=str(linked),
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert len(lines) == 2, proc.stdout
        before, after = lines
        assert before == str(linked), before
        assert after == str(linked), f"anchor followed the cwd into {after!r}"
