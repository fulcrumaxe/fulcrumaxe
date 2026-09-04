"""Tests for Phase A.5 transcript classifiers (Discussion #548).

All tests use fixture JSONL files under backend/tests/fixtures/transcripts/phase_a5/.
No LLM calls, no gh API calls, no subprocess side-effects.

HARD RULE: These tests MUST NOT invoke claude, claude -p, _start_loop_run,
or trigger /loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts" / "phase_a5"

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from transcript_reader import iter_turns
from testsupport.transcript_fixtures import render_fixture
from run_analyst import (
    Finding,
    _is_self_referential_context,
    classify_thinking_block_excessive,
    classify_full_file_read_when_grep,
    classify_edit_then_revert,
    classify_line_number_drift,
    classify_no_pull_before_branch,
    classify_question_pile_up,
    classify_memory_md_ignored,
    classify_self_referenced_classifier_match,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _turns(classifier_name: str, fixture_file: str):
    path = render_fixture(FIXTURES_DIR / classifier_name / fixture_file)
    return list(iter_turns(path))


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def test_is_self_referential_context_positive():
    text = "The classify_thinking_block_excessive pattern fires here"
    m_start = text.index("classify_")
    assert _is_self_referential_context(text, (m_start, m_start + 9)) is True


def test_is_self_referential_context_negative():
    text = "The server should handle timeouts gracefully and return errors"
    assert _is_self_referential_context(text, (4, 10)) is False


def test_is_self_referential_context_phase_a():
    text = "Discussion D#548 introduced phase_a5 enhancements"
    assert _is_self_referential_context(text, (12, 17)) is True


# ---------------------------------------------------------------------------
# 1. classify_thinking_block_excessive
# ---------------------------------------------------------------------------

def test_thinking_block_excessive_positive():
    turns = _turns("thinking_block_excessive", "positive.jsonl")
    findings = classify_thinking_block_excessive(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "thinking_block_excessive"
    assert findings[0].severity == "low"


def test_thinking_block_excessive_no_fire_self_ref():
    """Self-referential context must suppress the match (meta-region FP)."""
    turns = _turns("thinking_block_excessive", "negative_self_ref.jsonl")
    findings = classify_thinking_block_excessive(iter(turns))
    # The <thinking> block in this fixture contains META_TOKENS — must not fire
    assert findings == []


# ---------------------------------------------------------------------------
# 2. classify_full_file_read_when_grep
# ---------------------------------------------------------------------------

def test_full_file_read_when_grep_positive():
    turns = _turns("full_file_read_when_grep", "positive.jsonl")
    findings = classify_full_file_read_when_grep(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "full_file_read_when_grep"
    assert findings[0].severity == "medium"


def test_full_file_read_when_grep_negative():
    """Small file read followed by edit must not fire."""
    turns = _turns("full_file_read_when_grep", "negative.jsonl")
    findings = classify_full_file_read_when_grep(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 3. classify_edit_then_revert
# ---------------------------------------------------------------------------

def test_edit_then_revert_positive():
    turns = _turns("edit_then_revert", "positive.jsonl")
    findings = classify_edit_then_revert(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "edit_then_revert"
    assert findings[0].severity == "medium"


def test_edit_then_revert_no_fire_self_ref():
    """Edit-then-revert in run_analyst.py itself (classifier code) must be suppressed."""
    turns = _turns("edit_then_revert", "negative_self_ref.jsonl")
    findings = classify_edit_then_revert(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 4. classify_line_number_drift
# ---------------------------------------------------------------------------

def test_line_number_drift_positive():
    turns = _turns("line_number_drift", "positive.jsonl")
    findings = classify_line_number_drift(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "line_number_drift"
    assert findings[0].severity == "medium"


def test_line_number_drift_no_fire_self_ref():
    """Quoting a line number in meta-context (discussing classifiers) must not fire."""
    turns = _turns("line_number_drift", "negative_self_ref.jsonl")
    findings = classify_line_number_drift(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 5. classify_no_pull_before_branch
# ---------------------------------------------------------------------------

def test_no_pull_before_branch_positive():
    turns = _turns("no_pull_before_branch", "positive.jsonl")
    findings = classify_no_pull_before_branch(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "no_pull_before_branch"
    assert findings[0].severity == "medium"


def test_no_pull_before_branch_negative():
    """git pull --ff-only before checkout must suppress the finding."""
    turns = _turns("no_pull_before_branch", "negative.jsonl")
    findings = classify_no_pull_before_branch(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 6. classify_question_pile_up
# ---------------------------------------------------------------------------

def test_question_pile_up_positive():
    turns = _turns("question_pile_up", "positive.jsonl")
    findings = classify_question_pile_up(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "question_pile_up"
    assert findings[0].severity == "low"


def test_question_pile_up_no_fire_self_ref():
    """Questions in a meta-context turn (about classifiers) must be suppressed."""
    turns = _turns("question_pile_up", "negative_self_ref.jsonl")
    findings = classify_question_pile_up(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 7. classify_memory_md_ignored
# ---------------------------------------------------------------------------

def test_memory_md_ignored_positive():
    turns = _turns("memory_md_ignored", "positive.jsonl")
    findings = classify_memory_md_ignored(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "memory_md_ignored"
    assert findings[0].severity == "medium"


def test_memory_md_ignored_no_fire_self_ref():
    """git rm in meta-context (discussing classifiers) must not fire."""
    turns = _turns("memory_md_ignored", "negative_self_ref.jsonl")
    findings = classify_memory_md_ignored(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# 8. classify_self_referenced_classifier_match
# ---------------------------------------------------------------------------

def test_self_referenced_classifier_match_positive():
    """FP catcher fires when a pattern matched inside meta-context."""
    turns = _turns("self_referenced_classifier_match", "positive.jsonl")
    findings = classify_self_referenced_classifier_match(iter(turns))
    assert len(findings) >= 1
    assert findings[0].classifier == "self_referenced_classifier_match"
    assert findings[0].severity == "medium"


def test_self_referenced_classifier_match_negative():
    """Normal code-change turn with no meta-context must not fire."""
    turns = _turns("self_referenced_classifier_match", "negative.jsonl")
    findings = classify_self_referenced_classifier_match(iter(turns))
    assert findings == []


# ---------------------------------------------------------------------------
# All 8 classifiers are in _PHASE_A5_CLASSIFIERS
# ---------------------------------------------------------------------------

def test_phase_a5_classifiers_list_complete():
    from run_analyst import _PHASE_A5_CLASSIFIERS
    names = {fn.__name__ for fn in _PHASE_A5_CLASSIFIERS}
    required = {
        "classify_retro_skipped",
        "classify_thinking_block_excessive",
        "classify_full_file_read_when_grep",
        "classify_edit_then_revert",
        "classify_line_number_drift",
        "classify_no_pull_before_branch",
        "classify_question_pile_up",
        "classify_memory_md_ignored",
        "classify_self_referenced_classifier_match",
    }
    missing = required - names
    assert not missing, f"Missing classifiers in _PHASE_A5_CLASSIFIERS: {missing}"
