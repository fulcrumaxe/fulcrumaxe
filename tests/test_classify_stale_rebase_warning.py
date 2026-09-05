"""tests/test_classify_stale_rebase_warning.py

Tests for classify_stale_rebase_warning (backend/classifiers/stale_rebase_warning.py).

ACs from D#655 PR-a:
  AC1 — negative: transcript with git rebase before push → no findings
  AC2 — positive: transcript with git push but no rebase → ≥1 finding, severity=high
  AC3 — run_analyst._PHASE_A8_CLASSIFIERS includes classify_stale_rebase_warning
  AC4 — false-positive exclusions: branch deletion and tag pushes do not fire
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

from backend.classifiers.stale_rebase_warning import classify_stale_rebase_warning  # noqa: E402
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


def _bash(cmd: str, turn_idx: int = 1, agent_id: str = "executor-abc") -> _MockTurn:
    turn = _MockTurn(
        turn_idx=turn_idx,
        role="assistant",
        agent_id=agent_id,
        tool_calls=[{"name": "Bash", "input": {"command": cmd}}],
    )
    return turn


def _user_turn(agent_id: str = "executor-abc") -> _MockTurn:
    return _MockTurn(turn_idx=0, role="user", agent_id=agent_id)


def _run(turns: list) -> list:
    return classify_stale_rebase_warning(iter(turns))


# ---------------------------------------------------------------------------
# AC1 — negative: rebase before push → no findings
# ---------------------------------------------------------------------------

def test_rebase_before_push_no_finding():
    """git rebase origin/main followed by git push → no findings."""
    turns = [
        _user_turn(),
        _bash("git fetch origin && git rebase origin/main", turn_idx=1),
        _bash("git push -u origin HEAD", turn_idx=2),
    ]
    findings = _run(turns)
    assert findings == [], f"expected no findings, got {findings}"


def test_pull_rebase_before_push_no_finding():
    """git pull --rebase followed by git push → no findings."""
    turns = [
        _user_turn(),
        _bash("git pull --rebase origin main", turn_idx=1),
        _bash("git push -u origin HEAD", turn_idx=2),
    ]
    findings = _run(turns)
    assert findings == [], f"expected no findings, got {findings}"


def test_no_push_no_finding():
    """Transcript with no git push at all → no findings."""
    turns = [
        _user_turn(),
        _bash("git rebase origin/main", turn_idx=1),
        _bash("pytest tests/", turn_idx=2),
    ]
    findings = _run(turns)
    assert findings == []


def test_empty_transcript_no_finding():
    """Empty transcript → no findings, no crash."""
    assert _run([]) == []


# ---------------------------------------------------------------------------
# AC2 — positive: push without rebase → ≥1 finding, severity=high
# ---------------------------------------------------------------------------

def test_push_without_rebase_fires():
    """git push with no prior rebase → finding with severity=high."""
    turns = [
        _user_turn(),
        _bash("git add backend/foo.py", turn_idx=1),
        _bash("git commit -m 'add foo'", turn_idx=2),
        _bash("git push -u origin HEAD", turn_idx=3),
    ]
    findings = _run(turns)
    assert len(findings) >= 1, f"expected ≥1 finding, got {findings}"
    f = findings[0]
    assert f.severity == "high", f"expected severity=high, got {f.severity!r}"
    assert f.classifier == "stale_rebase_warning"
    assert "git push" in f.detail.lower() or "rebase" in f.detail.lower()


def test_push_only_fires_once():
    """Multiple pushes without rebase still returns exactly 1 finding (early-exit)."""
    turns = [
        _user_turn(),
        _bash("git push origin HEAD", turn_idx=1),
        _bash("git push -f origin HEAD", turn_idx=2),
    ]
    findings = _run(turns)
    assert len(findings) == 1, f"expected exactly 1 finding (early-exit), got {len(findings)}"


# ---------------------------------------------------------------------------
# AC3 — registration in _PHASE_A8_CLASSIFIERS
# ---------------------------------------------------------------------------

def test_registered_in_phase_a8_classifiers():
    """classify_stale_rebase_warning must appear in run_analyst._PHASE_A8_CLASSIFIERS."""
    assert classify_stale_rebase_warning in run_analyst._PHASE_A8_CLASSIFIERS, (
        "classify_stale_rebase_warning not found in _PHASE_A8_CLASSIFIERS"
    )


# ---------------------------------------------------------------------------
# AC4 — false-positive exclusions
# ---------------------------------------------------------------------------

def test_branch_delete_push_no_finding():
    """git push origin :old-branch (branch deletion form) must NOT fire."""
    turns = [
        _user_turn(),
        _bash("git push origin :old-feature-branch", turn_idx=1),
    ]
    findings = _run(turns)
    assert findings == [], (
        f"branch deletion push should not fire stale_rebase_warning, got {findings}"
    )


def test_tag_push_no_finding():
    """git push origin v1.2 (tag push) must NOT fire."""
    turns = [
        _user_turn(),
        _bash("git push origin v1.2.0", turn_idx=1),
    ]
    findings = _run(turns)
    assert findings == [], (
        f"tag push should not fire stale_rebase_warning, got {findings}"
    )


def test_push_tags_flag_no_finding():
    """git push --tags must NOT fire."""
    turns = [
        _user_turn(),
        _bash("git push origin --tags", turn_idx=1),
    ]
    findings = _run(turns)
    assert findings == [], (
        f"--tags push should not fire stale_rebase_warning, got {findings}"
    )
