"""tests/backend/test_fleet_concurrency_sqlite.py — Unit tests for fleet concurrency.

Tests: register/unregister/count, fleet cap enforcement, WAL contention
simulation via multi-threading.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_fleet_dir(tmp_path, monkeypatch):
    """Redirect fleet state to a temp directory for each test."""
    import backend.fleet.concurrency as fc

    monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
    monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
    monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")
    yield tmp_path


# ── Basic register / unregister / count ───────────────────────────────────────

def test_register_and_count():
    from backend.fleet.concurrency import register, count_fleet, count_project

    ok = register("af", "agent-1", "executor")
    assert ok is True
    assert count_fleet() == 1
    assert count_project("af") == 1
    assert count_project("projectb") == 0


def test_register_duplicate_is_idempotent():
    """Inserting the same (project, agent_id) twice should not raise and count stays 1."""
    from backend.fleet.concurrency import register, count_fleet

    register("af", "agent-1", "executor")
    register("af", "agent-1", "executor")  # INSERT OR IGNORE
    assert count_fleet() == 1


def test_unregister_removes_row():
    from backend.fleet.concurrency import register, unregister, count_fleet

    register("af", "agent-1", "executor")
    unregister("af", "agent-1")
    assert count_fleet() == 0


def test_unregister_nonexistent_is_noop():
    from backend.fleet.concurrency import unregister, count_fleet

    unregister("af", "ghost-agent")  # must not raise
    assert count_fleet() == 0


def test_multiple_projects():
    from backend.fleet.concurrency import register, count_fleet, count_project

    register("af", "agent-1", "executor")
    register("af", "agent-2", "code-reviewer")
    register("projectb", "agent-3", "executor")

    assert count_fleet() == 3
    assert count_project("af") == 2
    assert count_project("projectb") == 1


# ── Fleet cap enforcement ──────────────────────────────────────────────────────

def test_fleet_cap_default(isolated_fleet_dir):
    from backend.fleet.concurrency import fleet_cap
    assert fleet_cap() == 8


def test_fleet_cap_from_config(isolated_fleet_dir):
    config = isolated_fleet_dir / "config.json"
    config.write_text(json.dumps({"fleet_cap": 3}))

    from backend.fleet.concurrency import fleet_cap
    assert fleet_cap() == 3


def test_register_denied_when_cap_reached(isolated_fleet_dir):
    """When fleet is at cap, register returns False."""
    config = isolated_fleet_dir / "config.json"
    config.write_text(json.dumps({"fleet_cap": 2}))

    from backend.fleet.concurrency import register

    assert register("af", "agent-1", "executor") is True
    assert register("af", "agent-2", "executor") is True
    # Third attempt is over cap
    assert register("af", "agent-3", "executor") is False


def test_slot_freed_after_unregister_allows_new_register(isolated_fleet_dir):
    config = isolated_fleet_dir / "config.json"
    config.write_text(json.dumps({"fleet_cap": 1}))

    from backend.fleet.concurrency import register, unregister

    assert register("af", "agent-1", "executor") is True
    assert register("af", "agent-2", "executor") is False  # cap reached

    unregister("af", "agent-1")
    assert register("af", "agent-2", "executor") is True  # slot freed


# ── Cross-project cap enforcement (gate 2 from spec verification) ─────────────

def test_cross_project_cap(isolated_fleet_dir):
    """3 in projectb + 6 in af = 9 total; last one should deny (cap=8)."""
    config = isolated_fleet_dir / "config.json"
    config.write_text(json.dumps({"fleet_cap": 8}))

    from backend.fleet.concurrency import register, count_fleet

    # Register 3 mock agents in projectb
    for i in range(3):
        ok = register("projectb", f"projectb-agent-{i}", "executor")
        assert ok is True, f"projectb agent {i} should succeed"

    # Register 5 more in af (total 8 = cap)
    for i in range(5):
        ok = register("af", f"af-agent-{i}", "executor")
        assert ok is True, f"af agent {i} should succeed"

    assert count_fleet() == 8

    # One more should be denied (3+5+1=9 > 8)
    ok = register("af", "af-agent-overflow", "executor")
    assert ok is False, "9th agent should be denied (fleet cap=8)"


def test_kill_all_count_zero(isolated_fleet_dir):
    """After unregistering all agents, count_fleet() returns 0."""
    from backend.fleet.concurrency import register, unregister, count_fleet

    register("projectb", "projectb-1", "executor")
    register("af", "af-1", "executor")
    register("af", "af-2", "code-reviewer")

    unregister("projectb", "projectb-1")
    unregister("af", "af-1")
    unregister("af", "af-2")

    assert count_fleet() == 0


# ── WAL contention simulation ─────────────────────────────────────────────────

def test_concurrent_register_no_double_allocation(isolated_fleet_dir):
    """4 parallel threads trying to register when cap=3 — exactly 3 succeed."""
    config = isolated_fleet_dir / "config.json"
    config.write_text(json.dumps({"fleet_cap": 3}))

    from backend.fleet.concurrency import register, count_fleet

    results: list[bool] = []
    lock = threading.Lock()

    def _try_register(idx: int) -> None:
        ok = register("af", f"thread-agent-{idx}", "executor")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=_try_register, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert count_fleet() == 3
    assert sum(results) == 3, f"Expected exactly 3 successes, got {results}"


def test_concurrent_register_at_full_cap_all_denied(isolated_fleet_dir):
    """All concurrent registrations denied when fleet is already full."""
    config = isolated_fleet_dir / "config.json"
    config.write_text(json.dumps({"fleet_cap": 1}))

    from backend.fleet.concurrency import register, count_fleet

    # Fill the fleet
    assert register("af", "seed-agent", "executor") is True
    assert count_fleet() == 1

    results: list[bool] = []
    lock = threading.Lock()

    def _try_register(idx: int) -> None:
        ok = register("af", f"thread-agent-{idx}", "executor")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=_try_register, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert count_fleet() == 1, f"Fleet count should be 1, got {count_fleet()}"
    assert not any(results), f"All should be denied but got: {results}"


# ── list_agents ────────────────────────────────────────────────────────────────

def test_list_agents_returns_correct_rows():
    from backend.fleet.concurrency import register, list_agents

    register("af", "agent-1", "executor")
    register("projectb", "agent-2", "code-reviewer")

    agents = list_agents()
    assert len(agents) == 2
    projects = {a["project_name"] for a in agents}
    assert projects == {"af", "projectb"}
    for a in agents:
        assert set(a.keys()) >= {"project_name", "agent_id", "role", "started_at"}


# ── Config fallback ────────────────────────────────────────────────────────────

def test_fleet_cap_falls_back_on_malformed_config(isolated_fleet_dir):
    config = isolated_fleet_dir / "config.json"
    config.write_text("NOT JSON {{{")

    from backend.fleet.concurrency import fleet_cap, DEFAULT_FLEET_CAP
    assert fleet_cap() == DEFAULT_FLEET_CAP


def test_fleet_cap_falls_back_when_config_missing(isolated_fleet_dir):
    # Config not yet created
    from backend.fleet.concurrency import fleet_cap, DEFAULT_FLEET_CAP
    assert fleet_cap() == DEFAULT_FLEET_CAP


# ── EVENT_ID round-trip (BLOCKER 2 regression) ────────────────────────────────

def test_event_id_roundtrip_no_worktree_id():
    """register with EVENT_ID + unregister with same EVENT_ID drops count to 0.

    Simulates the WORKTREE_ID-unset scenario that caused the spawn-$$ leak:
    pre-spawn-check uses EVENT_ID_ARG; post-agent-hook uses TASK_EVENT_ID (same value).
    Both must resolve to the same agent_id so the DELETE matches the INSERT.
    """
    from backend.fleet.concurrency import register, unregister, count_fleet

    event_id = "evt-abc123"
    project = "af"
    role = "executor"

    # Simulate pre-spawn-check: register with EVENT_ID (WORKTREE_ID not set)
    assert register(project, event_id, role) is True
    assert count_fleet() == 1

    # Simulate post-agent-hook: unregister with the same EVENT_ID (TASK_EVENT_ID)
    unregister(project, event_id)
    assert count_fleet() == 0, "fleet slot must drop to 0 after unregister with same EVENT_ID"
