"""backend/classifiers/fixture_only_test_pass.py

Phase A.8 run-analyst classifier: detect transcripts where a test file was
written / edited containing only mock fixtures — no real subprocess or CLI
invocation of the artifact under test.

Rationale: "tests pass" is not "feature works" (CLAUDE.md).  Agents that
write test files full of unittest.mock / MagicMock patches and hardcoded
return values validate code-vs-spec but not spec-vs-reality.  If the test
file never calls the real binary/CLI, a broken artifact can ship green.

Detection (transcript-level):
  - Scan Write and Edit tool calls whose file_path ends in `test_*.py` or
    `*_test.py` (or any path under a `tests/` directory).
  - A test file is fixture-only if ALL of these are true:
      * The written content contains mock indicators
        (unittest.mock, MagicMock, patch, mocker, fixture return dict)
      * The written content does NOT contain a real invocation:
        subprocess.run, subprocess.Popen, subprocess.call, os.system,
        os.popen, shutil.which, or a `from subprocess import` import used
        without check=True (which itself would be a different finding).
  - Surface: "test suite passed but only fixture-mocked the artifact".
  - Fire at most once per transcript (one finding is diagnostic enough).

Excluded:
  - Test files that use subprocess at all (even if they also mock)
  - Integration test files (path contains `integration` or `e2e`)
  - Conftest.py (fixture definitions are expected there)

Severity: medium (individual occurrence is informational; auto-bug fires at
≥5 hits in 24h per D#655 spec).

Usage (registered in backend/run_analyst.py _PHASE_A8_CLASSIFIERS):
    from backend.classifiers.fixture_only_test_pass import classify_fixture_only_test_pass
"""

from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Matches test file paths
_TEST_FILE_PAT = re.compile(
    r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.py|tests/[^/]+\.py)$",
    re.IGNORECASE,
)

# Excluded test file patterns
_EXCLUDED_PATH_PAT = re.compile(
    r"(?:integration|e2e|conftest)",
    re.IGNORECASE,
)

# Mock / fixture indicators in test content
_MOCK_PAT = re.compile(
    r"\bunittest\.mock\b"
    r"|\bMagicMock\b"
    r"|\bMock\("
    r"|\bpatch\s*\("
    r"|\bmocker\b"
    r"|\bpytest\.fixture\b"
    r"|\breturn\s*\{[^}]*['\"][^'\"]+['\"]\s*:",   # hardcoded dict return
    re.IGNORECASE,
)

# Real subprocess / CLI invocation indicators
_REAL_INVOCATION_PAT = re.compile(
    r"\bsubprocess\.run\b"
    r"|\bsubprocess\.Popen\b"
    r"|\bsubprocess\.call\b"
    r"|\bsubprocess\.check_output\b"
    r"|\bsubprocess\.check_call\b"
    r"|\bos\.system\b"
    r"|\bos\.popen\b"
    r"|\bshutil\.which\b"
    r"|from subprocess import",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_fixture_only_test_pass(turns: "Iterable") -> list:
    """Return a Finding when a test file is written with only mock fixtures.

    Args:
        turns: Iterable of TranscriptTurn (duck-type with .role, .tool_calls, .turn_idx).

    Returns:
        list[Finding] — at most one finding per transcript.
    """
    try:
        from backend.run_analyst import Finding  # type: ignore[import]
    except ImportError:
        from run_analyst import Finding  # type: ignore[import]

    turns_list = list(turns)
    if not turns_list:
        return []

    for t in turns_list:
        for tc in t.tool_calls:
            tool_name = tc.get("name", "")
            if tool_name not in ("Write", "Edit"):
                continue

            inp = tc.get("input", {})

            # Write tool: file_path + content
            # Edit tool: file_path + new_string (the inserted content)
            file_path: str = inp.get("file_path", "") or ""
            content: str = inp.get("content", "") or inp.get("new_string", "") or ""

            if not file_path or not content:
                continue

            # Check if this is a test file
            if not _TEST_FILE_PAT.search(file_path):
                continue

            # Skip excluded paths
            if _EXCLUDED_PATH_PAT.search(file_path):
                continue

            # Skip if no mock indicators — not relevant
            if not _MOCK_PAT.search(content):
                continue

            # Skip if the file contains any real invocation
            if _REAL_INVOCATION_PAT.search(content):
                continue

            # Fixture-only test file detected
            return [Finding(
                classifier="fixture_only_test_pass",
                severity="medium",
                turn_index=t.turn_idx,
                detail=(
                    f"test suite passed but only fixture-mocked the artifact: "
                    f"{file_path}"
                ),
            )]

    return []
