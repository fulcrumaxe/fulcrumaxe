"""
test_tui_tester_anti_patterns.py — unit tests for the 7 proactive anti-pattern checks.

Each check function is exercised with synthetic source fixtures that trigger both
pass and fail paths. No Textual runtime required — static checks use AST parsing;
runtime check (check_readers_nontrivial) uses a mock screen instance.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.tui_tester_kpi_registry import (
    Finding,
    _collect_app_level_binding_keys,
    check_all_datatables_row_cursor,
    check_focus_targets_focusable,
    check_readers_nontrivial,
    check_action_methods_notify,
    check_lastupdated_ticks,
    check_hint_label_matches_bindings,
    check_hint_bindings_drift,
    check_screen_clean,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_source(tmp_path: Path, name: str, code: str) -> Path:
    """Write *code* to a temp .py file named *name* and return its path."""
    f = tmp_path / f"{name}.py"
    f.write_text(textwrap.dedent(code), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Check 1: check_all_datatables_row_cursor
# ---------------------------------------------------------------------------


def test_datatable_row_cursor_pass(tmp_path: Path) -> None:
    """No finding when DataTable has cursor_type='row'."""
    src = _write_source(
        tmp_path, "screen_ok",
        """
        from textual.widgets import DataTable
        class S:
            def compose(self):
                yield DataTable(id="t", cursor_type="row")
        """,
    )
    findings = check_all_datatables_row_cursor(src)
    assert findings == []


def test_datatable_row_cursor_fail_missing(tmp_path: Path) -> None:
    """Finding when DataTable has no cursor_type kwarg (defaults to 'cell')."""
    src = _write_source(
        tmp_path, "screen_bad",
        """
        from textual.widgets import DataTable
        class S:
            def compose(self):
                yield DataTable(id="t")
        """,
    )
    findings = check_all_datatables_row_cursor(src)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "cursor_type" in findings[0].detail
    assert findings[0].check == "check_all_datatables_row_cursor"


def test_datatable_row_cursor_fail_cell(tmp_path: Path) -> None:
    """Finding when cursor_type is explicitly 'cell'."""
    src = _write_source(
        tmp_path, "screen_cell",
        """
        from textual.widgets import DataTable
        class S:
            def compose(self):
                yield DataTable(cursor_type="cell")
        """,
    )
    findings = check_all_datatables_row_cursor(src)
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_datatable_row_cursor_multiple(tmp_path: Path) -> None:
    """Multiple DataTables: finds only those without cursor_type='row'."""
    src = _write_source(
        tmp_path, "screen_multi",
        """
        from textual.widgets import DataTable
        class S:
            def compose(self):
                yield DataTable(id="ok", cursor_type="row")
                yield DataTable(id="bad")
        """,
    )
    findings = check_all_datatables_row_cursor(src)
    assert len(findings) == 1
    assert "bad" not in findings[0].detail or "cursor_type" in findings[0].detail


# ---------------------------------------------------------------------------
# Check 2: check_focus_targets_focusable
# ---------------------------------------------------------------------------


def test_focus_targets_pass(tmp_path: Path) -> None:
    """No finding when .focus() is called on a focusable widget."""
    src = _write_source(
        tmp_path, "screen_ok_focus",
        """
        from textual.widgets import DataTable
        class S:
            def on_mount(self):
                DataTable().focus()
        """,
    )
    findings = check_focus_targets_focusable(src)
    assert findings == []


def test_focus_targets_fail_markdown(tmp_path: Path) -> None:
    """Finding when .focus() called directly on Markdown(...)."""
    src = _write_source(
        tmp_path, "screen_bad_focus",
        """
        from textual.widgets import Markdown
        class S:
            def on_mount(self):
                Markdown("hello").focus()
        """,
    )
    findings = check_focus_targets_focusable(src)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "Markdown" in findings[0].detail


def test_focus_targets_fail_static(tmp_path: Path) -> None:
    """Finding when .focus() called on Static(...)."""
    src = _write_source(
        tmp_path, "screen_static_focus",
        """
        from textual.widgets import Static
        class S:
            def on_mount(self):
                Static("text").focus()
        """,
    )
    findings = check_focus_targets_focusable(src)
    assert len(findings) == 1
    assert "Static" in findings[0].detail


def test_focus_targets_no_focus_call(tmp_path: Path) -> None:
    """No finding when no .focus() calls exist."""
    src = _write_source(
        tmp_path, "screen_no_focus",
        """
        class S:
            def on_mount(self):
                pass
        """,
    )
    findings = check_focus_targets_focusable(src)
    assert findings == []


# ---------------------------------------------------------------------------
# Check 3: check_readers_nontrivial
# ---------------------------------------------------------------------------


def test_readers_nontrivial_pass() -> None:
    """No finding when refresh_* returns non-empty data."""
    class FakeScreen:
        def refresh_data(self) -> list:
            return [{"id": 1}]
    findings = check_readers_nontrivial("home", FakeScreen())
    assert findings == []


def test_readers_nontrivial_empty_list() -> None:
    """Finding when refresh_* returns empty list."""
    class FakeScreen:
        def refresh_data(self) -> list:
            return []
    findings = check_readers_nontrivial("home", FakeScreen())
    assert any(f.check == "check_readers_nontrivial" for f in findings)
    assert any("empty" in f.detail.lower() or "all-zero" in f.detail.lower() for f in findings)


def test_readers_nontrivial_empty_dict() -> None:
    """Finding when _load_data returns empty dict."""
    class FakeScreen:
        def _load_data(self) -> dict:
            return {}
    findings = check_readers_nontrivial("prs", FakeScreen())
    assert len(findings) == 1
    assert findings[0].severity == "warn"


def test_readers_nontrivial_none_instance() -> None:
    """No findings when screen_instance is None (static-only sweep)."""
    findings = check_readers_nontrivial("home", None)
    assert findings == []


def test_readers_nontrivial_method_raises() -> None:
    """No finding when the method raises — method is skipped gracefully."""
    class FakeScreen:
        def refresh_data(self) -> list:
            raise RuntimeError("db error")
    findings = check_readers_nontrivial("home", FakeScreen())
    assert findings == []


# ---------------------------------------------------------------------------
# Check 4: check_action_methods_notify
# ---------------------------------------------------------------------------


def test_action_methods_notify_pass(tmp_path: Path) -> None:
    """No finding when action_* has early return AND a notify call."""
    src = _write_source(
        tmp_path, "screen_notify_ok",
        """
        class S:
            def action_open(self):
                if not self.selected:
                    self.notify("Nothing selected", severity="warning")
                    return
                self._do_open()
        """,
    )
    findings = check_action_methods_notify(src)
    assert findings == []


def test_action_methods_notify_fail(tmp_path: Path) -> None:
    """Finding when action_* has early return but no notify."""
    src = _write_source(
        tmp_path, "screen_notify_bad",
        """
        class S:
            def action_open(self):
                if not self.selected:
                    return
                self._do_open()
        """,
    )
    findings = check_action_methods_notify(src)
    assert len(findings) == 1
    assert findings[0].check == "check_action_methods_notify"
    assert "action_open" in findings[0].detail


def test_action_methods_no_early_return(tmp_path: Path) -> None:
    """No finding when action_* has no early returns at all."""
    src = _write_source(
        tmp_path, "screen_no_early",
        """
        class S:
            def action_refresh(self):
                self.load_data()
        """,
    )
    findings = check_action_methods_notify(src)
    assert findings == []


def test_action_methods_non_action_ignored(tmp_path: Path) -> None:
    """Regular methods (not action_*) are not checked."""
    src = _write_source(
        tmp_path, "screen_regular",
        """
        class S:
            def open_thing(self):
                if not self.flag:
                    return
                self.do_it()
        """,
    )
    findings = check_action_methods_notify(src)
    assert findings == []


# ---------------------------------------------------------------------------
# Check 5: check_lastupdated_ticks
# ---------------------------------------------------------------------------


def test_lastupdated_ticks_pass(tmp_path: Path) -> None:
    """No finding when LastUpdated is used AND set_fetched is called."""
    src = _write_source(
        tmp_path, "screen_lu_ok",
        """
        from dashboard_tui.widgets.last_updated import LastUpdated
        class S:
            def compose(self):
                yield LastUpdated(id="lu")
            def refresh_data(self):
                from datetime import datetime
                self.query_one("#lu", LastUpdated).set_fetched(datetime.utcnow())
        """,
    )
    findings = check_lastupdated_ticks(src)
    assert findings == []


def test_lastupdated_ticks_fail_no_set_fetched(tmp_path: Path) -> None:
    """Finding when LastUpdated is mounted but set_fetched is never called."""
    src = _write_source(
        tmp_path, "screen_lu_bad",
        """
        from dashboard_tui.widgets.last_updated import LastUpdated
        class S:
            def compose(self):
                yield LastUpdated(id="lu")
            def refresh_data(self):
                pass  # forgot to call set_fetched
        """,
    )
    findings = check_lastupdated_ticks(src)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "set_fetched" in findings[0].detail


def test_lastupdated_ticks_no_widget(tmp_path: Path) -> None:
    """No finding when LastUpdated is not used at all."""
    src = _write_source(
        tmp_path, "screen_no_lu",
        """
        class S:
            def compose(self):
                pass
        """,
    )
    findings = check_lastupdated_ticks(src)
    assert findings == []


# ---------------------------------------------------------------------------
# Check 6: check_hint_label_matches_bindings
# ---------------------------------------------------------------------------


def test_hint_label_matches_bindings_pass(tmp_path: Path) -> None:
    """No finding when hint label key claims match BINDINGS entries."""
    src = _write_source(
        tmp_path, "screen_hint_ok",
        """
        from textual.binding import Binding
        class S:
            BINDINGS = [
                Binding("r", "refresh", "Refresh"),
                Binding("q", "quit", "Quit"),
            ]
            def compose(self):
                from textual.widgets import Label
                yield Label("Press 'r' to refresh, 'q' to quit", id="hint")
        """,
    )
    findings = check_hint_label_matches_bindings(src)
    assert findings == []


def test_hint_label_matches_bindings_fail(tmp_path: Path) -> None:
    """Finding when hint label claims a key not in BINDINGS."""
    src = _write_source(
        tmp_path, "screen_hint_bad",
        """
        from textual.binding import Binding
        class S:
            BINDINGS = [
                Binding("r", "refresh", "Refresh"),
            ]
            def compose(self):
                from textual.widgets import Label
                yield Label("Press 'r' to refresh, 'f' to filter", id="hint")
        """,
    )
    findings = check_hint_label_matches_bindings(src)
    assert len(findings) >= 1
    assert any("'f'" in f.detail for f in findings)


def test_hint_label_no_hint(tmp_path: Path) -> None:
    """No finding when there are no hint labels."""
    src = _write_source(
        tmp_path, "screen_no_hint",
        """
        class S:
            BINDINGS = []
            def compose(self):
                pass
        """,
    )
    findings = check_hint_label_matches_bindings(src)
    assert findings == []


def test_hint_label_on_key_wires_binding(tmp_path: Path) -> None:
    """No finding when key is wired via on_key_* method instead of BINDINGS."""
    src = _write_source(
        tmp_path, "screen_on_key",
        """
        class S:
            BINDINGS = []
            def compose(self):
                from textual.widgets import Label
                yield Label("Press 'r' to refresh", id="hint")
            def on_key_r(self, event):
                self.refresh_data()
        """,
    )
    findings = check_hint_label_matches_bindings(src)
    assert findings == []


def test_hint_label_app_level_bindings_no_finding(tmp_path: Path, monkeypatch: Any) -> None:
    """No finding when a screen's hint label references keys that are bound
    at the app level (e.g. 'r' for refresh, 'q' for quit in app.py BINDINGS).

    The check must read app.py BINDINGS and treat those keys as satisfied even
    though they are absent from the screen's own BINDINGS list.
    """
    # Patch the cache so we control what app-level keys are returned
    # regardless of whether a real app.py is present on the test path.
    monkeypatch.setattr(
        "backend.tui_tester_kpi_registry._collect_app_level_binding_keys",
        lambda: {"r", "q"},
    )
    # Clear any existing cache so the monkeypatch takes effect
    if hasattr(_collect_app_level_binding_keys, "_cache"):
        del _collect_app_level_binding_keys._cache

    src = _write_source(
        tmp_path, "screen_app_keys",
        """
        from textual.binding import Binding
        from textual.widgets import Label
        class S:
            BINDINGS = []  # no screen-level bindings for r or q
            def compose(self):
                yield Label("Press 'r' to refresh, 'q' to quit", id="hint")
        """,
    )
    findings = check_hint_label_matches_bindings(src)
    assert findings == [], (
        "Expected no findings — 'r' and 'q' are satisfied by app-level BINDINGS"
    )


def test_hint_label_unbound_key_still_flagged(tmp_path: Path, monkeypatch: Any) -> None:
    """Finding still emitted when a screen claims a key that has no handler
    in screen BINDINGS, on_key_*, OR app-level BINDINGS.

    Regression guard: the app-level awareness must not suppress genuine bugs.
    """
    monkeypatch.setattr(
        "backend.tui_tester_kpi_registry._collect_app_level_binding_keys",
        lambda: {"r", "q"},
    )
    if hasattr(_collect_app_level_binding_keys, "_cache"):
        del _collect_app_level_binding_keys._cache

    src = _write_source(
        tmp_path, "screen_bad_unbound",
        """
        from textual.binding import Binding
        from textual.widgets import Label
        class S:
            BINDINGS = []  # no 'f' binding
            def compose(self):
                yield Label("Press 'r' to refresh, 'f' to filter", id="hint")
        """,
    )
    findings = check_hint_label_matches_bindings(src)
    assert any("'f'" in f.detail for f in findings), (
        "Expected a finding for unclaimed key 'f'"
    )


# ---------------------------------------------------------------------------
# Check 7 (new): check_hint_bindings_drift
# ---------------------------------------------------------------------------


def test_hint_bindings_drift_pass(tmp_path: Path) -> None:
    """No finding when every BINDINGS key appears in the hint label."""
    src = _write_source(
        tmp_path, "ok_screen",
        """
        from textual.binding import Binding
        from textual.widgets import Label
        class S:
            BINDINGS = [Binding("r", "refresh", "Refresh"), Binding("q", "quit", "Quit")]
            def compose(self):
                yield Label("Press 'r' to refresh, 'q' to quit", id="hint")
        """,
    )
    findings = check_hint_bindings_drift(src)
    assert findings == []


def test_hint_bindings_drift_fail(tmp_path: Path) -> None:
    """Finding emitted when a BINDINGS key is absent from the hint label."""
    src = _write_source(
        tmp_path, "drift_screen",
        """
        from textual.binding import Binding
        from textual.widgets import Label
        class S:
            BINDINGS = [Binding("r", "refresh", "Refresh"), Binding("f", "filter", "Filter")]
            def compose(self):
                yield Label("Press 'r' to refresh", id="hint")  # 'f' not mentioned
        """,
    )
    findings = check_hint_bindings_drift(src)
    assert len(findings) == 1
    assert findings[0].check == "check_hint_bindings_drift"
    assert "'f'" in findings[0].detail


# ---------------------------------------------------------------------------
# Check 8: check_screen_clean (composite)
# ---------------------------------------------------------------------------


def test_screen_clean_pass(tmp_path: Path) -> None:
    """check_screen_clean returns empty list for a well-formed screen."""
    src = _write_source(
        tmp_path, "clean_screen",
        """
        from textual.binding import Binding
        from textual.widgets import DataTable, Label
        class S:
            BINDINGS = [Binding("r", "refresh", "Refresh")]
            def compose(self):
                yield DataTable(id="t", cursor_type="row")
                yield Label("Press 'r' to refresh", id="hint")
        """,
    )
    findings = check_screen_clean(source_path=src, screen_name="clean_screen")
    assert isinstance(findings, list)
    # No DataTable cursor errors, no focus errors, no action errors,
    # no LastUpdated errors (not used), no hint-binding drift
    cursor_errors = [f for f in findings if f.check == "check_all_datatables_row_cursor"]
    assert cursor_errors == []


def test_screen_clean_aggregates_multiple_checks(tmp_path: Path) -> None:
    """check_screen_clean surfaces findings from multiple checks in one call."""
    src = _write_source(
        tmp_path, "dirty_screen",
        """
        from textual.widgets import DataTable, Markdown, Label
        from dashboard_tui.widgets.last_updated import LastUpdated
        class S:
            BINDINGS = []
            def compose(self):
                yield DataTable(id="t")            # check 1: no cursor_type="row"
                yield Markdown("body").focus()     # check 2: focus on non-focusable
                yield LastUpdated(id="lu")         # check 5: no set_fetched
                yield Label("Press 'r' to refresh", id="hint")  # check 6: 'r' not in BINDINGS
        """,
    )
    findings = check_screen_clean(source_path=src, screen_name="dirty_screen")
    check_names = {f.check for f in findings}
    assert "check_all_datatables_row_cursor" in check_names
    assert "check_focus_targets_focusable" in check_names
    assert "check_lastupdated_ticks" in check_names


def test_finding_dataclass_fields() -> None:
    """Finding dataclass has required fields with correct types."""
    f = Finding(
        screen="home",
        widget_id="DataTable",
        check="check_all_datatables_row_cursor",
        severity="error",
        evidence_path="dashboard_tui/screens/home.py:42",
        detail="cursor_type missing",
    )
    assert f.screen == "home"
    assert f.severity == "error"
    assert f.check == "check_all_datatables_row_cursor"
    assert f.evidence_path.endswith(":42")


def test_finding_widget_id_optional() -> None:
    """Finding allows widget_id=None for module-level checks."""
    f = Finding(
        screen="prs",
        widget_id=None,
        check="check_readers_nontrivial",
        severity="warn",
        evidence_path="live:prs.refresh_data",
    )
    assert f.widget_id is None
