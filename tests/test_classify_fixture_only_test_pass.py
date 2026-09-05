"""tests/test_classify_fixture_only_test_pass.py

Tests for classify_fixture_only_test_pass
(backend/classifiers/fixture_only_test_pass.py).

ACs from D#655 PR-b:
  AC1 — positive: test file with only mocks → ≥1 finding, severity=medium,
         classifier=fixture_only_test_pass, detail contains the expected surface string.
  AC2 — negative: test file with subprocess.run (real invocation) → no findings.
  AC3 — negative: non-test file with mocks → no findings.
  AC4 — negative: empty transcript → no findings, no crash.
  AC5 — run_analyst._PHASE_A8_CLASSIFIERS includes classify_fixture_only_test_pass.
  AC6 — negative: conftest.py excluded.
  AC7 — negative: integration test path excluded.
  AC8 — positive: Edit tool call on test file with mocks → fires.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

from backend.classifiers.fixture_only_test_pass import classify_fixture_only_test_pass  # noqa: E402
import run_analyst  # noqa: E402
from testsupport.fixture_paths import FIXTURE_MAIN_REPO  # noqa: E402

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
    return classify_fixture_only_test_pass(iter(turns))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURE_ONLY_CONTENT = """\
import unittest
from unittest.mock import MagicMock, patch

def test_something():
    mock_fn = MagicMock(return_value={"status": "ok", "code": 0})
    with patch("mymodule.run_command", mock_fn):
        result = mymodule.run_command("arg")
    assert result["status"] == "ok"
"""

_WITH_SUBPROCESS_CONTENT = """\
import unittest
from unittest.mock import MagicMock
import subprocess

def test_something():
    mock_fn = MagicMock(return_value={"status": "ok"})
    result = subprocess.run(["my-cli", "--help"], capture_output=True)
    assert result.returncode == 0
"""

_NO_MOCK_CONTENT = """\
import unittest

def test_something():
    assert 1 + 1 == 2
"""


# ---------------------------------------------------------------------------
# AC1 — positive: fixture-only test file fires
# ---------------------------------------------------------------------------

def test_fixture_only_write_fires():
    """Write to tests/test_foo.py with only mocks → finding with correct fields."""
    turns = [_write_turn("tests/test_foo.py", _FIXTURE_ONLY_CONTENT)]
    findings = _run(turns)
    assert len(findings) >= 1, f"expected ≥1 finding, got {findings}"
    f = findings[0]
    assert f.classifier == "fixture_only_test_pass"
    assert f.severity == "medium", f"expected severity=medium, got {f.severity!r}"
    assert "fixture-mocked" in f.detail or "fixture_only" in f.detail or "test_foo.py" in f.detail


def test_fixture_only_test_underscore_prefix():
    """test_*.py naming convention is recognized."""
    turns = [_write_turn(f"{FIXTURE_MAIN_REPO}/tests/test_analyzer.py", _FIXTURE_ONLY_CONTENT)]
    findings = _run(turns)
    assert len(findings) >= 1


def test_fixture_only_trailing_test():
    """*_test.py naming convention is recognized."""
    turns = [_write_turn(f"{FIXTURE_MAIN_REPO}/analyzer_test.py", _FIXTURE_ONLY_CONTENT)]
    findings = _run(turns)
    assert len(findings) >= 1


# ---------------------------------------------------------------------------
# AC2 — negative: test file with subprocess → no findings
# ---------------------------------------------------------------------------

def test_test_file_with_subprocess_no_finding():
    """Test file that uses subprocess.run (real invocation) does not fire."""
    turns = [_write_turn("tests/test_cli.py", _WITH_SUBPROCESS_CONTENT)]
    findings = _run(turns)
    assert findings == [], f"expected no findings, got {findings}"


# ---------------------------------------------------------------------------
# AC3 — negative: non-test file → no findings
# ---------------------------------------------------------------------------

def test_non_test_file_no_finding():
    """Write to a non-test file with mocks does not fire."""
    turns = [_write_turn("backend/my_module.py", _FIXTURE_ONLY_CONTENT)]
    findings = _run(turns)
    assert findings == [], f"expected no findings, got {findings}"


# ---------------------------------------------------------------------------
# AC4 — negative: empty transcript
# ---------------------------------------------------------------------------

def test_empty_transcript_no_finding():
    """Empty transcript → no findings, no crash."""
    assert _run([]) == []


def test_turns_without_write_no_finding():
    """Bash-only turns with no Write/Edit → no findings."""
    turn = _MockTurn(
        turn_idx=1,
        tool_calls=[{"name": "Bash", "input": {"command": "pytest tests/"}}],
    )
    assert _run([turn]) == []


# ---------------------------------------------------------------------------
# AC5 — registration in _PHASE_A8_CLASSIFIERS
# ---------------------------------------------------------------------------

def test_registered_in_phase_a8_classifiers():
    """classify_fixture_only_test_pass must appear in run_analyst._PHASE_A8_CLASSIFIERS."""
    assert classify_fixture_only_test_pass in run_analyst._PHASE_A8_CLASSIFIERS, (
        "classify_fixture_only_test_pass not found in _PHASE_A8_CLASSIFIERS"
    )


# ---------------------------------------------------------------------------
# AC6 — negative: conftest.py excluded
# ---------------------------------------------------------------------------

def test_conftest_excluded():
    """conftest.py is excluded — fixture definitions are expected there."""
    turns = [_write_turn("tests/conftest.py", _FIXTURE_ONLY_CONTENT)]
    findings = _run(turns)
    assert findings == [], f"conftest.py should not fire, got {findings}"


# ---------------------------------------------------------------------------
# AC7 — negative: integration test path excluded
# ---------------------------------------------------------------------------

def test_integration_path_excluded():
    """test files under integration/ are excluded."""
    turns = [_write_turn("tests/integration/test_api.py", _FIXTURE_ONLY_CONTENT)]
    findings = _run(turns)
    assert findings == [], f"integration test should not fire, got {findings}"


# ---------------------------------------------------------------------------
# AC8 — positive: Edit tool call fires too
# ---------------------------------------------------------------------------

def test_edit_tool_fires():
    """Edit (not Write) on a fixture-only test file also fires."""
    turns = [_edit_turn("tests/test_something.py", _FIXTURE_ONLY_CONTENT)]
    findings = _run(turns)
    assert len(findings) >= 1, f"expected ≥1 finding for Edit tool, got {findings}"
    assert findings[0].classifier == "fixture_only_test_pass"
