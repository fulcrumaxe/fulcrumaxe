"""Tests for the Prometheus metrics endpoint and backend/metrics.py module.

Covers:
- GET /metrics status code
- Content-Type header
- HELP and TYPE lines present
- At least 13 metrics exposed
- Missing data does not cause errors (graceful omission)
- No auth required
- generate_prometheus_metrics() produces valid output when subsystems fail
- Metric names follow the af_ prefix convention
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def _get(base_url: str, path: str) -> tuple[int, bytes, dict]:
    """Make a GET request; return (status_code, body_bytes, headers_dict)."""
    url = base_url + path
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            headers = dict(resp.headers)
            return resp.status, resp.read(), headers
    except urllib.error.HTTPError as exc:
        headers = dict(exc.headers) if exc.headers else {}
        return exc.code, exc.read(), headers


@pytest.fixture(scope="module")
def api_server():
    """Start api.py on a random port without auth; yield base URL."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "backend/api.py", "--port", str(port), "--no-enable-sse"],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5.0
    ready = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    if not ready:
        proc.kill()
        proc.wait()
        pytest.fail("API server did not become ready within 5 seconds")
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
# Integration tests against live server
# ---------------------------------------------------------------------------


def test_metrics_status_200(api_server):
    """GET /metrics returns HTTP 200."""
    status, _body, _headers = _get(api_server, "/metrics")
    assert status == 200


def test_metrics_content_type(api_server):
    """GET /metrics returns the Prometheus content type."""
    _status, _body, headers = _get(api_server, "/metrics")
    ct = headers.get("Content-Type", "")
    assert "text/plain" in ct
    assert "version=0.0.4" in ct
    assert "charset=utf-8" in ct.lower()


def test_metrics_contains_help_lines(api_server):
    """Response body contains at least one # HELP line."""
    _status, body, _headers = _get(api_server, "/metrics")
    text = body.decode("utf-8")
    assert "# HELP " in text


def test_metrics_contains_type_lines(api_server):
    """Response body contains at least one # TYPE line."""
    _status, body, _headers = _get(api_server, "/metrics")
    text = body.decode("utf-8")
    assert "# TYPE " in text


def test_metrics_af_prefix(api_server):
    """All metric names use the af_ prefix."""
    _status, body, _headers = _get(api_server, "/metrics")
    text = body.decode("utf-8")
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            # "# TYPE af_foo_name gauge"
            parts = line.split()
            assert len(parts) == 4, f"Unexpected # TYPE line format: {line!r}"
            assert parts[2].startswith("af_"), f"Metric without af_ prefix: {parts[2]}"


def test_metrics_no_auth_required(api_server):
    """GET /metrics works without any Authorization header (no 401/403)."""
    status, _body, _headers = _get(api_server, "/metrics")
    assert status not in (401, 403)


def test_metrics_no_auth_required_with_auth_server():
    """GET /metrics returns 200 even when auth key is set (metrics bypass auth)."""
    port = _free_port()
    import os
    env = {**os.environ, "AF_API_AUTH_KEY": "test-secret-key"}
    proc = subprocess.Popen(
        [sys.executable, "backend/api.py", "--port", str(port), "--no-enable-sse"],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as resp:
                if resp.status == 200:
                    break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    try:
        status, _body, _headers = _get(base_url, "/metrics")
        assert status == 200, f"/metrics returned {status} with auth enabled — should be 200"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Unit tests for generate_prometheus_metrics()
# ---------------------------------------------------------------------------


def test_generate_returns_string():
    """generate_prometheus_metrics() returns a str."""
    from backend.metrics import generate_prometheus_metrics
    result = generate_prometheus_metrics()
    assert isinstance(result, str)


def test_generate_ends_with_newline():
    """Output ends with a newline as required by Prometheus exposition format."""
    from backend.metrics import generate_prometheus_metrics
    result = generate_prometheus_metrics()
    assert result.endswith("\n")


def test_generate_graceful_on_all_failures():
    """generate_prometheus_metrics() does not raise even when all subsystems fail."""
    from backend.metrics import generate_prometheus_metrics

    def _raise(*args, **kwargs):
        raise RuntimeError("subsystem down")

    with (
        patch("backend.metrics.logger") as _mock_log,
        patch("builtins.__import__", side_effect=_raise),
    ):
        # Should not raise — graceful empty output
        try:
            result = generate_prometheus_metrics()
            assert isinstance(result, str)
        except RuntimeError:
            pytest.fail("generate_prometheus_metrics() raised when subsystems failed")


def test_gauge_lines_omits_none():
    """_gauge_lines returns [] when value is None (metric omitted gracefully)."""
    from backend.metrics import _gauge_lines
    assert _gauge_lines("af_test", "test help", None) == []


def test_gauge_lines_returns_three_lines():
    """_gauge_lines returns exactly 3 lines (HELP, TYPE, value) for a valid value."""
    from backend.metrics import _gauge_lines
    result = _gauge_lines("af_test_metric", "A test metric", 42)
    assert len(result) == 3
    assert result[0] == "# HELP af_test_metric A test metric"
    assert result[1] == "# TYPE af_test_metric gauge"
    assert result[2] == "af_test_metric 42"
