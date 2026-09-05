"""tests/test_spec_ready_gate_parity.py — parity between the two SPEC_READY
readers (D#1941): scripts/lib/spec-ready-gate.sh and
backend.discussion_status.is_spec_ready(). Both anchor to the body's first
non-empty line, but used to disagree on any body opening with one of 8
characters Python's str.splitlines() treats as a line break and bash's
newline-only split does not — the shell now delegates line-splitting to the
same Python parser instead of doing its own (see spec-ready-gate.sh), so this
drives one corpus through both and asserts they agree.

Deliberately pytest, not shell: scripts/preflight-fast.sh runs `pytest tests/`
but never executes tests/*.sh, so a shell-only parity check would not be
picked up automatically. tests/test_spec_ready_gate.sh is untouched and keeps
covering the shell gate's own fixture table.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.discussion_status import is_spec_ready  # noqa: E402


def _shell_open(body: str) -> bool:
    """True iff spec-ready-gate.sh's spec_ready_gate_check() opens for *body*.
    Passed as $1, never interpolated into script text."""
    script = 'source scripts/lib/spec-ready-gate.sh\nspec_ready_gate_check "$1" >/dev/null 2>&1\n'
    result = subprocess.run(
        ["bash", "-c", script, "_", body], cwd=REPO_ROOT, capture_output=True, text=True
    )
    return result.returncode == 0


def _python_ready(body: str) -> bool:
    return is_spec_ready(body)


# The 8 characters Python's splitlines() treats as line breaks and bash's
# newline-only split does not — the exact divergence this fixes. Each case
# puts a non-marker char on "line 1" by the old shell definition and a real
# SPEC_READY marker on "line 1" by Python's; both readers must now BLOCK.
_LINEBREAK_CHARS = {
    "form_feed": "\x0c",
    "vertical_tab": "\x0b",
    "file_separator": "\x1c",
    "group_separator": "\x1d",
    "record_separator": "\x1e",
    "next_line": "\x85",
    "line_separator": " ",
    "paragraph_separator": " ",
}
_LINEBREAK_CASES = [
    (f"linebreak_{name}", f"x{char}<!-- STATUS:SPEC_READY -->\n\nProse.\n", False)
    for name, char in _LINEBREAK_CHARS.items()
]

# Reconstructed classes from PR #1933's review (D#1798) — none involve the 8
# characters above, so both readers already agreed; kept for corpus breadth.
_RECONSTRUCTED_CASES = [
    ("leading_whitespace", "   <!-- STATUS:SPEC_READY -->\n\nProse.\n", True),
    ("leading_tab", "\t<!-- STATUS:SPEC_READY -->\n\nProse.\n", True),
    ("zero_width_space", "​<!-- STATUS:SPEC_READY -->\n\nProse.\n", True),
    ("nbsp", " <!-- STATUS:SPEC_READY -->\n\nProse.\n", True),
    ("rtl_override", "‮<!-- STATUS:SPEC_READY -->\n\nProse.\n", True),
    ("crlf", "<!-- STATUS:SPEC_READY -->\r\n\r\nProse.\r\n", True),
    ("nested_html_comments", "<!-- outer <!-- STATUS:SPEC_READY --> outer -->\n\nProse.\n", True),
    (
        "duplicate_marker_first_wins_ready",
        "<!-- STATUS:SPEC_READY SINCE:2026-07-30T00:00:00Z -->\n\n## Update\n\n"
        "<!-- STATUS:DONE SINCE:2026-07-31T00:00:00Z -->\n\nStale duplicate below.\n",
        True,
    ),
    (
        "duplicate_marker_first_wins_blocked",
        "<!-- STATUS:DRAFT SINCE:2026-07-30T00:00:00Z -->\n\n## Update\n\n"
        "<!-- STATUS:SPEC_READY SINCE:2026-07-31T00:00:00Z -->\n\nReady claimed below.\n",
        False,
    ),
]

# The three accidental-marker classes the gate exists to reject (D#1798).
# Both readers must fail closed on all three, unchanged by this refactor.
_ACCIDENTAL_MARKER_CASES = [
    (
        "accidental_fenced_code_block",
        "## Acceptance\n\nExample marker, not a real one:\n\n```\n<!-- STATUS:SPEC_READY -->\n```\n\nNo real marker elsewhere.\n",
        False,
    ),
    (
        "accidental_quoted_pm_rejection",
        "<!-- STATUS:NEEDS_REVISION SINCE:2026-07-30T00:00:00Z -->\n\n"
        "Rejected because it claimed STATUS: SPEC_READY prematurely.\n",
        False,
    ),
    (
        "accidental_stale_duplicate_below_live_status",
        "<!-- STATUS:DRAFT SINCE:2026-07-29T00:00:00Z -->\n\n## Update\n\n"
        "<!-- STATUS:SPEC_READY SINCE:2026-07-30T00:00:00Z -->\n\nReady per stale duplicate below.\n",
        False,
    ),
]

ALL_CASES = _LINEBREAK_CASES + _RECONSTRUCTED_CASES + _ACCIDENTAL_MARKER_CASES
assert len(ALL_CASES) >= 15  # Spec item 3
assert len(_LINEBREAK_CASES) == 8  # Spec item 2


@pytest.mark.parametrize("name,body,expected_ready", ALL_CASES, ids=[c[0] for c in ALL_CASES])
def test_shell_and_python_agree(name, body, expected_ready):
    """Both readers must return the same verdict, and it must be the expected
    one — a single-path check would miss a reintroduced divergence."""
    shell_result = _shell_open(body)
    python_result = _python_ready(body)
    assert shell_result == expected_ready, f"{name}: shell -> {shell_result}, want {expected_ready}"
    assert python_result == expected_ready, f"{name}: python -> {python_result}, want {expected_ready}"
    assert shell_result == python_result, f"{name}: shell/python disagree ({shell_result}/{python_result})"


def test_infrastructure_failure_fails_closed(tmp_path):
    """Spec item 8: an unreachable parser must BLOCK, not open — the shell
    now depends on it for line-splitting too, not just for parsing."""
    (tmp_path / "discussion_status.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    script = 'source "$1"\n_SPEC_READY_GATE_REPO_ROOT="$2"\nspec_ready_gate_check "$3" >/dev/null 2>&1\n'
    gate_path = str(REPO_ROOT / "scripts" / "lib" / "spec-ready-gate.sh")
    body = "<!-- STATUS:SPEC_READY -->\n\nProse.\n"
    result = subprocess.run(
        ["bash", "-c", script, "_", gate_path, str(tmp_path), body],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "gate opened with an unreachable STATUS parser"
