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
set, an empty post-filter set, a `backend/db.py` that is not tracked, a file the
scan could not read, and a read count of zero are each a non-zero exit that says
which one happened. A broken `git` raises out of
`testsupport.git_tracked.git_tracked_files` rather than degrading to an empty
set, for the same reason.

Enumerating a file is not reading it, and the count printed is the count READ.
An earlier revision of this guard skipped an unreadable file with a bare
`continue` and printed the enumerated total, so a tracked file whose working-tree
copy was missing left the total unmoved and the verdict green — measured: the
same staged offender reported `604 scanned / FAIL / exit=1` with the file on
disk and `604 scanned / PASS / exit=0` with it deleted, while the offending blob
sat in the index and was what `git commit` would have written. It degraded all
the way down: every working-tree copy removed still printed the full total and
passed, having read nothing.

An unreadable tracked file is therefore FATAL here, not skipped. The guard cannot
say anything about a file it did not read, and this repo's dominant defect is a
check reporting success it never measured. The cost of that choice is a checkout
where the index and the working tree legitimately disagree — a sparse or partial
checkout materialises a fraction of what `git ls-files` lists — which now gets a
loud FAIL naming the files. That is the honest answer for such a checkout ("I
cannot judge these"), and CI checks out in full, so the required path is
unaffected.

Usage
-----
    python3 scripts/ci/state-paths-freeze-guard.py
    python3 scripts/ci/state-paths-freeze-guard.py --repo-root DIR

`--repo-root` changes exactly one thing: which checkout is enumerated and read.
Discovery, filtering, both regexes, every failure branch and every exit code
below are the same code on the same path, so a `--repo-root` run is evidence
about the real run. It exists because "an empty index must fail" is only
demonstrable by pointing the real guard at an empty checkout.

Exit 0: every tracked, in-scope file was read and is clean.
Exit 1: an offender was found, or the scan could not honestly cover its subject.
Exit 2: usage error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

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


class ScanRefusal(RuntimeError):
    """The scan could not honestly cover its subject, so it refuses to report.

    One class, five messages — an empty index, an index with nothing in scope, a
    backend/db.py that is not tracked, a read count of zero, and a file that
    could not be read. Callers that need to tell them apart match on the message;
    every one of them is a non-zero exit.
    """


class ScanResult(NamedTuple):
    """What a scan covered, and what it found.

    `files` is what was enumerated and `read` is what was actually opened. They
    are separate fields because collapsing them is precisely the bug this guard
    shipped with: reporting the enumerated total as though it were the total
    read.
    """

    files: list[Path]
    read: list[Path]
    import_offenders: list[str]
    freeze_offenders: list[str]


def _excluded(rel_parts: tuple[str, ...]) -> bool:
    """True when a repo-relative path sits under an out-of-scope directory."""
    return any(part in EXCLUDED_DIR_NAMES for part in rel_parts[:-1])


def _rel(path: Path, repo_root: Path) -> Path | None:
    """path relative to repo_root, or None when it does not sit under it.

    git_tracked_files resolves symlinks, so a tracked symlink pointing outside
    the checkout comes back as a path this guard cannot describe in repo terms.
    That is a refusal like any other rather than an uncaught ValueError — every
    other way this guard stops names a file on a FAIL line.
    """
    try:
        return path.relative_to(repo_root)
    except ValueError:
        return None


def scan_files(repo_root: Path) -> list[Path]:
    """Every tracked, in-scope source file under repo_root, sorted.

    Raises ScanRefusal when the answer is empty — a scan of zero files reports
    zero offenders and would pass forever.
    """
    repo_root = repo_root.resolve()
    tracked = git_tracked_files(repo_root)
    if not tracked:
        raise ScanRefusal(
            f"git ls-files reported an empty index for {repo_root} — refusing to "
            f"report zero offenders from zero files scanned"
        )

    escaped = sorted(str(path) for path in tracked if _rel(path, repo_root) is None)
    if escaped:
        raise ScanRefusal(
            f"{len(escaped)} tracked path(s) resolve outside {repo_root}, so this "
            f"guard cannot say what part of the repo they are: {', '.join(escaped)}"
        )

    files = sorted(
        path
        for path in tracked
        if path.suffix in SCAN_SUFFIXES and not _excluded(path.relative_to(repo_root).parts)
    )
    if not files:
        raise ScanRefusal(
            f"{len(tracked)} tracked file(s) under {repo_root}, none of them an "
            f"in-scope {'/'.join(sorted(SCAN_SUFFIXES))} source file — refusing to "
            f"report zero offenders from zero files scanned"
        )
    return files


def scan(repo_root: Path) -> ScanResult:
    """Read every tracked, in-scope source file and report what is in them.

    Offenders are `path:lineno: text` strings, repo-relative, so a CI log names
    the file a human has to open. Raises ScanRefusal rather than returning a
    partial answer: a file that could not be read is one this guard cannot vouch
    for, and vouching for it anyway is the whole defect class.
    """
    repo_root = repo_root.resolve()
    files = scan_files(repo_root)

    db_py = (repo_root / DB_PY_REL).resolve()
    if db_py not in set(files):
        raise ScanRefusal(
            f"{DB_PY_REL} is not a tracked, in-scope file under {repo_root} — the "
            f"module-level-freeze check for it would scan nothing and pass. If the "
            f"file moved, point DB_PY_REL at its new path"
        )

    read: list[Path] = []
    unreadable: list[str] = []
    import_offenders: list[str] = []
    freeze_offenders: list[str] = []

    for path in files:
        rel = path.relative_to(repo_root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # git tracks it, so it is repo source; this guard could not read it,
            # so it has nothing to say about it. Recorded by name and refused
            # below rather than skipped — a skipped file is an offender this
            # guard would report as clean.
            unreadable.append(f"{rel}: {type(exc).__name__}: {exc}")
            continue
        read.append(path)
        check_freeze = path == db_py
        for lineno, line in enumerate(text.splitlines(), start=1):
            if BANNED_IMPORT_RE.match(line):
                import_offenders.append(f"{rel}:{lineno}: {line.strip()}")
            if check_freeze and DB_PY_FREEZE_RE.match(line):
                freeze_offenders.append(f"{rel}:{lineno}: {line.strip()}")

    if not read:
        raise ScanRefusal(
            f"read 0 of {len(files)} tracked source file(s) under {repo_root} — "
            f"refusing to report zero offenders from zero files read:\n  - "
            + "\n  - ".join(unreadable)
        )
    if unreadable:
        raise ScanRefusal(
            f"{len(unreadable)} of {len(files)} tracked source file(s) under "
            f"{repo_root} could not be read, so this guard cannot vouch for "
            f"them:\n  - " + "\n  - ".join(unreadable)
        )

    return ScanResult(files, read, import_offenders, freeze_offenders)


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
        # ScanRefusal is a RuntimeError, as is the git failure raised out of
        # git_tracked_files. Both mean the same thing here — the scan cannot be
        # trusted — and both report the same way.
        result = scan(repo_root)
    except RuntimeError as exc:
        print(f"{NAME}: FAIL — {exc}", file=sys.stderr)
        return 1

    # Read, not enumerated. These are equal on every path that reaches here,
    # because an unread file is a refusal above — printing both is what makes
    # that checkable from a log instead of taken on trust.
    print(
        f"{NAME}: read {len(result.read)} of {len(result.files)} tracked "
        f"source file(s) in {repo_root}"
    )

    offenders = result.import_offenders + result.freeze_offenders
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
