#!/usr/bin/env python3
"""scripts/ci/gate1-routing-ledger-check.py — reconciles the Gate 1 unrouted-
path ledger, in both directions (D#2566 PR-2).

Modelled on scripts/ci/guard-registry-check.py's both-directions shape, but
sized to what this ledger actually needs. The ledger's own entries are
checked for rot in one direction; a second, opt-in direction lets a
specific, deliberately-chosen set of paths be scanned for the opposite
problem (something that should be listed and isn't) without ever growing
that scope on its own. See CANDIDATE SCAN below for why that direction is
opt-in rather than a scan of the whole tree.

LEDGER-ROT CHECKS (always run, against the ledger's own current entries):
  (b) every entry has a non-empty, reviewer-checkable reason
  (c) every entry names a path that still exists in this tree
  (d) every entry names a path that would STILL route to no suite --
      checked against the two hand-listed registries run-pr-tests.sh
      claims scripts/*.sh paths through (D#2566 security review); an
      entry claimed by either registry now routes to a real suite and
      must be removed, not left rotting there
  (e) no entry names a `.py` path -- `.py` modules are never eligible for
      this ledger at all (scripts/lib/gate1-receipt-check.sh enforces the
      same rule at authorization time; a defect statement ("no suite
      covers this") is not a justification for a module that could be
      part of the trust boundary -- D#2566 security review, D#2577)

CANDIDATE SCAN (direction "a" — something that should be ledgered isn't)
--------------------------------------------------------------------------
Deliberately NOT a scan of every scripts/*.sh / scripts/lib/*.sh file in
the tree by default: this repo carries ~190 of them today, and scanning
all of them would force this ledger to carry ~180+ entries on day one,
most of which SHOULD be rejected outright rather than exempted — exactly
the mistake item 17 (a PR touching only scripts/lib/two-gate-check.sh)
exists to catch. A clean tree today has ZERO ledger entries, on purpose:
nothing in this repo has been reviewed and deliberately accepted as
"untested and that's fine" yet, and this script must not manufacture
pressure to add such an entry just to keep itself green.

Set GATE1_LEDGER_CANDIDATE_GLOBS (colon-separated, fnmatch-style, relative
to the repo root) to scan a specific, deliberately-chosen set of paths for
this direction — unset by default, so a normal run only checks the
ledger's own entries above. This is how item 19(a) gets demonstrated:
point it at a known-unledgered path (e.g. scripts/lib/two-gate-check.sh,
item 17's own example — it must stay unledgered, so pointing here at it is
exactly the right fixture) and watch this exit non-zero, per D#1984 — not
by scanning the whole tree in production.

Overrides (test isolation, same convention as gate1-verify-containment.sh's
GATE1_VERIFY_STATE_DIR/GATE1_VERIFY_CHECKOUT_DIR):
  GATE1_LEDGER_REPO_ROOT   defaults to this file's own repo root
  GATE1_LEDGER_PATH        defaults to scripts/ci/gate1-routing-ledger.json
                           under the resolved repo root

Usage: python3 scripts/ci/gate1-routing-ledger-check.py
Exit 0 = clean. Exit 1 = at least one problem, all printed to stderr.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]

# The two hand-listed registries run-pr-tests.sh claims scripts/*.sh paths
# through (D#2566 security review finding) -- kept here as a literal,
# reviewer-checkable list rather than parsed out of run-pr-tests.sh, so this
# check has no runtime dependency on that file's own case statement. Re-
# verify against `git show code-plane/main:scripts/run-pr-tests.sh` if
# either registry there ever changes.
CLAIMED_BY_A_REGISTRY = {
    "scripts/triage-orphan-diffs.sh",
    "scripts/reap-worktrees.sh",
    "scripts/lib/orphan-triage.sh",
    "scripts/lib/worktree-registry.sh",
    "scripts/post-merge-hook.sh",
    "scripts/lib/auto-pull-step.sh",
    "scripts/lib/auto-pull-recover.sh",
}


def _repo_root() -> Path:
    override = os.environ.get("GATE1_LEDGER_REPO_ROOT")
    return Path(override).resolve() if override else _DEFAULT_REPO_ROOT


def _ledger_path(repo_root: Path) -> Path:
    override = os.environ.get("GATE1_LEDGER_PATH")
    return Path(override).resolve() if override else repo_root / "scripts" / "ci" / "gate1-routing-ledger.json"


def load_ledger(ledger_path: Path) -> dict[str, str]:
    if not ledger_path.exists():
        return {}
    with open(ledger_path) as f:
        data = json.load(f)
    ledger = data.get("ledger", {})
    return ledger if isinstance(ledger, dict) else {}


def candidate_paths(repo_root: Path) -> list[str]:
    globs_env = os.environ.get("GATE1_LEDGER_CANDIDATE_GLOBS", "")
    if not globs_env:
        return []
    globs = [g for g in globs_env.split(":") if g]
    out: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root).as_posix()
        if any(fnmatch.fnmatch(rel, g) for g in globs):
            out.append(rel)
    return sorted(out)


def main() -> int:
    repo_root = _repo_root()
    ledger_path = _ledger_path(repo_root)
    ledger = load_ledger(ledger_path)

    problems: list[str] = []

    for path, reason in ledger.items():
        if not str(reason).strip():
            problems.append(f"empty reason: {path}")
            continue
        if path.endswith(".py"):
            problems.append(f".py path is never eligible for this ledger: {path}")
            continue
        if not (repo_root / path).is_file():
            problems.append(f"path no longer exists: {path}")
            continue
        if path in CLAIMED_BY_A_REGISTRY:
            problems.append(f"path now claimed by a hand-listed registry (routes to a real suite): {path}")

    candidates = candidate_paths(repo_root)
    for path in candidates:
        if path in CLAIMED_BY_A_REGISTRY:
            continue
        if path not in ledger:
            problems.append(f"unrouted and unledgered: {path}")

    if problems:
        for p in problems:
            print(f"gate1-routing-ledger-check: {p}", file=sys.stderr)
        return 1

    scanned = f", scanned {len(candidates)} candidate(s)" if os.environ.get("GATE1_LEDGER_CANDIDATE_GLOBS") else ""
    print(f"gate1-routing-ledger-check: clean ({len(ledger)} ledger entries{scanned})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
