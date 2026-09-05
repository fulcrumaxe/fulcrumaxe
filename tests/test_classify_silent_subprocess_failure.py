"""tests/test_classify_silent_subprocess_failure.py

Tests for classify_silent_subprocess_failure
(backend/classifiers/silent_subprocess_failure.py).

ACs from D#655 PR-b:
  AC1 — positive: subprocess.run without check=True + .stdout access + no
         .returncode check → ≥1 finding, severity=high.
  AC2 — positive: explicit check=False + .stderr access + no .returncode → fires.
  AC3 — negative: check=True → no findings.
  AC4 — negative: .returncode accessed in the window → no findings.
  AC5 — negative: subprocess.run but output NOT consumed → no findings.
  AC6 — negative: empty transcript → no findings, no crash.
  AC7 — run_analyst._PHASE_A8_CLASSIFIERS includes classify_silent_subprocess_failure.
  AC8 — negative: Bash tool call (not Write/Edit) → no findings.
  AC9 — positive: Edit tool call with the pattern → fires.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

from backend.classifiers.silent_subprocess_failure import classify_silent_subprocess_failure  # noqa: E402
import run_analyst  # noqa: E402

Finding = run_analyst.Finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MockTurn:
    def __init__(
        self,
        turn_idx: int,
        role: str = "assistant",
        agent_id: str = "executor-abc",
        tool_calls: list | None = None,
    ):
        self.turn_idx = turn_idx
        self.role = role
        self.agent_id = agent_id
        self.tool_calls: list = tool_calls or []
        self.tool_results: list = []


def _write_turn(file_path: str, content: str, turn_idx: int = 1) -> _MockTurn:
    return _MockTurn(
        turn_idx=turn_idx,
        tool_calls=[{
            "name": "Write",
            "input": {"file_path": file_path, "content": content},
        }],
    )


def _edit_turn(file_path: str, new_string: str, turn_idx: int = 1) -> _MockTurn:
    return _MockTurn(
        turn_idx=turn_idx,
        tool_calls=[{
            "name": "Edit",
            "input": {
                "file_path": file_path,
                "old_string": "pass",
                "new_string": new_string,
            },
        }],
    )


def _run(turns: list) -> list:
    return classify_silent_subprocess_failure(iter(turns))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Positive: no check=True, consumes .stdout, ignores .returncode
_SILENT_FAILURE_CONTENT = """\
import subprocess

def run_cmd(args):
    result = subprocess.run(args, capture_output=True, text=True)
    output = result.stdout
    return output.strip()
"""

# Positive: explicit check=False, consumes .stderr, no returncode check
_EXPLICIT_CHECK_FALSE_CONTENT = """\
import subprocess

def run_cmd(args):
    proc = subprocess.run(args, check=False, capture_output=True)
    err = proc.stderr
    return err
"""

# Negative: check=True → CalledProcessError raised automatically
_CHECK_TRUE_CONTENT = """\
import subprocess

def run_cmd(args):
    result = subprocess.run(args, check=True, capture_output=True)
    return result.stdout
"""

# Negative: .returncode is checked in the window
_WITH_RETURNCODE_CHECK_CONTENT = """\
import subprocess

def run_cmd(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"failed: {result.stderr}")
    return result.stdout
"""

# Negative: subprocess.run called but output not consumed
_NO_OUTPUT_CONSUMPTION_CONTENT = """\
import subprocess

def run_cmd(args):
    result = subprocess.run(args, capture_output=True)
    return result.returncode
"""


# ---------------------------------------------------------------------------
# AC1 — positive: omitted check=True, .stdout consumed, no .returncode
# ---------------------------------------------------------------------------

def test_silent_failure_no_check_fires():
    """subprocess.run with no check=True + .stdout access + no returncode → fires."""
    turns = [_write_turn("backend/runner.py", _SILENT_FAILURE_CONTENT)]
    findings = _run(turns)
    assert len(findings) >= 1, f"expected ≥1 finding, got {findings}"
    f = findings[0]
    assert f.classifier == "silent_subprocess_failure"
    assert f.severity == "high", f"expected severity=high, got {f.severity!r}"
    assert "returncode" in f.detail or "ignored exit" in f.detail or "runner.py" in f.detail


# ---------------------------------------------------------------------------
# AC2 — positive: explicit check=False, .stderr consumed
# ---------------------------------------------------------------------------

def test_explicit_check_false_fires():
    """subprocess.run(check=False) + .stderr access + no returncode → fires."""
    turns = [_write_turn("scripts/deploy.py", _EXPLICIT_CHECK_FALSE_CONTENT)]
    findings = _run(turns)
    assert len(findings) >= 1, f"expected ≥1 finding, got {findings}"
    assert findings[0].classifier == "silent_subprocess_failure"


# ---------------------------------------------------------------------------
# AC3 — negative: check=True
# ---------------------------------------------------------------------------

def test_check_true_no_finding():
    """subprocess.run(check=True) does not fire — CalledProcessError handles it."""
    turns = [_write_turn("backend/runner.py", _CHECK_TRUE_CONTENT)]
    findings = _run(turns)
    assert findings == [], f"check=True should not fire, got {findings}"


# ---------------------------------------------------------------------------
# AC4 — negative: .returncode accessed in the window
# ---------------------------------------------------------------------------

def test_returncode_check_no_finding():
    """Code that checks .returncode in the same window does not fire."""
    turns = [_write_turn("backend/runner.py", _WITH_RETURNCODE_CHECK_CONTENT)]
    findings = _run(turns)
    assert findings == [], f"returncode check should suppress finding, got {findings}"


# ---------------------------------------------------------------------------
# AC5 — negative: subprocess.run but output not consumed
# ---------------------------------------------------------------------------

def test_no_output_consumption_no_finding():
    """subprocess.run with no .stdout/.stderr access does not fire."""
    turns = [_write_turn("backend/runner.py", _NO_OUTPUT_CONSUMPTION_CONTENT)]
    findings = _run(turns)
    assert findings == [], f"no output consumption should not fire, got {findings}"


# ---------------------------------------------------------------------------
# AC6 — negative: empty transcript
# ---------------------------------------------------------------------------

def test_empty_transcript_no_finding():
    """Empty transcript → no findings, no crash."""
    assert _run([]) == []


# ---------------------------------------------------------------------------
# AC7 — registration in _PHASE_A8_CLASSIFIERS
# ---------------------------------------------------------------------------

def test_registered_in_phase_a8_classifiers():
    """classify_silent_subprocess_failure must appear in run_analyst._PHASE_A8_CLASSIFIERS."""
    assert classify_silent_subprocess_failure in run_analyst._PHASE_A8_CLASSIFIERS, (
        "classify_silent_subprocess_failure not found in _PHASE_A8_CLASSIFIERS"
    )


# ---------------------------------------------------------------------------
# AC8 — negative: Bash tool call is not scanned
# ---------------------------------------------------------------------------

def test_bash_tool_not_scanned():
    """Bash commands (not Write/Edit) are not scanned for this pattern."""
    turn = _MockTurn(
        turn_idx=1,
        tool_calls=[{
            "name": "Bash",
            "input": {
                "command": "result = subprocess.run(['cmd'], check=False); print(result.stdout)",
            },
        }],
    )
    findings = _run([turn])
    assert findings == [], f"Bash tool calls should not be scanned, got {findings}"


# ---------------------------------------------------------------------------
# AC9 — positive: Edit tool call
# ---------------------------------------------------------------------------

def test_edit_tool_fires():
    """Edit (not Write) with the silent-failure pattern also fires."""
    turns = [_edit_turn("backend/runner.py", _SILENT_FAILURE_CONTENT)]
    findings = _run(turns)
    assert len(findings) >= 1, f"expected ≥1 finding for Edit tool, got {findings}"
    assert findings[0].classifier == "silent_subprocess_failure"
