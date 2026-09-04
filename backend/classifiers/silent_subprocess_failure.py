"""backend/classifiers/silent_subprocess_failure.py

Phase A.8 run-analyst classifier: detect transcripts where an agent writes
code containing `subprocess.run(..., check=False)` (or omits check=True) and
then accesses .stdout / .stderr without checking .returncode.

Rationale: the existing `tool_output_ignored` classifier fires broadly (63
hits).  This classifier is the precise version: it specifically detects the
pattern where code explicitly opts out of automatic error-raising but then
ignores the return code while consuming output.  That pattern silently
swallows subprocess failures.

Surface: "subprocess at file:line returned non-zero but code ignored exit"

Detection (transcript-level, Write/Edit tool calls):
  - Find code blocks that contain `subprocess.run(` with `check=False`
    (or without `check=True` at all — omission is equivalent since the
    default is False).
  - Check if the same code block accesses `.stdout` or `.stderr` from the
    result without also accessing `.returncode` or calling `.check_returncode()`.
  - "Same code block" = within 10 lines of the subprocess.run call (a
    conservative locality window).

Excluded:
  - `subprocess.run(..., check=True)` — raises CalledProcessError automatically.
  - Bash tool-call commands (we only scan Write/Edit content, not Bash commands).
  - Code that accesses .returncode or calls .check_returncode() anywhere in
    the scanned window — even a loose proximity check is a positive signal.

Severity: high (silent failure is a real correctness issue; matches the
`tool_output_ignored` family which is also high-severity).

Usage (registered in backend/run_analyst.py _PHASE_A8_CLASSIFIERS):
    from backend.classifiers.silent_subprocess_failure import classify_silent_subprocess_failure
"""

from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Matches a subprocess.run call (captures the line content for context)
_SUBPROCESS_RUN_PAT = re.compile(
    r"\bsubprocess\.run\s*\(",
    re.IGNORECASE,
)

# check=True explicitly — safe, CalledProcessError is raised
_CHECK_TRUE_PAT = re.compile(r"\bcheck\s*=\s*True\b")

# check=False explicitly — caller has opted out of auto-raise
_CHECK_FALSE_PAT = re.compile(r"\bcheck\s*=\s*False\b")

# Consuming output: .stdout or .stderr access
_OUTPUT_CONSUME_PAT = re.compile(r"\.\s*(?:stdout|stderr)\b")

# Safe: .returncode access or .check_returncode() call
_RETURNCODE_PAT = re.compile(r"\.\s*returncode\b|\.\s*check_returncode\s*\(")

# Window size (lines) to look for output consumption after subprocess.run
_WINDOW = 10


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _scan_content(content: str, file_path: str) -> tuple[int, str] | None:
    """Scan file content for the silent-failure pattern.

    Returns (line_number, detail_string) for the first match, or None.
    Line numbers are 1-based.
    """
    lines = content.splitlines()

    for i, line in enumerate(lines):
        if not _SUBPROCESS_RUN_PAT.search(line):
            continue

        # Extract the window: the subprocess.run line plus up to _WINDOW following lines.
        # Also include a few lines before (for variable assignment like `result = subprocess.run(`)
        window_lines = lines[max(0, i - 2): i + _WINDOW + 1]
        window_text = "\n".join(window_lines)

        # If check=True is anywhere in the window → safe, skip.
        if _CHECK_TRUE_PAT.search(window_text):
            continue

        # If neither check=False nor omission — check=False is the default,
        # so absence of check=True means check is effectively False.
        # We flag both explicit check=False and omission of check.
        # (No additional condition needed — the absence of _CHECK_TRUE_PAT suffices.)

        # Does the window consume .stdout or .stderr?
        if not _OUTPUT_CONSUME_PAT.search(window_text):
            # Not consuming output — not our pattern.
            continue

        # Does the window also check .returncode?
        if _RETURNCODE_PAT.search(window_text):
            # Code does check return code → not silent failure.
            continue

        line_num = i + 1  # 1-based
        return (line_num, f"subprocess at {file_path}:{line_num} returned non-zero but code ignored exit")

    return None


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_silent_subprocess_failure(turns: "Iterable") -> list:
    """Return a Finding when code uses subprocess.run without exit-code checks.

    Args:
        turns: Iterable of TranscriptTurn (duck-type with .role, .tool_calls, .turn_idx).

    Returns:
        list[Finding] — at most one finding per transcript (first occurrence).
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
            file_path: str = inp.get("file_path", "") or ""
            content: str = inp.get("content", "") or inp.get("new_string", "") or ""

            if not content:
                continue

            match = _scan_content(content, file_path)
            if match is not None:
                _line_num, detail = match
                return [Finding(
                    classifier="silent_subprocess_failure",
                    severity="high",
                    turn_index=t.turn_idx,
                    detail=detail,
                )]

    return []
