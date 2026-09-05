"""Regression guard for the D#2088 sys.path shadow.

backend/conftest.py used to insert backend/ itself at sys.path[0]. Since
backend/hooks/ is a real package (containing only wrong_premise_guard.py),
that made `import hooks` resolve to backend/hooks/ instead of the real
repo-root hooks/ package -- which has no sandbox_rules or repo_root -- and
broke any standalone, targeted collection of a test file that hadn't
already been rescued by an earlier-collected file's own sys.path fix
(backend/tests/test_a2a_broker.py, which happens to sort first
alphabetically). The fix moved the insert to the repo root instead.

Two independent things are checked here:

1. backend/tests/test_prompt_lane.py must collect on its own, as a
   standalone subprocess invocation -- not just as part of the full
   backend/tests/ directory run, where an unrelated earlier file's
   sys.path insert happened to paper over this.

2. No directory under backend/ should start sharing its name with a real,
   committed package at the repo root without that being a deliberate,
   reviewed change. `hooks` and `tests` already collide today (that's the
   known, accepted D#2088 finding -- not fixed here) and neither name is
   hard-coded below: the check diffs what's on disk right now against
   what's committed at HEAD, so a *new*, uncommitted collision is caught
   on arrival regardless of what it's called.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # Strip PYTEST_CURRENT_TEST: this helper's whole point is to measure what
    # a genuinely standalone invocation does, and pytest only ever sets that
    # var during a *running* test, never at collection time. Left inherited,
    # it leaks from the outer pytest process running *this* test into the
    # child and makes the child behave as if it were mid-test-run rather
    # than a cold collection, which is not what a real standalone run sees.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, env=env
    )


def test_prompt_lane_collects_standalone() -> None:
    """Criterion 1: a targeted, standalone collection must succeed and
    find the file's tests -- not merely avoid crashing."""
    result = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/test_prompt_lane.py",
            "--collect-only",
            "-q",
        ]
    )
    assert result.returncode == 0, (
        f"standalone collection of backend/tests/test_prompt_lane.py failed "
        f"(rc={result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
    assert "no tests collected" not in result.stdout
    assert "ModuleNotFoundError" not in result.stdout
    assert " 0 tests collected" not in result.stdout


def test_prompt_lane_collection_anti_vacuity() -> None:
    """Criterion 8: pointing the same harness at a path that does not
    exist must fail loudly and name the missing path. If this passed, the
    rc==0 check above would be measuring nothing."""
    result = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests/does_not_exist.py",
            "--collect-only",
            "-q",
        ]
    )
    assert result.returncode != 0
    assert "does_not_exist.py" in (result.stdout + result.stderr)


def _fs_real_packages(directory: Path) -> set[str]:
    """Immediate child directory names of `directory` that are real Python
    packages (contain __init__.py), as the filesystem stands right now."""
    return {
        child.name
        for child in directory.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }


def _fs_dir_names(directory: Path) -> set[str]:
    """Immediate child directory names of `directory`, real package or not.
    A plain directory (no __init__.py) is still importable as a PEP 420
    namespace package, and a *real* package elsewhere on sys.path always
    wins over a namespace-package merge regardless of search order -- so
    the repo-root side of a dangerous collision doesn't need __init__.py to
    be at risk (that's exactly backend/scripts/__init__.py vs. the
    real, __init__.py-less repo-root scripts/ in mutation B below)."""
    return {child.name for child in directory.iterdir() if child.is_dir()}


def _committed_real_packages(rel_dir: str) -> set[str]:
    """Same computation as _fs_real_packages, but against git HEAD rather
    than the working tree, keyed off a path relative to the repo root
    ("" for the repo root itself)."""
    ref = f"HEAD:{rel_dir}" if rel_dir else "HEAD"
    top = _run(["git", "ls-tree", ref])
    if top.returncode != 0:
        raise RuntimeError(f"git ls-tree {ref} failed: {top.stderr}")

    child_dirs = []
    for line in top.stdout.splitlines():
        meta, name = line.split("\t", 1)
        if meta.split(" ")[1] == "tree":
            child_dirs.append(name)

    packages = set()
    for name in child_dirs:
        sub_ref = f"HEAD:{rel_dir}/{name}" if rel_dir else f"HEAD:{name}"
        sub = _run(["git", "ls-tree", sub_ref])
        if sub.returncode == 0 and any(
            line.split("\t", 1)[1] == "__init__.py"
            for line in sub.stdout.splitlines()
        ):
            packages.add(name)
    return packages


def _committed_dir_names(rel_dir: str) -> set[str]:
    """Same computation as _fs_dir_names, but against git HEAD."""
    ref = f"HEAD:{rel_dir}" if rel_dir else "HEAD"
    top = _run(["git", "ls-tree", ref])
    if top.returncode != 0:
        raise RuntimeError(f"git ls-tree {ref} failed: {top.stderr}")
    names = set()
    for line in top.stdout.splitlines():
        meta, name = line.split("\t", 1)
        if meta.split(" ")[1] == "tree":
            names.add(name)
    return names


def test_no_new_backend_root_package_collision() -> None:
    """Criterion 6, second bullet: no real package under backend/ should
    start sharing a name with anything importable at the repo root that it
    didn't already share at HEAD. A collision by itself isn't the bug --
    `hooks` and `tests` already collide today and that's tracked, not fixed
    here. What must never slip in unnoticed is a *new* one, because that is
    exactly the shape of shadow that broke test_prompt_lane.py: if
    backend/ is ever put ahead of the repo root on sys.path again, every
    name in this set resolves to the wrong package.

    The repo-root side only needs to be a plain directory, not a real
    package (__init__.py): a directory with no __init__.py is still
    importable as a PEP 420 namespace package, and a real package
    elsewhere on sys.path always wins over a namespace-package merge
    regardless of search order -- so a bare directory at the repo root is
    just as much at risk as a real package there."""
    backend_dir = REPO_ROOT / "backend"

    fs_collisions = _fs_real_packages(backend_dir) & (
        _fs_dir_names(REPO_ROOT) - {"backend"}
    )
    committed_collisions = _committed_real_packages("backend") & (
        _committed_dir_names("") - {"backend"}
    )

    assert fs_collisions == committed_collisions, (
        "backend/<name> real packages colliding with a repo-root directory "
        f"changed relative to HEAD: committed={sorted(committed_collisions)} "
        f"working-tree={sorted(fs_collisions)}"
    )
