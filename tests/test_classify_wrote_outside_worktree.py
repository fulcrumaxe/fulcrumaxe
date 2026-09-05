"""tests/test_classify_wrote_outside_worktree.py

Tests for the `wrote_outside_worktree` classifier in backend/run_analyst.py
(Discussion #592 PR-a).

Covers:
  - Correct write (to worktree path) → no finding
  - Write to main repo root → HIGH finding
  - Write to .autonomous-team/ (shared metadata) → no finding (excluded)
  - Write to /tmp/ → no finding (outside main repo)
  - Relative path → no finding (ambiguous, skip)
  - Multiple contaminated writes → multiple findings (one per occurrence)
  - Agent with no worktree on disk (agent_id present but dir absent) → still flags
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import run_analyst  # noqa: E402

from backend.repo_root import main_repo_root  # noqa: E402

classify_wrote_outside_worktree = run_analyst.classify_wrote_outside_worktree
Finding = run_analyst.Finding


# ---------------------------------------------------------------------------
# Minimal TranscriptTurn factory
# ---------------------------------------------------------------------------

def _edit_turn(
    file_path: str,
    turn_idx: int = 0,
    tool_name: str = "Edit",
) -> "run_analyst.TranscriptTurn":  # type: ignore[name-defined]
    """Build a synthetic TranscriptTurn with an Edit (or Write) tool call."""
    try:
        from transcript_reader import TranscriptTurn
    except ImportError:
        sys.path.insert(0, str(_REPO / "backend"))
        from transcript_reader import TranscriptTurn  # type: ignore[import]

    return TranscriptTurn(
        turn_idx=turn_idx,
        role="assistant",
        text="",
        tool_calls=[
            {
                "name": tool_name,
                "id": f"tool_{turn_idx:04d}",
                "input": {
                    "file_path": file_path,
                    "old_string": "old",
                    "new_string": "new",
                },
            }
        ],
        tool_results=[],
        raw={},
    )


def _bash_turn(cmd: str, turn_idx: int = 99) -> "run_analyst.TranscriptTurn":  # type: ignore[name-defined]
    """Build a synthetic TranscriptTurn with a Bash tool call (should never fire)."""
    try:
        from transcript_reader import TranscriptTurn
    except ImportError:
        sys.path.insert(0, str(_REPO / "backend"))
        from transcript_reader import TranscriptTurn  # type: ignore[import]

    return TranscriptTurn(
        turn_idx=turn_idx,
        role="assistant",
        text="",
        tool_calls=[
            {
                "name": "Bash",
                "id": f"bash_{turn_idx:04d}",
                "input": {"command": cmd},
            }
        ],
        tool_results=[],
        raw={},
    )


# The agent ID used in tests.  The worktree at this path does NOT need to
# exist on disk — the classifier has a fallback path for that case.
_AGENT_ID = "testdeadbeef1234"

# Fixture paths are built from the canonical resolver, never written as
# literals. They used to name one operator's home directory, and when the
# checkout moved they stopped matching the root the classifier computes — so
# every "should fire" case quietly asserted against an empty finding list, and
# this suite sat at 6 failed / 8 passed while the classifier was broken too.
#
# _MAIN_ROOT comes from backend.repo_root directly rather than from
# run_analyst._MAIN_REPO_ROOT on purpose: reading the subject's own constant
# would make every path below agree with the classifier by construction, and
# the suite would then pass no matter what that constant said. Sourcing it
# independently keeps the classifier's choice of root under test — see the
# mutation evidence in the PR that introduced this.
_MAIN_ROOT = str(main_repo_root())

_WORKTREE_ROOT = f"{_MAIN_ROOT}/.claude/worktrees/agent-{_AGENT_ID}"
_MAIN_BACKEND = f"{_MAIN_ROOT}/backend/server.py"
_WORKTREE_BACKEND = f"{_WORKTREE_ROOT}/backend/server.py"
_SHARED_PATH = f"{_MAIN_ROOT}/.autonomous-team/config.json"
_TMP_PATH = "/tmp/somefile.txt"
_RELATIVE_PATH = "backend/server.py"


# ---------------------------------------------------------------------------
# Tests: no false positives
# ---------------------------------------------------------------------------

class TestNoFinding:
    """Cases that must NOT fire the classifier."""

    def test_correct_worktree_write(self):
        """Edit to the agent's own worktree path — no finding."""
        turns = [_edit_turn(_WORKTREE_BACKEND)]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert findings == [], f"Unexpected findings: {findings}"

    def test_shared_autonomous_team_path(self):
        """Edit to .autonomous-team/ is shared by design — no finding."""
        turns = [_edit_turn(_SHARED_PATH)]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert findings == [], f"Unexpected findings: {findings}"

    def test_tmp_path_not_main_repo(self):
        """Edit to /tmp/ is outside the main repo — no finding."""
        turns = [_edit_turn(_TMP_PATH)]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert findings == [], f"Unexpected findings: {findings}"

    def test_relative_path_skipped(self):
        """Relative path is ambiguous — skip it, no finding."""
        turns = [_edit_turn(_RELATIVE_PATH)]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert findings == [], f"Unexpected findings: {findings}"

    def test_bash_tool_not_checked(self):
        """Bash tool calls are not Edit/Write — never fire."""
        turns = [_bash_turn(f"cat {_MAIN_BACKEND}")]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert findings == [], f"Unexpected findings: {findings}"

    def test_no_agent_id_no_finding(self):
        """Without an agent_id we cannot determine isolation context — skip."""
        turns = [_edit_turn(_MAIN_BACKEND)]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id="")
        assert findings == [], f"Unexpected findings: {findings}"

    def test_dot_claude_shared_path(self):
        """Edit to .claude/ subtree (e.g. settings) is excluded."""
        turns = [_edit_turn(f"{_MAIN_ROOT}/.claude/settings.json")]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert findings == [], f"Unexpected findings: {findings}"

    def test_write_tool_correct_path(self):
        """Write (not Edit) to worktree — no finding."""
        turns = [_edit_turn(_WORKTREE_BACKEND, tool_name="Write")]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert findings == [], f"Unexpected findings: {findings}"


# ---------------------------------------------------------------------------
# Tests: path leakage (should fire)
# ---------------------------------------------------------------------------

class TestFindings:
    """Cases that MUST produce HIGH findings."""

    def test_main_repo_write_fires(self):
        """Edit to a main-repo path from a worktree agent — HIGH finding."""
        turns = [_edit_turn(_MAIN_BACKEND)]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert len(findings) == 1
        f = findings[0]
        assert f.classifier == "wrote_outside_worktree"
        assert f.severity == "high"
        assert _MAIN_BACKEND in f.detail
        assert _AGENT_ID[:8] in f.detail

    def test_write_tool_main_repo_fires(self):
        """Write (not Edit) to main-repo path — HIGH finding."""
        turns = [_edit_turn(_MAIN_BACKEND, tool_name="Write")]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert len(findings) == 1
        assert findings[0].severity == "high"

    def test_multiple_contaminated_writes(self):
        """Three contaminated writes → three separate findings (one per occurrence)."""
        turns = [
            _edit_turn(f"{_MAIN_ROOT}/backend/api.py", turn_idx=0),
            _edit_turn(f"{_MAIN_ROOT}/tui/src/App.tsx", turn_idx=1),
            _edit_turn(f"{_MAIN_ROOT}/scripts/preflight.sh", turn_idx=2),
        ]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert len(findings) == 3
        assert all(f.severity == "high" for f in findings)
        assert all(f.classifier == "wrote_outside_worktree" for f in findings)

    def test_mixed_correct_and_contaminated(self):
        """Mix of correct worktree writes and main-repo contamination."""
        turns = [
            _edit_turn(_WORKTREE_BACKEND, turn_idx=0),       # correct — no finding
            _edit_turn(_MAIN_BACKEND, turn_idx=1),            # contaminated — finding
            _edit_turn(_SHARED_PATH, turn_idx=2),             # shared — no finding
            _edit_turn(f"{_MAIN_ROOT}/backend/cost_tracker.py", turn_idx=3),  # contaminated
        ]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert len(findings) == 2
        contaminated_turns = {f.turn_index for f in findings}
        assert contaminated_turns == {1, 3}

    def test_unknown_agent_with_agent_id_flags(self):
        """Agent with a hash-like ID but no worktree dir on disk still flags main-repo writes."""
        # Use an ID that definitely won't have a worktree dir on disk
        ghost_id = "ghostagent9999abcd"
        turns = [_edit_turn(_MAIN_BACKEND)]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=ghost_id)
        # Should still fire — the agent had an ID indicating worktree spawn
        assert len(findings) == 1
        assert findings[0].severity == "high"

    def test_turn_index_preserved(self):
        """Finding's turn_index matches the actual turn that contained the bad write."""
        turns = [
            _edit_turn(_WORKTREE_BACKEND, turn_idx=5),   # correct
            _edit_turn(_MAIN_BACKEND, turn_idx=12),       # bad
        ]
        findings = classify_wrote_outside_worktree(iter(turns), agent_id=_AGENT_ID)
        assert len(findings) == 1
        assert findings[0].turn_index == 12
