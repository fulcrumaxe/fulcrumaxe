"""
backend/gate_streak.py — "gate declined to gate" streak counter (D#2271 PR-a).

138 identical CI-disabled stand-down rows sat in the audit trail for two
weeks, each one correct and each one silently uninformative on its own — a
signal that fires identically every time and blocks nothing reads as no
signal at all. This module is the fix: it turns the raw audit trail into a
single number, the count of merges since the CI gate last verified
something for real, so the number itself carries the escalation instead of
a human having to notice a repeated row.

Design (see Discussion #2271 for the full rationale): assert on the presence
of a positive verification signal, never on an enumerated set of named
negatives. `scripts/lib/ci-status-check.sh`'s `check_ci_status()` writes
exactly one positive-marker kind — POSITIVE_MARKER_KIND below — on the one
branch that means a required check-run was actually read and found green.
Every OTHER audit row that carries a `kind` field counts as one unverified
event, with a single named exception (REFUSAL_KIND — see its docstring).
Nothing here enumerates bypass names: a bypass added tomorrow, under any
name, still increments the streak just by not being the positive marker. A
bypass that writes no audit row of its own still increments it too, because
the merge scripts write a fallback marker (`ci_gate_unverified_merge`,
written by `ci_note_merge_if_unverified` in ci-status-check.sh) whenever a
merge proceeds without CI_STATUS_STATE reaching "pass" and without the
caller having already written its own row — that fallback is itself just
another non-positive `kind`, picked up by the same rule, not by name.

CLI:
    python3 backend/gate_streak.py            # prints the bare integer
    python3 backend/gate_streak.py --render   # prints the human-facing line
                                               # (nothing printed at streak=0)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Written by check_ci_status()'s STATUS=pass branch. The ONLY kind this
# module treats specially in the "reset" direction.
POSITIVE_MARKER_KIND = "ci_gate_verified"

# The one explicit exception in the other direction. This row's kind means
# the gate ran and correctly REFUSED the merge — no merge happened, so it is
# not part of "merges since the last verified merge" at all. This is not a
# bypass name; it is not one of the specific pre-existing decline-reason
# kinds this module is required to never reference (see D#2271 Spec AC-7 —
# those are the enumerable, growing surface this design deliberately never
# names). A gate correctly saying no is a closed, single concept, not a list.
REFUSAL_KIND = "ci_gate_block"


def _audit_path() -> Path:
    """Resolve the audit log path.

    Honors the same CI_STATUS_TEST_MODE=1 / CI_STATUS_TEST_AUDIT_FILE test
    seam as scripts/lib/ci-status-check.sh's `_ci_audit_path`, so a single
    fixture file can drive both the shell side (which writes the rows) and
    this reader from one test invocation.
    """
    if os.environ.get("CI_STATUS_TEST_MODE") == "1":
        test_file = os.environ.get("CI_STATUS_TEST_AUDIT_FILE")
        if test_file:
            return Path(test_file)
    from backend import state_paths  # noqa: PLC0415 — see module's pytest guard

    return Path(state_paths.AUDIT_LOG)


def compute_streak(path: Path | str) -> int:
    """Count kind-bearing audit rows since the last positive marker.

    Walks *path* in order. Any row with a non-empty ``kind`` field is one
    of three things:

    - the positive marker: resets the running count to 0
    - the refusal kind: ignored entirely (no merge happened)
    - anything else (a named bypass today, an unnamed one tomorrow, or the
      unconditional fallback marker): +1

    Rows with no ``kind`` field belong to a different schema entirely (the
    generic AuditTrail.emit source/action/key family used by everything
    else in the audit trail) and are skipped — they are not part of this
    class of event.
    """
    path = Path(path)
    streak = 0
    if not path.exists():
        return streak
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if not kind:
                continue
            if kind == POSITIVE_MARKER_KIND:
                streak = 0
            elif kind == REFUSAL_KIND:
                continue
            else:
                streak += 1
    return streak


def current_streak() -> int:
    """Streak computed against the real (or test-seam-redirected) audit log."""
    return compute_streak(_audit_path())


def render_line(streak: int) -> str | None:
    """Human-facing escalation line, or None when there is nothing to say.

    Tiered on the streak itself (D#2271 AC-5): a streak of 1 and a streak of
    47 must read as different messages, not just different digits, or this
    just becomes the next thing that fires identically every time and gets
    ignored the way 138 identical rows did.
    """
    if streak <= 0:
        return None
    if streak == 1:
        return f"  CI GATE STREAK  {streak} merge since the last verified CI pass"
    if streak < 10:
        return f"  CI GATE STREAK  {streak} merges since the last verified CI pass"
    if streak < 25:
        return (
            f"  CI GATE STREAK  WARNING — {streak} merges since the last verified "
            "CI pass — nothing has re-verified CI in a while"
        )
    return (
        f"  CI GATE STREAK  CRITICAL — {streak} merges since the last verified "
        "CI pass — this has gone unverified for a long time"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report the D#2271 'gate declined to gate' merge streak."
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="print the human-facing escalation line instead of the bare integer "
        "(prints nothing at streak=0)",
    )
    args = parser.parse_args()
    streak = current_streak()
    if args.render:
        line = render_line(streak)
        if line:
            print(line)
    else:
        print(streak)


if __name__ == "__main__":
    main()
