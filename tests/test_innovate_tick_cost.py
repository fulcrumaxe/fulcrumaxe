"""
Tests for D#1080 — innovate_tick_internal must not record fake 0.05 cost.

Acceptance criteria:
  AC#3: given N synthetic agent_runs each with K tokens, the reported tick
        cost from the spawn tracker is None (not the 0.05 hardcoded literal).
  AC#4: spawn cap counter still increments normally — each tick costs 1 against
        the per-hour cap regardless of token accounting.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fixtures — isolated blackboard so tests never touch real state
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_blackboard(tmp_path, monkeypatch):
    """Reload claude_spawn_tracker with a temp blackboard for each test."""
    import backend.blackboard as bb_mod
    import backend.claude_spawn_tracker as cst_mod

    original_init = bb_mod.Blackboard.__init__
    tmp_bb = tmp_path / "blackboard"
    tmp_bb.mkdir()

    def patched_init(self, root=None):
        original_init(self, root=str(tmp_bb))

    monkeypatch.setattr(bb_mod.Blackboard, "__init__", patched_init)
    importlib.reload(cst_mod)
    yield cst_mod
    importlib.reload(cst_mod)


# ---------------------------------------------------------------------------
# Core: unmetered source stores None, not 0.05
# ---------------------------------------------------------------------------


def test_innovate_tick_stores_none_cost(isolated_blackboard):
    """innovate_tick_internal events must have est_cost_usd=None, not 0.05."""
    cst = isolated_blackboard
    with patch.object(cst, "_post_team_log"):
        cst.record(source="innovate_tick_internal")

    events = cst._load_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "innovate_tick_internal"
    assert ev["est_cost_usd"] is None, (
        f"Expected None but got {ev['est_cost_usd']!r} — "
        "the hardcoded 0.05 placeholder is still being stored"
    )
    assert ev["est_tokens"] is None


def test_innovate_tick_does_not_pollute_spend_sum(isolated_blackboard):
    """Unmetered ticks must not inflate spend_1h_usd / spend_24h_usd."""
    cst = isolated_blackboard
    with patch.object(cst, "_post_team_log"):
        # One metered spawn (real cost)
        cst.record(source="loop_run", est_cost_usd=0.10)
        # Three unmetered innovate ticks
        cst.record(source="innovate_tick_internal")
        cst.record(source="innovate_tick_internal")
        cst.record(source="innovate_tick_internal")

    state = cst.get_state()
    # Only the metered spawn contributes to spend
    assert state["spend_1h_usd"] == pytest.approx(0.10, abs=1e-6), (
        f"spend_1h_usd={state['spend_1h_usd']} — innovate ticks should not add to spend"
    )
    assert state["spend_24h_usd"] == pytest.approx(0.10, abs=1e-6)
    # But all four spawns are counted against the cap
    assert state["spawns_1h"] == 4


def test_spawn_cap_still_counts_innovate_ticks(isolated_blackboard, monkeypatch):
    """Spawn cap must treat each tick as 1 even though cost is None (AC#4)."""
    cst = isolated_blackboard
    monkeypatch.setattr(
        cst,
        "_load_config",
        lambda: {
            "spawns_per_hour_max": 2,
            "spend_per_hour_usd_max": 100.0,
            "spawns_24h_max": 1000,
            "auto_reset_idle_seconds": 3600,
            "cost_per_spawn_usd_default": 0.05,
        },
    )
    with patch.object(cst, "_post_team_log"):
        cst.record(source="innovate_tick_internal")  # 1 — ok
        cst.record(source="innovate_tick_internal")  # 2 — ok
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="innovate_tick_internal")  # 3 — trips cap

    assert cst.is_tripped()


def test_metered_source_still_uses_config_default(isolated_blackboard, monkeypatch):
    """Non-unmetered sources without explicit cost should still fall back to config default."""
    cst = isolated_blackboard
    monkeypatch.setattr(
        cst,
        "_load_config",
        lambda: {
            "spawns_per_hour_max": 1000,
            "spend_per_hour_usd_max": 100.0,
            "spawns_24h_max": 1000,
            "auto_reset_idle_seconds": 3600,
            "cost_per_spawn_usd_default": 0.07,
        },
    )
    with patch.object(cst, "_post_team_log"):
        cst.record(source="loop_run")

    events = cst._load_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["est_cost_usd"] == pytest.approx(0.07, abs=1e-6)
    assert ev["est_tokens"] == 0
