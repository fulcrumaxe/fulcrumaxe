#!/usr/bin/env python3
"""registry-status-parity-guard.py — D#2145 PR-b: queue/gate parity + staleness.

Background
----------
`backend/next_work.py` (the queue) and `scripts/lib/spec-ready-gate.sh` (the
spawn gate) used to disagree about a Discussion's status because they read it
through two different parsers: the gate went through
`backend.discussion_status.extract_status_anchored()` (anchored to the body's
first non-empty line), while the queue went through `DiscussionRegistry.
_parse_status()`, which ran its own private, whole-body regex
(`_STATUS_RE = re.compile(r"<!--\\s*STATUS:(\\w+)(?:[^>]*)-->")`). That
regex's `[^>]*` crosses fenced code blocks and paragraph breaks freely — on
D#2145's own pre-Spec body it matched a 1466-character span that started
inside a fenced code block and terminated on a `-->` sitting in an unrelated
prose sentence, misreading the body as SPEC_READY when the authoritative
(first-line) status was unset. `_parse_status()` now delegates to the same
anchored parser the gate uses (see `backend/registry.py`), removing the
second implementation. This guard is the regression test for that class of
bug — a live "the queue and the gate secretly disagree" divergence — and it
also proves the registry.json this reads isn't silently stale, a distinct
cause: D#2453 was stamped `DONE` and closed at 02:18 UTC, and the queue still
reported it `SPEC_READY` at 02:49 — 31 minutes later — because
`DiscussionRegistry.load()` never inspects `synced_at`. Anchoring the parser
does not fix staleness; both are checked here, separately.

What this checks
-----------------
Entirely offline, against a synthetic fixture registry (no GitHub call, no
GH_TOKEN, see Constraints in D#2145's Spec):

  1. Parity — for every OPEN fixture row, the registry's recorded `status`
     must equal what `DiscussionRegistry._parse_status()` (the shared,
     anchored parser at the registry boundary) computes from that row's
     body. The fixture spans every status-producing shape this Discussion's
     Spec calls out by number: a well-formed marker, a body with no marker
     anywhere (must fall back to DISCUSSING, never drop out of the queue —
     acceptance item 9), the fenced-decoy shape reconstructed from D#2145's
     own pre-Spec body (acceptance item 10), and the DONE-over-fenced-decoy
     shape that produced the D#2453 symptom (acceptance item 11). CLOSED
     fixture rows are included too, specifically to prove they are excluded
     from the compared set rather than silently padding it.
  2. Non-vacuity — the number of OPEN rows compared is > 0 and is printed
     (acceptance item 13, first assertion).
  3. Canary — a deliberately mutated fixture row (its recorded `status` set
     to disagree with what the parser would compute from its body) is
     detected as a mismatch. Proves the comparison isn't vacuously reporting
     agreement no matter what it's given (acceptance item 13, second
     assertion).
  4. Staleness — `registry.json`'s `synced_at` age is checked against
     STALENESS_THRESHOLD_SECONDS. A fresh `synced_at` must NOT be reported
     stale; a deliberately old one MUST be (acceptance item 14). This is
     proven against two separate fixture registries — staleness is not a
     side effect of the parity check above, it is checked and printed
     independently of whether the rows parse correctly.

This is a behavioral probe, not a lint over source text: it never reads
backend/registry.py's source, greps a call site, or asserts on a diff. It
builds fixture registries via `DiscussionRegistry(state_dir=<tmpdir>)` (the
class accepts an injected state dir) and calls the real methods.

Run from the repo root:

    python3 scripts/ci/registry-status-parity-guard.py

Exit 0: parity holds, the fixture proves it compared something and can
        detect a real disagreement, and staleness detection works both ways.
Exit 1: a mismatch was found in the clean fixture, the comparison was
        vacuous, the canary went undetected, or staleness detection failed
        either direction.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# D#2453: stamped DONE and closed at 02:18 UTC, still read as SPEC_READY by
# the queue at 02:49 — 31 minutes later. Anything past half an hour is worth
# a loud "stale" rather than a silent "agrees".
STALENESS_THRESHOLD_SECONDS = 1800


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Fixture construction — the ONLY place any body/status pairing is chosen.
# Every shape below is named for the D#2145 acceptance item it exercises.
# ---------------------------------------------------------------------------

def build_fixture_rows() -> list[dict]:
    rows: list[dict] = []
    number = 90101  # implausible as a real Discussion number

    def add(body: str, status: str, *, closed: bool) -> None:
        nonlocal number
        rows.append({
            "number": number,
            "title": f"fixture {status} {'closed' if closed else 'open'} #{number}",
            "status": status,
            "category": "General",
            "created_at": "2026-01-01T00:00:00+00:00",
            "closed_at": "2026-01-02T00:00:00+00:00" if closed else None,
            "pr": None,
            "labels": [],
            "body": body,
        })
        number += 1

    # Well-formed markers — must keep parsing exactly as before (D#2145
    # "Failure conditions": extract_status()'s callers must not change
    # behaviour for well-formed markers).
    add("<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\n\nBody text.\n",
        "SPEC_READY", closed=False)
    add("<!-- STATUS:IMPLEMENTING SINCE:2026-01-01T00:00:00Z -->\n\nBody text.\n",
        "IMPLEMENTING", closed=False)
    add("<!-- STATUS:REVIEWING PR:#321 SINCE:2026-01-01T00:00:00Z -->\n\nBody text.\n",
        "REVIEWING", closed=False)

    # No marker anywhere — must still be DISCUSSING, not UNKNOWN (item 9).
    # This is the 84-open-Discussion trap: a naive anchoring that returned
    # UNKNOWN here would silently empty the queue's actionable set.
    add("Just prose. No STATUS marker anywhere in this body.\n",
        "DISCUSSING", closed=False)

    # Positive control — D#2145's own pre-Spec shape (item 10): no marker on
    # the first non-empty line, and a fenced code block later in the body
    # contains a bare `<!-- STATUS:SPEC_READY` decoy whose terminating `-->`
    # sits in unrelated prose further down — the exact shape that let the
    # old whole-body `_STATUS_RE` span 1466 characters and misread
    # SPEC_READY. Reconstructed verbatim by deleting the line-1 marker from
    # this Discussion's own filed body.
    add(
        "Filed on D#1941's own explicit recommendation. That Discussion excluded\n"
        "this deliberately and asked for it to be filed separately.\n"
        "\n"
        "## The gate\n"
        "\n"
        "```python\n"
        "_STATUS_PATTERN = re.compile(r\"<!--\\s*STATUS:(\\w+)\")\n"
        "```\n"
        "\n"
        "There is no `-->` in that pattern. An unterminated marker opens the gate:\n"
        "\n"
        "```\n"
        "$ printf '<!-- STATUS:SPEC_READY\\n\\nbody text\\n' \\\n"
        "    | python3 backend/discussion_status.py extract-status --stdin\n"
        "SPEC_READY\n"
        "```\n"
        "\n"
        "Whether requiring `-->` is right at all is what this Spec settles. -->\n",
        "DISCUSSING", closed=False,
    )

    # Positive control — the DONE-over-fence shape (item 11): a well-formed,
    # terminated marker on line 1 (DONE) plus a fenced code block later that
    # quotes a different, terminated SPEC_READY marker as documentation.
    # This is the shape that produced the D#2453 symptom, and the shape
    # D#2145's own body will have once it is closed.
    add(
        "<!-- STATUS:DONE SINCE:2026-01-03T00:00:00Z -->\n"
        "\n"
        "This Discussion is closed. For reference, the fail-open marker looked\n"
        "like this:\n"
        "\n"
        "```\n"
        "<!-- STATUS:SPEC_READY -->\n"
        "```\n",
        "DONE", closed=False,
    )

    # Closed rows in a few buckets — included to prove the comparison
    # actually excludes them (a comparison that accidentally counted closed
    # rows would still pass numerically here, but the "compared" count
    # printed below would be wrong, and the exclusion is asserted directly).
    add("<!-- STATUS:DONE SINCE:2026-01-01T00:00:00Z -->\n", "DONE", closed=True)
    add("Just prose, closed.\n", "DISCUSSING", closed=True)

    return rows


def write_registry(state_dir: Path, rows: list[dict], synced_at: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "synced_at": synced_at,
        "discussions": rows,
        "velocity": {},
    }
    (state_dir / "registry.json").write_text(json.dumps(payload), encoding="utf-8")


def compare(reg, rows: list[dict]) -> tuple[int, list[tuple[int, str, str]]]:
    """Compare each OPEN row's recorded `status` against what the shared,
    anchored parser computes from its body. Returns (compared_count,
    mismatches), where each mismatch is (number, recorded, parsed)."""
    compared = 0
    mismatches: list[tuple[int, str, str]] = []
    for row in rows:
        if row.get("closed_at") is not None:
            continue
        compared += 1
        parsed = reg._parse_status(row.get("body", ""))
        recorded = row.get("status")
        if parsed != recorded:
            mismatches.append((row["number"], recorded, parsed))
    return compared, mismatches


def check_staleness(
    synced_at: str, threshold_seconds: int, now: datetime
) -> tuple[bool, float | None]:
    """Return (is_stale, age_seconds). age_seconds is None when synced_at
    could not be parsed — an unparseable timestamp fails closed (stale)."""
    try:
        ts = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return True, None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (now - ts).total_seconds()
    return age > threshold_seconds, age


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from backend.registry import DiscussionRegistry  # noqa: PLC0415

    rows = build_fixture_rows()
    open_rows = [r for r in rows if r.get("closed_at") is None]
    closed_rows = [r for r in rows if r.get("closed_at") is not None]

    # Self-check: the fixture must actually carry closed rows to prove
    # exclusion, and open rows across more than one status to prove this
    # isn't a single-bucket coincidence.
    if not closed_rows:
        print("FAIL fixture-not-discriminating: no closed rows in the fixture "
              "— cannot prove closed rows are excluded from the comparison")
        return 1
    if len({r["status"] for r in open_rows}) < 3:
        print("FAIL fixture-not-discriminating: fewer than 3 distinct open "
              "statuses in the fixture")
        return 1

    now = datetime.now(timezone.utc)
    fresh_synced = _iso(now)

    with tempfile.TemporaryDirectory(prefix="registry-status-parity-guard-") as tmp:
        state_dir = Path(tmp)
        write_registry(state_dir, rows, synced_at=fresh_synced)
        reg = DiscussionRegistry(state_dir=state_dir)
        data = reg.load()
        loaded_rows = data["discussions"]

        # --- (1) + (2): parity and non-vacuity -----------------------------
        compared, mismatches = compare(reg, loaded_rows)
        print(f"compared {compared} open row(s) against the shared anchored parser")
        if compared == 0:
            print("FAIL vacuous-comparison: compared count is 0")
            return 1
        if compared != len(open_rows):
            print(
                f"FAIL closed-row-leak: expected to compare {len(open_rows)} "
                f"open row(s), compared {compared} — closed rows are not "
                "being excluded correctly"
            )
            return 1
        if mismatches:
            print("FAIL clean fixture produced mismatches (expected zero):")
            for num, recorded, parsed in mismatches:
                print(f"  D#{num}: registry status={recorded!r} parser={parsed!r}")
            return 1
        print(f"agreement: zero mismatches across {compared} open row(s)")

        # --- (3): canary — a real disagreement must be caught --------------
        mutated_rows = [dict(r) for r in loaded_rows]
        corrupt_number = None
        for r in mutated_rows:
            if r.get("closed_at") is None:
                corrupt_number = r["number"]
                # Force disagreement regardless of the row's real status.
                r["status"] = "__DELIBERATELY_WRONG__"
                break
        if corrupt_number is None:
            print("FAIL fixture-not-discriminating: no open row available to mutate")
            return 1
        _, mutated_mismatches = compare(reg, mutated_rows)
        mutated_numbers = {n for n, _, _ in mutated_mismatches}
        if corrupt_number not in mutated_numbers:
            print(
                f"FAIL canary-not-detected: mutating D#{corrupt_number}'s "
                "recorded status was not reported as a mismatch — this "
                "fixture cannot discriminate a broken comparison from a "
                "working one"
            )
            return 1
        print(
            f"canary: a deliberately mutated row (D#{corrupt_number}) was "
            "detected as a mismatch"
        )

        # --- (4): staleness, both directions --------------------------------
        is_stale_fresh, age_fresh = check_staleness(
            fresh_synced, STALENESS_THRESHOLD_SECONDS, now
        )
        if is_stale_fresh:
            print(
                f"FAIL fresh synced_at (age={age_fresh}s) was reported STALE "
                "— staleness threshold or clock handling is broken"
            )
            return 1
        print(
            f"staleness: fresh synced_at (age={age_fresh:.0f}s) correctly "
            "reports not-stale"
        )

    # Second fixture, deliberately old synced_at — proves the stale case is
    # actually reachable, not just the not-stale case (item 14).
    old_synced = _iso(now - timedelta(seconds=STALENESS_THRESHOLD_SECONDS + 60))
    with tempfile.TemporaryDirectory(prefix="registry-status-parity-guard-stale-") as tmp2:
        state_dir2 = Path(tmp2)
        write_registry(state_dir2, rows, synced_at=old_synced)
        reg2 = DiscussionRegistry(state_dir=state_dir2)
        data2 = reg2.load()
        is_stale_old, age_old = check_staleness(
            data2["synced_at"], STALENESS_THRESHOLD_SECONDS, now
        )
        if not is_stale_old:
            print(
                f"FAIL deliberately old synced_at (age={age_old}s, threshold="
                f"{STALENESS_THRESHOLD_SECONDS}s) was NOT reported stale"
            )
            return 1
        print(
            f"staleness: deliberately old synced_at (age={age_old:.0f}s) "
            "correctly reports STALE"
        )

    print("registry-status-parity-guard: all clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
