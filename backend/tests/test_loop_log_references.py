"""Tests for backend/loop_log_references.extract_references."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.loop_log_references import extract_references


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refs(log: str):
    return extract_references(log)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_string():
    result = _refs("")
    assert result == {"discussions": [], "prs": []}


def test_none_style_empty():
    """Passing an empty-ish value still returns empty lists."""
    result = _refs("   ")
    assert result["discussions"] == []
    assert result["prs"] == []


def test_no_references():
    result = _refs("Starting loop iteration. All checks passed. Done.")
    assert result == {"discussions": [], "prs": []}


def test_single_discussion_and_pr():
    result = _refs("Processing D#412 and PR #1141.")
    assert result == {"discussions": [412], "prs": [1141]}


def test_multiple_refs():
    result = _refs("Merged PR #1141 and PR #1162. Closed D#412, D#835.")
    assert result["discussions"] == [412, 835]
    assert result["prs"] == [1141, 1162]


def test_deduplication():
    """Duplicate refs are collapsed to a single entry."""
    result = _refs("D#412 and D#412 and PR #1141 and PR #1141")
    assert result == {"discussions": [412], "prs": [1141]}


def test_mixed_case_pr():
    """PR matching is case-insensitive for the prefix."""
    result = _refs("pr #99 and PR #100 and Pr #101")
    assert result["prs"] == [99, 100, 101]


def test_pr_with_and_without_space():
    """Both 'PR#N' and 'PR #N' are captured."""
    result = _refs("See PR#55 and also PR #56")
    assert result["prs"] == [55, 56]


def test_no_false_positive_on_bare_hash():
    """'#123' alone (no D or PR prefix) should NOT match."""
    result = _refs("Closed #123 and fixed #456")
    assert result == {"discussions": [], "prs": []}


def test_word_boundary_guards():
    """'XD#1' should not match D#1 — must have word boundary before D."""
    result = _refs("XD#1 and MYPR#2")
    assert result == {"discussions": [], "prs": []}


def test_cap_at_50_discussions():
    # Generate 60 unique discussion references
    text = " ".join(f"D#{i}" for i in range(1, 61))
    result = _refs(text)
    assert len(result["discussions"]) == 50
    assert result["discussions"] == list(range(1, 51))


def test_cap_at_50_prs():
    text = " ".join(f"PR #{i}" for i in range(100, 161))
    result = _refs(text)
    assert len(result["prs"]) == 50
    assert result["prs"] == list(range(100, 150))


def test_sorted_ascending():
    result = _refs("D#999 D#1 D#42 PR #500 PR #7")
    assert result["discussions"] == [1, 42, 999]
    assert result["prs"] == [7, 500]


def test_real_log_snippet():
    """Realistic log excerpt from an actual loop run."""
    log = """\
[11:01] executor-412: started — implementing #412
[11:03] Processing Discussion D#412 for retention sweep
[11:04] Created PR #1162 for executor worktree
[11:05] Also referencing D#835 and PR #1141 from earlier run
[11:06] executor-412: done — PR #1162 merged
"""
    result = _refs(log)
    assert result["discussions"] == [412, 835]
    assert result["prs"] == [1141, 1162]
