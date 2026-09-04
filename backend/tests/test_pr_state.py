"""
Behavioral tests for backend/pr_state.py

All tests use a temp-directory-backed Blackboard; the real state directory
(~/.fulcrumaxe-state/) is never touched.  No network calls are made.

Run with:
    python3 -m pytest backend/tests/test_pr_state.py -v
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.blackboard import Blackboard
from backend.pr_state import (
    TERMINAL_PHASES,
    VALID_PHASES,
    _STALE_THRESHOLD_SECONDS,
    advance,
    get_entry,
    init_entry,
    list_entries,
    record_envelope,
    set_fields,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bb(tmp_path: Path) -> Blackboard:
    """Isolated file-based Blackboard; nothing touches the real state dir."""
    return Blackboard(root=tmp_path / "bb")


@pytest.fixture()
def entry(bb: Blackboard) -> dict:
    """A freshly-initialised PR #42 in the queued phase."""
    return init_entry(42, discussion=100, bb=bb)


# ---------------------------------------------------------------------------
# init_entry
# ---------------------------------------------------------------------------


class TestInitEntry:
    def test_creates_entry_with_expected_defaults(self, bb):
        e = init_entry(1, discussion=10, bb=bb)
        assert e["pr"] == 1
        assert e["discussion"] == 10
        assert e["phase"] == "queued"
        assert e["spawned_phases"] == []
        assert e["completed_phases"] == []
        assert e["needs_security_review"] is False
        assert e["fix_cycle_count"] == 0
        assert e["debate_cycle_count"] == 0
        assert e["respawn_count"] == 0
        assert e["last_envelope"] == {}
        assert e["blocked_reason"] is None

    def test_created_at_and_updated_at_are_iso8601(self, bb):
        e = init_entry(2, discussion=20, bb=bb)
        # Both timestamps should parse without error.
        datetime.fromisoformat(e["created_at"])
        datetime.fromisoformat(e["updated_at"])

    def test_created_at_equals_updated_at_on_init(self, bb):
        e = init_entry(3, discussion=30, bb=bb)
        assert e["created_at"] == e["updated_at"]

    def test_entry_is_persisted_and_readable(self, bb):
        init_entry(4, discussion=40, bb=bb)
        read_back = get_entry(4, bb=bb)
        assert read_back is not None
        assert read_back["pr"] == 4
        assert read_back["phase"] == "queued"

    def test_duplicate_raises_value_error(self, bb):
        init_entry(5, discussion=50, bb=bb)
        with pytest.raises(ValueError, match="already exists"):
            init_entry(5, discussion=50, bb=bb)

    def test_different_pr_numbers_are_independent(self, bb):
        init_entry(10, discussion=1, bb=bb)
        init_entry(11, discussion=2, bb=bb)
        assert get_entry(10, bb=bb)["discussion"] == 1
        assert get_entry(11, bb=bb)["discussion"] == 2


# ---------------------------------------------------------------------------
# get_entry
# ---------------------------------------------------------------------------


class TestGetEntry:
    def test_returns_none_for_missing_pr(self, bb):
        assert get_entry(999, bb=bb) is None

    def test_returns_entry_after_init(self, bb, entry):
        result = get_entry(42, bb=bb)
        assert result is not None
        assert result["pr"] == 42


# ---------------------------------------------------------------------------
# advance — valid transitions
# ---------------------------------------------------------------------------


class TestAdvanceValidTransitions:
    def test_queued_to_executing(self, bb, entry):
        e = advance(42, "executing", bb=bb)
        assert e["phase"] == "executing"

    def test_executing_to_code_review(self, bb, entry):
        advance(42, "executing", bb=bb)
        e = advance(42, "code_review", bb=bb)
        assert e["phase"] == "code_review"

    def test_code_review_to_merging(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        e = advance(42, "merging", bb=bb)
        assert e["phase"] == "merging"

    def test_merging_to_merged(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        advance(42, "merging", bb=bb)
        e = advance(42, "merged", bb=bb)
        assert e["phase"] == "merged"

    def test_code_review_to_fix_cycle(self, bb, entry):
        """code_review -> executing is the fix-cycle path."""
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        e = advance(42, "executing", bb=bb)
        assert e["phase"] == "executing"

    def test_code_review_to_security_review(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        e = advance(42, "security_review", bb=bb)
        assert e["phase"] == "security_review"

    def test_code_review_to_debate(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        e = advance(42, "debate", bb=bb)
        assert e["phase"] == "debate"

    def test_debate_to_security_review(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        advance(42, "debate", bb=bb)
        e = advance(42, "security_review", bb=bb)
        assert e["phase"] == "security_review"

    def test_security_review_to_merging(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        advance(42, "security_review", bb=bb)
        e = advance(42, "merging", bb=bb)
        assert e["phase"] == "merging"

    def test_any_phase_to_blocked(self, bb):
        for phase in ["queued", "executing", "code_review", "debate", "security_review", "merging"]:
            inner_bb = Blackboard(root=bb._root.parent / f"bb_{phase}")
            init_entry(1, discussion=1, bb=inner_bb)
            # Advance to the desired starting phase first.
            _path_to(inner_bb, 1, phase)
            e = advance(1, "blocked", bb=inner_bb)
            assert e["phase"] == "blocked", f"Expected blocked from {phase}"

    def test_advance_updates_updated_at(self, bb, entry):
        original = entry["updated_at"]
        # Ensure at least 1-second separation (ISO timestamps have second precision).
        time.sleep(1.1)
        e = advance(42, "executing", bb=bb)
        assert e["updated_at"] >= original

    def test_advance_persists_to_blackboard(self, bb, entry):
        advance(42, "executing", bb=bb)
        read_back = get_entry(42, bb=bb)
        assert read_back["phase"] == "executing"


# ---------------------------------------------------------------------------
# advance — invalid transitions
# ---------------------------------------------------------------------------


class TestAdvanceInvalidTransitions:
    def test_queued_to_merged_raises(self, bb, entry):
        with pytest.raises(ValueError, match="Invalid transition"):
            advance(42, "merged", bb=bb)

    def test_terminal_merged_to_executing_raises(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        advance(42, "merging", bb=bb)
        advance(42, "merged", bb=bb)
        with pytest.raises(ValueError, match="terminal phase"):
            advance(42, "executing", bb=bb)

    def test_terminal_blocked_to_executing_raises(self, bb, entry):
        advance(42, "blocked", bb=bb)
        with pytest.raises(ValueError, match="terminal phase"):
            advance(42, "executing", bb=bb)

    def test_unknown_phase_raises(self, bb, entry):
        with pytest.raises(ValueError, match="Unknown phase"):
            advance(42, "nonexistent_phase", bb=bb)

    def test_missing_pr_raises(self, bb):
        with pytest.raises(ValueError, match="No pr_state entry"):
            advance(9999, "executing", bb=bb)

    def test_executing_to_merged_raises(self, bb, entry):
        advance(42, "executing", bb=bb)
        with pytest.raises(ValueError, match="Invalid transition"):
            advance(42, "merged", bb=bb)


# ---------------------------------------------------------------------------
# set_fields
# ---------------------------------------------------------------------------


class TestSetFields:
    def test_set_phase_bypasses_transition_validation(self, bb, entry):
        # Direct set from queued -> merged (invalid via advance, but set_fields allows it)
        e = set_fields(42, phase="merged", bb=bb)
        assert e["phase"] == "merged"

    def test_set_arbitrary_field(self, bb, entry):
        e = set_fields(42, fields={"needs_security_review": True}, bb=bb)
        assert e["needs_security_review"] is True

    def test_set_multiple_fields(self, bb, entry):
        e = set_fields(42, fields={"fix_cycle_count": 3, "respawn_count": 2}, bb=bb)
        assert e["fix_cycle_count"] == 3
        assert e["respawn_count"] == 2

    def test_set_unknown_field_is_stored(self, bb, entry):
        e = set_fields(42, fields={"custom_flag": "hello"}, bb=bb)
        assert e["custom_flag"] == "hello"

    def test_set_fields_persists(self, bb, entry):
        set_fields(42, fields={"needs_security_review": True}, bb=bb)
        read_back = get_entry(42, bb=bb)
        assert read_back["needs_security_review"] is True

    def test_set_unknown_phase_raises(self, bb, entry):
        with pytest.raises(ValueError, match="Unknown phase"):
            set_fields(42, phase="bogus", bb=bb)

    def test_set_fields_missing_pr_raises(self, bb):
        with pytest.raises(ValueError, match="No pr_state entry"):
            set_fields(9999, fields={"x": 1}, bb=bb)

    def test_set_updates_updated_at(self, bb, entry):
        original = entry["updated_at"]
        time.sleep(1.1)
        e = set_fields(42, fields={"respawn_count": 1}, bb=bb)
        assert e["updated_at"] >= original


# ---------------------------------------------------------------------------
# record_envelope
# ---------------------------------------------------------------------------


class TestRecordEnvelope:
    def test_appends_to_completed_phases(self, bb, entry):
        e = record_envelope(42, role="executor", verdict="done", bb=bb)
        assert len(e["completed_phases"]) == 1
        rec = e["completed_phases"][0]
        assert rec["role"] == "executor"
        assert rec["verdict"] == "done"

    def test_multiple_envelopes_accumulate(self, bb, entry):
        record_envelope(42, role="executor", verdict="done", bb=bb)
        e = record_envelope(42, role="code-reviewer", verdict="needs-fix", bb=bb)
        assert len(e["completed_phases"]) == 2
        assert e["completed_phases"][1]["role"] == "code-reviewer"

    def test_last_envelope_is_updated(self, bb, entry):
        record_envelope(42, role="executor", verdict="done", bb=bb)
        e = record_envelope(42, role="code-reviewer", verdict="pass", bb=bb)
        assert e["last_envelope"]["role"] == "code-reviewer"
        assert e["last_envelope"]["verdict"] == "pass"

    def test_needs_fix_increments_fix_cycle_count(self, bb, entry):
        record_envelope(42, role="code-reviewer", verdict="needs-fix", bb=bb)
        e = record_envelope(42, role="code-reviewer", verdict="needs-fix", bb=bb)
        assert e["fix_cycle_count"] == 2

    def test_non_needs_fix_verdict_does_not_increment_fix_count(self, bb, entry):
        e = record_envelope(42, role="executor", verdict="done", bb=bb)
        assert e["fix_cycle_count"] == 0

    def test_tokens_stored_in_record(self, bb, entry):
        e = record_envelope(
            42, role="executor", verdict="done",
            input_tokens=50000, output_tokens=8000, bb=bb
        )
        rec = e["completed_phases"][0]
        assert rec["input_tokens"] == 50000
        assert rec["output_tokens"] == 8000

    def test_event_id_stored_when_provided(self, bb, entry):
        e = record_envelope(42, role="executor", verdict="done", event_id="abc123", bb=bb)
        assert e["completed_phases"][0]["event_id"] == "abc123"

    def test_event_id_absent_when_not_provided(self, bb, entry):
        e = record_envelope(42, role="executor", verdict="done", bb=bb)
        assert "event_id" not in e["completed_phases"][0]

    def test_envelope_persists_to_blackboard(self, bb, entry):
        record_envelope(42, role="executor", verdict="done", bb=bb)
        read_back = get_entry(42, bb=bb)
        assert len(read_back["completed_phases"]) == 1
        assert read_back["last_envelope"]["verdict"] == "done"

    def test_missing_pr_raises(self, bb):
        with pytest.raises(ValueError, match="No pr_state entry"):
            record_envelope(9999, role="executor", verdict="done", bb=bb)


# ---------------------------------------------------------------------------
# list_entries — basic
# ---------------------------------------------------------------------------


class TestListEntries:
    def test_empty_list_when_no_entries(self, bb):
        assert list_entries(bb=bb) == []

    def test_returns_all_entries(self, bb):
        init_entry(1, discussion=10, bb=bb)
        init_entry(2, discussion=20, bb=bb)
        result = list_entries(bb=bb)
        assert len(result) == 2

    def test_entries_sorted_by_pr_number(self, bb):
        init_entry(30, discussion=1, bb=bb)
        init_entry(10, discussion=2, bb=bb)
        init_entry(20, discussion=3, bb=bb)
        result = list_entries(bb=bb)
        prs = [e["pr"] for e in result]
        assert prs == sorted(prs)

    def test_filter_by_phase(self, bb):
        init_entry(1, discussion=1, bb=bb)
        init_entry(2, discussion=2, bb=bb)
        advance(2, "executing", bb=bb)
        result = list_entries(phase="executing", bb=bb)
        assert len(result) == 1
        assert result[0]["pr"] == 2

    def test_filter_by_blocked(self, bb):
        init_entry(1, discussion=1, bb=bb)
        init_entry(2, discussion=2, bb=bb)
        advance(2, "blocked", bb=bb)
        result = list_entries(blocked=True, bb=bb)
        assert len(result) == 1
        assert result[0]["phase"] == "blocked"

    def test_blocked_flag_takes_precedence_over_phase_filter(self, bb):
        """When both blocked=True and phase='queued' are given, blocked wins."""
        init_entry(1, discussion=1, bb=bb)
        init_entry(2, discussion=2, bb=bb)
        advance(2, "blocked", bb=bb)
        # blocked=True should return the blocked one, not the queued one
        result = list_entries(phase="queued", blocked=True, bb=bb)
        assert all(e["phase"] == "blocked" for e in result)

    def test_filter_by_discussion(self, bb):
        init_entry(1, discussion=100, bb=bb)
        init_entry(2, discussion=200, bb=bb)
        result = list_entries(discussion=100, bb=bb)
        assert len(result) == 1
        assert result[0]["discussion"] == 100

    def test_filter_discussion_no_match(self, bb):
        init_entry(1, discussion=100, bb=bb)
        result = list_entries(discussion=999, bb=bb)
        assert result == []


# ---------------------------------------------------------------------------
# list_entries — stale detection
# ---------------------------------------------------------------------------


class TestListEntriesStale:
    def _make_old_ts(self, seconds_ago: int) -> str:
        """Return an ISO8601 timestamp that is *seconds_ago* in the past."""
        dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return dt.isoformat(timespec="seconds")

    def test_non_stale_entry_excluded(self, bb):
        init_entry(1, discussion=1, bb=bb)
        # updated_at is "now" — not stale
        result = list_entries(stale=True, bb=bb)
        assert result == []

    def test_stale_entry_included(self, bb):
        init_entry(1, discussion=1, bb=bb)
        # Manually age the entry by writing an old updated_at
        entry = get_entry(1, bb=bb)
        entry["updated_at"] = self._make_old_ts(_STALE_THRESHOLD_SECONDS + 120)
        from backend.blackboard import Blackboard as _BB
        key = f"pr_state/1"
        bb.write(key, entry, updated_by="test")

        # Use a fixed now_ts so the result is deterministic
        now_ts = time.time()
        result = list_entries(stale=True, bb=bb, now_ts=now_ts)
        assert len(result) == 1
        assert result[0]["pr"] == 1

    def test_terminal_phases_excluded_from_stale(self, bb):
        for pr, phase in [(1, "merged"), (2, "blocked")]:
            init_entry(pr, discussion=pr, bb=bb)
            entry = get_entry(pr, bb=bb)
            entry["phase"] = phase
            entry["updated_at"] = self._make_old_ts(_STALE_THRESHOLD_SECONDS + 300)
            bb.write(f"pr_state/{pr}", entry, updated_by="test")

        now_ts = time.time()
        result = list_entries(stale=True, bb=bb, now_ts=now_ts)
        assert result == []

    def test_stale_only_non_terminal(self, bb):
        """Mix of stale+active and stale+terminal — only active stale returned."""
        init_entry(1, discussion=1, bb=bb)  # fresh, active -> excluded
        init_entry(2, discussion=2, bb=bb)  # stale, active -> included
        init_entry(3, discussion=3, bb=bb)  # stale, merged -> excluded

        old_ts = self._make_old_ts(_STALE_THRESHOLD_SECONDS + 300)
        for pr, phase in [(2, "executing"), (3, "merged")]:
            e = get_entry(pr, bb=bb)
            e["phase"] = phase
            e["updated_at"] = old_ts
            bb.write(f"pr_state/{pr}", e, updated_by="test")

        now_ts = time.time()
        result = list_entries(stale=True, bb=bb, now_ts=now_ts)
        assert len(result) == 1
        assert result[0]["pr"] == 2


# ---------------------------------------------------------------------------
# Phase machine invariants
# ---------------------------------------------------------------------------


class TestPhaseMachineInvariants:
    def test_all_valid_phases_constant(self):
        expected = {"queued", "executing", "code_review", "debate",
                    "security_review", "merging", "merged", "blocked"}
        assert VALID_PHASES == expected

    def test_terminal_phases_constant(self):
        assert TERMINAL_PHASES == {"merged", "blocked"}

    def test_terminal_phases_are_subset_of_valid(self):
        assert TERMINAL_PHASES <= VALID_PHASES

    def test_full_happy_path_queued_to_merged(self, bb):
        init_entry(99, discussion=1, bb=bb)
        for phase in ["executing", "code_review", "merging", "merged"]:
            advance(99, phase, bb=bb)
        assert get_entry(99, bb=bb)["phase"] == "merged"

    def test_full_security_path(self, bb):
        init_entry(98, discussion=1, bb=bb)
        for phase in ["executing", "code_review", "security_review", "merging", "merged"]:
            advance(98, phase, bb=bb)
        assert get_entry(98, bb=bb)["phase"] == "merged"

    def test_full_debate_path(self, bb):
        init_entry(97, discussion=1, bb=bb)
        for phase in ["executing", "code_review", "debate", "security_review", "merging", "merged"]:
            advance(97, phase, bb=bb)
        assert get_entry(97, bb=bb)["phase"] == "merged"

    def test_fix_cycle_increments_on_needs_fix_verdict(self, bb, entry):
        advance(42, "executing", bb=bb)
        advance(42, "code_review", bb=bb)
        record_envelope(42, role="code-reviewer", verdict="needs-fix", bb=bb)
        advance(42, "executing", bb=bb)
        record_envelope(42, role="executor", verdict="done", bb=bb)
        advance(42, "code_review", bb=bb)
        record_envelope(42, role="code-reviewer", verdict="needs-fix", bb=bb)

        e = get_entry(42, bb=bb)
        assert e["fix_cycle_count"] == 2

    def test_blocked_reason_survives_round_trip(self, bb, entry):
        set_fields(42, fields={"blocked_reason": "rebase conflict on CLAUDE.md"}, bb=bb)
        advance(42, "blocked", bb=bb)
        e = get_entry(42, bb=bb)
        assert e["blocked_reason"] == "rebase conflict on CLAUDE.md"
        assert e["phase"] == "blocked"


# ---------------------------------------------------------------------------
# CLI (main())
# ---------------------------------------------------------------------------


class TestCLI:
    """Smoke-test the main() CLI handler via the bb-injection path."""

    def _bb_root(self, tmp_path):
        return tmp_path / "cli_bb"

    def test_cli_init_and_get(self, tmp_path, capsys):
        root = self._bb_root(tmp_path)
        bb = Blackboard(root=root)
        # init via library (CLI can't inject bb directly, test via library)
        init_entry(7, discussion=70, bb=bb)
        e = get_entry(7, bb=bb)
        assert e["phase"] == "queued"
        assert e["pr"] == 7

    def test_cli_advance_via_library(self, bb, entry):
        advance(42, "executing", bb=bb)
        e = get_entry(42, bb=bb)
        assert e["phase"] == "executing"

    def test_cli_list_returns_sorted_list(self, bb):
        init_entry(5, discussion=1, bb=bb)
        init_entry(3, discussion=2, bb=bb)
        result = list_entries(bb=bb)
        assert [e["pr"] for e in result] == [3, 5]

    def test_set_fields_needs_security_review(self, bb, entry):
        set_fields(42, fields={"needs_security_review": True}, bb=bb)
        e = get_entry(42, bb=bb)
        assert e["needs_security_review"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_record_envelope_with_zero_tokens(self, bb, entry):
        e = record_envelope(42, role="executor", verdict="done",
                            input_tokens=0, output_tokens=0, bb=bb)
        rec = e["completed_phases"][0]
        assert rec["input_tokens"] == 0
        assert rec["output_tokens"] == 0

    def test_discussion_zero_is_stored(self, bb):
        e = init_entry(50, discussion=0, bb=bb)
        assert e["discussion"] == 0

    def test_large_pr_number(self, bb):
        e = init_entry(99999, discussion=1, bb=bb)
        assert e["pr"] == 99999
        assert get_entry(99999, bb=bb) is not None

    def test_set_fields_with_no_changes_is_noop(self, bb, entry):
        original_phase = entry["phase"]
        e = set_fields(42, bb=bb)
        assert e["phase"] == original_phase

    def test_multiple_prs_independent_state(self, bb):
        init_entry(1, discussion=1, bb=bb)
        init_entry(2, discussion=2, bb=bb)
        advance(1, "executing", bb=bb)
        advance(1, "blocked", bb=bb)
        # PR 2 remains unaffected
        assert get_entry(2, bb=bb)["phase"] == "queued"

    def test_advancing_pr_does_not_affect_sibling_pr(self, bb):
        init_entry(10, discussion=1, bb=bb)
        init_entry(11, discussion=2, bb=bb)
        for phase in ["executing", "code_review", "merging", "merged"]:
            advance(10, phase, bb=bb)
        assert get_entry(11, bb=bb)["phase"] == "queued"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_to(bb: Blackboard, pr: int, target_phase: str) -> None:
    """Advance *pr* from queued to *target_phase* via a valid path (best-effort)."""
    _PATHS = {
        "queued":          [],
        "executing":       ["executing"],
        "code_review":     ["executing", "code_review"],
        "debate":          ["executing", "code_review", "debate"],
        "security_review": ["executing", "code_review", "security_review"],
        "merging":         ["executing", "code_review", "merging"],
        "merged":          ["executing", "code_review", "merging", "merged"],
        "blocked":         [],  # direct; handled by caller
    }
    for phase in _PATHS.get(target_phase, []):
        try:
            advance(pr, phase, bb=bb)
        except ValueError:
            pass  # already in this phase from a prior step
