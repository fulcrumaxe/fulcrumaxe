"""tests/test_classify_gate_check_skipped.py

Tests for classify_gate_check_skipped (backend/classifiers/gate_check_skipped.py).

ACs from D#655 PR-a:
  AC1 — negative: transcript with security gate before merge → no findings
  AC2 — positive: merge without security gate → ≥1 finding, severity=high
  AC3 — run_analyst._PHASE_A8_CLASSIFIERS includes classify_gate_check_skipped
  AC4 — real team-lead-iteration.sh auto-merge flow does NOT fire
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

from backend.classifiers.gate_check_skipped import classify_gate_check_skipped  # noqa: E402
import run_analyst  # noqa: E402

Finding = run_analyst.Finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockTurn:
    """Minimal duck-type for TranscriptTurn."""

    def __init__(
        self,
        turn_idx: int,
        role: str = "assistant",
        text: str = "",
        agent_id: str = "",
        tool_calls: list | None = None,
    ):
        self.turn_idx = turn_idx
        self.role = role
        self.text = text
        self.agent_id = agent_id
        self.tool_calls: list = tool_calls or []
        self.tool_results: list = []


def _bash(cmd: str, turn_idx: int = 1, agent_id: str = "impl-coordinator-abc") -> _MockTurn:
    return _MockTurn(
        turn_idx=turn_idx,
        role="assistant",
        agent_id=agent_id,
        tool_calls=[{"name": "Bash", "input": {"command": cmd}}],
    )


def _user_turn(agent_id: str = "impl-coordinator-abc") -> _MockTurn:
    return _MockTurn(turn_idx=0, role="user", agent_id=agent_id)


def _run(turns: list) -> list:
    return classify_gate_check_skipped(iter(turns))


# ---------------------------------------------------------------------------
# AC1 — negative: security gate before merge → no findings
# ---------------------------------------------------------------------------

def test_security_gate_before_merge_no_finding():
    """security-review-passed check before gh pr merge → no findings."""
    turns = [
        _user_turn(),
        _bash(
            "gh pr view 645 --json labels --jq '[.labels[].name]' | grep security-review-passed",
            turn_idx=1,
        ),
        _bash("gh pr merge 645 --squash --auto", turn_idx=2),
    ]
    findings = _run(turns)
    assert findings == [], f"expected no findings, got {findings}"


def test_security_trigger_label_before_merge_no_finding():
    """security-trigger label apply before merge → no findings."""
    turns = [
        _user_turn(),
        _bash(
            "gh api repos/autonomous-agent-7/autonomous-forever/issues/645/labels "
            "-f name='security-review-triggered'",
            turn_idx=1,
        ),
        _bash("gh pr merge 645 --squash", turn_idx=2),
    ]
    findings = _run(turns)
    assert findings == [], f"expected no findings, got {findings}"


def test_no_merge_no_finding():
    """Transcript with no gh pr merge → no findings."""
    turns = [
        _user_turn(),
        _bash("gh pr view 645 --json labels", turn_idx=1),
    ]
    findings = _run(turns)
    assert findings == []


def test_empty_transcript_no_finding():
    """Empty transcript → no findings, no crash."""
    assert _run([]) == []


# ---------------------------------------------------------------------------
# AC2 — positive: merge without security gate → ≥1 finding, severity=high
# ---------------------------------------------------------------------------

def test_merge_without_gate_fires():
    """gh pr merge with no preceding security gate → finding with severity=high."""
    turns = [
        _user_turn(),
        _bash("gh pr view 645 --json state", turn_idx=1),
        _bash("gh pr merge 645 --squash --auto", turn_idx=2),
    ]
    findings = _run(turns)
    assert len(findings) >= 1, f"expected ≥1 finding, got {findings}"
    f = findings[0]
    assert f.severity == "high", f"expected severity=high, got {f.severity!r}"
    assert f.classifier == "gate_check_skipped"
    assert "merge" in f.detail.lower() or "security" in f.detail.lower()


def test_merge_fires_once_early_exit():
    """Multiple merges without gate still returns 1 finding (early-exit guard)."""
    turns = [
        _user_turn(),
        _bash("gh pr merge 645 --squash", turn_idx=1),
        _bash("gh pr merge 650 --squash", turn_idx=2),
    ]
    findings = _run(turns)
    assert len(findings) == 1, f"expected exactly 1 finding (early-exit), got {len(findings)}"


def test_gate_after_merge_still_fires():
    """Security gate that appears AFTER merge doesn't retroactively prevent the finding."""
    turns = [
        _user_turn(),
        _bash("gh pr merge 645 --squash", turn_idx=1),
        _bash(
            "gh api repos/.../labels -f name='security-review-passed'",
            turn_idx=2,
        ),
    ]
    findings = _run(turns)
    assert len(findings) >= 1, "gate after merge should still trigger a finding"


# ---------------------------------------------------------------------------
# AC3 — registration in _PHASE_A8_CLASSIFIERS
# ---------------------------------------------------------------------------

def test_registered_in_phase_a8_classifiers():
    """classify_gate_check_skipped must appear in run_analyst._PHASE_A8_CLASSIFIERS."""
    assert classify_gate_check_skipped in run_analyst._PHASE_A8_CLASSIFIERS, (
        "classify_gate_check_skipped not found in _PHASE_A8_CLASSIFIERS"
    )


# ---------------------------------------------------------------------------
# AC4 — real team-lead-iteration.sh auto-merge flow must NOT fire
# ---------------------------------------------------------------------------

def test_team_lead_auto_merge_no_false_positive():
    """Simulate the real team-lead-iteration.sh auto-merge loop.

    The script does a double-check label fetch (`gh pr view N --json labels`)
    before every merge.  This MUST satisfy the gate so legitimate non-security
    auto-merges are not flagged.

    Mirrors scripts/team-lead-iteration.sh lines ~465-480.
    """
    turns = [
        _user_turn(agent_id="team-lead-abc"),
        # Step: fetch open PRs to decide merge candidates
        _bash(
            "gh pr list --state open --json number,title,labels "
            "--repo autonomous-agent-7/autonomous-forever",
            turn_idx=1,
            agent_id="team-lead-abc",
        ),
        # Step: double-check labels before merge (the real gate check)
        _bash(
            "gh pr view 672 --repo autonomous-agent-7/autonomous-forever "
            "--json labels --jq '[.labels[].name]'",
            turn_idx=2,
            agent_id="team-lead-abc",
        ),
        # Step: merge
        _bash(
            "gh pr merge 672 --squash --delete-branch "
            "--repo autonomous-agent-7/autonomous-forever",
            turn_idx=3,
            agent_id="team-lead-abc",
        ),
    ]
    findings = _run(turns)
    assert findings == [], (
        f"team-lead auto-merge with --json labels fetch should not fire "
        f"gate_check_skipped, got {findings}"
    )
