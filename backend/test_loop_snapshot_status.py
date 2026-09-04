"""test_loop_snapshot_status.py — Unit tests for STATUS extraction regex in loop-subsystem-snapshot.py.

Verifies that _STATUS_RE correctly parses:
  - HTML-comment form: <!-- STATUS:SPEC_READY SINCE:... -->
  - Bare-line form:    STATUS: DISCUSSING
  - Missing STATUS:    falls back to UNKNOWN
  - Multiple STATUS:   first one wins (re.search returns first match)
  - Extra trailing metadata: only the uppercase token is captured
"""

import re
import sys
from pathlib import Path

import pytest

# Import the compiled regex directly from the snapshot script.
# The script lives under scripts/ which is not a package, so we adjust sys.path.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

# Load the module without executing it as __main__ by importing via importlib.
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "loop_subsystem_snapshot",
    _SCRIPTS_DIR / "loop-subsystem-snapshot.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_STATUS_RE: re.Pattern = _mod._STATUS_RE  # type: ignore[attr-defined]


def extract_status(body: str) -> str:
    """Mirror the extraction logic used in the snapshot writer."""
    m = _STATUS_RE.search(body)
    return m.group(1) if m else "UNKNOWN"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_html_comment_spec_ready():
    """Canonical HTML-comment form for SPEC_READY."""
    body = "<!-- STATUS:SPEC_READY SINCE:2026-05-10T00:00:00Z -->\n\n## Problem\n..."
    assert extract_status(body) == "SPEC_READY"


def test_html_comment_implementing_with_pr():
    """HTML-comment with PR metadata — only token captured."""
    body = "<!-- STATUS:IMPLEMENTING PR:#123 SINCE:2026-05-10T12:00:00Z -->\n\n## Spec"
    assert extract_status(body) == "IMPLEMENTING"


def test_html_comment_reviewing():
    body = "<!-- STATUS:REVIEWING PR:#55 SINCE:2026-05-10T14:00:00Z -->"
    assert extract_status(body) == "REVIEWING"


def test_html_comment_done():
    body = "<!-- STATUS:DONE PR:#99 SINCE:2026-05-10T18:00:00Z -->"
    assert extract_status(body) == "DONE"


def test_bare_line_form_with_space():
    """Bare-line form with a space after the colon."""
    body = "STATUS: DISCUSSING\n\nMore body text."
    assert extract_status(body) == "DISCUSSING"


def test_bare_line_form_no_space():
    """Bare-line form without a space (edge case)."""
    body = "STATUS:CONSENSUS\n"
    assert extract_status(body) == "CONSENSUS"


def test_missing_status_returns_unknown():
    """Bodies with no STATUS marker fall back to UNKNOWN."""
    body = "## Feature request\n\nNo status line here."
    assert extract_status(body) == "UNKNOWN"


def test_empty_body_returns_unknown():
    assert extract_status("") == "UNKNOWN"


def test_multiple_status_lines_first_wins():
    """When two STATUS markers exist, the first one wins (re.search semantics)."""
    body = (
        "<!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->\n"
        "<!-- STATUS:DONE SINCE:2026-05-10T00:00:00Z -->\n"
    )
    assert extract_status(body) == "SPEC_READY"


def test_trailing_metadata_not_captured():
    """Extra trailing text (SINCE, PR, -->) must not bleed into the captured value."""
    body = "<!-- STATUS:DISCUSSING SINCE:2026-05-10T05:56:44Z -->"
    result = extract_status(body)
    assert result == "DISCUSSING"
    assert " " not in result
    assert "SINCE" not in result
    assert "-->" not in result


def test_case_sensitive_status_keyword():
    """Lowercase 'status:' should not match (convention requires uppercase STATUS)."""
    body = "status: discussing\n"
    assert extract_status(body) == "UNKNOWN"


def test_unknown_status_value_still_captured():
    """An unrecognised but syntactically valid status is captured as-is."""
    body = "<!-- STATUS:QUEUED_PENDING SINCE:2026-05-10 -->"
    assert extract_status(body) == "QUEUED_PENDING"
