"""tests/test_classify_spawn_injection.py

Tests for classify_spawn_injection in backend/classifiers/spawn_injection_audit.py.

Covers all 9 acceptance criteria from D#615:
  AC1  — all 4 markers present     → no findings
  AC2  — single marker missing      → exactly 1 finding, severity=medium
  AC3  — all 4 markers missing      → exactly 4 findings
  AC4  — mechanical role            → no findings regardless of content
  AC5  — no user-role turn          → no findings, no crash
  AC6  — run_analyst integration    → classifier is registered in _PHASE_A3_CLASSIFIERS
  AC7  — severity=medium fires bug-filer threshold (covered via AC2/AC3 severity checks)
  AC8  — run_analyst.py diff is registration-only (structural, verified via import test)
  AC9  — module under 120 lines
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow imports from repo root and backend/
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

from backend.classifiers.spawn_injection_audit import (  # noqa: E402
    classify_spawn_injection,
    EXPECTED_MARKERS,
    MECHANICAL_ROLES,
)
import run_analyst  # noqa: E402

Finding = run_analyst.Finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_MARKERS_PROMPT = "\n".join(EXPECTED_MARKERS) + "\nsome other content"

_NO_MARKERS_PROMPT = "just a plain spawn prompt with no injection blocks"


class _MockTurn:
    """Minimal duck-type for TranscriptTurn."""

    def __init__(
        self,
        turn_idx: int,
        role: str,
        text: str = "",
        agent_id: str = "",
    ):
        self.turn_idx = turn_idx
        self.role = role
        self.text = text
        self.agent_id = agent_id
        self.tool_calls: list[dict] = []
        self.tool_results: list[dict] = []


def _user_turn(text: str, agent_id: str = "executor-abc123", turn_idx: int = 0) -> _MockTurn:
    return _MockTurn(turn_idx=turn_idx, role="user", text=text, agent_id=agent_id)


def _run(turns: list[_MockTurn]) -> list[Finding]:
    return classify_spawn_injection(iter(turns))


# ---------------------------------------------------------------------------
# AC1 — all markers present → no findings
# ---------------------------------------------------------------------------

def test_all_markers_present_returns_empty():
    """Transcript whose first user turn contains all 4 markers → []."""
    turns = [_user_turn(_ALL_MARKERS_PROMPT)]
    findings = _run(turns)
    assert findings == [], f"expected no findings, got {findings}"


# ---------------------------------------------------------------------------
# AC2 — single marker missing → exactly 1 finding, severity=medium
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing_marker", EXPECTED_MARKERS)
def test_single_missing_marker(missing_marker: str):
    """Missing exactly one marker → 1 Finding, severity=medium, names the marker."""
    prompt = "\n".join(m for m in EXPECTED_MARKERS if m != missing_marker)
    turns = [_user_turn(prompt)]
    findings = _run(turns)
    assert len(findings) == 1, (
        f"expected 1 finding for missing {missing_marker!r}, got {len(findings)}"
    )
    f = findings[0]
    assert f.severity == "medium", f"expected severity=medium, got {f.severity!r}"
    assert f.classifier == "spawn_injection_audit"
    assert missing_marker in f.detail, (
        f"expected detail to mention {missing_marker!r}, got {f.detail!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — all 4 markers missing → exactly 4 findings
# ---------------------------------------------------------------------------

def test_all_markers_missing_returns_four_findings():
    """Transcript with no injection markers → 4 findings, one per marker."""
    turns = [_user_turn(_NO_MARKERS_PROMPT)]
    findings = _run(turns)
    assert len(findings) == 4, f"expected 4 findings, got {len(findings)}: {findings}"
    assert all(f.severity == "medium" for f in findings)
    assert all(f.classifier == "spawn_injection_audit" for f in findings)
    # Each expected marker should appear in exactly one finding's detail.
    for marker in EXPECTED_MARKERS:
        matched = [f for f in findings if marker in f.detail]
        assert len(matched) == 1, f"marker {marker!r} not found in exactly one finding"


# ---------------------------------------------------------------------------
# AC4 — mechanical role → no findings regardless of content
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mech_token", sorted(MECHANICAL_ROLES))
def test_mechanical_role_skipped(mech_token: str):
    """agent_id containing a MECHANICAL_ROLES token → [] regardless of prompt content."""
    agent_id = f"infra-{mech_token}-xyz"
    turns = [_user_turn(_NO_MARKERS_PROMPT, agent_id=agent_id)]
    findings = _run(turns)
    assert findings == [], (
        f"expected no findings for mechanical role {agent_id!r}, got {findings}"
    )


def test_mechanical_role_exact_match():
    """agent_id == 'reaper' (exact mechanical token) → no findings."""
    turns = [_user_turn(_NO_MARKERS_PROMPT, agent_id="reaper")]
    assert _run(turns) == []


# ---------------------------------------------------------------------------
# AC5 — no user-role turn → no findings, no crash
# ---------------------------------------------------------------------------

def test_no_user_turn_returns_empty():
    """Transcript with only assistant turns (no user turn) → [] with no exception."""
    turns = [
        _MockTurn(turn_idx=0, role="assistant", text="some assistant output"),
        _MockTurn(turn_idx=1, role="assistant", text="more output"),
    ]
    findings = _run(turns)
    assert findings == []


def test_empty_turn_list_returns_empty():
    """Empty transcript → []."""
    assert _run([]) == []


# ---------------------------------------------------------------------------
# AC6 — run_analyst._PHASE_A3_CLASSIFIERS includes classify_spawn_injection
# ---------------------------------------------------------------------------

def test_registered_in_phase_a3_classifiers():
    """classify_spawn_injection must appear in run_analyst._PHASE_A3_CLASSIFIERS."""
    assert classify_spawn_injection in run_analyst._PHASE_A3_CLASSIFIERS, (
        "classify_spawn_injection not found in _PHASE_A3_CLASSIFIERS"
    )


# ---------------------------------------------------------------------------
# AC7 — severity=medium is the bug-filer threshold (verified via finding attrs)
# ---------------------------------------------------------------------------

def test_finding_severity_is_medium_for_bug_filer():
    """Medium severity triggers D#609 analyst_bug_filer — verified by checking severity."""
    turns = [_user_turn(_NO_MARKERS_PROMPT)]
    findings = _run(turns)
    severities = {f.severity for f in findings}
    assert severities == {"medium"}, (
        f"expected all findings to be medium (bug-filer threshold), got {severities}"
    )


# ---------------------------------------------------------------------------
# AC8 — registration-only diff (structural: import resolves, list contains entry)
# ---------------------------------------------------------------------------

def test_run_analyst_import_is_registration_only():
    """The run_analyst import of classify_spawn_injection resolves to our module function."""
    imported_fn = getattr(run_analyst, "classify_spawn_injection", None)
    assert imported_fn is classify_spawn_injection, (
        "run_analyst.classify_spawn_injection should resolve to the same function object"
    )


# ---------------------------------------------------------------------------
# AC9 — module under 120 lines
# ---------------------------------------------------------------------------

def test_module_line_count_under_120():
    """spawn_injection_audit.py must be under 120 lines (per D#615 spec)."""
    module_path = _REPO / "backend" / "classifiers" / "spawn_injection_audit.py"
    lines = module_path.read_text().splitlines()
    assert len(lines) < 120, (
        f"spawn_injection_audit.py has {len(lines)} lines — spec cap is 120"
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_user_turn_not_first_turn():
    """First turn is system; user turn at index 1 is still found and checked."""
    sys_turn = _MockTurn(turn_idx=0, role="system", text="system context")
    usr_turn = _user_turn(_NO_MARKERS_PROMPT, turn_idx=1)
    findings = _run([sys_turn, usr_turn])
    assert len(findings) == 4


def test_only_second_user_turn_checked():
    """Only the FIRST user-role turn is inspected; later turns are ignored."""
    first_user = _user_turn(_ALL_MARKERS_PROMPT, turn_idx=0)
    # Second user turn (e.g. follow-up message) is missing markers — should not trigger.
    second_user = _user_turn(_NO_MARKERS_PROMPT, turn_idx=2)
    findings = _run([first_user, second_user])
    assert findings == [], (
        "should not fire when only the second user turn is missing markers"
    )


# ---------------------------------------------------------------------------
# Regression: turn-0 "Read /tmp/..." directives must not produce findings.
# These are the new-style spawn prompts where the agent receives a short
# "Read <file>" message at turn 0 and reads the actual briefing from a file.
# Checking these was the source of ~41 false-positive findings per day.
# ---------------------------------------------------------------------------

def test_turn0_read_directive_produces_no_findings():
    """Turn 0 with the standard Team Lead spawn prompt format ('Read /tmp/...')
    must produce zero findings.

    This is a regression guard: the classifier was firing for EVERY agent
    spawned with this pattern because the inline user message never contains
    the injection markers (they arrive via file content).
    """
    turns = [_user_turn("Read /tmp/p_exec_spawn_audit_fix.txt.", turn_idx=0)]
    findings = _run(turns)
    assert findings == [], (
        f"Expected 0 findings for 'Read /tmp/...' spawn prompt --- got {len(findings)}. "
        "Turn-0 file-read directives are fixed input, not signals of injection bypass."
    )


def test_turn0_read_directive_with_full_briefing_path_produces_no_findings():
    """Variant: 'Read /tmp/p_at_673.txt for your full briefing.' -> 0 findings."""
    turns = [_user_turn("Read /tmp/p_at_673.txt for your full briefing.", turn_idx=0)]
    findings = _run(turns)
    assert findings == [], (
        "Briefing-file read directive at turn 0 must not trigger the classifier."
    )
