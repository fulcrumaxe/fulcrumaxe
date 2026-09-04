#!/usr/bin/env python3
"""registry-queue-count-guard.py — behavioral guard against queue counts that
ignore `closed_at` (D#2310).

Background
----------
`backend/cli.py:69` and `scripts/loop-preflight.sh`'s registry summary each
used to re-derive queue-state counts (SPEC_READY, DISCUSSING, ...) from every
row in the registry, with no filter on `closed_at`. The result overstated the
live queue by roughly an order of magnitude — "304 DISCUSSING" was really 17
open, "3 IMPLEMENTING" and "3 REVIEWING" were zero open. The correct filter
already existed (`backend/registry.py`'s `stats()`); the bug was that two
other call sites never used it. This guard is the regression test for that
class of bug: it exercises `DiscussionRegistry.queue_summary()` — the one
shared implementation both fixed call sites now read from — against a
fixture registry containing closed rows in every status bucket, then proves
the fixture is load-bearing by defeating the filter and confirming the
answer changes.

This is a behavioral probe, not a lint over source text: it never reads a
source file, greps a call site, or asserts on a diff. It builds a fixture
registry via `DiscussionRegistry(state_dir=<tmpdir>)` (the class accepts an
injected state dir — no network, no `GH_TOKEN`, no live GitHub call) and
calls the real method.

No hardcoded bucket counts: every expectation below is derived from
BUCKET_SPEC, the same construction dict the fixture registry is built from.
Changing BUCKET_SPEC's numbers changes both the fixture and what the guard
expects, together — nothing here embeds a count measured from the live
registry (which is exactly the kind of value D#2310 showed decaying within
a day: 17 open DISCUSSING measured, 16 the next day).

Run from the repo root:

    python3 scripts/ci/registry-queue-count-guard.py

Exit 0: the shared filter is in place and this fixture proves it matters.
Exit 1: the open-only counts are wrong, or defeating the filter produced no
        observable difference (the fixture could not discriminate).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Fixture construction — the ONLY place any count is chosen. Every bucket
# carries at least one closed row (criterion: "a fixture containing closed
# rows in every status bucket") and every bucket but DONE also carries at
# least one open row, so open-only buckets is not vacuously empty anywhere.
# DONE additionally gets one anomalous OPEN row: real DONE Discussions are
# always closed, but `queue_summary()`'s `done` field must count DONE rows
# over ALL rows regardless of open/closed (D#2310 Spec item 4) — an open
# DONE row here proves that field isn't secretly open-filtered too.
# ---------------------------------------------------------------------------
BUCKET_SPEC: dict[str, dict[str, int]] = {
    "DISCUSSING":   {"open": 3, "closed": 7},
    "SPEC_READY":   {"open": 2, "closed": 5},
    "IMPLEMENTING": {"open": 1, "closed": 4},
    "REVIEWING":    {"open": 1, "closed": 4},
    "DONE":         {"open": 1, "closed": 5},
    "NEW":          {"open": 4, "closed": 2},
    "BLOCKED":      {"open": 1, "closed": 1},
    "DRAFT":        {"open": 1, "closed": 2},
    "IDEA_RAW":     {"open": 1, "closed": 1},
}


def build_fixture_discussions() -> list[dict]:
    """Materialize BUCKET_SPEC into registry-shaped discussion rows."""
    discussions: list[dict] = []
    number = 90001  # implausible as a real Discussion number
    for status, counts in BUCKET_SPEC.items():
        for _ in range(counts["open"]):
            discussions.append({
                "number": number,
                "title": f"fixture {status} open #{number}",
                "status": status,
                "category": "General",
                "created_at": "2026-01-01T00:00:00+00:00",
                "closed_at": None,
                "pr": None,
                "labels": [],
            })
            number += 1
        for _ in range(counts["closed"]):
            discussions.append({
                "number": number,
                "title": f"fixture {status} closed #{number}",
                "status": status,
                "category": "General",
                "created_at": "2026-01-01T00:00:00+00:00",
                "closed_at": "2026-01-02T00:00:00+00:00",
                "pr": None,
                "labels": [],
            })
            number += 1
    return discussions


def expected_from_spec() -> dict:
    """Derive every expectation mechanically from BUCKET_SPEC — never a
    typed-in literal. This is what makes the guard immune to the exact decay
    D#2310 measured (17 open DISCUSSING -> 16 one day later): nothing here
    is a snapshot of live registry state.
    """
    total = sum(c["open"] + c["closed"] for c in BUCKET_SPEC.values())
    open_total = sum(c["open"] for c in BUCKET_SPEC.values())
    buckets = {status: c["open"] for status, c in BUCKET_SPEC.items() if c["open"] > 0}
    done = BUCKET_SPEC["DONE"]["open"] + BUCKET_SPEC["DONE"]["closed"]
    return {
        "total": total,
        "open_total": open_total,
        "excluded_closed": total - open_total,
        "buckets": buckets,
        "done": done,
    }


def write_fixture_registry(state_dir: Path, discussions: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "synced_at": "2026-01-03T00:00:00+00:00",
        "discussions": discussions,
        "velocity": {},
    }
    (state_dir / "registry.json").write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from backend.registry import DiscussionRegistry  # noqa: PLC0415

    # Self-check: guard against someone weakening BUCKET_SPEC into a fixture
    # that can no longer discriminate (criterion: "a fixture of only open
    # rows... must not be able to report success"). Fail loudly here rather
    # than silently passing a vacuous check below.
    empty_closed = [s for s, c in BUCKET_SPEC.items() if c["closed"] == 0]
    if empty_closed:
        print(
            "FAIL fixture-not-discriminating: BUCKET_SPEC has zero closed "
            f"rows for {empty_closed} — every bucket must carry at least "
            "one closed row or this guard cannot prove the filter matters"
        )
        return 1

    discussions = build_fixture_discussions()
    expected = expected_from_spec()

    with tempfile.TemporaryDirectory(prefix="registry-queue-count-guard-") as tmp:
        state_dir = Path(tmp)
        write_fixture_registry(state_dir, discussions)
        reg = DiscussionRegistry(state_dir=state_dir)

        # --- Assertion (a): the real, un-defeated implementation reports
        # open-only counts matching what the fixture was built with. -------
        good = reg.queue_summary()
        failures: list[str] = []

        for field in ("total", "open_total", "excluded_closed", "done"):
            if good.get(field) != expected[field]:
                failures.append(
                    f"{field}: expected {expected[field]!r} (derived from "
                    f"BUCKET_SPEC), got {good.get(field)!r}"
                )

        if good.get("buckets") != expected["buckets"]:
            failures.append(
                f"buckets: expected {expected['buckets']!r} (open-only, "
                f"derived from BUCKET_SPEC), got {good.get('buckets')!r}"
            )

        if failures:
            print("FAIL queue_summary() did not match the fixture's open-only construction:")
            for f in failures:
                print(f"  - {f}")
            return 1

        print(
            f"queue_summary(): total={good['total']} open_total={good['open_total']} "
            f"excluded_closed={good['excluded_closed']} done={good['done']}"
        )
        print(f"  open-only buckets: {good['buckets']}")

        # --- Assertion (b): the canary — same computation with the shared
        # filter defeated — produces a DIFFERENT answer. If it doesn't, this
        # fixture could not have caught a regression and must fail loudly
        # rather than report success (D#1984 trap, per the Spec). ----------
        orig_open_only = DiscussionRegistry._open_only
        try:
            DiscussionRegistry._open_only = staticmethod(lambda rows: rows)  # no-op: count everything
            defeated = reg.queue_summary()
        finally:
            DiscussionRegistry._open_only = orig_open_only

        if defeated["buckets"] == good["buckets"] and defeated["excluded_closed"] == good["excluded_closed"]:
            print(
                "FAIL canary-not-detected: defeating DiscussionRegistry._open_only "
                "produced an identical queue_summary() result — this fixture cannot "
                "discriminate a broken filter from a working one"
            )
            return 1

        print(
            "canary: defeating the open-filter changed the result as expected "
            f"(buckets went from {good['buckets']} to {defeated['buckets']}, "
            f"excluded_closed from {good['excluded_closed']} to {defeated['excluded_closed']})"
        )

    print("registry-queue-count-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
