"""Tests for backend.stats.dial_usage and backend.rpc.stats_dial_usage.

All tests run in an isolated state directory so they never touch the real
~/.autonomous-forever-state/ directory.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point STATE_DIR at a temp dir and reload relevant modules."""
    import os
    original = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

    import backend.state_paths as _sp
    import backend.dial_registry as _dr

    importlib.reload(_sp)
    importlib.reload(_dr)

    yield tmp_path

    # Restore env before reloading so backend.state_paths.STATE_DIR gets the
    # real production path (not the tmp dir).  monkeypatch.setenv teardown
    # runs AFTER fixture teardown, so we must undo it explicitly here.
    if original is not None:
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = original
    else:
        os.environ.pop("AUTONOMOUS_TEAM_STATE_DIR", None)
    importlib.reload(_sp)
    importlib.reload(_dr)


def _import_reader():
    import backend.stats.dial_usage as m
    importlib.reload(m)
    return m


def _import_rpc_handler():
    import backend.rpc.stats_dial_usage as m
    importlib.reload(m)
    return m


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago_iso(hours: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ts.isoformat(timespec="seconds")


def _append_audit(state_dir: Path, row: dict) -> None:
    audit = state_dir / "audit.jsonl"
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Test 1: live registry read — 13 classes with expected shape
# ---------------------------------------------------------------------------

class TestLiveRegistryRead:
    def test_returns_13_classes(self, state_dir):
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)

        current_dials = result["current_dials"]
        assert len(current_dials) == 13

    def test_class_shape(self, state_dir):
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)

        for cls in result["current_dials"]:
            assert "name" in cls
            assert "level" in cls
            assert "verb_label" in cls
            assert "ceiling" in cls
            assert "active_directives" in cls
            assert "ttl_revert_at" in cls

    def test_known_class_present(self, state_dir):
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)

        names = {c["name"] for c in result["current_dials"]}
        assert "agent.spawn" in names
        assert "sandbox.modify" in names

    def test_verb_label_populated(self, state_dir):
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)

        agent_spawn = next(c for c in result["current_dials"] if c["name"] == "agent.spawn")
        assert agent_spawn["verb_label"] == "Spawn agents"


# ---------------------------------------------------------------------------
# Test 2: 24h counter rollups
# ---------------------------------------------------------------------------

class TestCounters24h:
    def test_accepted_count(self, state_dir):
        # Write 3 accepted dial_change rows within 24h
        for _ in range(3):
            _append_audit(state_dir, {
                "kind": "dial_change",
                "class": "agent.spawn",
                "prev_level": 4,
                "new_level": 3,
                "timestamp": _now_iso(),
            })
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)
        assert result["last_24h"]["accepted"] == 3

    def test_rejected_counts_by_reason(self, state_dir):
        reasons = [
            ("ceiling_violation", 2),
            ("unauthenticated_source", 1),
            ("invalid_level", 3),
        ]
        for reason, count in reasons:
            for _ in range(count):
                _append_audit(state_dir, {
                    "kind": "dial_directive_rejected",
                    "class": "sandbox.modify",
                    "level": 5,
                    "reason": reason,
                    "timestamp": _now_iso(),
                })
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)
        rej = result["last_24h"]["rejected_by_reason"]
        assert rej["ceiling_violation"] == 2
        assert rej["unauthenticated_source"] == 1
        assert rej["invalid_level"] == 3

    def test_old_rows_excluded(self, state_dir):
        # One row 25 hours ago — should not be counted
        _append_audit(state_dir, {
            "kind": "dial_change",
            "class": "agent.spawn",
            "prev_level": 4,
            "new_level": 3,
            "timestamp": _ago_iso(25),
        })
        # One row 1 hour ago — should be counted
        _append_audit(state_dir, {
            "kind": "dial_change",
            "class": "agent.spawn",
            "prev_level": 3,
            "new_level": 4,
            "timestamp": _ago_iso(1),
        })
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)
        assert result["last_24h"]["accepted"] == 1


# ---------------------------------------------------------------------------
# Test 3: empty state (no audit file, no directives)
# ---------------------------------------------------------------------------

class TestEmptyState:
    def test_no_audit_file(self, state_dir):
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)

        # No audit file — counters should all be zero
        assert result["last_24h"]["accepted"] == 0
        assert result["last_24h"]["ceiling_violations"] == 0
        assert result["last_24h"]["rejected_by_reason"]["ceiling_violation"] == 0
        assert result["last_24h"]["last_ceiling_exceeded"] is None

    def test_classes_still_returned_when_no_activity(self, state_dir):
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)
        # Should still return all 13 classes from the live registry
        assert len(result["current_dials"]) == 13


# ---------------------------------------------------------------------------
# Test 4: ceiling_violation count + last timestamp
# ---------------------------------------------------------------------------

class TestCeilingViolations:
    def test_ceiling_violation_count(self, state_dir):
        for _ in range(4):
            _append_audit(state_dir, {
                "kind": "dial_directive_rejected",
                "class": "sandbox.modify",
                "level": 5,
                "reason": "ceiling_violation",
                "timestamp": _now_iso(),
            })
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)
        assert result["last_24h"]["ceiling_violations"] == 4

    def test_last_ceiling_exceeded_populated(self, state_dir):
        ts_early = _ago_iso(2)
        ts_late = _ago_iso(1)
        for ts in [ts_early, ts_late]:
            _append_audit(state_dir, {
                "kind": "dial_directive_rejected",
                "class": "methodology.change",
                "level": 5,
                "reason": "ceiling_violation",
                "timestamp": ts,
            })
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)
        lce = result["last_24h"]["last_ceiling_exceeded"]
        assert lce is not None
        assert lce["class"] == "methodology.change"
        # Should reflect the later timestamp
        assert lce["timestamp"] == ts_late

    def test_last_ceiling_exceeded_none_when_no_violations(self, state_dir):
        # Only accepted rows
        _append_audit(state_dir, {
            "kind": "dial_change",
            "class": "agent.spawn",
            "prev_level": 4,
            "new_level": 3,
            "timestamp": _now_iso(),
        })
        m = _import_reader()
        result = m.read_dial_usage(state_dir=state_dir)
        assert result["last_24h"]["last_ceiling_exceeded"] is None


# ---------------------------------------------------------------------------
# Test 5: project-scoping — different state_dirs return independent data
# ---------------------------------------------------------------------------

class TestProjectScoping:
    def test_different_state_dirs_are_independent(self, tmp_path):
        state_a = tmp_path / "state-a"
        state_b = tmp_path / "state-b"
        state_a.mkdir()
        state_b.mkdir()

        # Write 2 accepted rows to state-a only
        for _ in range(2):
            _append_audit(state_a, {
                "kind": "dial_change",
                "class": "agent.spawn",
                "prev_level": 4,
                "new_level": 3,
                "timestamp": _now_iso(),
            })

        import backend.state_paths as _sp
        import backend.dial_registry as _dr
        importlib.reload(_sp)
        importlib.reload(_dr)

        import backend.stats.dial_usage as m
        importlib.reload(m)

        result_a = m.read_dial_usage(state_dir=state_a)
        result_b = m.read_dial_usage(state_dir=state_b)

        assert result_a["last_24h"]["accepted"] == 2
        assert result_b["last_24h"]["accepted"] == 0

    def test_rpc_handler_none_project_returns_data(self, state_dir):
        rpc = _import_rpc_handler()
        result = rpc.handle({})
        assert "current_dials" in result
        assert "last_24h" in result

    def test_rpc_handler_project_name_param(self, state_dir):
        rpc = _import_rpc_handler()
        # Passing project_name=None should still return valid data
        result = rpc.handle({"project_name": None})
        assert "current_dials" in result
        assert "last_24h" in result
