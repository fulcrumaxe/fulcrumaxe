"""
Tests for backend/changelog.py — generate_changelog() and generate_pr_index().

Uses in-memory mock PR dicts — no subprocess calls, no network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.changelog import (
    generate_changelog,
    generate_pr_index,
    _parse_merge_date,
    _extract_discussion_number,
    _sort_prs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pr(number, title, merged_at="2026-01-10T12:00:00Z", body="", author="bot"):
    return {
        "number": number,
        "title": title,
        "mergedAt": merged_at,
        "body": body,
        "author": {"login": author},
    }


# ---------------------------------------------------------------------------
# _parse_merge_date
# ---------------------------------------------------------------------------

def test_parse_merge_date_iso_with_z():
    assert _parse_merge_date("2026-01-15T08:30:00Z") == "2026-01-15"


def test_parse_merge_date_empty_string():
    assert _parse_merge_date("") == ""


def test_parse_merge_date_none():
    assert _parse_merge_date(None) == ""


def test_parse_merge_date_invalid_format():
    assert _parse_merge_date("not-a-date") == ""


# ---------------------------------------------------------------------------
# _extract_discussion_number
# ---------------------------------------------------------------------------

def test_extract_discussion_number_simple():
    assert _extract_discussion_number("Implements #42") == "42"


def test_extract_discussion_number_missing():
    assert _extract_discussion_number("No references here") == ""


def test_extract_discussion_number_empty_body():
    assert _extract_discussion_number("") == ""


def test_extract_discussion_number_none_body():
    assert _extract_discussion_number(None) == ""


def test_extract_discussion_number_first_reference():
    # Should return the first match
    assert _extract_discussion_number("See #10 and #20") == "10"


# ---------------------------------------------------------------------------
# _sort_prs
# ---------------------------------------------------------------------------

def test_sort_prs_descending():
    prs = [
        _pr(1, "Old", "2026-01-01T00:00:00Z"),
        _pr(2, "New", "2026-01-15T00:00:00Z"),
        _pr(3, "Middle", "2026-01-10T00:00:00Z"),
    ]
    sorted_prs = _sort_prs(prs)
    assert sorted_prs[0]["number"] == 2
    assert sorted_prs[-1]["number"] == 1


def test_sort_prs_empty():
    assert _sort_prs([]) == []


# ---------------------------------------------------------------------------
# generate_changelog
# ---------------------------------------------------------------------------

def test_generate_changelog_returns_string():
    out = generate_changelog([])
    assert isinstance(out, str)


def test_generate_changelog_empty_prs():
    out = generate_changelog([])
    assert "# Changelog" in out
    assert "No merged PRs found" in out


def test_generate_changelog_contains_pr_title():
    prs = [_pr(10, "Add dark mode")]
    out = generate_changelog(prs)
    assert "Add dark mode" in out


def test_generate_changelog_contains_pr_link():
    prs = [_pr(10, "Something")]
    out = generate_changelog(prs)
    assert "github.com" in out
    assert "/pull/10" in out


def test_generate_changelog_contains_author():
    prs = [_pr(10, "Feature", author="alice")]
    out = generate_changelog(prs)
    assert "@alice" in out


def test_generate_changelog_date_grouping():
    prs = [
        _pr(1, "PR one", "2026-01-10T08:00:00Z"),
        _pr(2, "PR two", "2026-01-10T18:00:00Z"),
        _pr(3, "PR three", "2026-01-11T10:00:00Z"),
    ]
    out = generate_changelog(prs)
    # Both dates should appear as section headers
    assert "## 2026-01-11" in out
    assert "## 2026-01-10" in out
    # Newer date section should appear before older
    assert out.index("## 2026-01-11") < out.index("## 2026-01-10")


def test_generate_changelog_has_footer():
    out = generate_changelog([_pr(1, "X")])
    assert "Do not edit manually" in out


def test_generate_changelog_has_generated_comment():
    out = generate_changelog([])
    assert "<!-- generated:" in out


# ---------------------------------------------------------------------------
# generate_pr_index
# ---------------------------------------------------------------------------

def test_generate_pr_index_returns_string():
    out = generate_pr_index([])
    assert isinstance(out, str)


def test_generate_pr_index_empty_prs():
    out = generate_pr_index([])
    assert "# PR Index" in out
    assert "No merged PRs found" in out


def test_generate_pr_index_contains_table_headers():
    prs = [_pr(1, "Feature")]
    out = generate_pr_index(prs)
    assert "| PR |" in out
    assert "| Title |" in out
    assert "| Discussion |" in out
    assert "| Merged |" in out


def test_generate_pr_index_row_with_discussion_link():
    prs = [_pr(55, "Implement feature", body="This implements #108 from the spec.")]
    out = generate_pr_index(prs)
    assert "#108" in out
    assert "/discussions/108" in out


def test_generate_pr_index_row_without_discussion():
    prs = [_pr(55, "Hotfix", body="No discussion ref")]
    out = generate_pr_index(prs)
    # Should show dash for missing discussion
    assert "—" in out


def test_generate_pr_index_escapes_pipe_in_title():
    prs = [_pr(1, "Feature A|B")]
    out = generate_pr_index(prs)
    assert "Feature A\\|B" in out


def test_generate_pr_index_merged_at_date():
    prs = [_pr(1, "X", merged_at="2026-03-05T10:00:00Z")]
    out = generate_pr_index(prs)
    assert "2026-03-05" in out


def test_generate_pr_index_sorted_newest_first():
    prs = [
        _pr(1, "Old", "2026-01-01T00:00:00Z"),
        _pr(2, "New", "2026-01-20T00:00:00Z"),
    ]
    out = generate_pr_index(prs)
    # PR #2 should appear before PR #1 (newest first)
    assert out.index("#2") < out.index("#1")


def test_generate_pr_index_has_footer():
    out = generate_pr_index([_pr(1, "X")])
    assert "Do not edit manually" in out
