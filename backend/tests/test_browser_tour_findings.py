"""
tests/test_browser_tour_findings.py — unit tests for backend/browser_tour_filer.py

Tests:
  - parse_and_file filters severity < medium correctly
  - parse_and_file constructs the right Discussion title for post-merge tours
  - parse_and_file constructs the right Discussion title for nightly tours
  - dry-run mode produces output but makes no API calls
  - empty issues list produces no filings
  - all eligible severities are filed

HARD RULE: NEVER invoke claude, claude -p, _start_loop_run, or /loop here.
GraphQL calls are mocked via monkeypatching.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.browser_tour_filer import (
    FILING_SEVERITIES,
    _build_discussion_body,
    parse_and_file,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_envelope(issues: list[dict[str, Any]], pr: int | None = 42) -> dict[str, Any]:
    return {
        "agent": "browser-tester",
        "trigger": "post-merge",
        "pr": pr,
        "affected_pages": ["/ideas", "/prs"],
        "verdict": "fail" if any(i.get("severity") in FILING_SEVERITIES for i in issues) else "pass",
        "issues": issues,
    }


MEDIUM_ISSUE = {
    "file": "/ideas",
    "line": None,
    "severity": "medium",
    "message": "Chart fails to render when dataset is empty",
}

HIGH_ISSUE = {
    "file": "dashboard/src/components/KpiChart.tsx",
    "line": 42,
    "severity": "high",
    "message": "KPI chart shows NaN instead of zero",
}

ERROR_ISSUE = {
    "file": "/prs",
    "line": None,
    "severity": "error",
    "message": "React render error: Cannot read properties of undefined",
}

LOW_ISSUE = {
    "file": "/ideas",
    "line": None,
    "severity": "low",
    "message": "Tooltip text has a typo",
}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFilingSeverityFilter:
    def test_low_severity_not_filed(self) -> None:
        """Findings with severity=low are NOT filed."""
        envelope = _make_envelope([LOW_ISSUE])
        with patch("backend.browser_tour_filer._get_repo_id", return_value="R_123"), \
             patch("backend.browser_tour_filer._get_discussion_category_id", return_value="DC_456"), \
             patch("backend.browser_tour_filer._file_discussion") as mock_file:
            mock_file.return_value = None
            result = parse_and_file(envelope)
        assert result == [], "low severity should produce no filings"
        mock_file.assert_not_called()

    def test_medium_severity_filed(self) -> None:
        """Findings with severity=medium ARE filed."""
        envelope = _make_envelope([MEDIUM_ISSUE])
        with patch("backend.browser_tour_filer._get_repo_id", return_value="R_123"), \
             patch("backend.browser_tour_filer._get_discussion_category_id", return_value="DC_456"), \
             patch("backend.browser_tour_filer._file_discussion", return_value="https://github.com/x/y/discussions/99") as mock_file:
            result = parse_and_file(envelope)
        assert len(result) == 1
        assert result[0]["severity"] == "medium"
        assert result[0]["filed"] is True
        mock_file.assert_called_once()

    def test_high_severity_filed(self) -> None:
        """Findings with severity=high ARE filed."""
        envelope = _make_envelope([HIGH_ISSUE])
        with patch("backend.browser_tour_filer._get_repo_id", return_value="R_123"), \
             patch("backend.browser_tour_filer._get_discussion_category_id", return_value="DC_456"), \
             patch("backend.browser_tour_filer._file_discussion", return_value="https://github.com/x/y/discussions/100"):
            result = parse_and_file(envelope)
        assert len(result) == 1
        assert result[0]["severity"] == "high"

    def test_error_severity_filed(self) -> None:
        """Findings with severity=error ARE filed."""
        envelope = _make_envelope([ERROR_ISSUE])
        with patch("backend.browser_tour_filer._get_repo_id", return_value="R_123"), \
             patch("backend.browser_tour_filer._get_discussion_category_id", return_value="DC_456"), \
             patch("backend.browser_tour_filer._file_discussion", return_value="https://github.com/x/y/discussions/101"):
            result = parse_and_file(envelope)
        assert len(result) == 1

    def test_mixed_severities_only_filing_threshold(self) -> None:
        """Only severity >= medium are filed; low is filtered out."""
        envelope = _make_envelope([LOW_ISSUE, MEDIUM_ISSUE, HIGH_ISSUE])
        with patch("backend.browser_tour_filer._get_repo_id", return_value="R_123"), \
             patch("backend.browser_tour_filer._get_discussion_category_id", return_value="DC_456"), \
             patch("backend.browser_tour_filer._file_discussion", return_value="https://github.com/x/y/discussions/102"):
            result = parse_and_file(envelope)
        assert len(result) == 2, "only medium + high should be filed (not low)"

    def test_empty_issues_list(self) -> None:
        """Empty issues list produces no filings and no API calls."""
        envelope = _make_envelope([])
        with patch("backend.browser_tour_filer._file_discussion") as mock_file:
            result = parse_and_file(envelope)
        assert result == []
        mock_file.assert_not_called()


class TestDiscussionTitleFormat:
    def test_post_merge_title_includes_pr(self) -> None:
        """Post-merge titles include 'PR #N regression:' prefix."""
        envelope = _make_envelope([MEDIUM_ISSUE], pr=42)
        with patch("backend.browser_tour_filer._get_repo_id", return_value="R_123"), \
             patch("backend.browser_tour_filer._get_discussion_category_id", return_value="DC_456"), \
             patch("backend.browser_tour_filer._file_discussion", return_value="https://example.com/d/1") as mock_file:
            parse_and_file(envelope)
        _title, _body, _cat, _repo = mock_file.call_args[0]
        assert "PR #42 regression:" in _title
        assert "[Bug]" in _title

    def test_nightly_title_no_pr(self) -> None:
        """Nightly tour titles use 'Browser-tester:' prefix (no PR number)."""
        envelope = _make_envelope([MEDIUM_ISSUE], pr=None)
        tour_meta = {
            "pr": None,
            "trigger": "nightly",
            "affected_pages": ["/"],
            "queued_at": "2026-05-10T04:00:00Z",
            "tour_file": "",
        }
        with patch("backend.browser_tour_filer._get_repo_id", return_value="R_123"), \
             patch("backend.browser_tour_filer._get_discussion_category_id", return_value="DC_456"), \
             patch("backend.browser_tour_filer._file_discussion", return_value="https://example.com/d/2") as mock_file:
            parse_and_file(envelope, tour_meta=tour_meta)
        _title, _body, _cat, _repo = mock_file.call_args[0]
        assert "[Bug] Browser-tester:" in _title
        assert "PR #" not in _title


class TestDryRun:
    def test_dry_run_no_api_calls(self, capsys: pytest.CaptureFixture) -> None:
        """dry_run=True prints mutations but never calls _file_discussion with real API."""
        envelope = _make_envelope([MEDIUM_ISSUE, HIGH_ISSUE])
        # _file_discussion in dry_run returns a placeholder URL without subprocess
        result = parse_and_file(envelope, dry_run=True)
        # dry-run returns one record per filing issue
        assert len(result) == 2
        for r in result:
            assert r["url"] is not None
            assert "DRY-RUN" in (r["url"] or "")

    def test_dry_run_output_mentions_title(self, capsys: pytest.CaptureFixture) -> None:
        """dry_run prints the Discussion title to stdout."""
        envelope = _make_envelope([HIGH_ISSUE])
        result = parse_and_file(envelope, dry_run=True)
        captured = capsys.readouterr()
        assert "Would file Discussion" in captured.out


class TestDiscussionBody:
    def test_body_contains_status_line(self) -> None:
        """Discussion body starts with a STATUS:DISCUSSING comment."""
        issue = MEDIUM_ISSUE
        tour_meta: dict[str, Any] = {
            "pr": 42,
            "trigger": "post-merge",
            "affected_pages": ["/ideas"],
            "queued_at": "2026-05-10T09:00:00Z",
            "tour_file": "",
        }
        body = _build_discussion_body(issue, tour_meta)
        assert "STATUS:DISCUSSING" in body

    def test_body_cross_links_pr(self) -> None:
        """Discussion body mentions the originating PR number."""
        issue = MEDIUM_ISSUE
        tour_meta: dict[str, Any] = {
            "pr": 42,
            "trigger": "post-merge",
            "affected_pages": ["/ideas"],
            "queued_at": "2026-05-10T09:00:00Z",
            "tour_file": "",
        }
        body = _build_discussion_body(issue, tour_meta)
        assert "#42" in body

    def test_nightly_body_no_pr_reference(self) -> None:
        """Nightly tour body does not mention a PR."""
        issue = HIGH_ISSUE
        tour_meta: dict[str, Any] = {
            "pr": None,
            "trigger": "nightly",
            "affected_pages": ["/"],
            "queued_at": "2026-05-10T04:00:00Z",
            "tour_file": "",
        }
        body = _build_discussion_body(issue, tour_meta)
        assert "nightly" in body.lower()
        assert "PR #" not in body
