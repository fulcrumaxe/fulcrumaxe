"""Tests for backend/claude_spawn_tracker.py."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is on path for running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_blackboard(tmp_path, monkeypatch):
    """Use a temp blackboard dir so tests don't bleed into real state."""
    import backend.blackboard as bb_mod
    import backend.claude_spawn_tracker as cst_mod

    real_bb_path = Path(__file__).resolve().parent.parent / ".autonomous-team" / "blackboard"
    tmp_bb = tmp_path / "blackboard"
    tmp_bb.mkdir()

    # Patch Blackboard to use tmp dir
    original_root = bb_mod.Blackboard.__init__

    def patched_init(self, root=None):
        original_root(self, root=str(tmp_bb))

    monkeypatch.setattr(bb_mod.Blackboard, "__init__", patched_init)

    # Re-create the module-level _bb instance in the tracker
    import importlib
    importlib.reload(cst_mod)

    yield cst_mod

    # Reload again to restore real blackboard after test
    importlib.reload(cst_mod)


@pytest.fixture()
def cfg_1spawn_per_hour(tmp_path, monkeypatch, clean_blackboard):
    """Override config: spawns_per_hour_max=1."""
    cst = clean_blackboard
    monkeypatch.setattr(
        cst,
        "_load_config",
        lambda: {
            "spawns_per_hour_max": 1,
            "spend_per_hour_usd_max": 100.0,
            "spawns_24h_max": 1000,
            "auto_reset_idle_seconds": 3600,
            "cost_per_spawn_usd_default": 0.01,
        },
    )
    return cst


@pytest.fixture()
def cfg_low_spend(tmp_path, monkeypatch, clean_blackboard):
    """Override config: spend_per_hour_usd_max=0.01."""
    cst = clean_blackboard
    monkeypatch.setattr(
        cst,
        "_load_config",
        lambda: {
            "spawns_per_hour_max": 1000,
            "spend_per_hour_usd_max": 0.01,
            "spawns_24h_max": 1000,
            "auto_reset_idle_seconds": 3600,
            "cost_per_spawn_usd_default": 0.01,
        },
    )
    return cst


@pytest.fixture()
def cfg_24h_max_2(tmp_path, monkeypatch, clean_blackboard):
    """Override config: spawns_24h_max=2."""
    cst = clean_blackboard
    monkeypatch.setattr(
        cst,
        "_load_config",
        lambda: {
            "spawns_per_hour_max": 1000,
            "spend_per_hour_usd_max": 100.0,
            "spawns_24h_max": 2,
            "auto_reset_idle_seconds": 3600,
            "cost_per_spawn_usd_default": 0.01,
        },
    )
    return cst


# ---------------------------------------------------------------------------
# Threshold tests
# ---------------------------------------------------------------------------


def test_spawns_per_hour_trips_on_second_spawn(cfg_1spawn_per_hour):
    cst = cfg_1spawn_per_hour
    with patch.object(cst, "_post_team_log"):
        cst.record(source="test")  # first — OK
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")  # second — trips
    assert cst.is_tripped()
    state = cst.get_state()
    assert state["tripped"] is True
    meta = state["tripped_meta"]
    assert meta is not None
    assert meta["threshold_name"] == "spawns_per_hour_max"


def test_spend_per_hour_trips_on_first_expensive_spawn(cfg_low_spend):
    cst = cfg_low_spend
    with patch.object(cst, "_post_team_log"):
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test", est_cost_usd=0.02)  # 0.02 > 0.01
    assert cst.is_tripped()
    meta = cst.get_state()["tripped_meta"]
    assert meta["threshold_name"] == "spend_per_hour_usd_max"


def test_spawns_24h_max_trips_on_third_spawn(cfg_24h_max_2):
    cst = cfg_24h_max_2
    with patch.object(cst, "_post_team_log"):
        cst.record(source="test")
        cst.record(source="test")
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")
    meta = cst.get_state()["tripped_meta"]
    assert meta["threshold_name"] == "spawns_24h_max"


def test_no_trip_within_limits(clean_blackboard):
    """Normal usage well under all thresholds — no trip."""
    cst = clean_blackboard
    with patch.object(cst, "_post_team_log"):
        cst.record(source="test", est_cost_usd=0.001)
        cst.record(source="test", est_cost_usd=0.001)
    assert not cst.is_tripped()


# ---------------------------------------------------------------------------
# Reset tests
# ---------------------------------------------------------------------------


def test_manual_reset_clears_all_keys(cfg_1spawn_per_hour):
    cst = cfg_1spawn_per_hour
    with patch.object(cst, "_post_team_log"):
        cst.record(source="test")
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")
    assert cst.is_tripped()
    cst.reset()
    assert not cst.is_tripped()
    state = cst.get_state()
    assert state["tripped"] is False
    assert state["tripped_meta"] is None
    # Banner key should be gone
    assert cst._bb.read(cst._BANNER_KEY) is None


# ---------------------------------------------------------------------------
# Auto-reset tests
# ---------------------------------------------------------------------------


def test_auto_reset_after_idle(monkeypatch, clean_blackboard):
    cst = clean_blackboard
    monkeypatch.setattr(
        cst,
        "_load_config",
        lambda: {
            "spawns_per_hour_max": 1,
            "spend_per_hour_usd_max": 100.0,
            "spawns_24h_max": 1000,
            "auto_reset_idle_seconds": 2,
            "cost_per_spawn_usd_default": 0.01,
        },
    )
    with patch.object(cst, "_post_team_log"):
        cst.record(source="test")
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")
    assert cst.is_tripped()

    # Manually backdate last_attempt_at so auto-reset fires
    meta = cst._bb.read(cst._META_KEY)
    past = "2000-01-01T00:00:00Z"
    meta["last_attempt_at"] = past
    cst._bb.write(cst._META_KEY, meta, updated_by="test")

    # Now get_state should auto-reset
    state = cst.get_state()
    assert state["tripped"] is False


def test_continuous_attempts_prevent_auto_reset(monkeypatch, clean_blackboard):
    cst = clean_blackboard
    monkeypatch.setattr(
        cst,
        "_load_config",
        lambda: {
            "spawns_per_hour_max": 1,
            "spend_per_hour_usd_max": 100.0,
            "spawns_24h_max": 1000,
            "auto_reset_idle_seconds": 10,
            "cost_per_spawn_usd_default": 0.01,
        },
    )
    with patch.object(cst, "_post_team_log"):
        cst.record(source="test")
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")
    # keep attempting — last_attempt_at stays fresh
    for _ in range(3):
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")
    # Still tripped (last_attempt_at is recent)
    assert cst.is_tripped()


# ---------------------------------------------------------------------------
# Single team-log warning idempotency
# ---------------------------------------------------------------------------


def test_single_team_log_warning_per_trip(cfg_1spawn_per_hour):
    cst = cfg_1spawn_per_hour
    call_count = 0

    def fake_log(msg):
        nonlocal call_count
        call_count += 1

    with patch.object(cst, "_post_team_log", side_effect=fake_log):
        cst.record(source="test")
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")
        # Additional refused calls should NOT post more warnings
        for _ in range(3):
            with pytest.raises(cst.SpawnBlocked):
                cst.record(source="test")

    assert call_count == 1


# ---------------------------------------------------------------------------
# Per-source counts
# ---------------------------------------------------------------------------


def test_per_source_counts(clean_blackboard):
    cst = clean_blackboard
    with patch.object(cst, "_post_team_log"):
        cst.record(source="loop_run")
        cst.record(source="loop_run")
        cst.record(source="dashboard")
    state = cst.get_state()
    assert state["per_source"]["loop_run"] == 2
    assert state["per_source"]["dashboard"] == 1


# ---------------------------------------------------------------------------
# Concurrency safety
# ---------------------------------------------------------------------------


def test_concurrent_records_are_counted_exactly(clean_blackboard):
    cst = clean_blackboard
    errors: list[Exception] = []

    def _do_record():
        try:
            with patch.object(cst, "_post_team_log"):
                cst.record(source="concurrent_test", est_cost_usd=0.0001)
        except cst.SpawnBlocked:
            pass  # may trip on high counts but count should still be accurate
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_do_record) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    state = cst.get_state()
    total = state["spawns_24h"]
    # All 10 spawns should be counted (some may have tripped after recording)
    assert total == 10


# ---------------------------------------------------------------------------
# Banner key set on trip
# ---------------------------------------------------------------------------


def test_banner_key_set_on_trip(cfg_1spawn_per_hour):
    cst = cfg_1spawn_per_hour
    with patch.object(cst, "_post_team_log"):
        cst.record(source="test")
        with pytest.raises(cst.SpawnBlocked):
            cst.record(source="test")
    banner = cst._bb.read(cst._BANNER_KEY)
    assert banner is not None
    assert banner["level"] == "error"
    assert "spawn_breaker" in banner["message"].lower() or "tripped" in banner["message"].lower()
