"""
End-to-end smoke test for backend/api.py.

Starts the API server as a subprocess on a random port, waits for readiness,
exercises every endpoint, and tears down the process cleanly after all tests.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Readiness deadline — bump to 15s to survive cold-import worktrees where
# Python has no warmed bytecode cache.  Override via AF_API_SMOKE_TIMEOUT.
_READINESS_TIMEOUT = float(os.environ.get("AF_API_SMOKE_TIMEOUT", "15"))

# Maximum port-conflict retries (TOCTOU: another process may grab the port
# between _free_port() returning and api.py actually binding).
_PORT_RETRY_MAX = 3


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def _get(base_url: str, path: str) -> tuple[int, bytes]:
    """Make a GET request; return (status_code, body_bytes)."""
    url = base_url + path
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(base_url: str, path: str, payload: dict) -> tuple[int, bytes]:
    """Make a POST request with a JSON body; return (status_code, body_bytes)."""
    url = base_url + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _start_server(port: int) -> subprocess.Popen:
    """Launch api.py on *port* and return the Popen handle."""
    return subprocess.Popen(
        [sys.executable, "backend/api.py", "--port", str(port), "--no-enable-sse"],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_ready(proc: subprocess.Popen, base_url: str) -> tuple[bool, str, str]:
    """Poll /health until ready or deadline.

    Returns (ready, stdout_text, stderr_text).
    Exits early if the subprocess dies before the deadline (fix #4).
    """
    deadline = time.monotonic() + _READINESS_TIMEOUT
    while time.monotonic() < deadline:
        # Fix #4: liveness check — if the process has already exited, bail out
        # immediately rather than waiting out the full timeout.
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as resp:
                if resp.status == 200:
                    return True, "", ""
        except Exception:  # noqa: BLE001
            time.sleep(0.2)

    # Subprocess exited or deadline passed — collect output for diagnostics.
    proc.kill()
    try:
        out, err = proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return (
        False,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


@pytest.fixture(scope="module")
def api_server():
    """Start api.py on a random port; yield base URL; teardown on exit.

    Retries up to _PORT_RETRY_MAX times on port-conflict to handle TOCTOU
    races (fix #3).  Surfaces full stderr on startup failure (fix #1).
    """
    last_error: str = ""

    for attempt in range(1, _PORT_RETRY_MAX + 1):
        port = _free_port()
        proc = _start_server(port)
        base_url = f"http://127.0.0.1:{port}"

        ready, stdout_text, stderr_text = _wait_for_ready(proc, base_url)

        if ready:
            break

        # Fix #1: include stdout + stderr in the failure message so agents
        # see the actual ImportError / port collision / DB-lock immediately.
        last_error = (
            f"Attempt {attempt}/{_PORT_RETRY_MAX}: "
            f"API server did not become ready within {_READINESS_TIMEOUT}s\n"
            f"stdout: {stdout_text}\n"
            f"stderr: {stderr_text}"
        )

        # Fix #3: if stderr looks like a port-in-use error, retry with a
        # different port; otherwise fail immediately.
        port_collision = (
            "Address already in use" in stderr_text
            or "port" in stderr_text.lower()
        )
        if not port_collision or attempt == _PORT_RETRY_MAX:
            pytest.fail(last_error)
        # brief pause before retry so the OS recycles the port
        time.sleep(0.5)
    else:
        pytest.fail(last_error)

    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# GET endpoint tests
# ---------------------------------------------------------------------------


def test_health(api_server):
    status, body = _get(api_server, "/health")
    assert status == 200
    data = json.loads(body)
    assert data.get("ok") is True
    # Loop metrics fields are present (values may be None if no metrics file)
    assert "loop_last_run" in data
    assert "loop_duration_s" in data
    assert "loop_idle_rate" in data


def test_health_loop(api_server):
    status, body = _get(api_server, "/health/loop")
    assert status == 200
    data = json.loads(body)
    # Dashboard LoopHealth shape
    assert "lastRun" in data
    assert "status" in data
    assert "duration" in data
    assert data["status"] in ("ok", "error", "idle", "warning")


def test_budget_status(api_server):
    status, body = _get(api_server, "/budget/status")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, dict)


def test_registry(api_server):
    status, body = _get(api_server, "/registry")
    assert status == 200
    data = json.loads(body)
    assert "stats" in data


def test_registry_stats(api_server):
    status, body = _get(api_server, "/registry/stats")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, dict)


def test_control(api_server):
    status, body = _get(api_server, "/control")
    assert status == 200
    data = json.loads(body)
    assert "gates" in data
    assert "policies" in data


def test_control_gates(api_server):
    status, body = _get(api_server, "/control/gates")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, (dict, list))


def test_control_audit(api_server):
    status, body = _get(api_server, "/control/audit")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, (dict, list))


def test_agents(api_server):
    status, body = _get(api_server, "/agents")
    assert status == 200
    data = json.loads(body)
    assert "agents" in data
    assert isinstance(data["agents"], list)


def test_dashboard(api_server):
    status, body = _get(api_server, "/dashboard")
    assert status == 200
    # Dashboard returns HTML
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


def test_not_found(api_server):
    status, _body = _get(api_server, "/nonexistent")
    assert status == 404


# ---------------------------------------------------------------------------
# POST endpoint tests
# ---------------------------------------------------------------------------


def test_budget_init(api_server):
    status, body = _post(api_server, "/budget/init", {})
    assert status == 200
    data = json.loads(body)
    assert data.get("ok") is True
    assert "status" in data


def test_control_set(api_server):
    status, body = _post(api_server, "/control/set", {"key": "smoke_test_key", "value": "smoke"})
    assert status == 200
    data = json.loads(body)
    assert data.get("ok") is True
    assert data.get("key") == "smoke_test_key"
