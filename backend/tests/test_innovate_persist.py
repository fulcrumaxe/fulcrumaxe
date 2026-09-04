"""Regression tests for Innovate toggle persistence bugs.

Verifies three issues fixed together:

1. _set_innovate() now writes ``enabled`` into the state file (it used to
   write back the old file contents unchanged, so the flag was always lost
   across restarts).

2. _innovate_state() is now file-authoritative for ``enabled``.  The
   control-plane gate is only the initial seed when no file exists yet.

3. _INNOVATE_STATE_PATH is now derived from STATE_DIR, not CWD, so two
   backends starting from the same working directory get independent files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEAVY_MOCKS = [
    "backend.agent_cards",
    "backend.audit_trail",
    "backend.api_version",
    "backend.plugin_loader",
    "backend.budget",
    "backend.cost_tracker",
    "backend.config_watcher",
    "backend.dashboard",
    "backend.event_bus",
    "backend.health_monitor",
    "backend.kpi_engine",
    "backend.module_health",
    "backend.dep_graph",
    "backend.metrics",
    "backend.rate_limiter",
    "backend.rbac",
    "backend.registry",
]


def _get_api_mod():
    """Return backend.api with heavy deps mocked out."""
    for mod in _HEAVY_MOCKS:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    import backend.api as api_mod  # noqa: PLC0415
    return api_mod


# ---------------------------------------------------------------------------
# Bug 1 + 2: toggle persists 'enabled' and _innovate_state reads it back
# ---------------------------------------------------------------------------

class TestInnovateTogglePersistence:
    """Toggle off → restart → still off."""

    @pytest.fixture(autouse=True)
    def _cleanup_innovate_state_path(self):
        # D#1810 round 3: _INNOVATE_STATE_PATH is resolved via
        # backend.api.__getattr__ now, not a frozen constant. Every test in
        # this class does `monkeypatch.setattr(api, "_INNOVATE_STATE_PATH",
        # state_file)` on the real, shared backend.api module (via
        # _get_api_mod(), not a fresh copy) — monkeypatch's normal teardown
        # restores the *snapshotted* value via setattr rather than removing
        # the name, which leaves it permanently frozen in module globals
        # (defeating __getattr__) for the rest of the pytest session. delattr
        # instead, same remedy as TestStateDbWritable in test_health_report.py.
        yield
        api = _get_api_mod()
        if "_INNOVATE_STATE_PATH" in vars(api):
            del api._INNOVATE_STATE_PATH

    def _make_cp_mock(self, gate_value: bool = True):
        """Return a fake ControlPlane whose get() returns gate_value."""
        cp = MagicMock()
        cp.get.return_value = gate_value
        return cp

    def test_toggle_off_writes_enabled_false_to_file(self, tmp_path, monkeypatch):
        """After _set_innovate(False) the state file must contain enabled=false."""
        api = _get_api_mod()
        state_file = tmp_path / "innovate-state.json"
        monkeypatch.setattr(api, "_INNOVATE_STATE_PATH", state_file)

        cp_mock = self._make_cp_mock(True)
        with patch("backend.control_plane.ControlPlane", return_value=cp_mock):
            api._set_innovate(False)

        data = json.loads(state_file.read_text())
        assert data["enabled"] is False, (
            "_set_innovate(False) must write enabled:false into the file"
        )

    def test_toggle_on_writes_enabled_true_to_file(self, tmp_path, monkeypatch):
        api = _get_api_mod()
        state_file = tmp_path / "innovate-state.json"
        monkeypatch.setattr(api, "_INNOVATE_STATE_PATH", state_file)

        cp_mock = self._make_cp_mock(False)
        with patch("backend.control_plane.ControlPlane", return_value=cp_mock):
            api._set_innovate(True)

        data = json.loads(state_file.read_text())
        assert data["enabled"] is True

    def test_state_persists_across_restart(self, tmp_path, monkeypatch):
        """Simulate restart: write disabled state, then call _innovate_state()
        without touching the control-plane gate.  Must return enabled=False."""
        api = _get_api_mod()
        state_file = tmp_path / "innovate-state.json"
        # Write the state that a prior run would have left behind
        state_file.write_text(json.dumps({
            "enabled": False,
            "last_iteration_at": "2026-05-18T00:00:00Z",
            "iteration_count": 36,
        }))
        monkeypatch.setattr(api, "_INNOVATE_STATE_PATH", state_file)

        result = api._innovate_state()

        assert result["enabled"] is False, (
            "File-authoritative read must return False even if gate defaults True"
        )

    def test_file_enabled_key_takes_priority_over_gate(self, tmp_path, monkeypatch):
        """Even if the gate says True, file enabled:False must win."""
        api = _get_api_mod()
        state_file = tmp_path / "innovate-state.json"
        state_file.write_text(json.dumps({"enabled": False, "iteration_count": 5}))
        monkeypatch.setattr(api, "_INNOVATE_STATE_PATH", state_file)

        # Gate claims True — should be ignored
        cp_mock = self._make_cp_mock(True)
        with patch("backend.control_plane.ControlPlane", return_value=cp_mock):
            result = api._innovate_state()

        assert result["enabled"] is False

    def test_no_file_falls_back_to_gate(self, tmp_path, monkeypatch):
        """When no file exists the gate value is used as the seed."""
        api = _get_api_mod()
        state_file = tmp_path / "innovate-state.json"
        # File does NOT exist
        monkeypatch.setattr(api, "_INNOVATE_STATE_PATH", state_file)

        cp_mock = self._make_cp_mock(False)
        with patch("backend.control_plane.ControlPlane", return_value=cp_mock):
            result = api._innovate_state()

        assert result["enabled"] is False

    def test_set_innovate_preserves_existing_iteration_count(self, tmp_path, monkeypatch):
        """Toggling must not zero out iteration_count or last_iteration_at."""
        api = _get_api_mod()
        state_file = tmp_path / "innovate-state.json"
        state_file.write_text(json.dumps({
            "enabled": True,
            "last_iteration_at": "2026-05-17T10:00:00Z",
            "iteration_count": 42,
        }))
        monkeypatch.setattr(api, "_INNOVATE_STATE_PATH", state_file)

        cp_mock = self._make_cp_mock(True)
        with patch("backend.control_plane.ControlPlane", return_value=cp_mock):
            api._set_innovate(False)

        data = json.loads(state_file.read_text())
        assert data["iteration_count"] == 42
        assert data["last_iteration_at"] == "2026-05-17T10:00:00Z"
        assert data["enabled"] is False


# ---------------------------------------------------------------------------
# Bug 3: state path is under STATE_DIR, not CWD
# ---------------------------------------------------------------------------

class TestInnovateStatePath:
    def test_state_path_is_under_state_dir(self):
        """_INNOVATE_STATE_PATH must live under _STATE_DIR, not cwd."""
        api = _get_api_mod()
        from backend.state_paths import STATE_DIR  # noqa: PLC0415

        assert api._INNOVATE_STATE_PATH == STATE_DIR / "innovate-state.json", (
            "Path must be STATE_DIR/innovate-state.json for per-project isolation"
        )

    def test_state_path_not_relative_to_cwd(self):
        """Verify the path is absolute (CWD-relative paths cause sharing between projects)."""
        api = _get_api_mod()
        assert api._INNOVATE_STATE_PATH.is_absolute(), (
            "_INNOVATE_STATE_PATH must be absolute, not CWD-relative"
        )
