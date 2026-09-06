#!/usr/bin/env python3
"""state-paths-freeze-guard.py — no module-level freeze of a state_paths
constant survives in tracked repo source.

What the invariant is
---------------------
`backend/state_paths.py` resolves STATE_DIR / STATS_DB / STATE_DB / AUDIT_LOG /
CIRCUIT_BREAKER_HISTORY / BLACKBOARD_DIR / EXTERNAL_INTAKE_BASELINES /
PARITY_HISTORY through a PEP 562 module `__getattr__`, on every access. That is
what makes a late `AUTONOMOUS_TEAM_STATE_DIR` override work regardless of import
order. Two shapes defeat it and both are banned here:

  1. `from state_paths import STATS_DB` at module level in some other file —
     the name is bound once, at that module's import time, and never follows a
     later override.
  2. `_CONST = _resolver()` at module level in `backend/db.py` — the one
     sanctioned crossing into that file, which must not come back.

Why this is a guard and not only a test
---------------------------------------
The invariant had exactly one defender, a pytest case, and CI runs no pytest —
the four required checks are `tui`, `dashboard`, `ts-backend` and
`backend (import-smoke)`, none of which invokes it. So the invariant was
defended by nothing on any merge. As a file in `scripts/ci/` it is discovered by
`scripts/ci/run-guards.sh` by directory listing and runs inside
`backend (import-smoke)` on every PR. No workflow YAML references it by name,
deliberately: a file that is both discovered and separately referenced fails
`scripts/ci/guard-registry-check.py` for running twice.

Why the subject set is the git index
------------------------------------
The pytest case this replaces enumerated candidates with `Path.rglob`, a walk of
the working tree. Untracked debris on a checkout therefore counted as repo
source: four untracked `loop-bootstrap/backend-snapshot/*.py` files turned it red
on an operator host while the same tree extracted from `main` was green. A check
whose verdict depends on which machine ran it is not a check, and this one cost
real time — it read as "the fix did not land" and sent its reader hunting a third
freeze that did not exist.

`git ls-files` is the definition of "in the repo" every other consumer here uses,
and it resolves the class rather than the instance: runtime debris is untracked,
so it is out of scope by construction and no exclusion list has to grow to keep
it that way. The INDEX, not HEAD, is the boundary — a freeze is caught the moment
it is staged, which is the moment it becomes source. `tests/test_no_planted_
spawn_ids.py` records the same reasoning for the same reasons.

The index decides WHICH files are read; each one is then read from the working
tree, not from its staged blob. So a tracked file with an uncommitted local edit
is judged on the edit — which is what a developer running this before committing
expects — while a file git does not track is not judged at all.

Scanning nothing is a failure, not a pass
-----------------------------------------
`git ls-files` exits 0 while printing nothing (a fresh `git init`, an empty
index), and a scan of zero files reports zero offenders and goes green while
guarding nothing. This repo has hit that shape repeatedly. So an empty tracked
set, an empty post-filter set, and a `backend/db.py` that is not tracked are each
a non-zero exit that says which one happened. A broken `git` raises out of
`testsupport.git_tracked.git_tracked_files` rather than degrading to an empty
set, for the same reason.

Usage
-----
    python3 scripts/ci/state-paths-freeze-guard.py
    python3 scripts/ci/state-paths-freeze-guard.py --repo-root DIR

`--repo-root` changes exactly one thing: which checkout is enumerated and read.
Discovery, filtering, both regexes, every failure branch and every exit code
below are the same code on the same path, so a `--repo-root` run is evidence
about the real run. It exists because "an empty index must fail" is only
demonstrable by pointing the real guard at an empty checkout.

Exit 0: every tracked, in-scope file is clean.
Exit 1: an offender was found, or the scan could not honestly cover anything.
Exit 2: usage error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# testsupport/ is a top-level package; this file is two directories below the
# repo root and is invoked as `python3 scripts/ci/<name>.py`, so sys.path[0] is
# scripts/ci/. Anchor on this file's own location rather than the cwd — and
# note this is deliberately NOT the --repo-root override: the helper always
# comes from the checkout the guard itself lives in, only the scanned tree moves.
sys.path.insert(0, str(REPO_ROOT))

from testsupport.git_tracked import git_tracked_files  # noqa: E402

NAME = "state-paths-freeze-guard"

# Every name backend/state_paths.py resolves lazily. Importing any of them by
# value at module level re-freezes it.
FROZEN_NAMES = (
    "STATE_DIR",
    "STATS_DB",
    "STATE_DB",
    "AUDIT_LOG",
    "CIRCUIT_BREAKER_HISTORY",
    "BLACKBOARD_DIR",
    "EXTERNAL_INTAKE_BASELINES",
    "PARITY_HISTORY",
)

BANNED_IMPORT_RE = re.compile(
    r"^from\s+(backend\.)?state_paths\s+import\s+" + r"(" + "|".join(FROZEN_NAMES) + r")\b"
)

# `_DB_PATH = _resolve_db_path()` — a module-level constant bound to a resolver's
# return value. Scoped to backend/db.py, which is where the one sanctioned
# instance lived. Widening it tree-wide is real work with pre-existing hits that
# need triage first, and it is tracked separately; it is not folded in here.
DB_PY_FREEZE_RE = re.compile(r"^_?[A-Z_]+\s*=\s*_?[a-z_]+\(\)\s*$")
DB_PY_REL = Path("backend/db.py")

SCAN_SUFFIXES = frozenset({".py", ".sh"})

# archive/ is frozen by the Archive Protocol and must never be edited to appease
# a sweep. tests/ carries deliberate fixtures of the banned shape. .claude/ and
# node_modules/ are tooling and vendored code. Matched at any depth: cheap, and
# it does not depend on where in the tree a directory with one of these names
# turns up.
EXCLUDED_DIR_NAMES = frozenset({"archive", "tests", ".claude", "node_modules"})


class EmptyScanError(RuntimeError):
    """The scan could not cover anything, so it cannot vouch for anything."""


def _excluded(rel_parts: tuple[str, ...]) -> bool:
    """True when a repo-relative path sits under an out-of-scope directory."""
    return any(part in EXCLUDED_DIR_NAMES for part in rel_parts[:-1])


def scan_files(repo_root: Path) -> list[Path]:
    """Every tracked, in-scope source file under repo_root, sorted.

    Raises EmptyScanError when the answer is empty — a scan of zero files
    reports zero offenders and would pass forever.
    """
    repo_root = repo_root.resolve()
    tracked = git_tracked_files(repo_root)
    if not tracked:
        raise EmptyScanError(
            f"git ls-files reported an empty index for {repo_root} — refusing to "
            f"report zero offenders from zero files scanned"
        )

    files = sorted(
        path
        for path in tracked
        if path.suffix in SCAN_SUFFIXES and not _excluded(path.relative_to(repo_root).parts)
    )
    if not files:
        raise EmptyScanError(
            f"{len(tracked)} tracked file(s) under {repo_root}, none of them an "
            f"in-scope {'/'.join(sorted(SCAN_SUFFIXES))} source file — refusing to "
            f"report zero offenders from zero files scanned"
        )
    return files


def scan(repo_root: Path) -> tuple[list[Path], list[str], list[str]]:
    """(files scanned, banned-import offenders, backend/db.py freeze offenders).

    Offenders are `path:lineno: text` strings, repo-relative, so a CI log names
    the file a human has to open.
    """
    repo_root = repo_root.resolve()
    files = scan_files(repo_root)

    db_py = (repo_root / DB_PY_REL).resolve()
    if db_py not in set(files):
        raise EmptyScanError(
            f"{DB_PY_REL} is not a tracked, in-scope file under {repo_root} — the "
            f"module-level-freeze check for it would scan nothing and pass. If the "
            f"file moved, point DB_PY_REL at its new path"
        )

    import_offenders: list[str] = []
    freeze_offenders: list[str] = []

    for path in files:
        rel = path.relative_to(repo_root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A tracked path that cannot be read here is a checkout problem, not
            # an offender. It is reported as neither, which is why the scanned
            # count is printed alongside the verdict.
            continue
        check_freeze = path == db_py
        for lineno, line in enumerate(text.splitlines(), start=1):
            if BANNED_IMPORT_RE.match(line):
                import_offenders.append(f"{rel}:{lineno}: {line.strip()}")
            if check_freeze and DB_PY_FREEZE_RE.match(line):
                freeze_offenders.append(f"{rel}:{lineno}: {line.strip()}")

    return files, import_offenders, freeze_offenders


REMEDY = (
    "\n  Import the module and read the attribute at call time —\n"
    "  `import backend.state_paths as sp` then `sp.STATS_DB` where it is used —\n"
    "  so a later AUTONOMOUS_TEAM_STATE_DIR override is still followed."
)


def main(argv: list[str]) -> int:
    repo_root = REPO_ROOT
    args = argv[1:]
    if args:
        if len(args) != 2 or args[0] != "--repo-root":
            print(f"usage: {Path(argv[0]).name} [--repo-root DIR]", file=sys.stderr)
            return 2
        repo_root = Path(args[1])
        if not repo_root.is_dir():
            print(f"{NAME}: FAIL — {repo_root} is not a directory", file=sys.stderr)
            return 1

    try:
        files, import_offenders, freeze_offenders = scan(repo_root)
    except (EmptyScanError, RuntimeError) as exc:
        print(f"{NAME}: FAIL — {exc}", file=sys.stderr)
        return 1

    print(f"{NAME}: {len(files)} tracked source file(s) scanned in {repo_root}")

    offenders = import_offenders + freeze_offenders
    if offenders:
        print(f"\n{NAME}: FAIL — module-level state_paths freeze(s):", file=sys.stderr)
        for line in offenders:
            print(f"  - {line}", file=sys.stderr)
        print(REMEDY, file=sys.stderr)
        return 1

    print(f"{NAME}: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
