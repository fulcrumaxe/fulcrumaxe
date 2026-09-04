"""Acceptance suite for the BLOCKED-BY field on the STATUS line (D#1755).

Covers Spec items 1-7: parsing the field, resolving refs, failing closed, and
batching. Items 8-11 (the readers) are exercised by tests/test_spec_ready_gate.sh
and tests/test_loop_phased_step5.sh.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.blocked_by import (  # noqa: E402
    BlockerResolver,
    parse_ref,
    partition_spec_ready,
    unresolved_for_body,
)
from backend.discussion_status import (  # noqa: E402
    BLOCKED_BY_UNPARSEABLE,
    extract_blocked_by,
    extract_linked_pr,
    extract_status_anchored,
    is_spec_ready,
    set_status,
)


def status_line(*, blocked=None, status="SPEC_READY", order="blocked-first"):
    b = f"BLOCKED-BY:{blocked}" if blocked else ""
    if order == "blocked-first":
        parts = [f"STATUS:{status}", b, "SINCE:2026-07-28T06:30:00Z"]
    else:
        parts = [f"STATUS:{status}", "SINCE:2026-07-28T06:30:00Z", b]
    return "<!-- " + " ".join(p for p in parts if p) + " -->"


# ---------------------------------------------------------------------------
# Item 1 — parse the field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked,expected",
    [
        ("#1771", ["#1771"]),
        ("#1691,#1750,#1771", ["#1691", "#1750", "#1771"]),
        ("D#1746", ["D#1746"]),
        ("#1691,D#1746", ["#1691", "D#1746"]),
        (None, []),
    ],
)
def test_extract_blocked_by_cases(blocked, expected):
    assert extract_blocked_by(status_line(blocked=blocked)) == expected


def test_extract_blocked_by_is_token_order_independent():
    before = status_line(blocked="#1691,D#1746", order="blocked-first")
    after = status_line(blocked="#1691,D#1746", order="since-first")
    assert extract_blocked_by(before) == extract_blocked_by(after) == ["#1691", "D#1746"]


def test_extract_blocked_by_preserves_source_order():
    assert extract_blocked_by(status_line(blocked="#1771,#1691,D#1746")) == [
        "#1771",
        "#1691",
        "D#1746",
    ]


# ---------------------------------------------------------------------------
# Item 1 — a field that is PRESENT can fail, but must never disappear
# ---------------------------------------------------------------------------
#
# The first version of this parser used one regex for both presence and value,
# so "no BLOCKED-BY at all" and "a BLOCKED-BY whose value did not match" were
# the same answer — []. Every shape below therefore read as NOT blocked, and
# the gate opened. These pin each one.

# Exactly the shapes that used to silently allow. Kept as its own list so the
# fail-open pin below and the parse assertions above cannot drift apart.
SILENTLY_ALLOWED_BEFORE_THE_FIX = [
    "<!-- STATUS:SPEC_READY BLOCKED-BY: #1750 SINCE:2026-07-28T06:30:00Z -->",
    "<!-- STATUS:SPEC_READY BLOCKED-BY:#1691, D#1746 SINCE:2026-07-28T06:30:00Z -->",
    "<!-- STATUS:SPEC_READY BLOCKED-BY: SINCE:2026-07-28T06:30:00Z -->",
    "<!-- STATUS:SPEC_READY blocked-by:#1750 SINCE:2026-07-28T06:30:00Z -->",
]


@pytest.mark.parametrize(
    "line,expected",
    [
        # A space after the colon is tolerated: the PM's intent is legible, so
        # honour the ref rather than condemning it to permanently-malformed.
        (SILENTLY_ALLOWED_BEFORE_THE_FIX[0], ["#1750"]),
        # A space after the comma keeps BOTH refs. This is the dangerous one:
        # the old parser stopped at the space, so D#1746 vanished and the gate
        # opened the moment #1691 merged.
        (SILENTLY_ALLOWED_BEFORE_THE_FIX[1], ["#1691", "D#1746"]),
        # Present and empty — a sentinel, never [].
        (SILENTLY_ALLOWED_BEFORE_THE_FIX[2], [BLOCKED_BY_UNPARSEABLE]),
        (SILENTLY_ALLOWED_BEFORE_THE_FIX[3], ["#1750"]),
        # Bare field, no other token on the line.
        ("<!-- STATUS:SPEC_READY BLOCKED-BY: -->", [BLOCKED_BY_UNPARSEABLE]),
        # Space-separated instead of comma-separated: nothing is dropped — the
        # whole value survives as one ref and fails closed as malformed.
        ("<!-- STATUS:SPEC_READY BLOCKED-BY:#1691 D#1746 SINCE:X -->", ["#1691 D#1746"]),
        # An unrecognised token is not a terminator, so it lands in the value
        # and blocks. A line we cannot parse must not read as "clear to spawn".
        ("<!-- STATUS:SPEC_READY BLOCKED-BY:#1691 OWNER:pm SINCE:X -->", ["#1691 OWNER:pm"]),
        # Unterminated comment: presence still wins over an unreadable value.
        ("<!-- STATUS:SPEC_READY BLOCKED-BY:#1691", [BLOCKED_BY_UNPARSEABLE]),
    ],
)
def test_present_field_never_silently_yields_nothing(line, expected):
    assert extract_blocked_by(line) == expected


@pytest.mark.parametrize("line", SILENTLY_ALLOWED_BEFORE_THE_FIX)
def test_fail_open_shapes_are_pinned_closed(line):
    """One stray space, or one lowercase letter, must not turn the field off."""
    assert extract_blocked_by(line) != []


def test_blocked_by_does_not_disturb_the_existing_status_read():
    body = status_line(blocked="#1691,#1750") + "\n\nbody text"
    assert extract_status_anchored(body) == "SPEC_READY"
    assert is_spec_ready(body) is True


# ---------------------------------------------------------------------------
# Item 2 — prose and code fences are not constraints
# ---------------------------------------------------------------------------


def test_blocked_by_in_prose_is_ignored():
    body = status_line() + "\n\nA PM may write BLOCKED-BY:#1771 in prose to explain.\n"
    assert extract_blocked_by(body) == []


def test_blocked_by_in_code_fence_is_ignored():
    body = (
        status_line()
        + "\n\nExample of the convention:\n\n```\n"
        + status_line(blocked="#1691,#1750,#1771")
        + "\n```\n"
    )
    assert extract_blocked_by(body) == []


def test_blocked_by_on_a_later_status_comment_is_ignored():
    """A stale marker further down the body must not start blocking things."""
    body = status_line() + "\n\nEarlier state:\n\n" + status_line(blocked="#1771") + "\n"
    assert extract_blocked_by(body) == []


# ---------------------------------------------------------------------------
# Item 3 — regression pin for the defect that motivated D#1755
# ---------------------------------------------------------------------------

# The D#1755 body as of 2026-07-28: no STATUS line of its own, but it quotes the
# selector predicate in a code fence, so the literals STATUS:SPEC_READY,
# STATUS:DONE and STATUS:CLOSED all appear in prose.
D1755_BODY_2026_07_28 = """## What

`STATUS:SPEC_READY` means two different things.

```python
if 'STATUS:SPEC_READY' in body and 'STATUS:DONE' not in body and 'STATUS:CLOSED' not in body:
```

Nothing reads the invented marker.
"""


def test_d1755_body_regression_pin_one_answer_not_two():
    """Two readers, same body, opposite answers — pinned so it cannot come back."""
    body = D1755_BODY_2026_07_28

    # Reader A, the old selector predicate: the body quotes STATUS:DONE and
    # STATUS:CLOSED inside the fence, so the exclusion terms match on prose and
    # the Discussion is invisible to the selector.
    old_selector_would_pick = (
        "STATUS:SPEC_READY" in body
        and "STATUS:DONE" not in body
        and "STATUS:CLOSED" not in body
    )
    assert old_selector_would_pick is False

    # Reader B, the old spawn-agent.sh first-token grep: first STATUS token
    # anywhere in the body wins, and that is SPEC_READY from the same fence. So
    # the gate would have cleared an executor against a Discussion with no Spec.
    import re

    old_gate_token = re.search(r"STATUS:\s*([A-Z_]+)", body).group(1)
    assert old_gate_token == "SPEC_READY"
    assert old_gate_token != ("SPEC_READY" if old_selector_would_pick else "UNKNOWN")

    # Post-fix: every gating reader goes through the anchored parse and gets ONE
    # answer — this body has no authoritative status, so it is not spawnable.
    assert extract_status_anchored(body) == "UNKNOWN"
    assert is_spec_ready(body) is False
    assert extract_blocked_by(body) == []


# ---------------------------------------------------------------------------
# Items 4-6 — resolve the refs, and fail closed
# ---------------------------------------------------------------------------


def fetcher(pr_states=None, disc_bodies=None, counter=None):
    """Build a mock fetcher; no live API call anywhere in this suite."""

    def _f(pr_numbers, discussion_numbers):
        if counter is not None:
            counter.append((tuple(pr_numbers), tuple(discussion_numbers)))
        return {
            "pr": {n: (pr_states or {})[n] for n in pr_numbers if n in (pr_states or {})},
            "discussion": {
                n: (disc_bodies or {})[n] for n in discussion_numbers if n in (disc_bodies or {})
            },
        }

    return _f


@pytest.mark.parametrize("state,cleared", [("MERGED", True), ("CLOSED", True), ("OPEN", False)])
def test_pr_ref_resolution(state, cleared):
    r = BlockerResolver(fetcher=fetcher(pr_states={1771: state}))
    outstanding = r.unresolved(["#1771"])
    assert (outstanding == []) is cleared
    if not cleared:
        assert "OPEN" in outstanding[0][1]


@pytest.mark.parametrize(
    "status,cleared",
    [("DONE", True), ("CLOSED", True), ("SPEC_READY", False), ("IMPLEMENTING", False)],
)
def test_discussion_ref_resolution(status, cleared):
    body = f"<!-- STATUS:{status} SINCE:X -->\n\nbody"
    r = BlockerResolver(fetcher=fetcher(disc_bodies={1746: body}))
    assert (r.unresolved(["D#1746"]) == []) is cleared


def test_discussion_ref_uses_parsed_status_not_substring():
    """A Discussion merely quoting STATUS:DONE in prose clears nothing (item 5)."""
    body = "<!-- STATUS:SPEC_READY SINCE:X -->\n\nWe flip to `STATUS:DONE` when finished.\n"
    r = BlockerResolver(fetcher=fetcher(disc_bodies={1746: body}))
    assert r.unresolved(["D#1746"]) != []


@pytest.mark.parametrize("ref", ["banana", "#", "D#", "1771", "#12a", "D-1746"])
def test_malformed_ref_blocks(ref):
    r = BlockerResolver(fetcher=fetcher())
    outstanding = r.unresolved([ref])
    assert len(outstanding) == 1
    assert "malformed" in outstanding[0][1]


def test_nonexistent_ref_blocks():
    r = BlockerResolver(fetcher=fetcher(pr_states={}, disc_bodies={}))
    outstanding = dict(r.unresolved(["#999999", "D#999999"]))
    assert "not found" in outstanding["#999999"]
    assert "not found" in outstanding["D#999999"]


def test_resolution_failure_blocks_and_reports():
    def boom(pr_numbers, discussion_numbers):
        raise TimeoutError("gh api graphql timed out")

    outstanding = BlockerResolver(fetcher=boom).unresolved(["#1771"])
    assert len(outstanding) == 1
    # Reported, never swallowed.
    assert "could not resolve" in outstanding[0][1]
    assert "timed out" in outstanding[0][1]


def test_transient_failure_is_not_cached():
    calls = []

    def flaky(pr_numbers, discussion_numbers):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return {"pr": {1771: "MERGED"}, "discussion": {}}

    r = BlockerResolver(fetcher=flaky)
    assert r.unresolved(["#1771"]) != []
    assert r.unresolved(["#1771"]) == []


def test_parse_ref():
    assert parse_ref("#12") == ("pr", 12)
    assert parse_ref("D#12") == ("discussion", 12)
    assert parse_ref(" #12 ") == ("pr", 12)
    assert parse_ref("banana") is None


# ---------------------------------------------------------------------------
# Item 7 — batching
# ---------------------------------------------------------------------------


def test_one_round_trip_regardless_of_ref_count():
    calls = []
    r = BlockerResolver(
        fetcher=fetcher(
            pr_states={1691: "MERGED", 1750: "MERGED", 1771: "OPEN"},
            disc_bodies={1746: "<!-- STATUS:DONE SINCE:X -->"},
            counter=calls,
        )
    )
    r.unresolved(["#1691", "#1750", "#1771", "D#1746"])
    assert len(calls) == 1
    assert calls[0] == ((1691, 1750, 1771), (1746,))


def test_repeated_lookups_hit_the_cache():
    calls = []
    r = BlockerResolver(fetcher=fetcher(pr_states={1771: "OPEN"}, counter=calls))
    for _ in range(5):
        r.unresolved(["#1771"])
    assert len(calls) == 1
    assert r.call_count == 1


def test_selector_makes_one_round_trip_for_a_whole_batch():
    """The selector-level entry point: many Discussions, still one fetch."""
    calls = []
    discussions = [
        {"number": 1746, "title": "a", "body": status_line(blocked="#1691,#1750,#1771")},
        {"number": 1748, "title": "b", "body": status_line(blocked="D#1746")},
        {"number": 1761, "title": "c", "body": status_line()},
        {"number": 1900, "title": "d", "body": "<!-- STATUS:DISCUSSING SINCE:X -->"},
    ]
    r = BlockerResolver(
        fetcher=fetcher(
            pr_states={1691: "MERGED", 1750: "MERGED", 1771: "OPEN"},
            disc_bodies={1746: status_line()},
            counter=calls,
        )
    )
    ready, blocked = partition_spec_ready(discussions, resolver=r)

    assert len(calls) == 1, "selector must batch to a single round trip"
    assert [d["number"] for d in ready] == [1761]
    assert dict(blocked).keys() == {1746, 1748}
    assert "#1771" in dict(blocked)[1746]
    # Cleared refs are not reported as blockers.
    assert "#1691" not in dict(blocked)[1746]


def test_no_blocked_by_is_backward_compatible():
    """A body with no BLOCKED-BY behaves exactly as before — and fetches nothing."""
    calls = []
    discussions = [{"number": 1761, "title": "c", "body": status_line()}]
    r = BlockerResolver(fetcher=fetcher(counter=calls))
    ready, blocked = partition_spec_ready(discussions, resolver=r)
    assert [d["number"] for d in ready] == [1761]
    assert blocked == []
    assert calls == [], "no refs outstanding must mean no network call at all"


def test_unresolved_for_body_end_to_end():
    body = status_line(blocked="#1771") + "\n\nSpec text.\n"
    r = BlockerResolver(fetcher=fetcher(pr_states={1771: "OPEN"}))
    assert unresolved_for_body(body, resolver=r) != []


# ---------------------------------------------------------------------------
# The fail-open shapes, driven through the selector rather than the parser
# ---------------------------------------------------------------------------


def test_space_after_comma_still_honours_the_second_ref():
    """The one that actually spawns an executor early, end to end.

    #1691 has merged; D#1746 has not. Under the old parser D#1746 was never
    extracted, so this Discussion became spawnable the moment #1691 landed.
    """
    discussions = [
        {
            "number": 1761,
            "title": "parked",
            "body": "<!-- STATUS:SPEC_READY BLOCKED-BY:#1691, D#1746 SINCE:X -->\n\nSpec.\n",
        }
    ]
    r = BlockerResolver(
        fetcher=fetcher(
            pr_states={1691: "MERGED"},
            disc_bodies={1746: "<!-- STATUS:SPEC_READY SINCE:X -->"},
        )
    )
    ready, blocked = partition_spec_ready(discussions, resolver=r)
    assert ready == []
    assert "D#1746" in dict(blocked)[1761]


def test_present_but_empty_blocks_with_a_reason_naming_the_field():
    body = "<!-- STATUS:SPEC_READY BLOCKED-BY: SINCE:X -->\n\nSpec.\n"
    outstanding = unresolved_for_body(body, resolver=BlockerResolver(fetcher=fetcher()))
    assert len(outstanding) == 1
    assert "empty or unreadable" in outstanding[0][1]
    assert "BLOCKED-BY" in outstanding[0][1]


def test_lowercase_field_blocks():
    body = "<!-- STATUS:SPEC_READY blocked-by:banana SINCE:X -->\n\nSpec.\n"
    assert unresolved_for_body(body, resolver=BlockerResolver(fetcher=fetcher())) != []


def test_resolution_failure_costs_one_fetch_for_the_whole_batch():
    """A GitHub outage must not cost one 60s timeout per blocked Discussion.

    `_verdict` deliberately does not cache a fetch failure, so asking the
    resolver again per Discussion re-entered the fetcher every time — K+1 calls
    for K blocked Discussions, on a path the loop selector runs each iteration.
    """
    calls = []

    def boom(pr_numbers, discussion_numbers):
        calls.append((tuple(pr_numbers), tuple(discussion_numbers)))
        raise TimeoutError("gh api graphql timed out")

    discussions = [
        {"number": 1700 + i, "title": str(i), "body": status_line(blocked=f"#{1800 + i}")}
        for i in range(5)
    ]
    ready, blocked = partition_spec_ready(discussions, resolver=BlockerResolver(fetcher=boom))

    assert ready == []
    assert len(blocked) == 5, "every Discussion still blocks — failure means blocked"
    assert all("could not resolve" in reason for _, reason in blocked)
    assert len(calls) == 1, f"one failing fetch must not be retried per Discussion: {len(calls)}"


# ---------------------------------------------------------------------------
# set_status must not erase the field it now tells PMs to write
# ---------------------------------------------------------------------------


def test_set_status_carries_blocked_by_across_a_status_change():
    body = status_line(blocked="#1691,D#1746") + "\n\nSpec text.\n"
    out = set_status(body, "DISCUSSING", now_iso="2026-08-18T00:00:00Z")
    assert extract_status_anchored(out) == "DISCUSSING"
    assert extract_blocked_by(out) == ["#1691", "D#1746"]


def test_set_status_round_trips_a_present_but_empty_blocked_by():
    """Presence survives even when the value does not parse — still fail closed."""
    body = "<!-- STATUS:SPEC_READY BLOCKED-BY: SINCE:X -->\n\nSpec.\n"
    out = set_status(body, "DISCUSSING", now_iso="2026-08-18T00:00:00Z")
    assert extract_blocked_by(out) == [BLOCKED_BY_UNPARSEABLE]


def test_set_status_writes_no_blocked_by_when_there_was_none():
    body = status_line() + "\n\nSpec text.\n"
    out = set_status(body, "IMPLEMENTING", now_iso="2026-08-18T00:00:00Z")
    assert out.splitlines()[0] == "<!-- STATUS:IMPLEMENTING SINCE:2026-08-18T00:00:00Z -->"


def test_set_status_still_drops_pr_ref():
    """Pinned, not fixed: the PR: loss predates BLOCKED-BY and is its own call.

    Carrying a PR ref onto a new status would be a behaviour change with its own
    readers (extract_linked_pr), so it is recorded here as a known property
    rather than quietly altered alongside the BLOCKED-BY fix.
    """
    body = "<!-- STATUS:REVIEWING PR:#321 SINCE:X -->\n\nSpec.\n"
    out = set_status(body, "DONE", now_iso="2026-08-18T00:00:00Z")
    assert extract_linked_pr(out) is None
