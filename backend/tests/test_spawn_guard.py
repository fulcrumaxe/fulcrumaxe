"""Tests for backend/spawn_guard.py.

subprocess.Popen is NOT called here — these tests exercise SpawnGuard's
in-process logic only. Integration with _start_loop_run via HTTP is
covered in backend/test_api.py.
"""
from __future__ import annotations

import json
import time
import threading
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on the path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.spawn_guard import SpawnGuard, AcquireStatus, AcquireResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guard_with_gate(enabled: bool, min_interval: int = 1, per_source_cap: int = 1, global_cap: int = 2) -> SpawnGuard:
    """Create a fresh SpawnGuard with a mocked ControlPlane."""
    g = SpawnGuard()
    cp_mock = MagicMock()
    cp_mock.get.side_effect = lambda key: {
        "gates.allow_claude_spawn": enabled,
        "policies.spawn_guard.min_interval_seconds": min_interval,
        "policies.spawn_guard.per_source_cap": per_source_cap,
        "policies.spawn_guard.global_cap": global_cap,
    }.get(key)
    g._cp = cp_mock
    return g


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------

def test_gate_disabled_returns_gate_disabled():
    g = _guard_with_gate(enabled=False)
    result = g.acquire("test_source")
    assert result.status == AcquireStatus.GATE_DISABLED


def test_gate_enabled_allows_acquire():
    g = _guard_with_gate(enabled=True, min_interval=0)
    result = g.acquire("test_source")
    assert result.status == AcquireStatus.OK


def test_gate_missing_defaults_to_false():
    """When ControlPlane.get returns None for the gate key, default is False."""
    g = SpawnGuard()
    cp_mock = MagicMock()
    cp_mock.get.return_value = None  # all keys return None
    g._cp = cp_mock
    result = g.acquire("test_source")
    assert result.status == AcquireStatus.GATE_DISABLED


# ---------------------------------------------------------------------------
# Min-interval tests
# ---------------------------------------------------------------------------

def test_min_interval_blocks_second_fire_within_interval():
    """Two acquire calls for the same source within the interval: second is RATE_LIMITED."""
    g = _guard_with_gate(enabled=True, min_interval=60)
    r1 = g.acquire("src_A")
    assert r1.status == AcquireStatus.OK
    g.release("src_A")

    # Immediately acquire again — should be rate-limited
    r2 = g.acquire("src_A")
    assert r2.status == AcquireStatus.RATE_LIMITED
    assert 1 <= r2.retry_after_seconds <= 61


def test_different_sources_are_independent():
    """Acquiring source A does not rate-limit source B."""
    g = _guard_with_gate(enabled=True, min_interval=60, global_cap=4)
    r_a = g.acquire("src_A")
    assert r_a.status == AcquireStatus.OK

    r_b = g.acquire("src_B")
    assert r_b.status == AcquireStatus.OK

    g.release("src_A")
    g.release("src_B")


def test_release_then_reacquire_still_rate_limited():
    """Release restores the cap but NOT the interval — same source is still blocked."""
    g = _guard_with_gate(enabled=True, min_interval=60)
    g.acquire("src_A")
    g.release("src_A")

    r = g.acquire("src_A")
    assert r.status == AcquireStatus.RATE_LIMITED


# ---------------------------------------------------------------------------
# Cap tests
# ---------------------------------------------------------------------------

def test_per_source_cap_blocks_second_acquire_without_release():
    g = _guard_with_gate(enabled=True, min_interval=0, per_source_cap=1)
    r1 = g.acquire("src_A")
    assert r1.status == AcquireStatus.OK

    r2 = g.acquire("src_A")
    assert r2.status == AcquireStatus.CAP_REACHED

    g.release("src_A")


def test_global_cap_blocks_third_source():
    """With global_cap=2, a third source is blocked even though per-source cap is fine."""
    g = _guard_with_gate(enabled=True, min_interval=0, per_source_cap=2, global_cap=2)
    r1 = g.acquire("src_A")
    assert r1.status == AcquireStatus.OK

    r2 = g.acquire("src_B")
    assert r2.status == AcquireStatus.OK

    r3 = g.acquire("src_C")
    assert r3.status == AcquireStatus.CAP_REACHED

    g.release("src_A")
    g.release("src_B")


def test_release_restores_global_cap():
    g = _guard_with_gate(enabled=True, min_interval=0, per_source_cap=2, global_cap=2)
    g.acquire("src_A")
    g.acquire("src_B")
    g.release("src_A")

    # src_C should now succeed (global in-flight = 1 after release)
    r = g.acquire("src_C")
    assert r.status == AcquireStatus.OK
    g.release("src_B")
    g.release("src_C")


# ---------------------------------------------------------------------------
# Stats counter tests
# ---------------------------------------------------------------------------

def test_metrics_counter_increments_on_each_acquire():
    g = _guard_with_gate(enabled=True, min_interval=0, per_source_cap=10, global_cap=20)
    for i in range(5):
        r = g.acquire("src_A")
        assert r.status == AcquireStatus.OK, f"Iteration {i} failed: {r}"
        g.release("src_A")
        # Zero-out monotonic so next acquire isn't rate-limited
        g._by_source["src_A"].last_fire_ts = None

    stats = g.stats()
    assert stats["by_source"]["src_A"]["fires_total"] == 5


def test_stats_shows_gate_state():
    g = _guard_with_gate(enabled=True, min_interval=0)
    stats = g.stats()
    assert stats["gate_enabled"] is True

    g2 = _guard_with_gate(enabled=False)
    stats2 = g2.stats()
    assert stats2["gate_enabled"] is False


def test_stats_in_flight_decrements_after_release():
    g = _guard_with_gate(enabled=True, min_interval=0)
    g.acquire("src_A")
    assert g.stats()["by_source"]["src_A"]["in_flight"] == 1
    assert g.stats()["global_in_flight"] == 1

    g.release("src_A")
    assert g.stats()["by_source"]["src_A"]["in_flight"] == 0
    assert g.stats()["global_in_flight"] == 0


# ---------------------------------------------------------------------------
# assert_gate_present tests
# ---------------------------------------------------------------------------

def test_assert_gate_present_raises_when_key_missing():
    g = SpawnGuard()
    cp_mock = MagicMock()
    cp_mock.get.return_value = None
    g._cp = cp_mock
    with pytest.raises(RuntimeError, match="allow_claude_spawn"):
        g.assert_gate_present()


def test_assert_gate_present_passes_when_key_is_false():
    g = SpawnGuard()
    cp_mock = MagicMock()
    cp_mock.get.return_value = False
    g._cp = cp_mock
    g.assert_gate_present()  # must not raise


def test_assert_gate_present_passes_when_key_is_true():
    g = SpawnGuard()
    cp_mock = MagicMock()
    cp_mock.get.return_value = True
    g._cp = cp_mock
    g.assert_gate_present()  # must not raise


# ---------------------------------------------------------------------------
# Stats file round-trip
# ---------------------------------------------------------------------------

def test_stats_file_round_trip(tmp_path):
    """Guard writes stats to a file; read_stats_file() reads them back."""
    from backend import spawn_guard as sg_mod

    # Patch the stats file path to a temp location
    original_path = sg_mod._STATS_FILE
    sg_mod._STATS_FILE = tmp_path / "spawn-guard-stats.json"
    try:
        g = _guard_with_gate(enabled=True, min_interval=0)
        g.acquire("src_X")
        g.release("src_X")
        g._by_source["src_X"].last_fire_ts = None  # reset for clean state

        # File should have been written by release()
        data = SpawnGuard.read_stats_file()
        assert data is not None
        assert "by_source" in data
        assert "src_X" in data["by_source"]
        assert data["by_source"]["src_X"]["fires_total"] == 1
    finally:
        sg_mod._STATS_FILE = original_path


def test_read_stats_file_returns_none_when_missing(tmp_path):
    from backend import spawn_guard as sg_mod
    original_path = sg_mod._STATS_FILE
    sg_mod._STATS_FILE = tmp_path / "nonexistent.json"
    try:
        result = SpawnGuard.read_stats_file()
        assert result is None
    finally:
        sg_mod._STATS_FILE = original_path


# ---------------------------------------------------------------------------
# Thread safety smoke test
# ---------------------------------------------------------------------------

def test_concurrent_acquires_respect_global_cap():
    """Firing N threads simultaneously: only global_cap of them get OK."""
    GLOBAL_CAP = 2
    N = 6
    g = _guard_with_gate(enabled=True, min_interval=0, per_source_cap=N, global_cap=GLOBAL_CAP)

    results: list[AcquireStatus] = []
    lock = threading.Lock()

    def worker(i: int):
        r = g.acquire(f"src_{i}")
        with lock:
            results.append(r.status)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok_count = sum(1 for s in results if s == AcquireStatus.OK)
    assert ok_count <= GLOBAL_CAP
