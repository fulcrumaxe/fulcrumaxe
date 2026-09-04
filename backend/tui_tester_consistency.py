"""
tui_tester_consistency.py — cross-screen consistency invariants for the TUI tester.

Single-screen validators (D#855 Sub-PR 1) catch issues within one screen but
miss disagreements between screens.  For example, Loop Health may say "ok" while
Loop Controller shows "[stale]" — the two screens are telling the user different
things about the same underlying fact.

This module defines Invariant objects that capture such cross-screen expectations,
and check_all() runs them after a Pilot sweep, returning any mismatches as Violation
objects that the caller can merge into findings.json.

Five invariants are registered by default:
  loop_staleness_agreement   — Loop Health status vs Loop Controller [stale] marker
  stuck_count_agreement      — Agent Feed stuck>15min count vs Runs page Stuck Runs row count
  budget_agreement           — Home Weekly budget % vs Loop Controller Budget spent/ceiling
  open_pr_count_agreement    — Home 'Open PRs: N' vs PRs screen 'N all' counter
  last_run_agreement         — Home 'Last loop run' timestamp vs Loop Health most-recent row ts

Usage:
    from backend.tui_tester_consistency import check_all

    violations = check_all(pilot_screens)  # pilot_screens: dict[screen_name, str]
    # Each Violation is a dict compatible with the findings.json shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


@dataclass
class Invariant:
    """A cross-screen consistency expectation.

    name            — unique identifier, used in Violation.check_name
    screen_a_reader — extracts a comparable value from screen A text
    screen_b_reader — extracts a comparable value from screen B text
    comparator      — returns True when both values agree (no violation)
    """

    name: str
    screen_a_reader: Callable[[str], Optional[str]]
    screen_b_reader: Callable[[str], Optional[str]]
    comparator: Callable[[Optional[str], Optional[str]], bool]


@dataclass
class Violation:
    """A single cross-screen disagreement found by check_all()."""

    invariant_name: str
    screen_a_name: str
    screen_b_name: str
    screen_a_value: Optional[str]
    screen_b_value: Optional[str]
    detail: str

    def to_finding(self) -> dict:
        """Return a dict that fits the standard findings.json shape."""
        return {
            "tab": f"{self.screen_a_name}+{self.screen_b_name}",
            "widget_id": self.invariant_name,
            "check_name": "cross_screen_consistency",
            "status": "fail",
            "evidence_path": None,
            "detail": self.detail,
            "issue_type": "cross_screen_disagreement",
        }


# ---------------------------------------------------------------------------
# Reader helpers
# ---------------------------------------------------------------------------


def _read_loop_health_status(text: str) -> Optional[str]:
    """Return 'stale' if the Loop Health screen text contains a stale indicator,
    'ok' if it looks healthy, None if the text is absent/unparseable.

    Loop Health shows a table of loop runs.  The screen-level status row
    typically contains text like 'Status: ok' or 'Status: stale'.
    We also treat any occurrence of '[stale]' anywhere as stale.
    """
    if not text:
        return None
    lower = text.lower()
    if "[stale]" in lower or "status: stale" in lower or "stale" in lower:
        return "stale"
    if "status: ok" in lower or "ok" in lower:
        return "ok"
    return None


def _read_loop_controller_stale(text: str) -> Optional[str]:
    """Return 'stale' if the Loop Controller screen text contains '[stale]',
    'ok' otherwise (absent = ok for this screen).
    """
    if not text:
        return None
    if "[stale]" in text.lower():
        return "stale"
    return "ok"


def _read_agent_feed_stuck_count(text: str) -> Optional[str]:
    """Extract the stuck>15min count from Agent Feed status text.

    Expects text like: "running: 3 | stuck>15min: 2 | failed last hour: 1"
    Returns the count as a string, or None if not parseable.
    """
    if not text:
        return None
    m = re.search(r"stuck>15min:\s*(\d+)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _read_runs_stuck_count(text: str) -> Optional[str]:
    """Extract the Stuck Runs count from the Runs screen text.

    Expects a row like: "Stuck Runs  |  2" or "Stuck Runs: 2"
    Returns the count as a string, or None if not parseable.
    """
    if not text:
        return None
    m = re.search(r"Stuck\s+Runs[:\s|]+(\d+)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _read_home_budget_percent(text: str) -> Optional[str]:
    """Extract the weekly budget percentage from the Home screen.

    Looks for patterns like "Weekly budget: 42%" or "budget: 42.3%".
    Returns the numeric string (e.g. "42"), or None if not parseable.
    """
    if not text:
        return None
    m = re.search(r"budget[^:\n]*:\s*([\d.]+)\s*%", text, re.IGNORECASE)
    return m.group(1) if m else None


def _read_loop_controller_budget(text: str) -> Optional[str]:
    """Extract the budget percentage from the Loop Controller screen.

    Looks for patterns like "Budget: $42.10 / $100.00" and computes a
    rough percentage, or patterns like "Budget: 42%" directly.
    Returns the integer percentage as a string, or None.
    """
    if not text:
        return None
    # Direct percentage form
    m = re.search(r"Budget[^:\n]*:\s*([\d.]+)\s*%", text, re.IGNORECASE)
    if m:
        return str(int(float(m.group(1))))
    # Spent/ceiling form: "Budget: $42.10 / $100.00"
    m = re.search(
        r"Budget[^:\n]*:\s*\$?([\d.]+)\s*/\s*\$?([\d.]+)", text, re.IGNORECASE
    )
    if m:
        spent, ceiling = float(m.group(1)), float(m.group(2))
        if ceiling > 0:
            return str(int(round(spent / ceiling * 100)))
    return None


def _read_home_open_pr_count(text: str) -> Optional[str]:
    """Extract the open PR count from the Home screen.

    Looks for patterns like "Open PRs: 5".
    Returns the count as a string, or None.
    """
    if not text:
        return None
    m = re.search(r"Open\s+PRs[:\s]+(\d+)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _read_prs_all_count(text: str) -> Optional[str]:
    """Extract the total PR count from the PRs screen.

    Looks for patterns like "5 all" or "All: 5" or "all (5)".
    Returns the count as a string, or None.
    """
    if not text:
        return None
    m = re.search(r"(\d+)\s+all\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\ball[:\s(]+(\d+)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _read_home_last_run_ts(text: str) -> Optional[str]:
    """Extract the 'Last loop run' timestamp from the Home screen.

    Looks for patterns like "Last loop run: 10:42" or "Last loop run: 2026-05-14 10:42".
    Returns the time portion as a string (HH:MM), or None.
    """
    if not text:
        return None
    m = re.search(r"Last\s+loop\s+run[:\s]+([0-9]{1,2}:[0-9]{2})", text, re.IGNORECASE)
    return m.group(1) if m else None


def _read_loop_health_last_ts(text: str) -> Optional[str]:
    """Extract the most-recent row timestamp from the Loop Health screen.

    Loop Health shows a table of loop runs with timestamps in HH:MM format.
    The most-recent row is usually first.  We return the first HH:MM we find.
    Returns the time portion as a string (HH:MM), or None.
    """
    if not text:
        return None
    m = re.search(r"\b([0-9]{1,2}:[0-9]{2})\b", text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Comparators
# ---------------------------------------------------------------------------


def _staleness_agrees(a: Optional[str], b: Optional[str]) -> bool:
    """Both must agree on stale/ok.  If either is None, we cannot assert → pass."""
    if a is None or b is None:
        return True
    return a == b


def _counts_agree(a: Optional[str], b: Optional[str]) -> bool:
    """Numeric counts must match exactly.  None on either side → pass."""
    if a is None or b is None:
        return True
    try:
        return int(a) == int(b)
    except ValueError:
        return True  # unparseable — skip


def _budget_roughly_agrees(a: Optional[str], b: Optional[str]) -> bool:
    """Budget percentages must agree within ±5 percentage points.
    None on either side → pass.
    """
    if a is None or b is None:
        return True
    try:
        return abs(float(a) - float(b)) <= 5.0
    except ValueError:
        return True


def _timestamps_agree(a: Optional[str], b: Optional[str]) -> bool:
    """Timestamps (HH:MM) must match exactly.  None on either side → pass."""
    if a is None or b is None:
        return True
    return a.strip() == b.strip()


# ---------------------------------------------------------------------------
# Invariant registry
# ---------------------------------------------------------------------------


INVARIANTS: list[tuple[str, str, str, Invariant]] = [
    # (screen_a_key, screen_b_key, description, invariant)
    (
        "loop",
        "loop_controller",
        "Loop Health and Loop Controller must agree on staleness",
        Invariant(
            name="loop_staleness_agreement",
            screen_a_reader=_read_loop_health_status,
            screen_b_reader=_read_loop_controller_stale,
            comparator=_staleness_agrees,
        ),
    ),
    (
        "agent_feed",
        "runs",
        "Agent Feed stuck count must match Runs page Stuck Runs row",
        Invariant(
            name="stuck_count_agreement",
            screen_a_reader=_read_agent_feed_stuck_count,
            screen_b_reader=_read_runs_stuck_count,
            comparator=_counts_agree,
        ),
    ),
    (
        "home",
        "loop_controller",
        "Home weekly budget % must agree with Loop Controller budget spent/ceiling",
        Invariant(
            name="budget_agreement",
            screen_a_reader=_read_home_budget_percent,
            screen_b_reader=_read_loop_controller_budget,
            comparator=_budget_roughly_agrees,
        ),
    ),
    (
        "home",
        "prs",
        "Home 'Open PRs: N' must match PRs screen 'N all' counter",
        Invariant(
            name="open_pr_count_agreement",
            screen_a_reader=_read_home_open_pr_count,
            screen_b_reader=_read_prs_all_count,
            comparator=_counts_agree,
        ),
    ),
    (
        "home",
        "loop",
        "Home 'Last loop run' timestamp must match Loop Health most-recent row ts",
        Invariant(
            name="last_run_agreement",
            screen_a_reader=_read_home_last_run_ts,
            screen_b_reader=_read_loop_health_last_ts,
            comparator=_timestamps_agree,
        ),
    ),
]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def check_all(pilot_screens: dict) -> list[dict]:
    """Run all cross-screen invariants against captured screen texts.

    pilot_screens: dict mapping screen name (str) → captured text (str).
                   Keys must include the screen names referenced by each invariant.

    Returns a list of finding dicts (same shape as findings.json entries) for
    every invariant that is violated.  An empty list means all invariants passed.

    Invariants where either screen text is missing are silently skipped
    (no assertion when data is absent).
    """
    violations: list[dict] = []

    for screen_a_key, screen_b_key, _description, invariant in INVARIANTS:
        text_a = pilot_screens.get(screen_a_key, "")
        text_b = pilot_screens.get(screen_b_key, "")

        val_a = invariant.screen_a_reader(text_a)
        val_b = invariant.screen_b_reader(text_b)

        if not invariant.comparator(val_a, val_b):
            detail = (
                f"cross_screen_disagreement: {invariant.name} — "
                f"{screen_a_key}={val_a!r} vs {screen_b_key}={val_b!r}"
            )
            v = Violation(
                invariant_name=invariant.name,
                screen_a_name=screen_a_key,
                screen_b_name=screen_b_key,
                screen_a_value=val_a,
                screen_b_value=val_b,
                detail=detail,
            )
            violations.append(v.to_finding())

    return violations
