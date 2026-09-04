"""Tests for Phase A.4 transcript-anomaly classifiers (Discussion #523).

All tests use fixture JSONL files under backend/tests/fixtures/transcripts/phase_a4/.
No LLM calls, no gh API calls, no subprocess side-effects.

HARD RULE: These tests MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop.
"""

from __future__ import annotations

import sys
import unittest.mock
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "phase_a4"

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from transcript_reader import iter_turns
from testsupport.transcript_fixtures import render_fixture
from run_analyst import (
    Finding,
    classify_token_in_team_log,
    classify_curl_insecure_or_k,
    classify_python_verify_false,
    classify_localstorage_token,
    classify_nonexistent_pr_or_disc,
    classify_emoji_in_code_or_commit,
    classify_trailing_summary,
    classify_no_status_when_blocked,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _turns(classifier_name: str, fixture_file: str):
    path = render_fixture(FIXTURES_DIR / classifier_name / fixture_file)
    return list(iter_turns(path))


# ---------------------------------------------------------------------------
# 1. classify_token_in_team_log
# ---------------------------------------------------------------------------

def test_token_in_team_log_positive():
    turns = _turns("token_in_team_log", "positive.jsonl")
    findings = classify_token_in_team_log(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "token_in_team_log"
    assert findings[0].severity == "high"


def test_token_in_team_log_negative():
    turns = _turns("token_in_team_log", "negative.jsonl")
    findings = classify_token_in_team_log(iter(turns))
    assert findings == []


def test_token_in_team_log_no_fire_on_self_edit():
    """D#542: editing run_analyst.py containing a token pattern must NOT fire."""
    turns = _turns("token_in_team_log", "negative_self_edit.jsonl")
    findings = classify_token_in_team_log(iter(turns))
    assert findings == []


def test_token_in_team_log_no_fire_on_discussion_body():
    """D#542: posting a Discussion body containing a token mention must NOT fire."""
    turns = _turns("token_in_team_log", "negative_discussion_body.jsonl")
    findings = classify_token_in_team_log(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 2. classify_curl_insecure_or_k
# ---------------------------------------------------------------------------

def test_curl_insecure_or_k_positive():
    turns = _turns("curl_insecure_or_k", "positive.jsonl")
    findings = classify_curl_insecure_or_k(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "curl_insecure_or_k"
    assert findings[0].severity == "high"


def test_curl_insecure_or_k_negative():
    turns = _turns("curl_insecure_or_k", "negative.jsonl")
    findings = classify_curl_insecure_or_k(iter(turns))
    assert findings == []


def test_curl_insecure_no_fire_on_self_edit():
    """D#542: editing run_analyst.py to mention curl -k must NOT fire."""
    turns = _turns("curl_insecure_or_k", "negative_self_edit.jsonl")
    findings = classify_curl_insecure_or_k(iter(turns))
    assert findings == []


def test_curl_insecure_no_fire_on_discussion_body():
    """D#542: posting a Discussion body describing curl -k must NOT fire."""
    turns = _turns("curl_insecure_or_k", "negative_discussion_body.jsonl")
    findings = classify_curl_insecure_or_k(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 3. classify_python_verify_false
# ---------------------------------------------------------------------------

def test_python_verify_false_positive():
    turns = _turns("python_verify_false", "positive.jsonl")
    findings = classify_python_verify_false(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "python_verify_false"
    assert findings[0].severity == "high"


def test_python_verify_false_negative():
    turns = _turns("python_verify_false", "negative.jsonl")
    findings = classify_python_verify_false(iter(turns))
    assert findings == []


def test_python_verify_false_no_fire_on_self_edit():
    """D#542: editing run_analyst.py with verify=False in the new_string must NOT fire."""
    turns = _turns("python_verify_false", "negative_self_edit.jsonl")
    findings = classify_python_verify_false(iter(turns))
    assert findings == []


def test_python_verify_false_no_fire_on_discussion_body():
    """D#542: posting a Discussion body describing verify=False must NOT fire."""
    turns = _turns("python_verify_false", "negative_discussion_body.jsonl")
    findings = classify_python_verify_false(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 4. classify_localstorage_token
# ---------------------------------------------------------------------------

def test_localstorage_token_positive():
    turns = _turns("localstorage_token", "positive.jsonl")
    findings = classify_localstorage_token(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "localstorage_token"
    assert findings[0].severity == "high"


def test_localstorage_token_negative():
    turns = _turns("localstorage_token", "negative.jsonl")
    findings = classify_localstorage_token(iter(turns))
    assert findings == []


def test_localstorage_token_no_fire_on_self_edit():
    """D#542: editing run_analyst.py with localStorage pattern must NOT fire."""
    turns = _turns("localstorage_token", "negative_self_edit.jsonl")
    findings = classify_localstorage_token(iter(turns))
    assert findings == []


def test_localstorage_token_no_fire_on_discussion_body():
    """D#542: posting a Discussion body describing localStorage.setItem must NOT fire."""
    turns = _turns("localstorage_token", "negative_discussion_body.jsonl")
    findings = classify_localstorage_token(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 5. classify_nonexistent_pr_or_disc
# ---------------------------------------------------------------------------

def test_nonexistent_pr_or_disc_positive():
    """Uses mocked _gh_ref_exists to avoid real network calls."""
    turns = _turns("nonexistent_pr_or_disc", "positive.jsonl")
    with unittest.mock.patch("run_analyst._gh_ref_exists", return_value=False):
        findings = classify_nonexistent_pr_or_disc(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "nonexistent_pr_or_disc"
    assert findings[0].severity == "medium"


def test_nonexistent_pr_or_disc_negative():
    """No #NNN references in text — no findings regardless of mock."""
    turns = _turns("nonexistent_pr_or_disc", "negative.jsonl")
    with unittest.mock.patch("run_analyst._gh_ref_exists", return_value=False):
        findings = classify_nonexistent_pr_or_disc(iter(turns))
    assert findings == []


def test_nonexistent_pr_or_disc_ref_exists():
    """#NNN reference where both PR and Discussion exist — no finding."""
    turns = _turns("nonexistent_pr_or_disc", "positive.jsonl")
    with unittest.mock.patch("run_analyst._gh_ref_exists", return_value=True):
        findings = classify_nonexistent_pr_or_disc(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 6. classify_emoji_in_code_or_commit
# ---------------------------------------------------------------------------

def test_emoji_in_code_or_commit_positive():
    turns = _turns("emoji_in_code_or_commit", "positive.jsonl")
    findings = classify_emoji_in_code_or_commit(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "emoji_in_code_or_commit"
    assert findings[0].severity == "medium"


def test_emoji_in_code_or_commit_negative():
    turns = _turns("emoji_in_code_or_commit", "negative.jsonl")
    findings = classify_emoji_in_code_or_commit(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 7. classify_trailing_summary
# ---------------------------------------------------------------------------

def test_trailing_summary_positive():
    turns = _turns("trailing_summary", "positive.jsonl")
    findings = classify_trailing_summary(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "trailing_summary"
    assert findings[0].severity == "low"


def test_trailing_summary_negative():
    turns = _turns("trailing_summary", "negative.jsonl")
    findings = classify_trailing_summary(iter(turns))
    assert findings == []


def test_trailing_summary_user_asked():
    """When user explicitly asks for a summary, no finding should fire."""
    from transcript_reader import TranscriptTurn
    turns = [
        TranscriptTurn(0, "user", "Can you summarize what you did?", [], [], {}),
        TranscriptTurn(1, "assistant", "## Summary\n\nI added a feature.", [], [], {}),
    ]
    findings = classify_trailing_summary(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 8. classify_no_status_when_blocked
# ---------------------------------------------------------------------------

def test_no_status_when_blocked_positive():
    turns = _turns("no_status_when_blocked", "positive.jsonl")
    findings = classify_no_status_when_blocked(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "no_status_when_blocked"
    assert findings[0].severity == "medium"


def test_no_status_when_blocked_negative():
    turns = _turns("no_status_when_blocked", "negative.jsonl")
    findings = classify_no_status_when_blocked(iter(turns))
    assert findings == []
