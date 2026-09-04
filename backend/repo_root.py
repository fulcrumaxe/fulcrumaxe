"""backend/repo_root.py — canonical checkout-path resolver.

Single source of truth for *where the checkout is on disk*. Companion to
backend/_repo.py, which resolves the repo *slug* (owner/name); this module
resolves the repo *path*.

Two questions, kept deliberately separate because callers conflate them:

  repo_root()       the checkout this process is running in. Inside a linked
                    git working tree this is that working tree, not the
                    checkout it was branched from.
  main_repo_root()  the checkout a linked working tree was created from.
                    Outside one it equals repo_root().

Anything that reads or writes files belonging to the running agent wants
repo_root(). Anything that asks "did this write land outside the caller's
sandbox" wants main_repo_root() — the contamination classifier in
backend/run_analyst.py is the motivating consumer, and it was silently wrong
for months precisely because it used a file-location-derived constant that
named the linked tree while claiming to name the main repo.

History, because this docstring used to overclaim
--------------------------------------------------
This module has answered ``main_repo_root()`` since #1235 ("pin wiki writers
to main repo root so worktrees stay clean"). D#1997 proposed adding a second
module for the same question, on the false premise that nothing here resolved
a checkout path yet; review caught it and the behaviour was folded in here
instead. What D#1997 actually contributes is ``repo_root()`` — the
linked-tree-versus-main distinction, which did not exist before — plus a fix
to how both answers are anchored.

The fix: resolution is now anchored to *this file's* location, never to the
process cwd. The pre-#1997 implementation ran ``git rev-parse`` with no
``cwd=``, so it answered about wherever the caller happened to be standing.
Measured from ``/tmp`` inside a linked tree it returned that linked tree, and
run from inside an unrelated repository it returned that repository's root —
both while this docstring promised the main checkout. The two consumers that
predate the change (backend/status_page.py, scripts/corpus-drift-audit.py)
were getting those wrong answers. Anchoring to the module keeps the answer a
property of the installed tree rather than of the invocation.

Why hooks/repo_root.py is a third implementation and stays that way
-------------------------------------------------------------------
hooks/repo_root.py answers a nearly identical question and must NOT be folded
into this module. hooks/sandbox_rules.py imports it and is deliberately
subprocess-free — it runs inside a PreToolUse hook on every single tool call,
where shelling out to git would be both a latency cost and a re-entrancy
hazard. So hooks/repo_root.py derives its root from the filesystem alone, by
reading the ``.git`` entry, and it reports a ``confident`` flag this module
has no use for, because a sandbox tiering decision must refuse to guess where
this module is free to fall back. Different constraint, different
implementation, on purpose. Do not "consolidate" it; that breaks the hook.

Shell twin
----------
scripts/lib/repo-root-resolve.sh exposes the same two answers as
``_resolve_repo_root`` and ``_resolve_main_repo_root``. The two are kept in
byte-for-byte agreement by tests/test_repo_root_resolver.py, which asserts
parity rather than trusting it.

Neither function raises. backend/_repo.py deliberately fails loudly when it
cannot resolve a slug, because guessing a slug means acting against a repo
you may not own. A checkout path carries no such hazard and has a
correct-by-construction floor: this file lives at <root>/backend/, so walking
two parents up is always *a* right answer, even with no git on the box.

Environment override — convenience only, never load-bearing
-------------------------------------------------------------
ENV_REPO_ROOT (AUTONOMOUS_TEAM_REPO_ROOT) is settable by the very process
whose checkout it names, so it must never be the thing a security or
containment decision keys off. As of writing nothing does: both consumers
(backend/status_page.py, scripts/corpus-drift-audit.py) use the value to
*locate* files, never to authorise anything. That is exactly the property
that must hold going forward too, so it is written down rather than left
implicit.

hooks/repo_root.py solved this same hazard for its own env override,
SANDBOX_MAIN_REPO_ROOT: it documents the override as never load-bearing for
containment, and backs that with a filesystem-derived floor
(derive_main_repo_root()) the override cannot lift, which is what a future
containment or authorisation decision would need to key off instead of
repo_root()/main_repo_root(). This module mirrors that split:
``_derive_repo_root()`` and ``_derive_main_repo_root()`` below are the
env-immune floor, unused by any current caller, kept ready for the day one
needs a checkout path it can trust even when AUTONOMOUS_TEAM_REPO_ROOT is
set to something like ``/etc``.

Results are memoised, so the git subprocesses run at most once per process.
Anything that manipulates the environment and then re-resolves — tests,
mostly — must call ``_clear_caches()`` first.

For the current list of callers, run the grep:

    grep -rl 'from backend.repo_root import' --include=*.py .

A count written into this docstring would be stale by its first review; see
backend/_repo.py's own docstring for how that went last time.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

# The one environment override. Set it to force both answers away from what
# git reports — useful when a tree is staged somewhere git cannot see it.
#
# Convenience only, deliberately NOT load-bearing for any containment or
# authorisation decision — see the module docstring's "Environment override"
# section. Callers that need a value this override cannot steer want
# _derive_repo_root() / _derive_main_repo_root() instead.
ENV_REPO_ROOT = "AUTONOMOUS_TEAM_REPO_ROOT"

# This file is <repo-root>/backend/repo_root.py, so two parents up is the
# checkout root. Serves as the anchor for the git queries below and as the
# final fallback when git cannot answer at all.
_MODULE_ANCHOR = Path(__file__).resolve().parent.parent


def _git(*args: str, cwd: Path) -> str | None:
    """Run ``git *args`` anchored at *cwd*; return stripped stdout, or None.

    Every failure mode collapses to None on purpose — a missing git binary, a
    directory that is not a work tree, and a git too old for a flag used below
    are all "git cannot answer this", and every caller handles that the same
    way by falling back.
    """
    try:
        proc = subprocess.run(
            ("git", *args),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _derive_repo_root() -> Path:
    """Filesystem-derived checkout root. Never consults AUTONOMOUS_TEAM_REPO_ROOT.

    The env-immune floor described in ENV_REPO_ROOT's docstring: whatever
    future caller needs a repo_root() a sandboxed agent cannot steer via the
    environment should key off this instead of repo_root() itself.
    """
    top = _git("rev-parse", "--show-toplevel", cwd=_MODULE_ANCHOR)
    if top:
        return Path(top).resolve()
    return _MODULE_ANCHOR


def _main_repo_root_from(root: Path) -> Path:
    """Shared resolution logic behind main_repo_root(), parameterised on
    *root* so the real function and its env-immune floor can both use it
    without re-implementing the git-common-dir walk.

    ``git rev-parse --git-common-dir`` names the *shared* git directory:
    inside a linked working tree that is the main checkout's ``.git``, and
    outside one it is this checkout's own ``.git``. Its parent is therefore
    the main checkout in both cases, which is why there is no "am I in a
    linked tree" branch here to get wrong — that branch is the bug this
    function exists to retire.

    Falls back to *root* whenever the shared git directory does not sit
    inside a working tree: a bare repo, or a ``--separate-git-dir`` layout,
    where the parent directory is some unrelated folder rather than a
    checkout. Detected by name rather than assumed.
    """
    # --path-format=absolute needs git 2.31+; the bare form is the fallback
    # and may answer with a path relative to the work tree.
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root)
    if not common:
        common = _git("rev-parse", "--git-common-dir", cwd=root)
    if not common:
        return root

    # Joining an absolute right operand discards the left, so this handles the
    # absolute and the relative answer in one expression.
    common_path = (root / common).resolve()
    if common_path.name != ".git":
        return root

    parent = common_path.parent
    if not parent.is_dir():
        return root
    return parent


def _derive_main_repo_root() -> Path:
    """Filesystem-derived main-checkout root. Never consults
    AUTONOMOUS_TEAM_REPO_ROOT — the floor counterpart to _derive_repo_root(),
    see ENV_REPO_ROOT's docstring.
    """
    return _main_repo_root_from(_derive_repo_root())


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Absolute path of the checkout this process is running in.

    Resolution order:
      1. AUTONOMOUS_TEAM_REPO_ROOT — explicit override always wins.
      2. ``git rev-parse --show-toplevel``, anchored at this module. Inside a
         linked working tree this is that linked tree.
      3. This module's own location, two parents up.
    """
    env = os.environ.get(ENV_REPO_ROOT)
    if env:
        return Path(env).expanduser().resolve()

    return _derive_repo_root()


@lru_cache(maxsize=1)
def main_repo_root() -> Path:
    """Absolute path of the checkout a linked working tree was branched from.

    See _main_repo_root_from() for the resolution logic. This wrapper just
    supplies repo_root() (which honours AUTONOMOUS_TEAM_REPO_ROOT) as the
    starting point — see _derive_main_repo_root() for the env-immune floor.
    """
    return _main_repo_root_from(repo_root())


def _clear_caches() -> None:
    """Drop memoised results so the next call re-resolves.

    Only needed by callers that change the environment underneath this module.
    Production code resolves once and keeps the answer.
    """
    repo_root.cache_clear()
    main_repo_root.cache_clear()


__all__ = ["ENV_REPO_ROOT", "repo_root", "main_repo_root"]
