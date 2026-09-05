#!/usr/bin/env python3
"""rpc-scope-registry-guard.py — the registry must not contradict itself.

Why this exists (D#2327 PR-a)
------------------------------
`backend/rpc_project_scope.py` carries, for every RPC method, a
classification a machine reads (`SCOPED` / `GLOBAL` / `UNSCOPABLE`) and a
justification a human reads. On `main` at dc5868b2 those two disagreed for
`stats.loop_idle_ratio`: the entry was classified `SCOPED` while its own
reason said "it is NOT wrapped ... bound to the serving checkout, not the
requested project". Both fields were written deliberately, six days apart,
and nothing noticed. A classification that says SCOPED because someone typed
SCOPED, next to prose saying the opposite, is worse than no classification —
it is the field `dispatch_scoped()` and `all_classifications()` trust.

Nineteen entries had also come to share one justification string ("already
wrapped in `_with_project_stats_db()`") that nobody had checked against what
those handlers read. Prose cannot be checked. So the audit's answer also
lives in `_DATA_SOURCES`, a machine-readable per-method data source, and
this guard asserts the two stay consistent.

What it checks
--------------
1. No `SCOPED` entry whose reason text denies being wrapped or scoped
   (the D#2330 contradiction, in the exact shape it took).
2. No `SCOPED` entry whose audited data source is the serving checkout —
   that combination is a lie by construction.
3. Every remaining "already wrapped in `_with_project_stats_db()`" claim
   belongs to a method the audit verified actually reads STATS_DB.
4. `_DATA_SOURCES` keys are real classified methods carrying a known
   data-source value — a typo'd key would silently vouch for nothing.

A live canary runs first: the same checks are pointed at a synthetic
registry carrying each defect, and the guard exits 1 if it fails to catch
them. A check that cannot catch its own planted bug is worse than no check,
because it reports "all clear".

This is a static consistency check over the registry, deliberately. The
behavioral question — does a method actually answer two projects
differently — is `rpc-scope-cache-guard.py`'s (cache leakage) and D#2327
PR-b's (uniform project-blindness).

Run from the repo root:

    python3 scripts/ci/rpc-scope-registry-guard.py

Exit 0: the registry is internally consistent and the canary was caught.
Exit 1: a contradiction, an unverified shared claim, or an inert canary.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Phrases that assert a method is NOT scoped. A reason containing any of
# these alongside a SCOPED classification is the D#2330 contradiction.
# Matched case-insensitively against the reason text.
NEGATION_PHRASES = (
    "not wrapped",
    "not scoped",
    "serving checkout",
    "isn't wrapped",
    "is not reached",
    "cannot be scoped",
)

SHARED_WRAPPER_CLAIM = "already wrapped in _with_project_stats_db()"


def check_registry(classifications, data_sources, scoped, reached_sources, known_sources):
    """Return a list of failure strings for one (classifications, data_sources) pair.

    Pure over its arguments so the canary can run the identical logic
    against a synthetic registry — the check the canary proves is the check
    that runs on the real one, not a lookalike.
    """
    failures: list[str] = []

    for method in sorted(classifications):
        kind, reason = classifications[method]
        source = data_sources.get(method)
        lowered = reason.lower()

        # 1. Classification contradicts its own justification text.
        if kind == scoped:
            hits = [p for p in NEGATION_PHRASES if p in lowered]
            if hits:
                failures.append(
                    f"self-contradicting-entry: {method} is classified "
                    f"{scoped} but its reason says {hits!r} — the field a "
                    "machine reads and the field a human reads disagree. "
                    "Fix the scoping so the classification is true, or "
                    "change the classification"
                )

        # 2. SCOPED over a data source nothing per-request can reach.
        if kind == scoped and source is not None and source not in reached_sources:
            failures.append(
                f"scoped-over-unreachable-source: {method} is classified "
                f"{scoped} but its audited data source is {source!r}, which "
                "no per-request override reaches"
            )

        # 3. The shared wrapper claim, only where the audit verified it.
        if SHARED_WRAPPER_CLAIM in reason and source != "stats_db":
            failures.append(
                f"unverified-wrapper-claim: {method} still cites "
                f"{SHARED_WRAPPER_CLAIM!r} but its audited data source is "
                f"{source!r}, not 'stats_db' — being wrapped only scopes a "
                "handler whose reads bottom out in STATS_DB"
            )

    # 4. Ledger integrity.
    for method, source in sorted(data_sources.items()):
        if method not in classifications:
            failures.append(
                f"orphan-data-source: {method!r} has a _DATA_SOURCES entry "
                "but no classification — a typo'd key vouches for nothing"
            )
        if source not in known_sources:
            failures.append(
                f"unknown-data-source: {method} declares data source "
                f"{source!r}, which is not one of {sorted(known_sources)}"
            )

    return failures


def run_canary(scoped, reached_sources, known_sources) -> list[str]:
    """Point the real check at a registry carrying each defect on purpose.

    Returns a list of failures describing which planted defects went
    undetected. Empty means the check is live.
    """
    synthetic_classifications = {
        "__canary.contradiction": (
            scoped,
            "already wrapped in _with_project_stats_db() -- except it is "
            "NOT wrapped and reads the serving checkout's own file",
        ),
        "__canary.unreachable": (scoped, "reads a module constant"),
        "__canary.unverified_claim": (
            scoped,
            "already wrapped in _with_project_stats_db()",
        ),
    }
    synthetic_sources = {
        "__canary.contradiction": "stats_db",
        "__canary.unreachable": "serving_checkout",
        "__canary.unverified_claim": "project_repo",
        "__canary.orphan": "stats_db",
        "__canary.unreachable_typo": "not_a_real_source",
    }
    # The orphan/unknown pair needs a classification-less key to trip on.
    synthetic_classifications_for_orphan = dict(synthetic_classifications)

    found = check_registry(
        synthetic_classifications_for_orphan,
        synthetic_sources,
        scoped,
        reached_sources,
        known_sources,
    )
    joined = " | ".join(found)

    undetected = []
    for tag in (
        "self-contradicting-entry",
        "scoped-over-unreachable-source",
        "unverified-wrapper-claim",
        "orphan-data-source",
        "unknown-data-source",
    ):
        if tag not in joined:
            undetected.append(tag)
    return undetected


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))

    import backend.rpc_project_scope as rps

    classifications = rps.all_classifications()
    if len(classifications) < rps.MIN_REGISTRY_SIZE:
        print(
            f"FAIL registry-too-small: all_classifications() returned "
            f"{len(classifications)} entries, expected >= "
            f"{rps.MIN_REGISTRY_SIZE} — an import failure or empty "
            "enumeration would otherwise pass as 'everything consistent'"
        )
        return 1

    scoped = rps.SCOPED
    reached = rps.DATA_SOURCES_REACHED_BY_PROJECT
    known = set(reached) | {rps.DS_SERVING_CHECKOUT}

    # --- Live canary first, before a single real entry is judged. --------
    undetected = run_canary(scoped, reached, known)
    if undetected:
        print(
            "FAIL canary-not-detected — this guard has gone inert; planted "
            f"defects it failed to catch: {undetected}"
        )
        return 1
    print("canary: all five planted registry defects detected as expected")

    data_sources = rps.all_data_sources()
    failures = check_registry(classifications, data_sources, scoped, reached, known)

    audited = len(data_sources)
    shared_claims = sum(
        1 for _k, (_kind, reason) in classifications.items()
        if SHARED_WRAPPER_CLAIM in reason
    )
    print(
        f"rpc-scope-registry-guard: {len(classifications)} classified "
        f"methods, {audited} with an audited data source, {shared_claims} "
        f"still citing {SHARED_WRAPPER_CLAIM!r} (all verified as stats_db "
        f"reads), {len(failures)} failing"
    )

    if failures:
        print()
        for f in failures:
            print(f"FAIL {f}")
        return 1

    print("rpc-scope-registry-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
