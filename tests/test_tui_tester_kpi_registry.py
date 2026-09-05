"""
test_tui_tester_kpi_registry.py — verify the KPI registry matches actual screen Label widgets.

Strategy: parse each screen's compose() source with `ast` to extract Label() calls
where classes="kpi-label". Compare against REGISTRY[tab_id].kpi_labels.

This avoids launching a full Textual runtime while still reading real source.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Iterator

import pytest

from backend.tui_tester_kpi_registry import REGISTRY


# ---------------------------------------------------------------------------
# Source-based Label extractor
# ---------------------------------------------------------------------------


def _kpi_labels_from_source(source: str) -> list[str]:
    """Parse *source* and return Label() literal texts where classes contains 'kpi-label'.

    Handles both keyword styles:
      Label("text", classes="kpi-label")
      Label("text", classes="kpi-label other")
    """
    tree = ast.parse(source)
    labels: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match bare `Label(...)` or `widgets.Label(...)`
        if isinstance(func, ast.Name) and func.id != "Label":
            continue
        if isinstance(func, ast.Attribute) and func.attr != "Label":
            continue
        # Find classes= keyword
        classes_val = None
        for kw in node.keywords:
            if kw.arg == "classes" and isinstance(kw.value, ast.Constant):
                classes_val = kw.value.value
        if classes_val is None or "kpi-label" not in classes_val.split():
            continue
        # First positional arg is the label text
        if node.args and isinstance(node.args[0], ast.Constant):
            labels.append(node.args[0].value)
    return labels


# ---------------------------------------------------------------------------
# Mapping: tab_id -> screen module path
# ---------------------------------------------------------------------------

_SCREENS_DIR = Path(__file__).parent.parent / "dashboard_tui" / "screens"

# Only the screen-source comparison below reads dashboard_tui/ off disk; the
# registry's own consistency tests in this file don't and must keep running.
# A directory check rather than importorskip, because these tests parse the
# screen sources as text instead of importing them.
_NO_SCREENS = pytest.mark.skipif(
    not _SCREENS_DIR.is_dir(),
    reason="dashboard_tui/screens/ not present in this tree",
)

_TAB_TO_MODULE: dict[str, str] = {
    "home": "home.py",
    "prs": "prs.py",
    "discussions": "discussions.py",
    "loop": "loop_health.py",
    "runs": "runs/__init__.py",
    "agent_feed": "agent_feed.py",
    "stats": "stats.py",
    "pr_detail": "pr_detail.py",
    "loop_controller": "loop_controller.py",
    "ideas": "ideas.py",
    "settings": "settings.py",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@_NO_SCREENS
@pytest.mark.parametrize("tab_id,filename", _TAB_TO_MODULE.items())
def test_registry_kpi_labels_match_screen_source(tab_id: str, filename: str) -> None:
    """Registry kpi_labels for *tab_id* must exactly match Labels with class 'kpi-label'
    found in the corresponding screen source file."""
    screen_path = _SCREENS_DIR / filename
    assert screen_path.exists(), f"Screen file not found: {screen_path}"

    source = screen_path.read_text(encoding="utf-8")
    actual_labels = _kpi_labels_from_source(source)

    shape = REGISTRY.get(tab_id)
    assert shape is not None, f"tab_id '{tab_id}' missing from REGISTRY"

    assert shape.kpi_labels == actual_labels, (
        f"Registry mismatch for tab '{tab_id}'.\n"
        f"  registry : {shape.kpi_labels}\n"
        f"  screen   : {actual_labels}\n"
        f"Update backend/tui_tester_kpi_registry.py to match the screen."
    )


def test_all_registry_tabs_have_screen_file() -> None:
    """Every tab in REGISTRY maps to a known screen file."""
    for tab_id in REGISTRY:
        assert tab_id in _TAB_TO_MODULE, (
            f"REGISTRY has tab '{tab_id}' but _TAB_TO_MODULE has no mapping for it. "
            f"Add an entry to _TAB_TO_MODULE in this test."
        )


def test_loop_controller_placeholder_matches_screen() -> None:
    """D#794: loop_controller placeholder must match the actual empty-state label."""
    shape = REGISTRY["loop_controller"]
    assert shape.no_data_placeholder == "No active loops.", (
        f"loop_controller placeholder is '{shape.no_data_placeholder}' "
        f"but screen uses 'No active loops.'"
    )


def test_settings_registry_entry_exists() -> None:
    """D#795: settings tab must have a registry entry with both table IDs."""
    shape = REGISTRY.get("settings")
    assert shape is not None, "REGISTRY missing 'settings' entry"
    assert "settings-gates" in shape.interactive_tables
    assert "settings-audit" in shape.interactive_tables
    assert shape.interactive_tables["settings-gates"] == "passive: read-only v1"
    assert shape.interactive_tables["settings-audit"] == "passive: read-only v1"


def test_runs_percentiles_passive_sentinel() -> None:
    """D#798: percentiles-table must use string passive sentinel, not False."""
    shape = REGISTRY["runs"]
    val = shape.interactive_tables.get("percentiles-table")
    assert isinstance(val, str) and val.startswith("passive:"), (
        f"percentiles-table interactive_tables value should be a 'passive:' string, got {val!r}"
    )
