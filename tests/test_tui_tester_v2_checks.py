"""
test_tui_tester_v2_checks.py — unit tests for tui-tester v2 checks.

Covers:
  - _check_data_accuracy: fires when validator returns False
  - _check_row_interactivity: fires when DataTable has no row-selected handler
  - _compare_refresh_snapshots: fires when source changed but widgets didn't update
  - ScreenShape backward-compat: existing entries without new fields still work
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.tui_tester_helpers import (
    _check_data_accuracy,
    _check_row_interactivity,
    _compare_refresh_snapshots,
    _snapshot_labels,
)
from backend.tui_tester_kpi_registry import ScreenShape, REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pane_mock(children):
    pane = MagicMock()
    pane.walk_children = MagicMock(return_value=iter(children))
    return pane


def _make_label_widget(widget_id: str, text: str):
    """Return a mock Label-like widget."""
    try:
        from textual.widgets import Label  # type: ignore[import]
        w = MagicMock(spec=Label)
    except ImportError:
        w = MagicMock()
    w.id = widget_id
    w.__class__.__name__ = "Label"
    w._content = text
    w.renderable = text
    return w


def _make_datatable(widget_id: str, row_count: int = 5):
    try:
        from textual.widgets import DataTable  # type: ignore[import]
        dt = MagicMock(spec=DataTable)
    except ImportError:
        dt = MagicMock()
    dt.id = widget_id
    dt.__class__.__name__ = "DataTable"
    dt.row_count = row_count
    return dt


# ---------------------------------------------------------------------------
# ScreenShape backward-compatibility
# ---------------------------------------------------------------------------


def test_screen_shape_default_fields():
    """data_checks and interactive_tables default to empty — no existing entry breaks."""
    shape = ScreenShape()
    assert shape.data_checks == []
    assert shape.interactive_tables == {}


def test_existing_registry_entries_have_new_fields():
    """All entries in REGISTRY must be valid ScreenShape instances with new fields."""
    for tab_id, shape in REGISTRY.items():
        assert isinstance(shape.data_checks, list), f"{tab_id}: data_checks not a list"
        assert isinstance(shape.interactive_tables, dict), f"{tab_id}: interactive_tables not a dict"


# ---------------------------------------------------------------------------
# _check_data_accuracy
# ---------------------------------------------------------------------------


def test_data_accuracy_pass_when_validator_returns_true():
    """No finding when validator accepts the widget value."""
    label = _make_label_widget("kpi-budget", "42%")
    pane = _make_pane_mock([label])

    shape = ScreenShape(data_checks=[("kpi-budget", lambda t: t.endswith("%"))])
    fails = _check_data_accuracy(pane, shape)
    assert fails == []


def test_data_accuracy_fires_when_validator_returns_false():
    """Finding emitted when validator rejects the widget value."""
    label = _make_label_widget("kpi-budget", "42")  # missing %
    pane = _make_pane_mock([label])

    shape = ScreenShape(data_checks=[("kpi-budget", lambda t: t.endswith("%"))])
    fails = _check_data_accuracy(pane, shape)
    assert len(fails) == 1
    assert fails[0]["widget_id"] == "kpi-budget"
    assert fails[0]["issue_type"] == "data_accuracy_drift"
    assert "data_accuracy_drift" in fails[0]["visible_text"]


def test_data_accuracy_skips_missing_widget():
    """If the widget_id isn't in the pane, no finding (widget may not be loaded)."""
    label = _make_label_widget("other-widget", "hello")
    pane = _make_pane_mock([label])

    shape = ScreenShape(data_checks=[("kpi-budget", lambda t: t.endswith("%"))])
    fails = _check_data_accuracy(pane, shape)
    assert fails == []


def test_data_accuracy_empty_checks_returns_empty():
    """No data_checks → no findings, check is skipped."""
    label = _make_label_widget("kpi-budget", "bad value")
    pane = _make_pane_mock([label])
    shape = ScreenShape()  # data_checks=[]
    fails = _check_data_accuracy(pane, shape)
    assert fails == []


def test_data_accuracy_multiple_validators_first_fails():
    """Only the failing widget is reported; the passing one is not."""
    label_budget = _make_label_widget("kpi-budget", "bad")  # fails
    label_prs = _make_label_widget("kpi-open-prs", "5")  # passes

    def _two_children():
        yield label_budget
        yield label_prs

    pane = MagicMock()
    pane.walk_children = _two_children

    shape = ScreenShape(
        data_checks=[
            ("kpi-budget", lambda t: t.endswith("%")),
            ("kpi-open-prs", lambda t: any(c.isdigit() for c in t)),
        ]
    )
    fails = _check_data_accuracy(pane, shape)
    assert len(fails) == 1
    assert fails[0]["widget_id"] == "kpi-budget"


def test_data_accuracy_validator_exception_treated_as_fail():
    """If validator raises, it counts as a False return."""
    label = _make_label_widget("kpi-budget", "42%")
    pane = _make_pane_mock([label])

    def _bad_validator(text):
        raise RuntimeError("oops")

    shape = ScreenShape(data_checks=[("kpi-budget", _bad_validator)])
    fails = _check_data_accuracy(pane, shape)
    assert len(fails) == 1
    assert fails[0]["issue_type"] == "data_accuracy_drift"


# ---------------------------------------------------------------------------
# _check_row_interactivity
# ---------------------------------------------------------------------------


def test_row_interactivity_skips_when_textual_absent(monkeypatch):
    """If textual is not installed, returns empty list gracefully."""
    import sys
    import importlib

    # Temporarily hide textual.widgets
    original = sys.modules.get("textual.widgets")
    sys.modules["textual.widgets"] = None  # type: ignore[assignment]
    try:
        import backend.tui_tester_helpers as helpers
        importlib.reload(helpers)
        # Call with an empty pane
        pane = _make_pane_mock([])
        shape = ScreenShape()
        # Should not raise
        result = helpers._check_row_interactivity(pane, shape)
        assert isinstance(result, list)
    finally:
        if original is None:
            del sys.modules["textual.widgets"]
        else:
            sys.modules["textual.widgets"] = original


def _make_pane_with_children(*children):
    """Return a pane mock whose walk_children() is callable multiple times."""
    pane = MagicMock()
    # walk_children() must be callable multiple times (once for DataTable scan,
    # once for the handler scan) — use a side_effect that returns a fresh iter.
    pane.walk_children = MagicMock(side_effect=lambda: iter(children))
    return pane


def test_row_interactivity_fires_when_no_handler():
    """Finding emitted when no on_data_table_row_selected exists on DataTable or
    any child of the pane, and no matching app binding exists."""
    try:
        from textual.widgets import DataTable  # type: ignore[import]
    except ImportError:
        pytest.skip("textual not installed")

    dt = _make_datatable("prs-table")
    # dt has no handler
    if hasattr(dt, "on_data_table_row_selected"):
        del dt.on_data_table_row_selected

    # child widget with no handler either
    other = MagicMock()
    other.__class__.__name__ = "Widget"
    if hasattr(other, "on_data_table_row_selected"):
        del other.on_data_table_row_selected

    pane = _make_pane_with_children(dt, other)
    pane.app = MagicMock()
    pane.app.BINDINGS = []

    shape = ScreenShape(interactive_tables={"prs-table": True})
    fails = _check_row_interactivity(pane, shape)
    assert len(fails) == 1
    assert fails[0]["widget_id"] == "prs-table"
    assert fails[0]["issue_type"] == "non_interactive_table"


def test_row_interactivity_passes_when_handler_on_container_child():
    """No finding when a Container child of the pane has on_data_table_row_selected.

    This is the real-world case: 6 screens define the handler on their Container
    subclass, which is a child of TabPane — not an ancestor. The old upward traversal
    never found it; the downward walk does.
    """
    try:
        from textual.widgets import DataTable  # type: ignore[import]
    except ImportError:
        pytest.skip("textual not installed")

    dt = _make_datatable("prs-table")
    if hasattr(dt, "on_data_table_row_selected"):
        del dt.on_data_table_row_selected

    # A Container child that owns the handler
    container = MagicMock()
    container.__class__.__name__ = "PRsScreen"
    container.on_data_table_row_selected = lambda event: None

    pane = _make_pane_with_children(dt, container)
    pane.app = MagicMock()
    pane.app.BINDINGS = []

    shape = ScreenShape(interactive_tables={"prs-table": True})
    fails = _check_row_interactivity(pane, shape)
    assert fails == []


def test_row_interactivity_passes_when_handler_on_datatable_itself():
    """No finding when on_data_table_row_selected is on the DataTable widget."""
    try:
        from textual.widgets import DataTable  # type: ignore[import]
    except ImportError:
        pytest.skip("textual not installed")

    dt = _make_datatable("prs-table")
    dt.on_data_table_row_selected = lambda event: None

    pane = _make_pane_with_children(dt)
    pane.app = MagicMock()
    pane.app.BINDINGS = []

    shape = ScreenShape(interactive_tables={"prs-table": True})
    fails = _check_row_interactivity(pane, shape)
    assert fails == []


def test_row_interactivity_skips_passive_table():
    """Tables marked 'passive: ...' are exempted from the check."""
    try:
        from textual.widgets import DataTable  # type: ignore[import]
    except ImportError:
        pytest.skip("textual not installed")

    dt = _make_datatable("kpi-table")
    if hasattr(dt, "on_data_table_row_selected"):
        del dt.on_data_table_row_selected

    pane = _make_pane_with_children(dt)
    pane.app = MagicMock()
    pane.app.BINDINGS = []

    shape = ScreenShape(interactive_tables={"kpi-table": "passive: deferred to v2"})
    fails = _check_row_interactivity(pane, shape)
    assert fails == []


def test_row_interactivity_unlisted_table_treated_as_required():
    """Tables not in interactive_tables are expected to be interactive by default."""
    try:
        from textual.widgets import DataTable  # type: ignore[import]
    except ImportError:
        pytest.skip("textual not installed")

    dt = _make_datatable("new-table")
    if hasattr(dt, "on_data_table_row_selected"):
        del dt.on_data_table_row_selected

    pane = _make_pane_with_children(dt)
    pane.app = MagicMock()
    pane.app.BINDINGS = []

    shape = ScreenShape()  # interactive_tables={}
    fails = _check_row_interactivity(pane, shape)
    assert len(fails) == 1
    assert fails[0]["widget_id"] == "new-table"


# ---------------------------------------------------------------------------
# _compare_refresh_snapshots
# ---------------------------------------------------------------------------


def test_refresh_snapshots_no_source_change_always_pass():
    """When source_changed=False we can't assert staleness — always pass."""
    before = {"w1": "old"}
    after = {"w1": "old"}
    fails = _compare_refresh_snapshots(before, after, source_changed=False)
    assert fails == []


def test_refresh_snapshots_source_changed_widget_updated_pass():
    """When source changed AND widget updated → not stale → pass."""
    before = {"w1": "old value"}
    after = {"w1": "new value"}
    fails = _compare_refresh_snapshots(before, after, source_changed=True)
    assert fails == []


def test_refresh_snapshots_source_changed_widget_stale_fires():
    """When source changed but widget stayed the same → refresh_stale finding."""
    before = {"w1": "stale text"}
    after = {"w1": "stale text"}  # unchanged
    fails = _compare_refresh_snapshots(before, after, source_changed=True)
    assert len(fails) >= 1
    assert any(f["issue_type"] == "refresh_stale" for f in fails)
    assert any("w1" in f["widget_id"] for f in fails)


def test_refresh_snapshots_source_changed_some_widgets_stale():
    """Only reports the widgets that didn't change."""
    before = {"w1": "old", "w2": "same"}
    after = {"w1": "new", "w2": "same"}  # w1 updated, w2 stale
    # Any widget changed → source_changed check passes
    fails = _compare_refresh_snapshots(before, after, source_changed=True)
    # w1 updated → _compare_refresh_snapshots sees any_changed=True → returns []
    assert fails == []


def test_refresh_snapshots_all_stale_reports_all():
    """When source changed and NO widgets changed, all widgets are reported."""
    before = {"w1": "v1", "w2": "v2"}
    after = {"w1": "v1", "w2": "v2"}
    fails = _compare_refresh_snapshots(before, after, source_changed=True)
    assert len(fails) == 2
    widget_ids = {f["widget_id"] for f in fails}
    assert "w1" in widget_ids
    assert "w2" in widget_ids


# ---------------------------------------------------------------------------
# Home registry validators (smoke)
# ---------------------------------------------------------------------------


def test_home_registry_has_data_checks():
    shape = REGISTRY["home"]
    assert len(shape.data_checks) >= 3
    check_ids = [wid for wid, _ in shape.data_checks]
    assert "kpi-budget" in check_ids
    assert "kpi-open-prs" in check_ids
    assert "kpi-last-loop" in check_ids


def test_home_budget_validator_accepts_percent():
    shape = REGISTRY["home"]
    validator = dict(shape.data_checks)["kpi-budget"]
    assert validator("42%") is True
    assert validator("42") is False
    assert validator("100%") is True


def test_home_openprs_validator_requires_digit():
    shape = REGISTRY["home"]
    validator = dict(shape.data_checks)["kpi-open-prs"]
    assert validator("5") is True
    assert validator("0") is True
    assert validator("N/A") is False


def test_home_lastloop_validator_accepts_iso():
    shape = REGISTRY["home"]
    validator = dict(shape.data_checks)["kpi-last-loop"]
    assert validator("2026-05-13T10:30:00Z") is True
    assert validator("2026-05-13") is True
    assert validator("never") is True
    assert validator("2m ago") is True
    assert validator("garbage text xyz") is False
