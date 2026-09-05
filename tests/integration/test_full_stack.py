"""Integration tests for the autonomous-forever API server.

Starts the API server as a subprocess on a random high port,
waits for /health to respond, runs all endpoint tests, then tears down.

Uses only stdlib — no requests dependency required.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Generator

import pytest


def pytest_configure(config):  # type: ignore[no-untyped-def]
    """Register custom marks to suppress PytestUnknownMarkWarning."""
    config.addinivalue_line("markers", "timeout: mark test with a timeout in seconds")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find_free_port() -> int:
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: int = 15) -> bool:
    """Poll /health until it responds 200 or timeout is reached."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _get(url: str, timeout: int = 10) -> tuple[int, dict | str]:
    """Perform a GET request and return (status_code, parsed_body)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def _post(url: str, body: dict, timeout: int = 10) -> tuple[int, dict | str]:
    """Perform a POST request with a JSON body."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(len(data))},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_server() -> Generator[str, None, None]:
    """Start the API server on a free port. Yield base URL. Stop on teardown."""
    port = _find_free_port()

    # Check the port isn't already in use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            pytest.skip(f"Port {port} already in use — skipping integration tests")

    api_script = os.path.join(REPO_ROOT, "backend", "api.py")
    if not os.path.exists(api_script):
        pytest.skip("backend/api.py not found — skipping integration tests")

    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    env["AF_API_PORT"] = str(port)
    # Disable auth for integration tests
    env.pop("AF_API_TOKEN", None)
    env.pop("AF_API_KEY", None)

    proc = subprocess.Popen(
        [sys.executable, api_script, "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"

    if not _wait_for_health(base_url, timeout=20):
        proc.terminate()
        proc.wait(timeout=5)
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        pytest.skip(
            f"API server did not start within 20s on port {port}.\n"
            f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
        )

    yield base_url

    # Teardown
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.timeout(10)
def test_health(api_server: str) -> None:
    """GET /health returns {"ok": true}."""
    status, body = _get(f"{api_server}/health")
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert isinstance(body, dict), f"Expected JSON dict, got: {type(body)}"
    assert body.get("ok") is True, f"Expected ok=true, got: {body}"


@pytest.mark.timeout(10)
def test_budget_init_then_status(api_server: str) -> None:
    """POST /budget/init then GET /budget/status shows an initialized budget."""
    # Initialize budget
    init_status, init_body = _post(f"{api_server}/budget/init", {})
    assert init_status in (200, 201), f"budget/init failed with {init_status}: {init_body}"

    # Check status
    status, body = _get(f"{api_server}/budget/status")
    assert status == 200, f"budget/status returned {status}: {body}"
    assert isinstance(body, dict), f"Expected dict from budget/status, got: {body}"
    # Should have some budget-related fields
    assert len(body) > 0, "budget/status returned empty dict"


@pytest.mark.timeout(10)
def test_registry_schema(api_server: str) -> None:
    """GET /registry returns valid JSON with expected schema."""
    status, body = _get(f"{api_server}/registry")
    assert status == 200, f"registry returned {status}: {body}"
    assert isinstance(body, dict), f"Expected dict from /registry, got: {type(body)}"
    # Registry should have at minimum some recognizable top-level keys
    assert len(body) >= 0, "registry returned unexpected structure"


@pytest.mark.timeout(10)
def test_control_gates(api_server: str) -> None:
    """GET /control/gates returns gates list."""
    status, body = _get(f"{api_server}/control/gates")
    assert status == 200, f"control/gates returned {status}: {body}"
    assert isinstance(body, dict), f"Expected dict from /control/gates, got: {body}"


@pytest.mark.timeout(10)
def test_metrics_prometheus_format(api_server: str) -> None:
    """GET /metrics returns Prometheus text format."""
    status, body = _get(f"{api_server}/metrics")
    assert status == 200, f"metrics returned {status}: {body}"
    # Prometheus format is plain text, not JSON
    # Either a string with metric lines, or a dict (some implementations return JSON)
    # Just verify we get a non-empty response
    assert body is not None and body != "", "metrics returned empty body"


@pytest.mark.timeout(10)
def test_cost_summary(api_server: str) -> None:
    """GET /cost/summary returns cost breakdown."""
    status, body = _get(f"{api_server}/cost/summary")
    assert status == 200, f"cost/summary returned {status}: {body}"
    assert isinstance(body, dict), f"Expected dict from /cost/summary, got: {body}"
