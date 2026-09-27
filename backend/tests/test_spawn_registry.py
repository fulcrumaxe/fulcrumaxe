"""Tests for backend.spawn_registry (D#2615 ENG-0, correction C2).

Run with a scratch state dir, e.g.:
  AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" python3 -m pytest \
    backend/tests/test_spawn_registry.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend import spawn_registry


@pytest.fixture(autouse=True)
def scratch_state_dir(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))
    return state_dir


def test_find_by_event_id_returns_empty_when_registry_absent():
    assert spawn_registry.find_by_event_id("nothing-here-1") == []


def test_record_and_find_round_trips():
    spawn_registry.record_spawn("researcher-1-1000", "researcher", 1, "2026-09-27T00:00:00Z")
    rows = spawn_registry.find_by_event_id("researcher-1-1000")
    assert len(rows) == 1
    assert rows[0]["role"] == "researcher"
    assert rows[0]["discussion"] == 1
    assert rows[0]["created_at"] == "2026-09-27T00:00:00Z"


def test_find_by_event_id_does_not_match_a_different_id():
    spawn_registry.record_spawn("researcher-1-1000", "researcher", 1, "2026-09-27T00:00:00Z")
    assert spawn_registry.find_by_event_id("researcher-1-1001") == []


def test_duplicate_event_id_returns_every_matching_row():
    spawn_registry.record_spawn("executor-9-2000", "executor", 9, "2026-09-27T00:00:00Z")
    spawn_registry.record_spawn("executor-9-2000", "executor", 9, "2026-09-27T00:00:01Z")
    rows = spawn_registry.find_by_event_id("executor-9-2000")
    assert len(rows) == 2


def test_discussion_none_round_trips_for_nod_spawns():
    spawn_registry.record_spawn("mission-analyst-nod-3000", "mission-analyst", None, "2026-09-27T00:00:00Z")
    rows = spawn_registry.find_by_event_id("mission-analyst-nod-3000")
    assert rows[0]["discussion"] is None


def test_corrupt_line_is_skipped_not_raised(tmp_path, monkeypatch):
    spawn_registry.record_spawn("researcher-1-1000", "researcher", 1, "2026-09-27T00:00:00Z")
    path = spawn_registry._registry_path()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    rows = spawn_registry.find_by_event_id("researcher-1-1000")
    assert len(rows) == 1
