"""tests/test_classify_sleep_retry_loop.py

Tests for the classify_sleep_retry_loop classifier in backend/run_analyst.py
(Discussion #592 PR-b).

Covers:
  - `until ... sleep ... gh` pattern fires HIGH
  - `while ... sleep ... gh` pattern fires HIGH
  - `sleep N && gh` compound fires HIGH
  - Clean Bash commands do NOT fire
  - Non-Bash tool calls are ignored
  - Finding severity is "high"
  - Finding classifier name is "sleep_retry_loop"
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

# Allow imports from repo root and backend/
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import run_analyst  # noqa: E402

classify_sleep_retry_loop = run_analyst.classify_sleep_retry_loop
Finding = run_analyst.Finding


# ---------------------------------------------------------------------------
# Helpers to build minimal transcript turn fixtures
# ---------------------------------------------------------------------------

class _MockTurn:
    """Minimal stand-in for TranscriptTurn."""

    def __init__(self, turn_idx: int, tool_calls: list[dict], role: str = "assistant"):
        self.turn_idx = turn_idx
        self.tool_calls = tool_calls
        self.tool_results: list[dict] = []
        self.role = role
        self.text = ""


def _bash_turn(turn_idx: int, cmd: str) -> _MockTurn:
    return _MockTurn(
        turn_idx=turn_idx,
        tool_calls=[{"name": "Bash", "id": f"t{turn_idx}", "input": {"command": cmd}}],
    )


def _non_bash_turn(turn_idx: int, cmd: str) -> _MockTurn:
    """Simulate a non-Bash tool call (e.g. Edit or Write)."""
    return _MockTurn(
        turn_idx=turn_idx,
        tool_calls=[{"name": "Edit", "id": f"t{turn_idx}", "input": {"command": cmd}}],
    )


def _run(turns: list[_MockTurn]) -> list[Finding]:
    return classify_sleep_retry_loop(iter(turns))


# ---------------------------------------------------------------------------
# Tests — patterns that SHOULD fire
# ---------------------------------------------------------------------------

def test_until_sleep_gh_fires():
    """until ... sleep ... gh pattern should produce a HIGH finding."""
    cmd = "until gh pr create --title 'foo' --body 'bar'; do sleep 10; done"
    turns = [_bash_turn(0, cmd)]
    findings = _run(turns)
    assert len(findings) == 1, f"expected 1 finding, got {len(findings)}"
    assert findings[0].severity == "high"
    assert findings[0].classifier == "sleep_retry_loop"


def test_while_sleep_gh_fires():
    """while ... sleep ... gh pattern should produce a HIGH finding."""
    cmd = "while true; do sleep 30 && gh api -X POST repos/foo/bar/pulls; done"
    turns = [_bash_turn(1, cmd)]
    findings = _run(turns)
    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert findings[0].classifier == "sleep_retry_loop"


def test_sleep_and_gh_compound_fires():
    """sleep N && gh ... should be detected as a sleep-retry pattern."""
    cmd = "sleep 60 && gh pr create --head my-branch --base main --title 'retry'"
    turns = [_bash_turn(2, cmd)]
    findings = _run(turns)
    assert len(findings) == 1
    assert findings[0].severity == "high"


def test_sleep_semicolon_gh_fires():
    """sleep N; gh ... should be detected."""
    cmd = "sleep 300; gh api -X POST repos/foo/bar/issues/1/labels -f labels[]=done"
    turns = [_bash_turn(3, cmd)]
    findings = _run(turns)
    assert len(findings) == 1
    assert findings[0].severity == "high"


def test_multiple_turns_multiple_findings():
    """Each offending turn produces its own finding."""
    cmd_a = "until gh pr create; do sleep 5; done"
    cmd_b = "sleep 10 && gh pr comment 42 --body 'retry'"
    turns = [_bash_turn(0, cmd_a), _bash_turn(1, cmd_b)]
    findings = _run(turns)
    assert len(findings) == 2
    turn_indices = {f.turn_index for f in findings}
    assert turn_indices == {0, 1}


# ---------------------------------------------------------------------------
# Tests — patterns that should NOT fire
# ---------------------------------------------------------------------------

def test_clean_pr_create_does_not_fire():
    """gh pr create without sleep should NOT trigger the classifier."""
    cmd = "gh pr create --title 'Add feature' --body 'description' --base main"
    turns = [_bash_turn(0, cmd)]
    findings = _run(turns)
    assert len(findings) == 0, f"unexpected findings: {findings}"


def test_sleep_without_gh_does_not_fire():
    """sleep alone (not combined with gh) should NOT fire."""
    cmd = "sleep 2 && echo 'done'"
    turns = [_bash_turn(0, cmd)]
    findings = _run(turns)
    assert len(findings) == 0


def test_non_bash_tool_ignored():
    """An Edit tool call with sleep+gh text in input is ignored (not a Bash command)."""
    cmd = "sleep 10 && gh pr create"
    turns = [_non_bash_turn(0, cmd)]
    findings = _run(turns)
    assert len(findings) == 0


def test_empty_turns_returns_empty():
    """No turns → no findings."""
    findings = _run([])
    assert findings == []


def test_bash_turn_no_sleep_gh_label():
    """gh label command without sleep loop is fine."""
    cmd = (
        "gh api -X POST repos/autonomous-agent-7/autonomous-forever/issues/42/labels "
        "-f labels[]=code-review-passed"
    )
    turns = [_bash_turn(0, cmd)]
    findings = _run(turns)
    assert len(findings) == 0


def test_finding_detail_includes_command_snippet():
    """Finding detail should include a snippet of the offending command."""
    cmd = "until gh pr create --title 'x'; do sleep 10; done"
    turns = [_bash_turn(5, cmd)]
    findings = _run(turns)
    assert len(findings) == 1
    assert "sleep" in findings[0].detail.lower() or "gh" in findings[0].detail.lower()
