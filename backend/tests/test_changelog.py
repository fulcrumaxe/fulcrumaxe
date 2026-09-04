"""Behavioral tests for backend/changelog.py.

Covers:
  - _parse_merge_date: valid ISO-8601, Z suffix, empty, malformed
  - _extract_discussion_number: found, not found, empty/None body, multiple refs
  - _sort_prs: ordering, empty list, ties, missing mergedAt
  - generate_changelog: empty list, single PR, multiple dates grouped, unknown date,
    pipe in title (escaping in PR-Index), header/footer structure, author
  - generate_pr_index: header, table rows, discussion link, em-dash when absent,
    pipe escaping in title, footer
  - No real files are touched — generate_* return strings only.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend._repo import REPO as REPO_SLUG
from backend.changelog import (
    _extract_discussion_number,
    _parse_merge_date,
    _sort_prs,
    _REPO_ROOT,
    generate_changelog,
    generate_pr_index,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# REPO_SLUG mirrors backend.changelog's own repo resolution (backend._repo.REPO)
# rather than hard-coding a parallel literal — a hard-coded copy here silently
# drifted from reality once (D#1870: this constant still said the pre-rename
# "fulcrumaxe" slug after the resolver was fixed to resolve correctly).


def _make_pr(
    number: int = 1,
    title: str = "Test PR",
    body: str = "",
    merged_at: str = "2026-05-01T12:00:00Z",
    author_login: str = "alice",
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "mergedAt": merged_at,
        "author": {"login": author_login},
    }


# ---------------------------------------------------------------------------
# _parse_merge_date
# ---------------------------------------------------------------------------


class TestParseMergeDate:
    def test_standard_utc_z_suffix(self):
        assert _parse_merge_date("2026-05-01T12:34:56Z") == "2026-05-01"

    def test_offset_plus_zero(self):
        assert _parse_merge_date("2026-05-01T12:34:56+00:00") == "2026-05-01"

    def test_end_of_day(self):
        assert _parse_merge_date("2026-12-31T23:59:59Z") == "2026-12-31"

    def test_empty_string_returns_empty(self):
        assert _parse_merge_date("") == ""

    def test_none_returns_empty(self):
        # None is not explicitly typed but should be handled gracefully via falsiness
        assert _parse_merge_date(None) == ""  # type: ignore[arg-type]

    def test_malformed_returns_empty(self):
        assert _parse_merge_date("not-a-date") == ""

    def test_partial_date_only_returns_empty(self):
        # "2026-05-01" is a valid isoformat date but lacks time component — still valid
        result = _parse_merge_date("2026-05-01")
        # datetime.fromisoformat accepts date-only strings; result should be 2026-05-01
        assert result == "2026-05-01"

    def test_date_preserved_exactly(self):
        assert _parse_merge_date("2026-01-15T00:00:01Z") == "2026-01-15"


# ---------------------------------------------------------------------------
# _extract_discussion_number
# ---------------------------------------------------------------------------


class TestExtractDiscussionNumber:
    def test_finds_first_hash_ref(self):
        assert _extract_discussion_number("Related to #42") == "42"

    def test_finds_ref_at_start(self):
        assert _extract_discussion_number("#7 was the spec") == "7"

    def test_returns_first_when_multiple(self):
        # Should return the first match only
        assert _extract_discussion_number("closes #10 and #20") == "10"

    def test_empty_body_returns_empty(self):
        assert _extract_discussion_number("") == ""

    def test_none_body_returns_empty(self):
        assert _extract_discussion_number(None) == ""  # type: ignore[arg-type]

    def test_no_hash_ref_returns_empty(self):
        assert _extract_discussion_number("No discussion reference here") == ""

    def test_hash_with_multi_digit_number(self):
        assert _extract_discussion_number("Implements #1234") == "1234"

    def test_text_without_number_after_hash_returns_empty(self):
        # A bare '#' with no digits should not match
        assert _extract_discussion_number("# Heading with no number") == ""

    def test_inline_code_with_hash(self):
        result = _extract_discussion_number("See PR #99 for context")
        assert result == "99"


# ---------------------------------------------------------------------------
# _sort_prs
# ---------------------------------------------------------------------------


class TestSortPrs:
    def test_empty_list_returns_empty(self):
        assert _sort_prs([]) == []

    def test_single_pr_returned_as_is(self):
        pr = _make_pr(number=1, merged_at="2026-05-01T00:00:00Z")
        result = _sort_prs([pr])
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_most_recent_first(self):
        older = _make_pr(number=1, merged_at="2026-04-01T00:00:00Z")
        newer = _make_pr(number=2, merged_at="2026-05-01T00:00:00Z")
        result = _sort_prs([older, newer])
        assert result[0]["number"] == 2
        assert result[1]["number"] == 1

    def test_three_prs_correct_order(self):
        prs = [
            _make_pr(number=3, merged_at="2026-03-01T00:00:00Z"),
            _make_pr(number=1, merged_at="2026-05-01T00:00:00Z"),
            _make_pr(number=2, merged_at="2026-04-01T00:00:00Z"),
        ]
        result = _sort_prs(prs)
        assert [pr["number"] for pr in result] == [1, 2, 3]

    def test_missing_merged_at_sorts_last(self):
        with_date = _make_pr(number=1, merged_at="2026-05-01T00:00:00Z")
        without_date = _make_pr(number=2, merged_at="")
        without_date["mergedAt"] = None  # type: ignore[assignment]
        result = _sort_prs([without_date, with_date])
        assert result[0]["number"] == 1  # dated PR comes first (desc sort)

    def test_does_not_mutate_original_list(self):
        prs = [
            _make_pr(number=2, merged_at="2026-04-01T00:00:00Z"),
            _make_pr(number=1, merged_at="2026-05-01T00:00:00Z"),
        ]
        original_first = prs[0]["number"]
        _sort_prs(prs)
        assert prs[0]["number"] == original_first  # original unchanged


# ---------------------------------------------------------------------------
# generate_changelog
# ---------------------------------------------------------------------------


class TestGenerateChangelog:
    def test_empty_prs_contains_no_merged(self):
        output = generate_changelog([])
        assert "No merged PRs found." in output

    def test_empty_prs_has_header(self):
        output = generate_changelog([])
        assert "# Changelog" in output

    def test_empty_prs_has_footer(self):
        output = generate_changelog([])
        assert "Auto-generated by [[changelog.py]]" in output

    def test_single_pr_title_present(self):
        pr = _make_pr(number=10, title="Add feature X", merged_at="2026-05-01T10:00:00Z")
        output = generate_changelog([pr])
        assert "Add feature X" in output

    def test_single_pr_number_linked(self):
        pr = _make_pr(number=10, merged_at="2026-05-01T10:00:00Z")
        output = generate_changelog([pr])
        assert "#10" in output
        assert f"https://github.com/{REPO_SLUG}/pull/10" in output

    def test_single_pr_author_mentioned(self):
        pr = _make_pr(number=5, author_login="bob", merged_at="2026-05-01T10:00:00Z")
        output = generate_changelog([pr])
        assert "@bob" in output

    def test_single_pr_date_section_header(self):
        pr = _make_pr(number=1, merged_at="2026-05-03T10:00:00Z")
        output = generate_changelog([pr])
        assert "## 2026-05-03" in output

    def test_multiple_prs_same_date_grouped(self):
        prs = [
            _make_pr(number=1, title="First", merged_at="2026-05-01T08:00:00Z"),
            _make_pr(number=2, title="Second", merged_at="2026-05-01T18:00:00Z"),
        ]
        output = generate_changelog(prs)
        # Only one date header for 2026-05-01
        assert output.count("## 2026-05-01") == 1
        assert "First" in output
        assert "Second" in output

    def test_multiple_dates_most_recent_section_first(self):
        prs = [
            _make_pr(number=1, merged_at="2026-04-01T00:00:00Z"),
            _make_pr(number=2, merged_at="2026-05-01T00:00:00Z"),
        ]
        output = generate_changelog(prs)
        idx_may = output.index("## 2026-05-01")
        idx_apr = output.index("## 2026-04-01")
        assert idx_may < idx_apr  # May section appears before April section

    def test_pr_with_missing_merged_at_goes_to_unknown(self):
        pr = _make_pr(number=99, merged_at="")
        output = generate_changelog([pr])
        assert "## unknown" in output

    def test_has_generated_comment(self):
        output = generate_changelog([])
        assert output.startswith("<!-- generated:")

    def test_no_real_file_written(self, tmp_path):
        # generate_changelog is pure — it returns a string, never writes files
        output = generate_changelog([_make_pr()])
        assert isinstance(output, str)
        assert len(output) > 0

    def test_missing_author_uses_unknown(self):
        pr = _make_pr(number=3, merged_at="2026-05-01T00:00:00Z")
        pr["author"] = None  # type: ignore[assignment]
        output = generate_changelog([pr])
        assert "@unknown" in output

    def test_none_title_renders_fallback(self):
        # generate_changelog uses `pr.get("title") or "(no title)"` so a key that
        # exists with value None falls back to the placeholder, not the string "None".
        pr = _make_pr(number=4, merged_at="2026-05-01T00:00:00Z")
        pr["title"] = None  # type: ignore[assignment]
        output = generate_changelog([pr])
        assert "(no title)" in output
        assert "None" not in output


# ---------------------------------------------------------------------------
# generate_pr_index
# ---------------------------------------------------------------------------


class TestGeneratePrIndex:
    def test_empty_prs_contains_no_merged(self):
        output = generate_pr_index([])
        assert "No merged PRs found." in output

    def test_empty_prs_has_header(self):
        output = generate_pr_index([])
        assert "# PR Index" in output

    def test_empty_prs_has_footer(self):
        output = generate_pr_index([])
        assert "Auto-generated by [[changelog.py]]" in output

    def test_table_header_present(self):
        pr = _make_pr(number=1)
        output = generate_pr_index([pr])
        assert "| PR | Title | Discussion | Merged |" in output

    def test_table_separator_present(self):
        pr = _make_pr(number=1)
        output = generate_pr_index([pr])
        assert "|----|-------|------------|--------|" in output

    def test_pr_number_linked_in_table(self):
        pr = _make_pr(number=77)
        output = generate_pr_index([pr])
        assert f"[#77](https://github.com/{REPO_SLUG}/pull/77)" in output

    def test_discussion_link_present_when_body_has_ref(self):
        pr = _make_pr(number=5, body="Closes #42")
        output = generate_pr_index([pr])
        assert f"[#42](https://github.com/{REPO_SLUG}/discussions/42)" in output

    def test_discussion_cell_is_em_dash_when_no_ref(self):
        pr = _make_pr(number=5, body="No discussion here")
        output = generate_pr_index([pr])
        assert "| — |" in output or "—" in output

    def test_pipe_in_title_escaped(self):
        pr = _make_pr(number=6, title="A | B title")
        output = generate_pr_index([pr])
        assert "A \\| B title" in output

    def test_merged_date_in_row(self):
        pr = _make_pr(number=8, merged_at="2026-05-10T15:00:00Z")
        output = generate_pr_index([pr])
        assert "2026-05-10" in output

    def test_ordering_most_recent_first(self):
        prs = [
            _make_pr(number=1, merged_at="2026-04-01T00:00:00Z"),
            _make_pr(number=2, merged_at="2026-05-01T00:00:00Z"),
        ]
        output = generate_pr_index(prs)
        idx_2 = output.index("[#2]")
        idx_1 = output.index("[#1]")
        assert idx_2 < idx_1  # PR #2 (newer) appears before PR #1 (older)

    def test_no_real_file_written(self):
        output = generate_pr_index([_make_pr()])
        assert isinstance(output, str)
        assert len(output) > 0

    def test_has_generated_comment(self):
        output = generate_pr_index([])
        assert output.startswith("<!-- generated:")

    def test_multiple_prs_all_appear_in_table(self):
        prs = [_make_pr(number=i, merged_at=f"2026-05-{i:02d}T00:00:00Z") for i in range(1, 6)]
        output = generate_pr_index(prs)
        for i in range(1, 6):
            assert f"[#{i}]" in output


# ---------------------------------------------------------------------------
# CLI main() — --output-dir
# ---------------------------------------------------------------------------


class TestMainOutputDir:
    """--output-dir must write only under the given dir, never under
    _REPO_ROOT/wiki — derived artifacts belong in the GitHub Wiki clone,
    not the source tree (D#1908)."""

    def test_output_dir_writes_only_under_given_dir(self, tmp_path):
        out_dir = tmp_path / "wiki-clone"

        repo_wiki_changelog = _REPO_ROOT / "wiki" / "Changelog.md"
        repo_wiki_pr_index = _REPO_ROOT / "wiki" / "PR-Index.md"
        before_changelog_mtime = repo_wiki_changelog.stat().st_mtime if repo_wiki_changelog.exists() else None
        before_pr_index_mtime = repo_wiki_pr_index.stat().st_mtime if repo_wiki_pr_index.exists() else None

        with patch("backend.changelog.load_merged_prs", return_value=[_make_pr()]):
            rc = main(["generate", "--output-dir", str(out_dir)])
        assert rc == 0

        changelog_file = out_dir / "Changelog.md"
        pr_index_file = out_dir / "PR-Index.md"
        assert changelog_file.exists()
        assert pr_index_file.exists()

        # The source-tree wiki/ dir must be completely untouched: unchanged
        # mtime if the file already existed, still absent if it didn't.
        if before_changelog_mtime is None:
            assert not repo_wiki_changelog.exists()
        else:
            assert repo_wiki_changelog.stat().st_mtime == before_changelog_mtime
        if before_pr_index_mtime is None:
            assert not repo_wiki_pr_index.exists()
        else:
            assert repo_wiki_pr_index.stat().st_mtime == before_pr_index_mtime

    def test_output_dir_stdout_names_files_under_given_dir(self, tmp_path, capsys):
        out_dir = tmp_path / "wiki-clone"
        with patch("backend.changelog.load_merged_prs", return_value=[_make_pr()]):
            rc = main(["generate", "--output-dir", str(out_dir)])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert str(out_dir) in out
        assert str(_REPO_ROOT / "wiki") not in out
