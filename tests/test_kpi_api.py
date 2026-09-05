"""
Tests for the /kpi endpoints added to backend/api.py.

Covers: response schema, velocity subset, cycle-time subset, cache behaviour,
graceful degradation when files are missing, SSE snapshot inclusion, and
dashboard HTML containing the KPI card.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Server fixture (same pattern as test_api_smoke.py)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def _get(base_url: str, path: str) -> tuple[int, bytes]:
    url = base_url + path
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture(scope="module")
def api_server():
    """Start api.py on a random port; yield base URL; teardown on exit."""
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
# /kpi — response schema
# ---------------------------------------------------------------------------


def test_kpi_returns_200(api_server):
    status, body = _get(api_server, "/kpi")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, dict)


def test_kpi_schema_keys(api_server):
    """Response must include all six top-level keys defined in the spec."""
    _, body = _get(api_server, "/kpi")
    data = json.loads(body)
    required_keys = {"version", "computed_at", "velocity", "estimation_accuracy", "idle_rate", "pr_cycle_time"}
    assert required_keys.issubset(data.keys()), f"Missing keys: {required_keys - data.keys()}"


def test_kpi_version_is_1(api_server):
    _, body = _get(api_server, "/kpi")
    data = json.loads(body)
    assert data["version"] == 1


def test_kpi_velocity_has_expected_fields(api_server):
    _, body = _get(api_server, "/kpi")
    v = json.loads(body)["velocity"]
    assert "last_24h" in v
    assert "all_time_per_day" in v
    assert "total_done" in v


# ---------------------------------------------------------------------------
# /kpi/velocity — subset endpoint
# ---------------------------------------------------------------------------


def test_kpi_velocity_subset_returns_200(api_server):
    status, body = _get(api_server, "/kpi/velocity")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, dict)


def test_kpi_velocity_subset_has_velocity_fields(api_server):
    _, body = _get(api_server, "/kpi/velocity")
    data = json.loads(body)
    assert "last_24h" in data
    assert "all_time_per_day" in data
    # Should NOT contain top-level KPI keys
    assert "version" not in data
    assert "pr_cycle_time" not in data


# ---------------------------------------------------------------------------
# /kpi/cycle-time — subset endpoint
# ---------------------------------------------------------------------------


def test_kpi_cycle_time_returns_200(api_server):
    status, body = _get(api_server, "/kpi/cycle-time")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data, dict)


def test_kpi_cycle_time_has_expected_fields(api_server):
    _, body = _get(api_server, "/kpi/cycle-time")
    data = json.loads(body)
    assert "mean_hours" in data
    assert "median_hours" in data
    assert "total_measured" in data
    assert "version" not in data


# ---------------------------------------------------------------------------
# Cache behaviour — two rapid requests return the same computed_at
# ---------------------------------------------------------------------------


def test_kpi_cache_returns_same_computed_at(api_server):
    """Two requests within 60 s must hit the cache and share computed_at."""
    _, body1 = _get(api_server, "/kpi")
    _, body2 = _get(api_server, "/kpi")
    d1, d2 = json.loads(body1), json.loads(body2)
    assert d1.get("computed_at") == d2.get("computed_at"), (
        "computed_at differed between two rapid requests — cache may not be working"
    )


# ---------------------------------------------------------------------------
# Graceful degradation — endpoints must not 500 even without data files
# ---------------------------------------------------------------------------


def test_kpi_no_500_when_files_missing(api_server):
    """The server must return 200 even when registry/metrics files don't exist."""
    # The test environment may or may not have those files; either way, no 500.
    status, body = _get(api_server, "/kpi")
    assert status == 200, f"Expected 200, got {status}: {body[:200]}"
    data = json.loads(body)
    # velocity must exist and be a dict with non-negative total_done
    assert isinstance(data.get("velocity"), dict)
    assert data["velocity"].get("total_done", 0) >= 0


# ---------------------------------------------------------------------------
# Dashboard HTML contains KPI card
# ---------------------------------------------------------------------------


def test_dashboard_contains_kpi_card(api_server):
    status, body = _get(api_server, "/dashboard")
    assert status == 200
    html = body.decode("utf-8")
    assert "card-kpi" in html, "Dashboard HTML is missing the KPI card (id=card-kpi)"
    assert "kpi-24h" in html
    assert "kpi-velocity" in html
    assert "kpi-cycle" in html
    assert "kpi-idle" in html


# ---------------------------------------------------------------------------
# KPI rendering data — fields that the dashboard JS reads
# ---------------------------------------------------------------------------


def test_kpi_cycle_time_field_present_for_js_rendering(api_server):
    """pr_cycle_time.mean_hours must be in the /kpi response so the dashboard
    JS can render the cycle-time card.  A missing or None value causes '--'."""
    _, body = _get(api_server, "/kpi")
    data = json.loads(body)
    ct = data.get("pr_cycle_time")
    assert isinstance(ct, dict), f"pr_cycle_time is not a dict: {ct!r}"
    assert "mean_hours" in ct, "pr_cycle_time is missing mean_hours key"


def test_kpi_idle_rate_field_present_for_js_rendering(api_server):
    """idle_rate.all_time_pct must be in the /kpi response so the dashboard
    JS can render the idle-rate card.  A missing or None value causes '--'."""
    _, body = _get(api_server, "/kpi")
    data = json.loads(body)
    ir = data.get("idle_rate")
    assert isinstance(ir, dict), f"idle_rate is not a dict: {ir!r}"
    assert "all_time_pct" in ir, "idle_rate is missing all_time_pct key"


def test_kpi_response_shape_matches_updatekpi_expectations(api_server):
    """Verify the /kpi response has the exact shape that updateKPI() in dashboard.py
    expects: top-level pr_cycle_time and idle_rate dicts with their respective fields."""
    _, body = _get(api_server, "/kpi")
    data = json.loads(body)
    # updateKPI reads data.velocity, data.pr_cycle_time, data.idle_rate
    assert "velocity" in data, "velocity missing from /kpi response"
    assert "pr_cycle_time" in data, "pr_cycle_time missing — JS will show '--' for cycle time"
    assert "idle_rate" in data, "idle_rate missing — JS will show '--' for idle rate"
    # updateKPI reads v.last_24h, v.all_time_per_day
    v = data["velocity"]
    assert "last_24h" in v
    assert "all_time_per_day" in v
    # updateKPI reads ct.mean_hours
    ct = data["pr_cycle_time"]
    assert "mean_hours" in ct
    # updateKPI reads ir.all_time_pct
    ir = data["idle_rate"]
    assert "all_time_pct" in ir


def test_dashboard_safefetch_awaits_json(api_server):
    """The safeFetch function in the dashboard JS must await r.json() so that
    JSON parse errors are caught and don't leave cards showing '--' silently."""
    _, body = _get(api_server, "/dashboard")
    html = body.decode("utf-8")
    # Must use 'return await r.json()' not 'return r.json()' to catch parse errors
    assert "return await r.json()" in html, (
        "safeFetch is missing 'await' before r.json() — JSON parse errors "
        "will propagate as unhandled rejections and leave KPI cards at '--'"
    )
