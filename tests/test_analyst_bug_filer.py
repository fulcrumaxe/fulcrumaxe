"""tests/test_analyst_bug_filer.py

Tests for backend/analyst_bug_filer.py (Discussion #592 PR-c).

Covers:
  - Dry-run returns placeholder URL and prints Discussion body (exit 0)
  - Idempotency: second call with same agent_id is a no-op (returns None)
  - Unsupported classifier is skipped (returns None)
  - Severity below threshold is skipped (returns None)
  - Body contains expected marker, agent_id, and section headers
  - CLI --dry-run exits 0 and prints JSON result
  - file_wrote_outside_worktree_hits() deduplicates by agent_id
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "backend"))

import analyst_bug_filer  # noqa: E402
from testsupport.fixture_paths import FIXTURE_MAIN_REPO  # noqa: E402
from analyst_bug_filer import (  # noqa: E402
    AnalystBugFiler,
    DRY_RUN_URL,
    _build_body,
    _search_existing_discussions,
    file_wrote_outside_worktree_hits,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_HIT: dict = {
    "classifier": "wrote_outside_worktree",
    "severity": "high",
    "agent_id": "agent-abc123",
    "detail": f"Edit/Write on main-repo path '{FIXTURE_MAIN_REPO}/backend/foo.py' from worktree agent agent-abc123 — should write to worktree",
    "file_path": f"{FIXTURE_MAIN_REPO}/backend/foo.py",
    "branch": "main",
}


# ---------------------------------------------------------------------------
# Test 1: dry-run returns placeholder URL and prints body
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_returns_placeholder_url(self, capsys):
        filer = AnalystBugFiler()
        url = filer.file_bug(SAMPLE_HIT.copy(), dry_run=True)
        assert url == DRY_RUN_URL

    def test_dry_run_prints_body(self, capsys):
        filer = AnalystBugFiler()
        filer.file_bug(SAMPLE_HIT.copy(), dry_run=True)
        out = capsys.readouterr().out
        assert "Would file Discussion" in out
        assert "Worktree isolation violation" in out
        assert "agent-abc123" in out

    def test_dry_run_body_contains_marker(self):
        """Body must contain the idempotency marker."""
        hit = SAMPLE_HIT.copy()
        marker = f"<!-- analyst-bug:wrote_outside_worktree:{hit['agent_id']} -->"
        body = _build_body(hit, marker)
        assert marker in body

    def test_dry_run_body_contains_status_line(self):
        """Body must open with a STATUS:DISCUSSING line."""
        hit = SAMPLE_HIT.copy()
        marker = "<!-- analyst-bug:wrote_outside_worktree:agent-abc123 -->"
        body = _build_body(hit, marker)
        assert body.startswith("<!-- STATUS:DISCUSSING SINCE:")

    def test_dry_run_body_contains_file_path(self):
        """File path must appear in the body."""
        hit = SAMPLE_HIT.copy()
        marker = "<!-- analyst-bug:wrote_outside_worktree:agent-abc123 -->"
        body = _build_body(hit, marker)
        assert hit["file_path"] in body

    def test_dry_run_body_contains_reproduce_section(self):
        """Body must have a 'How to reproduce' section."""
        hit = SAMPLE_HIT.copy()
        marker = "<!-- analyst-bug:wrote_outside_worktree:agent-abc123 -->"
        body = _build_body(hit, marker)
        assert "How to reproduce" in body

    def test_dry_run_no_api_calls(self):
        """dry_run=True must not invoke subprocess.run for API calls."""
        with patch("analyst_bug_filer.subprocess.run") as mock_run:
            filer = AnalystBugFiler()
            url = filer.file_bug(SAMPLE_HIT.copy(), dry_run=True)
        mock_run.assert_not_called()
        assert url == DRY_RUN_URL


# ---------------------------------------------------------------------------
# Test 2: idempotency — second run with same agent_id is a no-op
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_run_skipped_when_marker_exists(self, capsys):
        """When Discussion with marker already exists, file_bug returns None."""
        marker = f"<!-- analyst-bug:wrote_outside_worktree:{SAMPLE_HIT['agent_id']} -->"

        def fake_search(m: str) -> bool:
            return m == marker

        with patch("analyst_bug_filer._search_existing_discussions", side_effect=fake_search):
            filer = AnalystBugFiler()
            result = filer.file_bug(SAMPLE_HIT.copy(), dry_run=False)

        assert result is None

    def test_second_run_prints_skipped(self, capsys):
        marker = f"<!-- analyst-bug:wrote_outside_worktree:{SAMPLE_HIT['agent_id']} -->"

        with patch("analyst_bug_filer._search_existing_discussions", return_value=True):
            filer = AnalystBugFiler()
            filer.file_bug(SAMPLE_HIT.copy(), dry_run=False)
        out = capsys.readouterr().out
        assert "Skipped" in out or "skipped" in out

    def test_different_agent_id_is_not_skipped(self):
        """A different agent_id produces a different marker and is NOT skipped."""
        hit2 = {**SAMPLE_HIT, "agent_id": "agent-xyz999"}
        marker2 = f"<!-- analyst-bug:wrote_outside_worktree:agent-xyz999 -->"

        # Only the original agent_id marker exists
        original_marker = f"<!-- analyst-bug:wrote_outside_worktree:agent-abc123 -->"

        def fake_search(m: str) -> bool:
            return m == original_marker  # only original exists

        with patch("analyst_bug_filer._search_existing_discussions", side_effect=fake_search):
            with patch("analyst_bug_filer._get_repo_id", return_value="REPO_ID"):
                with patch("analyst_bug_filer._get_category_id", return_value="CAT_ID"):
                    with patch(
                        "analyst_bug_filer._create_discussion",
                        return_value="https://github.com/autonomous-agent-7/autonomous-forever/discussions/999",
                    ):
                        filer = AnalystBugFiler()
                        url = filer.file_bug(hit2, dry_run=False)

        assert url is not None
        assert "999" in url


# ---------------------------------------------------------------------------
# Test 3: unsupported classifier is skipped
# ---------------------------------------------------------------------------

class TestUnsupportedClassifier:
    def test_unsupported_classifier_returns_none(self, capsys):
        hit = {**SAMPLE_HIT, "classifier": "some_other_classifier"}
        filer = AnalystBugFiler()
        result = filer.file_bug(hit, dry_run=True)
        assert result is None

    def test_unsupported_classifier_stderr_message(self, capsys):
        hit = {**SAMPLE_HIT, "classifier": "unknown_classifier"}
        filer = AnalystBugFiler()
        filer.file_bug(hit, dry_run=True)
        err = capsys.readouterr().err
        assert "unsupported" in err or "Skipping" in err


# ---------------------------------------------------------------------------
# Test 4: severity below threshold is skipped
# ---------------------------------------------------------------------------

class TestSeverityThreshold:
    def test_low_severity_skipped(self, capsys):
        hit = {**SAMPLE_HIT, "severity": "low"}
        filer = AnalystBugFiler()
        result = filer.file_bug(hit, dry_run=True)
        assert result is None

    def test_medium_severity_filed(self):
        hit = {**SAMPLE_HIT, "severity": "medium"}
        filer = AnalystBugFiler()
        url = filer.file_bug(hit, dry_run=True)
        assert url == DRY_RUN_URL

    def test_high_severity_filed(self):
        hit = {**SAMPLE_HIT, "severity": "high"}
        filer = AnalystBugFiler()
        url = filer.file_bug(hit, dry_run=True)
        assert url == DRY_RUN_URL


# ---------------------------------------------------------------------------
# Test 5: file_wrote_outside_worktree_hits deduplication
# ---------------------------------------------------------------------------

class TestFileFindingsBatch:
    def test_deduplicates_by_agent_id(self):
        """Two findings with same agent_id → only one filing attempt."""
        findings = [
            {**SAMPLE_HIT, "classifier": "wrote_outside_worktree"},
            {**SAMPLE_HIT, "classifier": "wrote_outside_worktree", "detail": "second hit"},
        ]
        with patch.object(AnalystBugFiler, "file_bug", return_value=DRY_RUN_URL) as mock_fb:
            results = file_wrote_outside_worktree_hits(findings, dry_run=True)
        # Only one unique agent_id → file_bug called once
        assert mock_fb.call_count == 1

    def test_filters_out_other_classifiers(self):
        """Findings from other classifiers are ignored."""
        findings = [
            {**SAMPLE_HIT, "classifier": "something_else"},
            {**SAMPLE_HIT, "classifier": "wrote_outside_worktree"},
        ]
        with patch.object(AnalystBugFiler, "file_bug", return_value=DRY_RUN_URL) as mock_fb:
            results = file_wrote_outside_worktree_hits(findings, dry_run=True)
        assert mock_fb.call_count == 1

    def test_returns_filed_true_on_success(self):
        findings = [{**SAMPLE_HIT}]
        with patch.object(AnalystBugFiler, "file_bug", return_value=DRY_RUN_URL):
            results = file_wrote_outside_worktree_hits(findings, dry_run=True)
        assert len(results) == 1
        assert results[0]["filed"] is True
        assert results[0]["agent_id"] == SAMPLE_HIT["agent_id"]

    def test_returns_filed_false_when_none(self):
        findings = [{**SAMPLE_HIT}]
        with patch.object(AnalystBugFiler, "file_bug", return_value=None):
            results = file_wrote_outside_worktree_hits(findings, dry_run=False)
        assert len(results) == 1
        assert results[0]["filed"] is False

    def test_empty_findings_returns_empty(self):
        results = file_wrote_outside_worktree_hits([], dry_run=True)
        assert results == []

    def test_prefers_high_over_medium_for_same_agent(self):
        """When same agent_id has both high and medium hits, high is used."""
        findings = [
            {**SAMPLE_HIT, "severity": "medium"},
            {**SAMPLE_HIT, "severity": "high"},
        ]
        captured_hits = []

        def capture_hit(hit, dry_run=False, category_name="General"):
            captured_hits.append(hit)
            return DRY_RUN_URL

        with patch.object(AnalystBugFiler, "file_bug", side_effect=capture_hit):
            file_wrote_outside_worktree_hits(findings, dry_run=True)

        assert len(captured_hits) == 1
        assert captured_hits[0]["severity"] == "high"


# ---------------------------------------------------------------------------
# Test 6: CLI --dry-run exits 0 and prints JSON
# ---------------------------------------------------------------------------

class TestCLI:
    def test_cli_dry_run_exit_0(self):
        hit_json = json.dumps(SAMPLE_HIT)
        result = subprocess.run(
            [sys.executable, str(_REPO / "backend" / "analyst_bug_filer.py"),
             "--hit", hit_json, "--dry-run"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def _parse_result_json(self, stdout: str) -> dict:
        """Extract the final result JSON object from CLI stdout.

        The CLI prints human-readable body text before the JSON result dict.
        The result JSON starts after the last occurrence of a line that is
        just '{' (not indented body content).
        """
        # The result JSON is the last {...} block in the output.
        # Use json.JSONDecoder.raw_decode to find and parse it, scanning from the end.
        decoder = json.JSONDecoder()
        # Walk backwards through the string looking for the last parseable JSON object
        text = stdout
        last_valid = None
        idx = 0
        while idx < len(text):
            pos = text.find("{", idx)
            if pos == -1:
                break
            try:
                obj, end = decoder.raw_decode(text, pos)
                if isinstance(obj, dict):
                    last_valid = obj
                idx = pos + 1
            except json.JSONDecodeError:
                idx = pos + 1
        assert last_valid is not None, f"No JSON object found in stdout: {text!r}"
        return last_valid

    def test_cli_dry_run_outputs_json(self):
        hit_json = json.dumps(SAMPLE_HIT)
        result = subprocess.run(
            [sys.executable, str(_REPO / "backend" / "analyst_bug_filer.py"),
             "--hit", hit_json, "--dry-run"],
            capture_output=True, text=True,
        )
        data = self._parse_result_json(result.stdout)
        assert data.get("classifier") == "wrote_outside_worktree"
        assert data.get("dry_run") is True

    def test_cli_dry_run_url_is_placeholder(self):
        hit_json = json.dumps(SAMPLE_HIT)
        result = subprocess.run(
            [sys.executable, str(_REPO / "backend" / "analyst_bug_filer.py"),
             "--hit", hit_json, "--dry-run"],
            capture_output=True, text=True,
        )
        data = self._parse_result_json(result.stdout)
        assert data.get("url") == DRY_RUN_URL

    def test_cli_missing_mode_flag_exits_nonzero(self):
        hit_json = json.dumps(SAMPLE_HIT)
        result = subprocess.run(
            [sys.executable, str(_REPO / "backend" / "analyst_bug_filer.py"),
             "--hit", hit_json],
            capture_output=True, text=True,
        )
        assert result.returncode != 0

    def test_cli_invalid_json_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, str(_REPO / "backend" / "analyst_bug_filer.py"),
             "--hit", "not-json", "--dry-run"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0
