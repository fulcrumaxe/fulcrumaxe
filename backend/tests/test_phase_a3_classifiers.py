"""Tests for Phase A.3 transcript-anomaly classifiers (Discussion #511).

All tests use fixture JSONL files under backend/tests/fixtures/transcripts/phase_a3/.
No LLM calls, no gh API calls, no subprocess side-effects.

HARD RULE: These tests MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "phase_a3"

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from transcript_reader import iter_turns
from testsupport.transcript_fixtures import render_fixture
from run_analyst import (
    Finding,
    classify_git_rm_usage,
    classify_preflight_skipped,
    classify_sensitive_file_unlabeled,
    classify_tool_output_ignored,
    classify_lied_exit_code,
    classify_claim_transcript_mismatch,
    classify_bash_retry_cosmetic_variants,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _turns_from_fixture(classifier_name: str, fixture_file: str):
    """Load turns from a phase_a3 fixture file."""
    path = render_fixture(FIXTURES_DIR / classifier_name / fixture_file)
    return list(iter_turns(path))


# ---------------------------------------------------------------------------
# 1. classify_git_rm_usage
# ---------------------------------------------------------------------------

def test_git_rm_usage_positive():
    turns = _turns_from_fixture("git_rm_usage", "positive.jsonl")
    findings = classify_git_rm_usage(iter(turns))
    assert len(findings) >= 1, "Expected finding for git rm on project file"
    assert findings[0].classifier == "git_rm_usage"
    assert findings[0].severity == "high"


def test_git_rm_usage_negative():
    turns = _turns_from_fixture("git_rm_usage", "negative.jsonl")
    findings = classify_git_rm_usage(iter(turns))
    assert findings == [], f"Expected no findings, got: {findings}"


# ---------------------------------------------------------------------------
# 2. classify_preflight_skipped
# ---------------------------------------------------------------------------

def test_preflight_skipped_positive():
    turns = _turns_from_fixture("preflight_skipped", "positive.jsonl")
    findings = classify_preflight_skipped(iter(turns))
    assert len(findings) >= 1, "Expected finding for missing preflight"
    assert findings[0].classifier == "preflight_skipped"
    assert findings[0].severity == "high"


def test_preflight_skipped_negative():
    turns = _turns_from_fixture("preflight_skipped", "negative.jsonl")
    findings = classify_preflight_skipped(iter(turns))
    assert findings == [], f"Expected no findings, got: {findings}"



# ---------------------------------------------------------------------------
# 4. classify_sensitive_file_unlabeled
# ---------------------------------------------------------------------------

def test_sensitive_file_unlabeled_positive():
    turns = _turns_from_fixture("sensitive_file_unlabeled", "positive.jsonl")
    findings = classify_sensitive_file_unlabeled(iter(turns))
    assert len(findings) >= 1, "Expected finding for unlabeled sensitive file edit"
    assert findings[0].classifier == "sensitive_file_unlabeled"
    assert findings[0].severity == "medium"


def test_sensitive_file_unlabeled_negative():
    turns = _turns_from_fixture("sensitive_file_unlabeled", "negative.jsonl")
    findings = classify_sensitive_file_unlabeled(iter(turns))
    assert findings == [], f"Expected no findings, got: {findings}"


# ---------------------------------------------------------------------------
# 5. classify_tool_output_ignored
# ---------------------------------------------------------------------------

def test_tool_output_ignored_positive():
    turns = _turns_from_fixture("tool_output_ignored", "positive.jsonl")
    findings = classify_tool_output_ignored(iter(turns))
    assert len(findings) >= 1, "Expected finding for ignored tool failure"
    assert findings[0].classifier == "tool_output_ignored"
    assert findings[0].severity == "high"


def test_tool_output_ignored_negative():
    turns = _turns_from_fixture("tool_output_ignored", "negative.jsonl")
    findings = classify_tool_output_ignored(iter(turns))
    assert findings == [], f"Expected no findings, got: {findings}"


def test_tool_output_ignored_real_pivot():
    """Assistant pivots via a new tool call after error — should NOT fire (condition 3)."""
    turns = _turns_from_fixture("tool_output_ignored", "real_pivot.jsonl")
    findings = classify_tool_output_ignored(iter(turns))
    assert findings == [], f"Expected no findings for tool pivot, got: {findings}"


def test_tool_output_ignored_structured_error_parsed():
    """gh api graphql returns structured JSON error, assistant parses it — should NOT fire."""
    turns = _turns_from_fixture("tool_output_ignored", "structured_error_parsed.jsonl")
    findings = classify_tool_output_ignored(iter(turns))
    assert findings == [], f"Expected no findings for parsed structured error, got: {findings}"


# ---------------------------------------------------------------------------
# 6. classify_lied_exit_code
# ---------------------------------------------------------------------------

def test_lied_exit_code_positive():
    turns = _turns_from_fixture("lied_exit_code", "positive.jsonl")
    findings = classify_lied_exit_code(iter(turns))
    assert len(findings) >= 1, "Expected finding for lied exit code"
    assert findings[0].classifier == "lied_exit_code"
    assert findings[0].severity == "high"


def test_lied_exit_code_negative():
    turns = _turns_from_fixture("lied_exit_code", "negative.jsonl")
    findings = classify_lied_exit_code(iter(turns))
    assert findings == [], f"Expected no findings, got: {findings}"


# ---------------------------------------------------------------------------
# 7. classify_claim_transcript_mismatch
# ---------------------------------------------------------------------------

def test_claim_transcript_mismatch_positive():
    turns = _turns_from_fixture("claim_transcript_mismatch", "positive.jsonl")
    findings = classify_claim_transcript_mismatch(iter(turns))
    assert len(findings) >= 1, "Expected finding for claim/transcript mismatch"
    assert findings[0].classifier == "claim_transcript_mismatch"
    assert findings[0].severity == "high"


def test_claim_transcript_mismatch_negative():
    turns = _turns_from_fixture("claim_transcript_mismatch", "negative.jsonl")
    findings = classify_claim_transcript_mismatch(iter(turns))
    assert findings == [], f"Expected no findings, got: {findings}"


# ---------------------------------------------------------------------------
# 8. classify_bash_retry_cosmetic_variants
# ---------------------------------------------------------------------------

def test_bash_retry_cosmetic_variants_positive():
    turns = _turns_from_fixture("bash_retry_cosmetic_variants", "positive.jsonl")
    findings = classify_bash_retry_cosmetic_variants(iter(turns))
    assert len(findings) >= 1, "Expected finding for cosmetic bash retry variants"
    assert findings[0].classifier == "bash_retry_cosmetic_variants"
    assert findings[0].severity == "medium"


def test_bash_retry_cosmetic_variants_negative():
    turns = _turns_from_fixture("bash_retry_cosmetic_variants", "negative.jsonl")
    findings = classify_bash_retry_cosmetic_variants(iter(turns))
    assert findings == [], f"Expected no findings, got: {findings}"


# ---------------------------------------------------------------------------
# Regression: classify_tool_output_ignored must use 3-condition gate
# Catches future stale-base merges that silently revert the tightening.
# ---------------------------------------------------------------------------

def test_tool_output_ignored_uses_is_error_gate():
    """Regression guard: function source must contain is_error check.

    D#530 / PR #528 silently stomped the 3-condition tightening from D#525.
    This test makes the regression explicit so it fails fast if the loose
    heuristic (_TOOL_FAIL_PAT without is_error) is re-introduced.
    """
    import inspect
    source = inspect.getsource(classify_tool_output_ignored)
    assert "is_error" in source, (
        "classify_tool_output_ignored must check is_error — "
        "the 3-condition gate (D#525) was silently removed. "
        "Re-apply: require tr.get('is_error') before _TOOL_REAL_ERROR_PAT check."
    )
