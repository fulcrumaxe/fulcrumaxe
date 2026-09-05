"""test_ideas_filters.py — unit tests for dashboard_tui.screens.ideas_filters.

Pure function tests — no I/O, no textual, no data_layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the worktree root is importable
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# dashboard_tui/ is not present in every tree that runs this suite (an adopter
# clone legitimately has no TUI). Skip rather than raise at collection time: an
# uncaught ImportError here aborts the whole run for every other test file too.
pytest.importorskip(
    "dashboard_tui.screens.ideas_filters",
    reason="dashboard_tui/ not present in this tree",
)

from dashboard_tui.screens.ideas_filters import (  # noqa: E402
    TYPE_FILTERS,
    STATUS_FILTERS,
    count_by_status,
    extract_status,
    extract_type,
    filter_and_sort,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _disc(number: int, title: str, body: str = "", labels: list | None = None, updated_at: str = "") -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": labels or [],
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# extract_status
# ---------------------------------------------------------------------------

class TestExtractStatus:
    def test_body_marker_spec_ready(self):
        d = _disc(1, "t", body="<!-- STATUS:SPEC_READY -->")
        assert extract_status(d) == "SPEC_READY"

    def test_body_marker_discussing(self):
        d = _disc(1, "t", body="<!-- STATUS:DISCUSSING -->")
        assert extract_status(d) == "DISCUSSING"

    def test_body_marker_done(self):
        d = _disc(1, "t", body="<!-- STATUS:DONE -->")
        assert extract_status(d) == "DONE"

    def test_label_fallback_spec_ready(self):
        d = _disc(1, "t", labels=["SPEC_READY"])
        assert extract_status(d) == "SPEC_READY"

    def test_label_dict_format(self):
        d = _disc(1, "t", labels=[{"name": "DISCUSSING"}])
        assert extract_status(d) == "DISCUSSING"

    def test_unknown_when_no_marker_or_label(self):
        d = _disc(1, "t")
        assert extract_status(d) == "UNKNOWN"

    def test_case_insensitive_marker(self):
        d = _disc(1, "t", body="<!-- status:spec_ready -->")
        assert extract_status(d) == "SPEC_READY"

    def test_empty_body_empty_labels(self):
        d = _disc(1, "t", body="", labels=[])
        assert extract_status(d) == "UNKNOWN"


# ---------------------------------------------------------------------------
# extract_type
# ---------------------------------------------------------------------------

class TestExtractType:
    def test_feature_prefix(self):
        assert extract_type("[Feature] Add widget") == "Feature"

    def test_bug_prefix(self):
        assert extract_type("[Bug] fix crash") == "Bug"

    def test_critical_prefix(self):
        assert extract_type("[Critical] outage") == "Critical"

    def test_no_prefix(self):
        assert extract_type("Some plain title") == "Other"

    def test_malformed_prefix_no_close(self):
        # No closing bracket — should not raise
        result = extract_type("[NoClose fix")
        assert isinstance(result, str)

    def test_empty_string(self):
        assert extract_type("") == "Other"

    def test_none_like_empty(self):
        # extract_type signature takes str, but guard against accidental None
        result = extract_type("")
        assert result == "Other"

    def test_extra_whitespace_in_bracket(self):
        assert extract_type("[ Feature ] title") == "Feature"


# ---------------------------------------------------------------------------
# filter_and_sort — status filtering
# ---------------------------------------------------------------------------

class TestFilterAndSort:
    def _make_set(self) -> list[dict]:
        return [
            _disc(1, "[Feature] alpha", body="<!-- STATUS:SPEC_READY -->", updated_at="2026-05-10T08:00:00Z"),
            _disc(2, "[Bug] beta",      body="<!-- STATUS:DISCUSSING -->",  updated_at="2026-05-12T08:00:00Z"),
            _disc(3, "[Feature] gamma", body="<!-- STATUS:DONE -->",        updated_at="2026-05-11T08:00:00Z"),
            _disc(4, "[Critical] delta",body="<!-- STATUS:SPEC_READY -->",  updated_at="2026-05-13T08:00:00Z"),
        ]

    def test_open_filter_excludes_done(self):
        rows = filter_and_sort(self._make_set(), status_filter="Open")
        statuses = [r["_status"] for r in rows]
        assert "DONE" not in statuses

    def test_open_filter_includes_spec_ready_and_discussing(self):
        rows = filter_and_sort(self._make_set(), status_filter="Open")
        statuses = set(r["_status"] for r in rows)
        assert statuses == {"SPEC_READY", "DISCUSSING"}

    def test_spec_ready_filter(self):
        rows = filter_and_sort(self._make_set(), status_filter="SPEC_READY")
        assert all(r["_status"] == "SPEC_READY" for r in rows)
        assert len(rows) == 2

    def test_all_filter_includes_done(self):
        rows = filter_and_sort(self._make_set(), status_filter="All")
        statuses = set(r["_status"] for r in rows)
        assert "DONE" in statuses

    def test_sort_spec_ready_before_discussing_before_done(self):
        rows = filter_and_sort(self._make_set(), status_filter="All")
        priority_seen = [r["_status"] for r in rows]
        spec_idx = next(i for i, s in enumerate(priority_seen) if s == "SPEC_READY")
        disc_idx = next(i for i, s in enumerate(priority_seen) if s == "DISCUSSING")
        done_idx = next(i for i, s in enumerate(priority_seen) if s == "DONE")
        assert spec_idx < disc_idx < done_idx

    def test_updated_at_desc_within_bucket(self):
        """Within SPEC_READY bucket: newer updatedAt comes first."""
        rows = filter_and_sort(self._make_set(), status_filter="SPEC_READY")
        # disc 4 updated 2026-05-13, disc 1 updated 2026-05-10 — 4 must be first
        assert rows[0]["number"] == 4
        assert rows[1]["number"] == 1

    def test_type_filter_feature(self):
        rows = filter_and_sort(self._make_set(), type_filter="Feature", status_filter="All")
        assert all(r["_type"] == "Feature" for r in rows)

    def test_type_filter_all_includes_everything(self):
        rows = filter_and_sort(self._make_set(), type_filter="All", status_filter="All")
        assert len(rows) == 4

    def test_row_cap(self):
        many = [
            _disc(i, f"[Feature] d{i}", body="<!-- STATUS:SPEC_READY -->", updated_at="2026-01-01T00:00:00Z")
            for i in range(300)
        ]
        rows = filter_and_sort(many, max_rows=200)
        assert len(rows) == 200

    def test_empty_input(self):
        assert filter_and_sort([]) == []

    def test_malformed_title_no_crash(self):
        d = _disc(99, "[[bad] title", body="<!-- STATUS:DISCUSSING -->", updated_at="2026-01-01T00:00:00Z")
        rows = filter_and_sort([d], status_filter="All")
        assert len(rows) == 1  # didn't crash

    def test_synthetic_keys_added(self):
        rows = filter_and_sort(self._make_set(), status_filter="All")
        for r in rows:
            assert "_status" in r
            assert "_type" in r


# ---------------------------------------------------------------------------
# count_by_status
# ---------------------------------------------------------------------------

class TestCountByStatus:
    def test_counts_correct(self):
        discs = [
            _disc(1, "t", body="<!-- STATUS:SPEC_READY -->"),
            _disc(2, "t", body="<!-- STATUS:SPEC_READY -->"),
            _disc(3, "t", body="<!-- STATUS:DISCUSSING -->"),
            _disc(4, "t", body="<!-- STATUS:DONE -->"),
        ]
        c = count_by_status(discs)
        assert c == {"SPEC_READY": 2, "DISCUSSING": 1, "DONE": 1}

    def test_unknown_not_counted(self):
        discs = [_disc(1, "t")]
        c = count_by_status(discs)
        assert c == {"SPEC_READY": 0, "DISCUSSING": 0, "DONE": 0}

    def test_empty(self):
        assert count_by_status([]) == {"SPEC_READY": 0, "DISCUSSING": 0, "DONE": 0}


# ---------------------------------------------------------------------------
# Filter cycle lists completeness
# ---------------------------------------------------------------------------

class TestFilterLists:
    def test_type_filters_contains_expected(self):
        for expected in ("All", "Feature", "Bug", "Critical", "Small", "Strategy"):
            assert expected in TYPE_FILTERS

    def test_status_filters_contains_expected(self):
        for expected in ("Open", "SPEC_READY", "DISCUSSING", "DONE", "All"):
            assert expected in STATUS_FILTERS

    def test_open_is_first_status(self):
        assert STATUS_FILTERS[0] == "Open"
