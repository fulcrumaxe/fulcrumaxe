"""Tests for the gates.loop_start control-plane gate on the loop.start RPC.

Verifies two requirements:
1. With gate off (default), loop.start raises ValueError("loop_start_disabled_by_gate").
2. With gate on, loop.start proceeds normally (creates a loop entry).

Run with:
    pytest backend/tests/test_loop_start_gate.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.server as server_mod
from backend.server import _rpc_loop_start


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cp(loop_start_enabled: bool) -> MagicMock:
    cp = MagicMock()
    cp.gate_enabled.return_value = loop_start_enabled
    return cp


# ---------------------------------------------------------------------------
# Gate-off: RPC must refuse
# ---------------------------------------------------------------------------

class TestLoopStartGateOff:
    """When gates.loop_start is false, loop.start must be refused."""

    def test_raises_value_error_with_expected_message(self):
        cp = _make_cp(loop_start_enabled=False)
        with patch("backend.server._rpc_loop_start.__module__"), \
             patch("backend.control_plane.ControlPlane", return_value=cp):
            with pytest.raises(ValueError, match="loop_start_disabled_by_gate"):
                _rpc_loop_start({})

    def test_gate_enabled_called_with_loop_start(self):
        cp = _make_cp(loop_start_enabled=False)
        with patch("backend.control_plane.ControlPlane", return_value=cp):
            with pytest.raises(ValueError):
                _rpc_loop_start({})
        cp.gate_enabled.assert_called_once_with("loop_start")

    def test_create_loop_not_called_when_gate_off(self):
        cp = _make_cp(loop_start_enabled=False)
        with patch("backend.control_plane.ControlPlane", return_value=cp), \
             patch("backend.active_loops.create_loop") as mock_create:
            with pytest.raises(ValueError):
                _rpc_loop_start({})
        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Gate-on: RPC must succeed
# ---------------------------------------------------------------------------

class TestLoopStartGateOn:
    """When gates.loop_start is true, loop.start proceeds normally."""

    def test_returns_loop_id_and_started_at(self):
        cp = _make_cp(loop_start_enabled=True)
        fake_entry = {"loop_id": "loop-abc123", "started_at": "2026-05-19T12:00:00Z"}
        with patch("backend.control_plane.ControlPlane", return_value=cp), \
             patch("backend.active_loops.create_loop", return_value=fake_entry), \
             patch("os.getpid", return_value=12345):
            result = _rpc_loop_start({"prompt": "run /loop iteration"})
        assert result == {"loop_id": "loop-abc123", "started_at": "2026-05-19T12:00:00Z"}

    def test_create_loop_called_with_correct_args(self):
        cp = _make_cp(loop_start_enabled=True)
        fake_entry = {"loop_id": "loop-xyz", "started_at": "2026-05-19T12:00:00Z"}
        with patch("backend.control_plane.ControlPlane", return_value=cp), \
             patch("backend.active_loops.create_loop", return_value=fake_entry) as mock_create, \
             patch("os.getpid", return_value=999):
            _rpc_loop_start({"prompt": "test prompt", "cadence_seconds": 300})
        mock_create.assert_called_once_with("test prompt", 300, 999)

    def test_cadence_none_when_not_provided(self):
        cp = _make_cp(loop_start_enabled=True)
        fake_entry = {"loop_id": "loop-one", "started_at": "2026-05-19T12:00:00Z"}
        with patch("backend.control_plane.ControlPlane", return_value=cp), \
             patch("backend.active_loops.create_loop", return_value=fake_entry) as mock_create, \
             patch("os.getpid", return_value=1):
            _rpc_loop_start({"prompt": "one-shot"})
        mock_create.assert_called_once_with("one-shot", None, 1)


# ---------------------------------------------------------------------------
# Default gate state
# ---------------------------------------------------------------------------

class TestDefaultGateValue:
    """gates.loop_start must default to False in _DEFAULT_GATES."""

    def test_default_is_false(self):
        from backend.control_plane import _DEFAULT_GATES
        assert _DEFAULT_GATES.get("loop_start") is False, (
            "gates.loop_start must default to False — CLI is the sole loop spawner"
        )
