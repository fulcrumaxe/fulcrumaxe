"""test_row_select_handlers.py — verify row-click handler presence for D#740.

Each screen with interactive tables must either:
  1. Define on_data_table_row_selected(), OR
  2. Be registered as row_select_passive=True in tui_tester_kpi_registry.

Tests are structural (inspect, not runtime Textual) — no terminal needed.
"""

from __future__ import annotations

import pytest

# Every test in this file inspects a dashboard_tui screen class, so the whole
# module is moot in a tree without dashboard_tui/ (an adopter clone legitimately
# has no TUI). The imports are inside the test bodies, so without this the
# absence shows up as 11 failures rather than an honest skip.
pytest.importorskip("dashboard_tui", reason="dashboard_tui/ not present in this tree")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_row_handler(cls) -> bool:
    """True if cls defines on_data_table_row_selected (not inherited from Container)."""
    return "on_data_table_row_selected" in cls.__dict__


# ---------------------------------------------------------------------------
# Screen classes that must have handlers
# ---------------------------------------------------------------------------


def test_discussions_has_row_handler():
    from dashboard_tui.screens.discussions import DiscussionsScreen
    assert _has_row_handler(DiscussionsScreen), (
        "DiscussionsScreen must define on_data_table_row_selected"
    )


def test_runs_has_row_handler():
    from dashboard_tui.screens.runs import RunsScreen
    assert _has_row_handler(RunsScreen), (
        "RunsScreen must define on_data_table_row_selected"
    )


def test_loop_health_has_row_handler():
    from dashboard_tui.screens.loop_health import LoopHealthScreen
    assert _has_row_handler(LoopHealthScreen), (
        "LoopHealthScreen must define on_data_table_row_selected"
    )


def test_agent_feed_has_row_handler():
    from dashboard_tui.screens.agent_feed import AgentFeedScreen
    assert _has_row_handler(AgentFeedScreen), (
        "AgentFeedScreen must define on_data_table_row_selected"
    )


def test_loop_controller_has_row_handler():
    from dashboard_tui.screens.loop_controller import LoopControllerScreen
    assert _has_row_handler(LoopControllerScreen), (
        "LoopControllerScreen must define on_data_table_row_selected"
    )


def test_ideas_has_row_handler():
    from dashboard_tui.screens.ideas import IdeasScreen
    assert _has_row_handler(IdeasScreen), (
        "IdeasScreen must define on_data_table_row_selected"
    )


# ---------------------------------------------------------------------------
# Stats is registered passive (no v1 handler)
# ---------------------------------------------------------------------------


def test_stats_registered_passive():
    from backend.tui_tester_kpi_registry import REGISTRY
    shape = REGISTRY.get("stats")
    assert shape is not None, "stats tab missing from REGISTRY"
    assert shape.row_select_passive is True, (
        "stats tab must be registered as row_select_passive=True in REGISTRY "
        "(kpi-table and classifier-table are no-op for v1)"
    )


# ---------------------------------------------------------------------------
# URL validation: discussions and ideas use int(number) before interpolating
# ---------------------------------------------------------------------------


def test_discussions_handler_validates_int(monkeypatch):
    """Enter (on_data_table_row_selected) now focuses the body pane — does NOT open browser.
    Browser-open moved to action_open_in_browser ('o' key), which uses _current_number
    that was already validated as int by on_data_table_row_highlighted.
    """
    from dashboard_tui.screens.discussions import DiscussionsScreen

    opened_urls = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    screen = DiscussionsScreen.__new__(DiscussionsScreen)
    screen._current_number = None

    class FakeEvent:
        row_key = "row-0"
        cursor_row = 0

    # query_one raises — simulates no Markdown widget mounted (unit context)
    def _raise(*a, **kw):
        raise Exception("not mounted")
    screen.query_one = _raise

    # Enter must not open browser (it focuses body pane; query_one raises → caught silently)
    screen.on_data_table_row_selected(FakeEvent())
    assert opened_urls == [], "Enter must not open browser (moved to 'o' key)"

    # action_open_in_browser with a valid _current_number DOES open browser
    screen._current_number = 42
    screen.action_open_in_browser()
    assert len(opened_urls) == 1
    assert "/discussions/42" in opened_urls[0]


def test_discussions_handler_rejects_non_int(monkeypatch):
    """action_open_in_browser only fires with _current_number already set as int.
    When _current_number is None (no row highlighted), no URL is opened.
    """
    from dashboard_tui.screens.discussions import DiscussionsScreen

    opened_urls = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    screen = DiscussionsScreen.__new__(DiscussionsScreen)
    screen._current_number = None
    # notify() requires a running app; stub it out since we only care about webbrowser
    screen.notify = lambda *a, **kw: None

    # With no row highlighted, action_open_in_browser is a no-op
    screen.action_open_in_browser()
    assert opened_urls == [], "No highlighted row must not trigger webbrowser.open"


def test_ideas_handler_validates_int(monkeypatch):
    """Ideas rows have string idea IDs (e.g. 'idea-abc123'), not discussion numbers.
    on_data_table_row_selected must be a no-op — write actions use keybinds instead.
    """
    from dashboard_tui.screens.ideas import IdeasScreen

    opened_urls = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    screen = IdeasScreen.__new__(IdeasScreen)

    class FakeTable:
        def get_row(self, key):
            return ["idea-abc123", "idea title", "3", "pending", "2d"]

    class FakeEvent:
        row_key = "row-0"
        cursor_row = 0

    screen.query_one = lambda selector, cls=None: FakeTable()
    screen.on_data_table_row_selected(FakeEvent())

    # Ideas screen row handler is a no-op — idea IDs are not discussion numbers
    assert opened_urls == [], "Ideas screen row handler must not open a browser URL"


def test_ideas_handler_rejects_non_int(monkeypatch):
    """Ideas handler must NOT open a URL regardless of row content."""
    from dashboard_tui.screens.ideas import IdeasScreen

    opened_urls = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    screen = IdeasScreen.__new__(IdeasScreen)

    class FakeTableBad:
        def get_row(self, key):
            return ["../etc/passwd", "evil", "bug", "open", "1h"]

    class FakeEvent:
        row_key = "row-0"
        cursor_row = 0

    screen.query_one = lambda selector, cls=None: FakeTableBad()
    screen.on_data_table_row_selected(FakeEvent())

    assert opened_urls == [], "Ideas screen row handler must never open browser URL"


# ---------------------------------------------------------------------------
# AgentFeedScreen layout — detail label must not be clipped (D#745)
# ---------------------------------------------------------------------------


def test_agent_feed_detail_label_has_fixed_height():
    """#agent-feed-detail must have an explicit height so it renders below the table.

    The DataTable was consuming all available vertical space via height:1fr, which
    clipped the detail label to zero height.  CSS must give the label a fixed height
    and constrain the table to 100% of its wrapper instead.
    """
    import re
    from dashboard_tui.screens.agent_feed import AgentFeedScreen

    css = AgentFeedScreen.DEFAULT_CSS

    # detail label must have a non-zero fixed height
    detail_block = re.search(r"#agent-feed-detail\s*\{([^}]*)\}", css, re.DOTALL)
    assert detail_block, "#agent-feed-detail block missing from DEFAULT_CSS"
    assert re.search(r"height\s*:\s*[1-9]\d*\s*;", detail_block.group(1)), (
        "#agent-feed-detail must declare a fixed height (e.g. height: 8;)"
    )

    # DataTable must NOT use height:1fr (would steal space from the detail label)
    table_block = re.search(r"#agent-feed-table\s*\{([^}]*)\}", css, re.DOTALL)
    assert table_block, "#agent-feed-table block missing from DEFAULT_CSS"
    assert not re.search(r"height\s*:\s*1fr", table_block.group(1)), (
        "#agent-feed-table must not use height:1fr — use height:100% so the "
        "detail label gets its fixed space"
    )

    # AgentFeedScreen must declare layout:vertical so children stack
    screen_block = re.search(r"AgentFeedScreen\s*\{([^}]*)\}", css, re.DOTALL)
    assert screen_block, "AgentFeedScreen block missing from DEFAULT_CSS"
    assert re.search(r"layout\s*:\s*vertical", screen_block.group(1)), (
        "AgentFeedScreen must declare layout: vertical;"
    )
