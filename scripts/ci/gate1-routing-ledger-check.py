#!/usr/bin/env python3
"""scripts/ci/gate1-routing-ledger-check.py — reconciles the Gate 1 unrouted-
path ledger, in both directions (D#2566 PR-2, predicate extended D#370 R5).

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
      checked against is_claimed_by_router(), which mirrors the router's own
      decision order (R3 guards arm, the four hand-listed registries with
      run-pr-tests.sh's R0 existence gate, R1 self-route, R2 stem mapping);
      an entry the router would claim now routes to a real suite and must be
      removed, not left rotting there
  (e) no entry names a `.py` path -- `.py` modules are never eligible for
      this ledger at all (scripts/lib/gate1-receipt-check.sh enforces the
      same rule at authorization time; a defect statement ("no suite
      covers this") is not a justification for a module that could be
      part of the trust boundary -- D#2566 security review, D#2577)

PREFIX ENTRIES (D#370 R8): a key ending in `/**` covers the whole subtree
beneath that prefix (the receipt check honors the same shape). The four
checks above apply subtree-shaped: the prefix dir must exist (c), no `.py`
file anywhere under it may be covered -- a `**.py`-matching prefix is
rejected outright (e) -- and no covered file may have become
router-claimed (d). Seeded with `apps/roku/**` only: BrightScript has no
automatable suite, so on-device/brs verification stays manual.

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
       python3 scripts/ci/gate1-routing-ledger-check.py --is-claimed <path>
Exit 0 = clean. Exit 1 = at least one problem, all printed to stderr.
--is-claimed is a query for the R5 parity test (not a check): it prints
"claimed: <path>" or "unclaimed: <path>" and always exits 0.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]

# The four hand-listed registries run-pr-tests.sh claims scripts-family paths
# through, plus each registry's suite files — kept here as literals,
# reviewer-checkable, with no runtime dependency on run-pr-tests.sh's own
# case statements (same rationale as the D#2566 original, which covered only
# the first two registries below). Re-verify against the router if any arm
# there ever changes; the R5 parity test in
# scripts/test-run-pr-tests-shell-routing.sh asserts this file and the router
# agree on a fixture set.
#   _REGISTRY_PATHS: changed paths each registry's case block matches.
#   _REGISTRY_SUITES: the suite files each registry's run block guards on —
#     a registry counts as CLAIMED only when at least one exists in the tree
#     (run-pr-tests.sh's R0 fail-closed gate, D#370).
_REGISTRY_PATHS = {
    "orphan-triage": frozenset({
        "scripts/triage-orphan-diffs.sh",
        "scripts/reap-worktrees.sh",
        "scripts/lib/orphan-triage.sh",
        "scripts/lib/worktree-registry.sh",
        "tests/test_triage_orphan_diffs.sh",
    }),
    "post-merge-hook": frozenset({
        "scripts/post-merge-hook.sh",
        "scripts/lib/auto-pull-step.sh",
        "scripts/lib/auto-pull-recover.sh",
        "tests/test_post_merge_hook_pull.sh",
        "tests/test_post_merge_hook_wiring.sh",
        "tests/test_no_heredoc_hook_copies.sh",
        "tests/test_auto_pull_recover.sh",
        "tests/test_post_merge_hook_unmerged_paths.sh",
        "tests/test_post_merge_hook_ac_rate.sh",
    }),
    "pr-plane": frozenset({
        "scripts/lib/pr-plane.sh",
        "scripts/lib/pr-tree.sh",
        "scripts/merge-and-hook.sh",
        "scripts/post-merge-hook.sh",
        "scripts/lib/auto-pull-step.sh",
        "scripts/spawn-agent.sh",
        "scripts/check-pr-dashboard-touched.sh",
        "tests/test_pr_plane.sh",
        "tests/test_pr_tree_provisioning.sh",
        "tests/test_merge_and_hook.sh",
        "tests/test_post_merge_hook_browser_queue.sh",
    }),
    "pr-amend-e2e": frozenset({
        "backend/prompt_builder.py",
        "backend/spawn_payload.py",
        "scripts/spawn-agent.sh",
        "tests/test_spawn_agent_pr_amend_e2e.sh",
    }),
}
_REGISTRY_SUITES = {
    "orphan-triage": (
        "tests/test_triage_orphan_diffs.sh",
        "tests/test_worktree_self_exclusion.sh",
    ),
    # Single-guard registry: run-pr-tests.sh's run block executes only
    # when test_post_merge_hook_pull.sh exists, so liveness is that one
    # file -- a sibling suite without it routes null (R0, D#370 fix round).
    "post-merge-hook": ("tests/test_post_merge_hook_pull.sh",),
    "pr-plane": (
        "tests/test_pr_plane.sh",
        "tests/test_pr_tree_provisioning.sh",
        "tests/test_merge_and_hook.sh",
        "tests/test_post_merge_hook_browser_queue.sh",
    ),
    "pr-amend-e2e": ("tests/test_spawn_agent_pr_amend_e2e.sh",),
}
# Router case-block order: the first registry matching a path sets its label
# (later blocks only fill still-empty labels), and R0 gates THAT label.
_REGISTRY_ORDER = ("orphan-triage", "post-merge-hook", "pr-plane", "pr-amend-e2e")


def _registry_label_for(path: str) -> str | None:
    for label in _REGISTRY_ORDER:
        if path in _REGISTRY_PATHS[label]:
            return label
    return None


def _registry_has_suite(label: str, repo_root: Path) -> bool:
    return any((repo_root / suite).is_file() for suite in _REGISTRY_SUITES[label])


def _is_self_routed_bash_suite(path: str) -> bool:
    """R1 mirror: tests/*.sh and scripts/test-*.sh, top level only."""
    if path.startswith("tests/"):
        rest = path[len("tests/"):]
        return "/" not in rest and rest.endswith(".sh")
    if path.startswith("scripts/"):
        rest = path[len("scripts/"):]
        return "/" not in rest and rest.startswith("test-") and rest.endswith(".sh")
    return False


def _stem_suites(path: str, repo_root: Path) -> list[str]:
    """R2 mirror of gate1_stem_suites: suite relpaths or []."""
    parent = str(Path(path).parent)
    if parent not in ("scripts", "scripts/lib"):
        return []
    base = Path(path).name
    if base.startswith("test-") and base.endswith(".sh"):
        return []
    if not (base.endswith(".sh") or base.endswith(".py")):
        return []
    stem = base.rsplit(".", 1)[0]
    if not stem:
        return []
    found = []
    exact = repo_root / "scripts" / f"test-{stem}.sh"
    if exact.is_file():
        found.append(f"scripts/test-{stem}.sh")
    found.extend(
        f"scripts/{p.name}"
        for p in sorted((repo_root / "scripts").glob(f"test-{stem}-*.sh"))
        if p.is_file()
    )
    return found


def is_claimed_by_router(path: str, repo_root: Path) -> bool:
    """True when run-pr-tests.sh would route `path` to a non-null suite.

    Mirrors the router's decision order: R3 guards arm, registry claim with
    the R0 existence gate, R1 self-route override, R2 stem fill. Naming
    convention + suite existence in the tree, read from `repo_root`.
    Deliberately scripts-family only: product arms (pytest, dashboard, tui,
    …) are outside the ledger's domain — check (c) existence plus check (e)
    .py-ineligibility already dispose of those entries in this repo, and
    candidate scans target scripts-family globs. The router's
    BASH_SUITE_DENYLIST is likewise not modelled (no scripts/test-*.sh
    entry exists); the parity fixture set stays clear of both gaps.
    """
    if path.startswith("scripts/ci/") and (repo_root / "scripts" / "ci" / "run-guards.sh").is_file():
        return True
    label = _registry_label_for(path)
    if label is not None and _registry_has_suite(label, repo_root):
        return True
    if _is_self_routed_bash_suite(path):
        return True
    if _stem_suites(path, repo_root):
        return True
    return False


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


def _ledger_covers(path: str, ledger: dict[str, str]) -> bool:
    """True when an exact or `/**` prefix entry with a non-empty reason covers `path`.

    Mirrors _ledger_reason in scripts/lib/gate1-receipt-check.sh: exact key
    first, else any subtree-prefix key. Used by the candidate scan so a
    ledgered subtree is not flagged as unledgered file by file.
    """
    reason = ledger.get(path)
    if reason and str(reason).strip():
        return True
    for key, val in ledger.items():
        if not key.endswith("/**") or not val or not str(val).strip():
            continue
        prefix = key[: -len("/**")]
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def main() -> int:
    repo_root = _repo_root()
    ledger_path = _ledger_path(repo_root)
    ledger = load_ledger(ledger_path)

    problems: list[str] = []

    for path, reason in ledger.items():
        if not str(reason).strip():
            problems.append(f"empty reason: {path}")
            continue
        if path.endswith("/**"):
            # R8 (D#370 PR2): subtree-prefix entry -- same four checks as
            # exact paths, subtree-shaped.
            prefix_dir = repo_root / path[: -len("/**")]
            if not prefix_dir.is_dir():
                problems.append(f"prefix entry names no directory in this tree: {path}")
                continue
            py_hits = sorted(p for p in prefix_dir.rglob("*.py") if p.is_file())
            if py_hits:
                problems.append(
                    f"prefix entry matches .py path(s), never eligible: {path} "
                    f"(e.g. {py_hits[0].relative_to(repo_root).as_posix()})"
                )
                continue
            claimed = sorted(
                p.relative_to(repo_root).as_posix()
                for p in prefix_dir.rglob("*")
                if p.is_file()
                and is_claimed_by_router(p.relative_to(repo_root).as_posix(), repo_root)
            )
            if claimed:
                problems.append(
                    f"prefix entry covers router-claimed path(s): {path} (e.g. {claimed[0]})"
                )
            continue
        if path.endswith(".py"):
            problems.append(f".py path is never eligible for this ledger: {path}")
            continue
        if not (repo_root / path).is_file():
            problems.append(f"path no longer exists: {path}")
            continue
        if is_claimed_by_router(path, repo_root):
            problems.append(f"path now claimed by the router (routes to a real suite): {path}")

    candidates = candidate_paths(repo_root)
    for path in candidates:
        if is_claimed_by_router(path, repo_root):
            continue
        if not _ledger_covers(path, ledger):
            problems.append(f"unrouted and unledgered: {path}")

    if problems:
        for p in problems:
            print(f"gate1-routing-ledger-check: {p}", file=sys.stderr)
        return 1

    scanned = f", scanned {len(candidates)} candidate(s)" if os.environ.get("GATE1_LEDGER_CANDIDATE_GLOBS") else ""
    print(f"gate1-routing-ledger-check: clean ({len(ledger)} ledger entries{scanned})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--is-claimed":
        target = sys.argv[2]
        print(f"{'claimed' if is_claimed_by_router(target, _repo_root()) else 'unclaimed'}: {target}")
        sys.exit(0)
    if len(sys.argv) != 1:
        print(f"usage: {sys.argv[0]} [--is-claimed <path>]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
