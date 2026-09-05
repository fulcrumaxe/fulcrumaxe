"""Smoke tests for backend/rpc/stats_*.py modules.

Each module must expose a handle(params: dict) -> dict function that returns
the correct response shape. Stats functions are mocked so tests run without
a live DuckDB store.

NOTE on patching: the RPC modules do ``from backend.stats_writer import X as _X``,
which binds the function at import time.  To intercept those bindings we must:
  1. Patch ``backend.stats_writer.<name>`` (not ``stats_writer.<name>``).
  2. Call ``_load_handler()`` *inside* the ``with patch(...)`` block so the
     ``from ... import`` statement executes while the mock is in place.
"""
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is importable (so ``backend.*`` imports resolve)
REPO_ROOT = Path(__file__).parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_handler(module_name: str):
    """Import backend.rpc.<module_name> and return its handle function.

    Must be called inside the relevant ``with patch(...)`` block so that the
    module's top-level ``from backend.stats_writer import …`` captures the mock.
    """
    spec = importlib.util.spec_from_file_location(
        module_name,
        BACKEND_DIR / "rpc" / f"{module_name}.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handle


# ---------------------------------------------------------------------------
# stats.role_success_rate
# ---------------------------------------------------------------------------

def test_role_success_rate_shape():
    mock_rows = [{"role": "executor", "success_rate": 0.9, "sample_size": 10}]
    with patch("backend.stats_writer.role_success_rate_24h", return_value=mock_rows):
        handle = _load_handler("stats_role_success_rate")
        result = handle({})
    assert "rows" in result
    assert result["rows"] == mock_rows


def test_role_success_rate_empty():
    with patch("backend.stats_writer.role_success_rate_24h", return_value=[]):
        handle = _load_handler("stats_role_success_rate")
        result = handle({})
    assert result == {"rows": []}


# ---------------------------------------------------------------------------
# stats.role_retry_rate
# ---------------------------------------------------------------------------

def test_role_retry_rate_shape():
    mock_rows = [{"role": "code-reviewer", "retry_rate": 0.2, "sample_size": 8}]
    with patch("backend.stats_writer.role_retry_rate_24h", return_value=mock_rows):
        handle = _load_handler("stats_role_retry_rate")
        result = handle({})
    assert "rows" in result
    assert result["rows"] == mock_rows


def test_role_retry_rate_empty():
    with patch("backend.stats_writer.role_retry_rate_24h", return_value=[]):
        handle = _load_handler("stats_role_retry_rate")
        result = handle({})
    assert result == {"rows": []}


# ---------------------------------------------------------------------------
# stats.team_lead_tokens
# ---------------------------------------------------------------------------

def test_team_lead_tokens_shape():
    mock_result = {"avg": 4500.0, "p50": 4200.0, "p95": 7800.0, "sample_size": 12}
    with patch("backend.stats_writer.team_lead_tokens_percentiles", return_value=mock_result):
        handle = _load_handler("stats_team_lead_tokens")
        result = handle({})
    assert result == mock_result


def test_team_lead_tokens_since_hours_param():
    captured = {}

    def fake_percentiles(since_hours=24):
        captured["since_hours"] = since_hours
        return {"avg": None, "p50": None, "p95": None, "sample_size": 0}

    with patch("backend.stats_writer.team_lead_tokens_percentiles", side_effect=fake_percentiles):
        handle = _load_handler("stats_team_lead_tokens")
        handle({"since_hours": "48"})
    assert captured["since_hours"] == 48


# ---------------------------------------------------------------------------
# stats.loop_idle_ratio
# ---------------------------------------------------------------------------

def test_loop_idle_ratio_shape():
    mock_result = {"ratio": 0.3, "idle_count": 3, "sample_size": 10}
    with patch("backend.stats_writer.loop_idle_ratio_24h", return_value=mock_result):
        handle = _load_handler("stats_loop_idle_ratio")
        result = handle({})
    assert result == mock_result


def test_loop_idle_ratio_null_when_small_sample():
    mock_result = {"ratio": None, "idle_count": 0, "sample_size": 2}
    with patch("backend.stats_writer.loop_idle_ratio_24h", return_value=mock_result):
        handle = _load_handler("stats_loop_idle_ratio")
        result = handle({})
    assert result["ratio"] is None


# ---------------------------------------------------------------------------
# stats.cost_spike_history
# ---------------------------------------------------------------------------

def test_cost_spike_history_shape():
    mock_spikes = [{"ts_iso": "2026-05-12T10:00:00Z", "value": 1200.0, "mu": 800.0, "sigma": 200.0}]
    with patch("backend.stats_writer.cost_spike_history", return_value=mock_spikes):
        handle = _load_handler("stats_cost_spike_history")
        result = handle({})
    assert result["count"] == 1
    assert result["spikes"] == mock_spikes
    assert result["last_spike_iso"] == "2026-05-12T10:00:00Z"


def test_cost_spike_history_empty():
    with patch("backend.stats_writer.cost_spike_history", return_value=[]):
        handle = _load_handler("stats_cost_spike_history")
        result = handle({})
    assert result == {"spikes": [], "count": 0, "last_spike_iso": None}


def test_cost_spike_history_hours_param():
    captured = {}

    def fake_history(hours=24):
        captured["hours"] = hours
        return []

    with patch("backend.stats_writer.cost_spike_history", side_effect=fake_history):
        handle = _load_handler("stats_cost_spike_history")
        handle({"hours": "72"})
    assert captured["hours"] == 72


# ---------------------------------------------------------------------------
# stats.avg_fix_rounds_per_pr
# ---------------------------------------------------------------------------

def test_avg_fix_rounds_shape():
    mock_result = {"avg_last_24h": 1.2, "sample_size": 8, "distribution": {"0": 2, "1": 4, "2": 2}}
    with patch("backend.stats_writer.avg_fix_rounds_24h", return_value=mock_result):
        handle = _load_handler("stats_avg_fix_rounds_per_pr")
        result = handle({})
    assert result == mock_result


def test_avg_fix_rounds_null_when_small_sample():
    mock_result = {"avg_last_24h": None, "sample_size": 3, "distribution": {}}
    with patch("backend.stats_writer.avg_fix_rounds_24h", return_value=mock_result):
        handle = _load_handler("stats_avg_fix_rounds_per_pr")
        result = handle({})
    assert result["avg_last_24h"] is None
    assert result["sample_size"] == 3


# ---------------------------------------------------------------------------
# Module structure: all 6 modules must have handle()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_name", [
    "stats_role_success_rate",
    "stats_role_retry_rate",
    "stats_team_lead_tokens",
    "stats_loop_idle_ratio",
    "stats_cost_spike_history",
    "stats_avg_fix_rounds_per_pr",
])
def test_module_has_handle_callable(module_name):
    handle = _load_handler(module_name)
    assert callable(handle), f"{module_name}.handle must be callable"
