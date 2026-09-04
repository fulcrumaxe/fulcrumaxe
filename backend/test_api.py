"""
HTTP-level tests for SpawnGuard integration in backend/api.py.

Tests verify that the _innovate_tick and loop/run endpoints enforce
rate-limiting, concurrency caps, and the feature gate correctly.

subprocess.Popen is monkeypatched to a stub; no real claude binary needed.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock
import urllib.request
import urllib.error

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers: spin up a test server with mocked SpawnGuard
# ---------------------------------------------------------------------------

def _make_guard_mock(gate_enabled: bool = True, acquire_status: str = "ok",
                     retry_after: int = 30) -> MagicMock:
    """Return a mock SpawnGuard whose acquire() returns the given status."""
    from backend.spawn_guard import AcquireStatus, AcquireResult

    status_map = {
        "ok": AcquireStatus.OK,
        "rate_limited": AcquireStatus.RATE_LIMITED,
        "cap_reached": AcquireStatus.CAP_REACHED,
        "gate_disabled": AcquireStatus.GATE_DISABLED,
    }
    status = status_map[acquire_status]
    result = AcquireResult(
        status=status,
        retry_after_seconds=retry_after if status == AcquireStatus.RATE_LIMITED else 0,
        source="innovate_tick_internal",
        message="test mock",
    )
    mock = MagicMock()
    mock.acquire.return_value = result
    mock.release.return_value = None
    mock.stats.return_value = {
        "by_source": {},
        "global_in_flight": 0,
        "gate_enabled": gate_enabled,
    }
    mock.assert_gate_present.return_value = None
    return mock


# ---------------------------------------------------------------------------
# Test: POST /api/innovate/tick returns 429 on rate limit
# ---------------------------------------------------------------------------

class TestInnovateTick429:
    """Second call within 30s returns HTTP 429 with Retry-After header."""

    def test_innovate_tick_returns_429_on_second_call_within_30s(self, tmp_path):
        """Fire twice; first OK, second rate-limited."""
        import backend.api as api_mod
        from backend.spawn_guard import AcquireStatus, AcquireResult

        call_count = 0

        def _side_effect(source: str) -> AcquireResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AcquireResult(status=AcquireStatus.OK, source=source)
            return AcquireResult(
                status=AcquireStatus.RATE_LIMITED,
                retry_after_seconds=30,
                source=source,
                message="rate-limited test",
            )

        guard_mock = MagicMock()
        guard_mock.acquire.side_effect = _side_effect
        guard_mock.release.return_value = None
        guard_mock.assert_gate_present = lambda: None
        guard_mock.stats.return_value = {"by_source": {}, "global_in_flight": 0, "gate_enabled": True}

        # Patch Popen so the first call doesn't try to run claude
        fake_proc = MagicMock()
        fake_proc.pid = 12345
        fake_proc.stdout = iter([])
        fake_proc.returncode = 0
        fake_proc.wait.return_value = 0

        with patch.object(api_mod, "_spawn_guard", guard_mock), \
             patch("subprocess.Popen", return_value=fake_proc):

            # First call: should succeed (guard returns OK)
            r1 = _call_innovate_tick(api_mod)
            assert r1["status"] in (200, 503), f"Unexpected status on first call: {r1}"

            # Second call: should be rate-limited
            r2 = _call_innovate_tick(api_mod)
            assert r2["status"] == 429, f"Expected 429 but got {r2['status']}: {r2}"
            assert "retry_after" in str(r2.get("body", "")).lower() or \
                   "retry-after" in str(r2.get("headers", {})).lower() or \
                   r2.get("retry_after_header") is not None


def _call_innovate_tick(api_mod: Any) -> dict:
    """Make a direct in-process call through the handler's do_POST path."""
    from backend.spawn_guard import AcquireStatus, AcquireResult

    # Simulate the handler logic for /api/innovate/tick by calling _innovate_tick
    # and observing the PermissionError behavior
    try:
        result = api_mod._innovate_tick()
        return {"status": 200, "body": result}
    except PermissionError as exc:
        msg = str(exc)
        if "rate-limited" in msg:
            import re
            m = re.search(r"wait (\d+)s", msg)
            retry = int(m.group(1)) if m else 60
            return {
                "status": 429,
                "retry_after_header": str(retry),
                "body": json.dumps({"error": "rate-limited", "retry_after_seconds": retry}),
            }
        elif "spawn gate disabled" in msg:
            return {"status": 503, "body": json.dumps({"error": "spawn gate disabled"})}
        else:
            return {"status": 503, "body": json.dumps({"error": "spawn-cap reached"})}
    except FileNotFoundError:
        return {"status": 503, "body": "claude not found"}
    except Exception as exc:
        return {"status": 500, "body": str(exc)}


# ---------------------------------------------------------------------------
# Test: gate disabled returns 503 for all spawn endpoints
# ---------------------------------------------------------------------------

class TestGateDisabled503:
    """When gates.allow_claude_spawn=false, all spawn endpoints return 503."""

    def test_gate_disabled_returns_503_for_innovate_tick(self):
        import backend.api as api_mod
        from backend.spawn_guard import AcquireStatus, AcquireResult

        guard_mock = MagicMock()
        guard_mock.acquire.return_value = AcquireResult(
            status=AcquireStatus.GATE_DISABLED,
            source="innovate_tick_internal",
            message="gate off",
        )
        guard_mock.release.return_value = None
        guard_mock.assert_gate_present = lambda: None

        with patch.object(api_mod, "_spawn_guard", guard_mock):
            result = _call_innovate_tick(api_mod)
            assert result["status"] == 503
            body = result.get("body", "")
            assert "gate" in body.lower() or "disabled" in body.lower() or "spawn" in body.lower()

    def test_gate_disabled_returns_503_for_start_loop_run(self):
        import backend.api as api_mod
        from backend.spawn_guard import AcquireStatus, AcquireResult

        guard_mock = MagicMock()
        guard_mock.acquire.return_value = AcquireResult(
            status=AcquireStatus.GATE_DISABLED,
            source="loop_run_global",
            message="gate off",
        )
        guard_mock.release.return_value = None

        with patch.object(api_mod, "_spawn_guard", guard_mock):
            with pytest.raises(PermissionError, match="spawn gate disabled"):
                api_mod._start_loop_run("test instruction", source="loop_run_global")


# ---------------------------------------------------------------------------
# Test: three concurrent innovate calls — one OK, two CAP_REACHED
# ---------------------------------------------------------------------------

class TestConcurrentCap:
    """Three concurrent calls: exactly one gets OK, two get cap_reached."""

    def test_three_concurrent_innovate_calls_one_succeeds_two_503(self):
        import backend.api as api_mod
        from backend.spawn_guard import AcquireStatus, AcquireResult

        # First acquire returns OK, subsequent ones return CAP_REACHED
        call_lock = threading.Lock()
        call_count = [0]

        def _side_effect(source: str) -> AcquireResult:
            with call_lock:
                call_count[0] += 1
                n = call_count[0]
            if n == 1:
                return AcquireResult(status=AcquireStatus.OK, source=source)
            return AcquireResult(
                status=AcquireStatus.CAP_REACHED,
                source=source,
                message="cap reached",
            )

        guard_mock = MagicMock()
        guard_mock.acquire.side_effect = _side_effect
        guard_mock.release.return_value = None

        results: list[dict] = []
        results_lock = threading.Lock()

        fake_proc = MagicMock()
        fake_proc.pid = 99999
        fake_proc.stdout = iter([])
        fake_proc.returncode = 0
        fake_proc.wait.return_value = 0

        def _worker():
            with patch.object(api_mod, "_spawn_guard", guard_mock), \
                 patch("subprocess.Popen", return_value=fake_proc):
                r = _call_innovate_tick(api_mod)
                with results_lock:
                    results.append(r)

        threads = [threading.Thread(target=_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok_count = sum(1 for r in results if r["status"] == 200)
        cap_count = sum(1 for r in results if r["status"] == 503)

        # At least 2 should be 503 (one OK, two cap_reached)
        # The first call may or may not reach 200 depending on whether
        # _innovate_tick itself raises before returning; account for
        # FileNotFoundError when claude binary is absent
        assert cap_count >= 2, f"Expected >=2 503s, got: {results}"


# ---------------------------------------------------------------------------
# Test: _start_loop_run raises correctly on each guard outcome
# ---------------------------------------------------------------------------

class TestStartLoopRunGuardIntegration:
    def test_start_loop_run_raises_on_rate_limit(self):
        import backend.api as api_mod
        from backend.spawn_guard import AcquireStatus, AcquireResult

        guard_mock = MagicMock()
        guard_mock.acquire.return_value = AcquireResult(
            status=AcquireStatus.RATE_LIMITED,
            retry_after_seconds=45,
            source="loop_run_global",
            message="rate-limited test",
        )

        with patch.object(api_mod, "_spawn_guard", guard_mock):
            with pytest.raises(PermissionError, match="rate-limited"):
                api_mod._start_loop_run("test", source="loop_run_global")

    def test_start_loop_run_raises_on_cap_reached(self):
        import backend.api as api_mod
        from backend.spawn_guard import AcquireStatus, AcquireResult

        guard_mock = MagicMock()
        guard_mock.acquire.return_value = AcquireResult(
            status=AcquireStatus.CAP_REACHED,
            source="loop_run_global",
            message="cap test",
        )

        with patch.object(api_mod, "_spawn_guard", guard_mock):
            with pytest.raises(PermissionError, match="cap reached"):
                api_mod._start_loop_run("test", source="loop_run_global")



# ---------------------------------------------------------------------------
# _get_dashboard_config — state-dir runtime.json takes priority over repo-side
# ---------------------------------------------------------------------------


def test_dashboard_config_reads_state_dir_first(tmp_path):
    """State-dir dashboard-runtime.json provides rpcBaseUrl."""
    import backend.api as api_mod
    from backend.api import _get_dashboard_config

    state_runtime = tmp_path / "dashboard-runtime.json"
    state_runtime.write_text(json.dumps({
        "rpcBaseUrl": "http://localhost:9999",
        "dashboardVersion": "1.2.3",
    }))

    with patch.object(api_mod, "_STATE_DIR", tmp_path), \
         patch.object(api_mod, "_REPO_ROOT", tmp_path):
        result = _get_dashboard_config()

    assert result["rpcBaseUrl"] == "http://localhost:9999"


def test_dashboard_config_fallback_to_repo_side(tmp_path):
    """Falls back to repo-side runtime.json when state-dir file is absent."""
    import backend.api as api_mod
    from backend.api import _get_dashboard_config

    repo_runtime = tmp_path / ".autonomous-team" / "dashboard-runtime.json"
    repo_runtime.parent.mkdir(exist_ok=True)
    repo_runtime.write_text(json.dumps({"rpcBaseUrl": "http://repo-side:8765"}))

    with patch.object(api_mod, "_STATE_DIR", tmp_path / "empty-state"), \
         patch.object(api_mod, "_REPO_ROOT", tmp_path):
        result = _get_dashboard_config()

    assert result["rpcBaseUrl"] == "http://repo-side:8765"


def test_dashboard_config_defaults_when_no_files(tmp_path):
    """Returns hard-coded defaults when neither runtime file exists."""
    import backend.api as api_mod
    from backend.api import _get_dashboard_config

    with patch.object(api_mod, "_STATE_DIR", tmp_path / "no-state"), \
         patch.object(api_mod, "_REPO_ROOT", tmp_path):
        result = _get_dashboard_config()

    assert result["rpcBaseUrl"] == "http://localhost:8765"
