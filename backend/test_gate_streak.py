"""
Tests for backend/gate_streak.py (D#2271 PR-a).

Each test writes an isolated JSONL fixture via tmp_path so nothing here
touches the real audit trail. Mirrors the fixture-writing convention in
backend/test_audit_trail_search.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend import gate_streak


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _stand_down(pr: int = 1) -> dict:
    return {"kind": "ci_gate_stood_down", "pr": pr, "head_sha": "deadbeef", "ts": "2026-09-01T00:00:00Z"}


def _verified(pr: int = 1) -> dict:
    return {"kind": "ci_gate_verified", "pr": pr, "head_sha": "deadbeef", "ts": "2026-09-01T00:00:00Z"}


def _block(pr: int = 1) -> dict:
    return {"kind": "ci_gate_block", "pr": pr, "head_sha": "deadbeef", "ts": "2026-09-01T00:00:00Z"}


# ---------------------------------------------------------------------------
# AC-2: fixture of stand-down rows and positive markers
# ---------------------------------------------------------------------------

class TestComputeStreak:
    def test_missing_file_is_zero(self, tmp_path: Path):
        assert gate_streak.compute_streak(tmp_path / "nope.jsonl") == 0

    def test_empty_file_is_zero(self, tmp_path: Path):
        p = tmp_path / "audit.jsonl"
        p.write_text("")
        assert gate_streak.compute_streak(p) == 0

    def test_five_stand_downs_no_marker_reports_five(self, tmp_path: Path):
        p = tmp_path / "audit.jsonl"
        _write(p, [_stand_down() for _ in range(5)])
        assert gate_streak.compute_streak(p) == 5

    def test_marker_resets_the_count(self, tmp_path: Path):
        p = tmp_path / "audit.jsonl"
        rows = [_stand_down() for _ in range(3)] + [_verified()] + [_stand_down() for _ in range(2)]
        _write(p, rows)
        assert gate_streak.compute_streak(p) == 2

    def test_all_verified_is_zero(self, tmp_path: Path):
        p = tmp_path / "audit.jsonl"
        _write(p, [_verified() for _ in range(4)])
        assert gate_streak.compute_streak(p) == 0

    def test_refusal_kind_neither_increments_nor_resets(self, tmp_path: Path):
        """D#2271 verification: ci_gate_block 'did gate — not in the class'."""
        p = tmp_path / "audit.jsonl"
        rows = [_stand_down(), _stand_down()] + [_block() for _ in range(20)] + [_stand_down()]
        _write(p, rows)
        # Only the three real stand-downs count; the 20 blocks are inert.
        assert gate_streak.compute_streak(p) == 3

    def test_rows_without_kind_are_ignored(self, tmp_path: Path):
        """Ordinary AuditTrail.emit rows (source/action/key schema) are a
        different family entirely and must not pollute this count."""
        p = tmp_path / "audit.jsonl"
        rows = [
            {"ts": "x", "source": "blackboard", "action": "write", "key": "k", "seq": 1},
            _stand_down(),
            {"ts": "x", "source": "control_plane", "action": "set", "key": "gates.foo", "seq": 2},
        ]
        _write(p, rows)
        assert gate_streak.compute_streak(p) == 1

    def test_malformed_lines_are_skipped(self, tmp_path: Path):
        p = tmp_path / "audit.jsonl"
        p.write_text("not json\n" + json.dumps(_stand_down()) + "\n\n")
        assert gate_streak.compute_streak(p) == 1

    # -----------------------------------------------------------------
    # AC-4: anti-rot — a novel, never-registered bypass kind still counts.
    # A denylist implementation fails this; this reader has no list to fail.
    # -----------------------------------------------------------------
    def test_novel_unregistered_kind_still_increments(self, tmp_path: Path):
        p = tmp_path / "audit.jsonl"
        _write(p, [{"kind": "zz_novel_bypass_20260903", "pr": 1, "ts": "x"}])
        assert gate_streak.compute_streak(p) == 1

    def test_fallback_kind_from_ci_note_merge_if_unverified_counts_too(self, tmp_path: Path):
        """The generic fallback marker (ci_gate_unverified_merge, written by
        scripts/lib/ci-status-check.sh's ci_note_merge_if_unverified when a
        merge proceeds with no other audit row) is itself just a non-positive
        kind — it needs no special-casing here."""
        p = tmp_path / "audit.jsonl"
        _write(p, [{"kind": "ci_gate_unverified_merge", "pr": 1, "ts": "x"}])
        assert gate_streak.compute_streak(p) == 1

    def test_module_source_never_names_the_forbidden_kinds(self):
        """AC-7: a grep of this module for the three bypass kind names it
        must never reference returns zero matches."""
        import inspect

        source = inspect.getsource(gate_streak)
        for forbidden in (
            "ci_gate_stood_down",
            "manual_merge_ci_bypass",
            "manual_merge_two_gate_bypass",
        ):
            assert forbidden not in source, f"{forbidden!r} must not appear in gate_streak.py"


# ---------------------------------------------------------------------------
# AC-5: tiered rendering
# ---------------------------------------------------------------------------

class TestRenderLine:
    def test_zero_is_none(self):
        assert gate_streak.render_line(0) is None

    def test_negative_is_none(self):
        assert gate_streak.render_line(-1) is None

    def test_one_and_forty_seven_differ_by_more_than_digits(self):
        line1 = gate_streak.render_line(1)
        line47 = gate_streak.render_line(47)
        assert line1 is not None and line47 is not None
        # Strip the digits out of each and confirm what's left still differs —
        # a pass/fail on a plain string comparison, per AC-5.
        stripped1 = "".join(c for c in line1 if not c.isdigit())
        stripped47 = "".join(c for c in line47 if not c.isdigit())
        assert stripped1 != stripped47, "streak=1 and streak=47 must read as different messages"

    def test_all_positive_streaks_render_something(self):
        for n in (1, 2, 9, 10, 24, 25, 100):
            assert gate_streak.render_line(n)


# ---------------------------------------------------------------------------
# _audit_path / current_streak test-seam honoring
# ---------------------------------------------------------------------------

class TestAuditPathTestSeam:
    def test_honors_ci_status_test_audit_file(self, tmp_path: Path, monkeypatch):
        fixture = tmp_path / "fixture.jsonl"
        _write(fixture, [_stand_down(), _stand_down()])
        monkeypatch.setenv("CI_STATUS_TEST_MODE", "1")
        monkeypatch.setenv("CI_STATUS_TEST_AUDIT_FILE", str(fixture))
        assert gate_streak.current_streak() == 2

    def test_ignores_test_audit_file_without_test_mode(self, tmp_path: Path, monkeypatch):
        fixture = tmp_path / "fixture.jsonl"
        _write(fixture, [_stand_down()])
        monkeypatch.delenv("CI_STATUS_TEST_MODE", raising=False)
        monkeypatch.setenv("CI_STATUS_TEST_AUDIT_FILE", str(fixture))
        # Without CI_STATUS_TEST_MODE=1 this must NOT read the fixture — it
        # falls through to state_paths.AUDIT_LOG, which requires a state
        # dir under pytest (see backend/state_paths.py's PYTEST_CURRENT_TEST
        # guard). Point it at a harmless scratch dir rather than asserting
        # on the real state dir.
        scratch = tmp_path / "state"
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(scratch))
        assert gate_streak.current_streak() == 0
