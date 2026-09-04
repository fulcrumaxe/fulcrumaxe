"""Tests for the three-section spec template parser in discussion_status.py."""

from __future__ import annotations

import subprocess
import sys

import pytest

from backend.discussion_status import get_sections, missing_sections, REQUIRED_SECTIONS


# ---------------------------------------------------------------------------
# Fixtures / sample bodies
# ---------------------------------------------------------------------------

NEW_FORMAT_BODY = """\
<!-- STATUS:SPEC_READY SINCE:2026-05-19T00:00:00Z -->

Some preamble text.

## Intent
- **Goal:** Do the thing.
- **Why now:** Because now.
- **Success conditions:**
  - It works.
- **Failure conditions:**
  - It doesn't work.
- **Constraints:** None.

## Spec (Acceptance)
1. `pytest` exits 0.
2. `grep foo bar.py` exits 0.

## Implementation Notes (advisory — executor may override)
- Use module X.
- See PR #999 for prior art.
"""

LEGACY_BODY = """\
STATUS:SPEC_READY

**Source:** D#1070 audit found 4 open offenders.

## Offender 1 — some handler

No project routing. Fix: wrap with _with_project_stats_db.

## Acceptance

After all 4 fixed:
1. `python3 scripts/audit-project-scoping.py` → exit 0, no findings.
"""

PARTIAL_BODY = """\
<!-- STATUS:SPEC_READY SINCE:2026-05-19T00:00:00Z -->

## Intent
- **Goal:** Partial example.

## Spec (Acceptance)
1. Some check.
"""

EMPTY_BODY = ""

STATUS_ONLY_BODY = "<!-- STATUS:SPEC_READY SINCE:2026-05-19T00:00:00Z -->\n\nSome text with no section headers."


# ---------------------------------------------------------------------------
# Tests: new-format body
# ---------------------------------------------------------------------------

class TestNewFormatBody:
    def test_returns_three_keys(self):
        result = get_sections(NEW_FORMAT_BODY)
        assert set(result.keys()) == {"intent", "spec", "implementation_notes"}

    def test_intent_non_empty(self):
        result = get_sections(NEW_FORMAT_BODY)
        assert result["intent"]
        assert "Goal" in result["intent"]

    def test_spec_non_empty(self):
        result = get_sections(NEW_FORMAT_BODY)
        assert result["spec"]
        assert "pytest" in result["spec"]

    def test_implementation_notes_non_empty(self):
        result = get_sections(NEW_FORMAT_BODY)
        assert result["implementation_notes"]
        assert "module X" in result["implementation_notes"]

    def test_no_missing_sections(self):
        assert missing_sections(NEW_FORMAT_BODY) == []

    def test_sections_do_not_bleed_into_each_other(self):
        result = get_sections(NEW_FORMAT_BODY)
        # Intent should not contain spec content
        assert "pytest" not in result["intent"]
        # Spec should not contain implementation notes
        assert "module X" not in result["spec"]


# ---------------------------------------------------------------------------
# Tests: legacy body (back-compat)
# ---------------------------------------------------------------------------

class TestLegacyBody:
    def test_returns_three_keys(self):
        result = get_sections(LEGACY_BODY)
        assert set(result.keys()) == {"intent", "spec", "implementation_notes"}

    def test_intent_is_empty(self):
        result = get_sections(LEGACY_BODY)
        assert result["intent"] == ""

    def test_implementation_notes_is_empty(self):
        result = get_sections(LEGACY_BODY)
        assert result["implementation_notes"] == ""

    def test_spec_contains_full_body(self):
        result = get_sections(LEGACY_BODY)
        # Full body (minus status comment) is in spec
        assert result["spec"]
        assert "4 open offenders" in result["spec"]

    def test_all_sections_missing_detected(self):
        missing = missing_sections(LEGACY_BODY)
        assert "Intent" in missing
        assert "Spec (Acceptance)" in missing
        assert "Implementation Notes" in missing

    def test_spec_non_empty_for_legacy(self):
        """Existing callers that only use spec key keep working."""
        result = get_sections(LEGACY_BODY)
        assert result["spec"]


# ---------------------------------------------------------------------------
# Tests: partial body (some but not all headers)
# ---------------------------------------------------------------------------

class TestPartialBody:
    def test_returns_three_keys(self):
        result = get_sections(PARTIAL_BODY)
        assert set(result.keys()) == {"intent", "spec", "implementation_notes"}

    def test_present_sections_parsed(self):
        result = get_sections(PARTIAL_BODY)
        assert "Partial example" in result["intent"]
        assert "Some check" in result["spec"]

    def test_missing_section_is_empty_string(self):
        result = get_sections(PARTIAL_BODY)
        assert result["implementation_notes"] == ""

    def test_missing_sections_detects_implementation_notes(self):
        missing = missing_sections(PARTIAL_BODY)
        assert "Implementation Notes" in missing
        assert "Intent" not in missing
        assert "Spec (Acceptance)" not in missing


# ---------------------------------------------------------------------------
# Tests: empty / edge-case bodies
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_body_returns_back_compat(self):
        result = get_sections(EMPTY_BODY)
        assert set(result.keys()) == {"intent", "spec", "implementation_notes"}
        assert result["intent"] == ""
        assert result["spec"] == ""
        assert result["implementation_notes"] == ""

    def test_none_body(self):
        result = get_sections(None)  # type: ignore[arg-type]
        assert set(result.keys()) == {"intent", "spec", "implementation_notes"}

    def test_status_only_body_is_legacy(self):
        result = get_sections(STATUS_ONLY_BODY)
        # Status comment stripped, text in spec
        assert "Some text" in result["spec"]
        assert result["intent"] == ""

    def test_missing_sections_on_empty(self):
        missing = missing_sections(EMPTY_BODY)
        assert set(missing) == set(REQUIRED_SECTIONS)


# ---------------------------------------------------------------------------
# Gate 2: Live smoke test against D#1121
# ---------------------------------------------------------------------------

class TestLiveD1121:
    """Smoke test: fetch D#1121 body live and verify back-compat."""

    @pytest.mark.integration
    def test_d1121_back_compat(self):
        """D#1121 is a legacy body — keys present, spec non-empty."""
        try:
            result = subprocess.run(
                [sys.executable, "backend/discussion_cache.py", "get-body", "1121"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            body = result.stdout
        except Exception as exc:
            pytest.skip(f"Could not fetch D#1121 body: {exc}")

        if not body.strip():
            pytest.skip("D#1121 body empty — offline or cache miss")

        sections = get_sections(body)
        assert set(sections.keys()) == {"intent", "spec", "implementation_notes"}
        assert sections["spec"], "legacy body must expose full body under 'spec'"
