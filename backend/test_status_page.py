"""test_status_page.py — Unit tests for status_page.render_status_page.

Verifies that the Project Health counting block correctly filters closed
discussions so "Discussing / open" only reflects genuinely open rows.
"""

import sys
from pathlib import Path

import pytest

# Ensure backend package is importable when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.status_page import render_status_page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_discussion(
    number: int,
    status: str,
    closed_at: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "number": number,
        "title": f"D#{number}",
        "status": status,
        "created_at": created_at,
        "closed_at": closed_at,
    }


def _parse_metric(content: str, label: str) -> str:
    """Extract the value cell for a given label row from a Markdown table."""
    for line in content.splitlines():
        if f"| {label} |" in line:
            parts = line.split("|")
            # parts: ['', ' label ', ' value ', '']
            return parts[2].strip()
    raise AssertionError(f"Metric '{label}' not found in rendered content")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_closed_discussions_excluded_from_open_count():
    """The 'Discussing / open' row must only count open DISCUSSING+SPEC_READY rows."""
    discussions = [
        # Open, should be counted
        _make_discussion(1, "DISCUSSING"),
        _make_discussion(2, "SPEC_READY"),
        # Closed (has closed_at), must NOT be counted in open metrics
        _make_discussion(3, "DISCUSSING", closed_at="2026-03-01T00:00:00Z"),
        _make_discussion(4, "SPEC_READY", closed_at="2026-03-02T00:00:00Z"),
        # DONE (closed), only counts in Completed
        _make_discussion(5, "DONE", closed_at="2026-03-03T00:00:00Z"),
    ]
    registry = {"discussions": discussions}
    content = render_status_page(registry, metrics=[], commits=[], config={})

    open_count = _parse_metric(content, "Discussing / open")
    assert open_count == "2", (
        f"Expected 2 open discussions, got {open_count!r}. "
        "Closed rows should be excluded from 'Discussing / open'."
    )


def test_in_progress_excludes_closed():
    """'In progress' must only count open IMPLEMENTING/REVIEWING rows."""
    discussions = [
        _make_discussion(1, "IMPLEMENTING"),                                # open
        _make_discussion(2, "REVIEWING"),                                   # open
        _make_discussion(3, "IMPLEMENTING", closed_at="2026-03-01T00:00:00Z"),  # closed
    ]
    registry = {"discussions": discussions}
    content = render_status_page(registry, metrics=[], commits=[], config={})

    in_progress = _parse_metric(content, "In progress")
    assert in_progress == "2", (
        f"Expected 2 in-progress discussions, got {in_progress!r}. "
        "Closed IMPLEMENTING rows must not appear in 'In progress'."
    )


def test_total_and_done_are_lifetime():
    """'Total discussions' and 'Completed (DONE)' are lifetime counts (include closed)."""
    discussions = [
        _make_discussion(1, "DONE", closed_at="2026-03-01T00:00:00Z"),
        _make_discussion(2, "DONE", closed_at="2026-03-02T00:00:00Z"),
        _make_discussion(3, "DISCUSSING"),
    ]
    registry = {"discussions": discussions}
    content = render_status_page(registry, metrics=[], commits=[], config={})

    total = _parse_metric(content, "Total discussions")
    done = _parse_metric(content, "Completed (DONE)")
    assert total == "3", f"Expected total=3, got {total!r}"
    assert done == "2", f"Expected done=2, got {done!r}"


def test_all_open_no_filtering_needed():
    """When all discussions are open, every status is counted normally."""
    discussions = [
        _make_discussion(1, "DISCUSSING"),
        _make_discussion(2, "SPEC_READY"),
        _make_discussion(3, "IMPLEMENTING"),
    ]
    registry = {"discussions": discussions}
    content = render_status_page(registry, metrics=[], commits=[], config={})

    open_count = _parse_metric(content, "Discussing / open")
    in_progress = _parse_metric(content, "In progress")
    assert open_count == "2"
    assert in_progress == "1"


def test_empty_registry_produces_no_registry_data_message():
    """Empty registry renders a fallback message, not a crash."""
    content = render_status_page({}, metrics=[], commits=[], config={})
    assert "No registry data available." in content
