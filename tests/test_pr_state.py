"""
Tests for backend/pr_state.py — PR lifecycle state machine.

All tests use an isolated Blackboard backed by tmp_path so the real
.autonomous-team/blackboard/ directory is never touched.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
from backend.pr_state import (
    TERMINAL_PHASES,
    VALID_PHASES,
    advance,
    get_entry,
    init_entry,
    list_entries,
    main,
    record_envelope,
    set_fields,
    _STALE_THRESHOLD_SECONDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bb(tmp_path):
    """Isolated Blackboard for each test."""
    return Blackboard(root=tmp_path / "blackboard")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_entry_with_queued_phase(self, bb):
        entry = init_entry(101, discussion=559, bb=bb)
        assert entry["pr"] == 101
        assert entry["discussion"] == 559
        assert entry["phase"] == "queued"

    def test_creates_entry_with_empty_history(self, bb):
        entry = init_entry(102, discussion=559, bb=bb)
        assert entry["spawned_phases"] == []
        assert entry["completed_phases"] == []
        assert entry["last_envelope"] == {}

    def test_creates_entry_with_zero_counters(self, bb):
        entry = init_entry(103, discussion=559, bb=bb)
        assert entry["fix_cycle_count"] == 0
        assert entry["respawn_count"] == 0

    def test_creates_entry_with_timestamps(self, bb):
        entry = init_entry(104, discussion=559, bb=bb)
        assert entry["created_at"]
        assert entry["updated_at"]

    def test_raises_if_already_exists(self, bb):
        init_entry(105, discussion=559, bb=bb)
        with pytest.raises(ValueError, match="already exists"):
            init_entry(105, discussion=559, bb=bb)

    def test_get_returns_created_entry(self, bb):
        init_entry(106, discussion=559, bb=bb)
        entry = get_entry(106, bb=bb)
        assert entry is not None
        assert entry["pr"] == 106

    def test_get_returns_none_for_missing(self, bb):
        assert get_entry(9999, bb=bb) is None


# ---------------------------------------------------------------------------
# advance — valid transitions
# ---------------------------------------------------------------------------

class TestAdvanceValid:
    def test_queued_to_executing(self, bb):
        init_entry(200, discussion=1, bb=bb)
        entry = advance(200, "executing", bb=bb)
        assert entry["phase"] == "executing"

    def test_executing_to_code_review(self, bb):
        init_entry(201, discussion=1, bb=bb)
        advance(201, "executing", bb=bb)
        entry = advance(201, "code_review", bb=bb)
        assert entry["phase"] == "code_review"

    def test_code_review_to_merging(self, bb):
        init_entry(202, discussion=1, bb=bb)
        advance(202, "executing", bb=bb)
        advance(202, "code_review", bb=bb)
        entry = advance(202, "merging", bb=bb)
        assert entry["phase"] == "merging"

    def test_merging_to_merged(self, bb):
        init_entry(203, discussion=1, bb=bb)
        advance(203, "executing", bb=bb)
        advance(203, "code_review", bb=bb)
        advance(203, "merging", bb=bb)
        entry = advance(203, "merged", bb=bb)
        assert entry["phase"] == "merged"

    def test_code_review_to_security_review(self, bb):
        init_entry(204, discussion=1, bb=bb)
        advance(204, "executing", bb=bb)
        advance(204, "code_review", bb=bb)
        entry = advance(204, "security_review", bb=bb)
        assert entry["phase"] == "security_review"

    def test_security_review_to_merging(self, bb):
        init_entry(205, discussion=1, bb=bb)
        advance(205, "executing", bb=bb)
        advance(205, "code_review", bb=bb)
        advance(205, "security_review", bb=bb)
        entry = advance(205, "merging", bb=bb)
        assert entry["phase"] == "merging"

    def test_fix_cycle_code_review_back_to_executing(self, bb):
        """needs-fix: code_review can transition back to executing."""
        init_entry(206, discussion=1, bb=bb)
        advance(206, "executing", bb=bb)
        advance(206, "code_review", bb=bb)
        entry = advance(206, "executing", bb=bb)
        assert entry["phase"] == "executing"

    def test_any_phase_to_blocked(self, bb):
        for pr, from_phase in enumerate(
            ["queued", "executing", "code_review", "security_review", "merging"],
            start=300,
        ):
            init_entry(pr, discussion=1, bb=bb)
            # Advance to desired from_phase
            transitions = {
                "queued": [],
                "executing": ["executing"],
                "code_review": ["executing", "code_review"],
                "security_review": ["executing", "code_review", "security_review"],
                "merging": ["executing", "code_review", "merging"],
            }
            for t in transitions[from_phase]:
                advance(pr, t, bb=bb)
            entry = advance(pr, "blocked", bb=bb)
            assert entry["phase"] == "blocked", f"PR {pr} from {from_phase}"


# ---------------------------------------------------------------------------
# advance — invalid transitions
# ---------------------------------------------------------------------------

class TestAdvanceInvalid:
    def test_cannot_go_from_merged_to_executing(self, bb):
        init_entry(400, discussion=1, bb=bb)
        advance(400, "executing", bb=bb)
        advance(400, "code_review", bb=bb)
        advance(400, "merging", bb=bb)
        advance(400, "merged", bb=bb)
        with pytest.raises(ValueError, match="Invalid transition"):
            advance(400, "executing", bb=bb)

    def test_cannot_go_from_blocked_to_executing(self, bb):
        init_entry(401, discussion=1, bb=bb)
        advance(401, "blocked", bb=bb)
        with pytest.raises(ValueError, match="Invalid transition"):
            advance(401, "executing", bb=bb)

    def test_cannot_skip_executing_from_queued(self, bb):
        init_entry(402, discussion=1, bb=bb)
        with pytest.raises(ValueError, match="Invalid transition"):
            advance(402, "code_review", bb=bb)

    def test_cannot_skip_code_review(self, bb):
        init_entry(403, discussion=1, bb=bb)
        advance(403, "executing", bb=bb)
        with pytest.raises(ValueError, match="Invalid transition"):
            advance(403, "merged", bb=bb)

    def test_unknown_phase_raises(self, bb):
        init_entry(404, discussion=1, bb=bb)
        with pytest.raises(ValueError, match="Unknown phase"):
            advance(404, "not_a_phase", bb=bb)

    def test_missing_entry_raises(self, bb):
        with pytest.raises(ValueError, match="No pr_state entry"):
            advance(9998, "executing", bb=bb)

    def test_cli_exits_1_on_invalid_transition(self, bb, tmp_path):
        """CLI exits 1 with error message on invalid transition."""
        init_entry(405, discussion=1, bb=bb)
        advance(405, "executing", bb=bb)
        advance(405, "code_review", bb=bb)
        advance(405, "merging", bb=bb)
        advance(405, "merged", bb=bb)

        import backend.pr_state as ps
        original = ps._get_bb
        ps._get_bb = lambda: bb
        try:
            rc = main(["advance", "405", "--to", "executing"])
        finally:
            ps._get_bb = original
        assert rc == 1


# ---------------------------------------------------------------------------
# record-envelope
# ---------------------------------------------------------------------------

class TestRecordEnvelope:
    def test_appends_to_completed_phases(self, bb):
        init_entry(500, discussion=1, bb=bb)
        advance(500, "executing", bb=bb)
        entry = record_envelope(500, role="executor", verdict="done", bb=bb)
        assert len(entry["completed_phases"]) == 1
        assert entry["completed_phases"][0]["role"] == "executor"
        assert entry["completed_phases"][0]["verdict"] == "done"

    def test_multiple_appends(self, bb):
        init_entry(501, discussion=1, bb=bb)
        advance(501, "executing", bb=bb)
        record_envelope(501, role="executor", verdict="done", bb=bb)
        advance(501, "code_review", bb=bb)
        record_envelope(501, role="code-reviewer", verdict="pass", bb=bb)
        entry = get_entry(501, bb=bb)
        assert len(entry["completed_phases"]) == 2

    def test_updates_last_envelope(self, bb):
        init_entry(502, discussion=1, bb=bb)
        advance(502, "executing", bb=bb)
        entry = record_envelope(502, role="executor", verdict="done",
                                 input_tokens=10000, output_tokens=2000, bb=bb)
        assert entry["last_envelope"]["role"] == "executor"
        assert entry["last_envelope"]["input_tokens"] == 10000
        assert entry["last_envelope"]["output_tokens"] == 2000

    def test_needs_fix_increments_fix_cycle_count(self, bb):
        init_entry(503, discussion=1, bb=bb)
        advance(503, "executing", bb=bb)
        advance(503, "code_review", bb=bb)
        entry = record_envelope(503, role="code-reviewer", verdict="needs-fix", bb=bb)
        assert entry["fix_cycle_count"] == 1

    def test_pass_does_not_increment_fix_cycle_count(self, bb):
        init_entry(504, discussion=1, bb=bb)
        advance(504, "executing", bb=bb)
        record_envelope(504, role="executor", verdict="done", bb=bb)
        entry = get_entry(504, bb=bb)
        assert entry["fix_cycle_count"] == 0

    def test_records_event_id(self, bb):
        init_entry(505, discussion=1, bb=bb)
        advance(505, "executing", bb=bb)
        entry = record_envelope(505, role="executor", verdict="done",
                                 event_id="executor-505-1234", bb=bb)
        assert entry["completed_phases"][0]["event_id"] == "executor-505-1234"

    def test_missing_entry_raises(self, bb):
        with pytest.raises(ValueError, match="No pr_state entry"):
            record_envelope(9997, role="executor", verdict="done", bb=bb)


# ---------------------------------------------------------------------------
# list — filters
# ---------------------------------------------------------------------------

class TestListFilters:
    def _populate(self, bb):
        """Create a set of entries in different phases."""
        init_entry(600, discussion=1, bb=bb)  # queued
        init_entry(601, discussion=1, bb=bb)
        advance(601, "executing", bb=bb)      # executing
        init_entry(602, discussion=1, bb=bb)
        advance(602, "executing", bb=bb)
        advance(602, "code_review", bb=bb)    # code_review
        init_entry(603, discussion=1, bb=bb)
        advance(603, "blocked", bb=bb)        # blocked

    def test_list_all(self, bb):
        self._populate(bb)
        entries = list_entries(bb=bb)
        assert len(entries) == 4

    def test_list_by_phase_executing(self, bb):
        self._populate(bb)
        entries = list_entries(phase="executing", bb=bb)
        assert len(entries) == 1
        assert entries[0]["pr"] == 601

    def test_list_by_phase_queued(self, bb):
        self._populate(bb)
        entries = list_entries(phase="queued", bb=bb)
        assert len(entries) == 1
        assert entries[0]["pr"] == 600

    def test_list_blocked(self, bb):
        self._populate(bb)
        entries = list_entries(blocked=True, bb=bb)
        assert len(entries) == 1
        assert entries[0]["pr"] == 603

    def test_list_empty_when_no_match(self, bb):
        self._populate(bb)
        entries = list_entries(phase="merging", bb=bb)
        assert entries == []

    def test_list_sorted_by_pr(self, bb):
        self._populate(bb)
        entries = list_entries(bb=bb)
        prs = [e["pr"] for e in entries]
        assert prs == sorted(prs)


# ---------------------------------------------------------------------------
# list --stale
# ---------------------------------------------------------------------------

class TestStaleDetection:
    def test_fresh_entry_not_stale(self, bb):
        init_entry(700, discussion=1, bb=bb)
        now = time.time()
        # Entry was just created — should not be stale
        entries = list_entries(stale=True, bb=bb, now_ts=now)
        assert len(entries) == 0

    def test_entry_at_59min_not_stale(self, bb):
        init_entry(701, discussion=1, bb=bb)
        now = time.time() + (59 * 60)  # simulate "now" is 59 min later
        entries = list_entries(stale=True, bb=bb, now_ts=now)
        assert len(entries) == 0

    def test_entry_at_61min_is_stale(self, bb):
        init_entry(702, discussion=1, bb=bb)
        now = time.time() + (61 * 60)  # simulate "now" is 61 min later
        entries = list_entries(stale=True, bb=bb, now_ts=now)
        assert len(entries) == 1
        assert entries[0]["pr"] == 702

    def test_exactly_60min_boundary_not_stale(self, bb):
        """An entry updated_at <= 59 minutes ago is not stale."""
        init_entry(703, discussion=1, bb=bb)
        # now_ts is 59 min after the entry was written — definitely not stale
        now = time.time() + (59 * 60)
        entries = list_entries(stale=True, bb=bb, now_ts=now)
        assert len(entries) == 0

    def test_merged_not_returned_as_stale(self, bb):
        init_entry(704, discussion=1, bb=bb)
        advance(704, "executing", bb=bb)
        advance(704, "code_review", bb=bb)
        advance(704, "merging", bb=bb)
        advance(704, "merged", bb=bb)
        now = time.time() + (90 * 60)
        entries = list_entries(stale=True, bb=bb, now_ts=now)
        assert all(e["pr"] != 704 for e in entries)

    def test_blocked_not_returned_as_stale(self, bb):
        init_entry(705, discussion=1, bb=bb)
        advance(705, "blocked", bb=bb)
        now = time.time() + (90 * 60)
        entries = list_entries(stale=True, bb=bb, now_ts=now)
        assert all(e["pr"] != 705 for e in entries)

    def test_multiple_stale_entries(self, bb):
        for pr in [710, 711, 712]:
            init_entry(pr, discussion=1, bb=bb)
        # 713 is recent
        init_entry(713, discussion=1, bb=bb)
        old_now = time.time() + (90 * 60)
        entries = list_entries(stale=True, bb=bb, now_ts=old_now)
        stale_prs = {e["pr"] for e in entries}
        assert {710, 711, 712, 713} == stale_prs


# ---------------------------------------------------------------------------
# CLI smoke tests (using monkeypatching to inject isolated bb)
# ---------------------------------------------------------------------------

class TestCLI:
    @pytest.fixture(autouse=True)
    def patch_bb(self, bb, monkeypatch):
        import backend.pr_state as ps
        monkeypatch.setattr(ps, "_get_bb", lambda: bb)

    def test_cli_init(self, capsys):
        rc = main(["init", "800", "--discussion", "559"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["pr"] == 800
        assert data["phase"] == "queued"

    def test_cli_get(self, capsys, bb):
        init_entry(801, discussion=559, bb=bb)
        rc = main(["get", "801"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["pr"] == 801

    def test_cli_get_missing_returns_null(self, capsys):
        rc = main(["get", "9990"])
        assert rc == 0
        out = capsys.readouterr().out
        assert json.loads(out) is None

    def test_cli_advance(self, capsys, bb):
        init_entry(802, discussion=1, bb=bb)
        rc = main(["advance", "802", "--to", "executing"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["phase"] == "executing"

    def test_cli_advance_invalid_exits_1(self, capsys, bb):
        init_entry(803, discussion=1, bb=bb)
        # queued -> code_review is not allowed
        rc = main(["advance", "803", "--to", "code_review"])
        assert rc == 1

    def test_cli_list(self, capsys, bb):
        init_entry(804, discussion=1, bb=bb)
        init_entry(805, discussion=1, bb=bb)
        rc = main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        prs = [e["pr"] for e in data]
        assert 804 in prs
        assert 805 in prs

    def test_cli_list_phase_filter(self, capsys, bb):
        init_entry(806, discussion=1, bb=bb)
        advance(806, "executing", bb=bb)
        rc = main(["list", "--phase", "executing"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert all(e["phase"] == "executing" for e in data)

    def test_cli_record_envelope(self, capsys, bb):
        init_entry(807, discussion=1, bb=bb)
        advance(807, "executing", bb=bb)
        rc = main(["record-envelope", "807", "--role", "executor",
                   "--verdict", "done", "--input-tokens", "50000", "--output-tokens", "8000"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["last_envelope"]["role"] == "executor"
        assert data["last_envelope"]["input_tokens"] == 50000
