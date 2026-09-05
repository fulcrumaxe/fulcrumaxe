"""tests/backend/test_fleet_projects.py — unit tests for fleet runtime discovery.

Covers:
  (a) port derivation from dashboard_port
  (b) derive_ports helper returns correct 4-tuple
  (c) GET /api/fleet/projects response shape
  (d) _probe_ports returns False when ports not listening
  (e) _read_runtime parses dashboard-runtime.json correctly
  (f) _probe_ports returns False when all port values are non-integer
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# (a) + (b) Port derivation
# ---------------------------------------------------------------------------


def test_derive_ports_base_5100():
    from backend.fleet.runtime import derive_ports

    ports = derive_ports(5100)
    assert ports == {"vite": 5100, "api": 5200, "rpc": 5300, "sse": 5400}


def test_derive_ports_default():
    from backend.fleet.runtime import derive_ports

    ports = derive_ports(5173)
    assert ports == {"vite": 5173, "api": 5273, "rpc": 5373, "sse": 5473}


def test_derive_ports_autonomous_forever():
    """Autonomous-forever uses explicit ports, but derivation should still work."""
    from backend.fleet.runtime import derive_ports

    # AF uses hardcoded ports (18099/8765/8420/5173), not derived ones.
    # But the derive_ports function itself must produce a consistent 4-tuple.
    ports = derive_ports(5000)
    assert ports["api"] == ports["vite"] + 100
    assert ports["rpc"] == ports["vite"] + 200
    assert ports["sse"] == ports["vite"] + 300


# ---------------------------------------------------------------------------
# (c) GET /api/fleet/projects response shape
# ---------------------------------------------------------------------------


def test_fleet_projects_response_shape():
    """discover_running_projects returns a list of dicts with required fields."""
    from backend.fleet.runtime import _read_runtime

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        runtime_data = {
            "project_name": "test-project",
            "project_repo": "test-org/test-project",
            "state_dir": tmpdir,
            "ports": {"vite": 5100, "api": 5200, "rpc": 5300, "sse": 5400},
            "pids": {"api": 1234, "server": 1235, "sse": 1236, "vite": 1237},
            "started_at": "2026-05-18T16:00:00Z",
        }
        runtime_file = tmp / "dashboard-runtime.json"
        runtime_file.write_text(json.dumps(runtime_data))

        # Patch _probe_ports to return True (don't actually TCP connect)
        with patch("backend.fleet.runtime._probe_ports", return_value=True):
            record = _read_runtime(runtime_file, tmpdir)

    assert record["name"] == "test-project"
    assert record["repo"] == "test-org/test-project"
    assert "ports" in record
    assert "pids" in record
    assert "alive" in record
    assert "started_at" in record
    assert record["ok"] is True


def test_fleet_projects_alive_false_when_ports_closed():
    """alive is False when ports are not listening."""
    from backend.fleet.runtime import _read_runtime

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        runtime_data = {
            "project_name": "offline-project",
            "project_repo": "test-org/offline-project",
            "state_dir": tmpdir,
            "ports": {"vite": 9991, "api": 9992, "rpc": 9993, "sse": 9994},
            "pids": {"api": 99, "server": 100, "sse": 101, "vite": 102},
            "started_at": "2026-05-18T10:00:00Z",
        }
        runtime_file = tmp / "dashboard-runtime.json"
        runtime_file.write_text(json.dumps(runtime_data))

        # Use real _probe_ports — these ports are not listening, so should be False
        record = _read_runtime(runtime_file, tmpdir)

    assert record["alive"] is False


# ---------------------------------------------------------------------------
# (d) _probe_ports
# ---------------------------------------------------------------------------


def test_probe_ports_returns_false_on_connection_refused():
    from backend.fleet.runtime import _probe_ports

    # Use ports that are almost certainly not listening
    result = _probe_ports({"vite": 19991, "api": 19992}, timeout_s=0.1)
    assert result is False


def test_probe_ports_returns_false_on_empty():
    from backend.fleet.runtime import _probe_ports

    result = _probe_ports({})
    assert result is False


def test_probe_ports_returns_false_when_all_ports_non_integer():
    """When all port values are non-integer, nothing is probed — must return False.

    Previously the function fell through to `return True` without probing
    anything, falsely reporting alive:true.
    """
    from backend.fleet.runtime import _probe_ports

    # Port values are strings — no integer port to probe
    result = _probe_ports({"vite": "5173", "api": "8099"})
    assert result is False


def test_probe_ports_returns_false_when_mixed_and_no_integer_connects():
    """Mixed dict with one string and one non-listening integer — must return False."""
    from backend.fleet.runtime import _probe_ports

    # "api" is a string (skipped), "vite" is an int but not listening
    result = _probe_ports({"api": "not-a-port", "vite": 19993}, timeout_s=0.1)
    assert result is False


def test_probe_ports_returns_true_when_all_connect():
    """probe_ports returns True when a mock socket connects successfully."""
    from backend.fleet.runtime import _probe_ports

    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_cm)
    mock_cm.__exit__ = MagicMock(return_value=False)

    with patch("socket.create_connection", return_value=mock_cm):
        result = _probe_ports({"vite": 5100, "api": 5200})

    assert result is True


# ---------------------------------------------------------------------------
# (e) _read_runtime handles missing / malformed files gracefully
# ---------------------------------------------------------------------------


def test_read_runtime_missing_file():
    from backend.fleet.runtime import _read_runtime

    missing = Path("/tmp/definitely-does-not-exist-xyz/dashboard-runtime.json")
    record = _read_runtime(missing, "/tmp/definitely-does-not-exist-xyz")

    assert record["ok"] is False
    assert "error" in record
    assert record["alive"] is False


def test_read_runtime_malformed_json():
    from backend.fleet.runtime import _read_runtime

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        bad = tmp / "dashboard-runtime.json"
        bad.write_text("{not valid json")

        record = _read_runtime(bad, tmpdir)

    assert record["ok"] is False
    assert "JSON parse error" in record.get("error", "")
    assert record["alive"] is False


def test_invalidate_cache():
    """invalidate_cache resets the module-level cache."""
    from backend.fleet import runtime

    # Populate the cache
    runtime._cache_ts = 9999999.0
    runtime._cache_result = [{"name": "cached"}]

    runtime.invalidate_cache()

    assert runtime._cache_ts == 0.0
    assert runtime._cache_result is None


# ---------------------------------------------------------------------------
# Partial-up false-positive guard (Gap 2 regression test)
# ---------------------------------------------------------------------------


def test_probe_ports_false_when_only_vite_up():
    """alive must be False when only Vite is listening and api/rpc/sse are not.

    This is the exact failure mode from the projectb acceptance run: only the
    frontend dev server bound its port, but the probe returned True because
    it was short-circuiting after the first successful connection.
    """
    import socket as _socket
    from unittest.mock import patch

    from backend.fleet.runtime import _probe_ports

    ports = {"vite": 5102, "api": 5202, "rpc": 5302, "sse": 5402}

    def _fake_connect(addr, timeout=1.0):
        host, port = addr
        if port == 5102:
            # Vite IS up — return a mock context manager
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            return cm
        raise ConnectionRefusedError(f"port {port} not listening")

    with patch("socket.create_connection", side_effect=_fake_connect):
        result = _probe_ports(ports, timeout_s=0.1)

    assert result is False, (
        "alive must be False when api/rpc/sse are not listening, "
        "even if Vite is up"
    )


def test_probe_ports_true_only_when_all_four_up():
    """alive is True only when all four services (vite, api, rpc, sse) respond."""
    from backend.fleet.runtime import _probe_ports

    ports = {"vite": 5102, "api": 5202, "rpc": 5302, "sse": 5402}

    # All four respond successfully
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_cm)
    mock_cm.__exit__ = MagicMock(return_value=False)

    with patch("socket.create_connection", return_value=mock_cm):
        result = _probe_ports(ports, timeout_s=0.1)

    assert result is True
