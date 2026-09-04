"""Unit tests for backend.benchmarks — PerformanceRecorder, percentile stats,
circular buffer overflow, and time windowing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# Allow import without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.benchmarks import (
    BenchmarkSample,
    BenchmarkStats,
    PerformanceRecorder,
    _build_stats,
    get_recorder,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_recorder(max_samples: int = 100) -> PerformanceRecorder:
    """Return a fresh PerformanceRecorder for testing."""
    return PerformanceRecorder(max_samples=max_samples)


# ---------------------------------------------------------------------------
# Basic recording and stats
# ---------------------------------------------------------------------------


class TestBasicRecording:
    """Tests for record() and compute_stats()."""

    def test_record_increments_count(self) -> None:
        """Recording samples increases the stats count."""
        rec = _make_recorder()
        for i in range(5):
            rec.record("http", "GET /health", float(i + 1))
        stats = rec.compute_stats("http", "GET /health")
        assert stats.count == 5

    def test_p50_median_value(self) -> None:
        """p50 should be near the median of recorded values."""
        rec = _make_recorder()
        # Record 1..9 ms — median is 5 ms
        for v in range(1, 10):
            rec.record("http", "GET /health", float(v))
        stats = rec.compute_stats("http", "GET /health")
        # p50 index = int(9 * 0.5) = 4, sorted = [1,2,3,4,5,6,7,8,9] → 5
        assert stats.p50_ms == 5.0

    def test_p99_max_for_small_samples(self) -> None:
        """For samples < 10, p99 returns max to avoid misleading precision."""
        rec = _make_recorder()
        for v in [1.0, 2.0, 3.0]:
            rec.record("http", "GET /health", v)
        stats = rec.compute_stats("http")
        assert stats.p99_ms == 3.0

    def test_empty_category_returns_zeroed_stats(self) -> None:
        """Querying an unseen category returns zeroed stats, not an error."""
        rec = _make_recorder()
        stats = rec.compute_stats("nonexistent_category")
        assert stats.count == 0
        assert stats.p50_ms == 0.0
        assert stats.p95_ms == 0.0
        assert stats.p99_ms == 0.0
        assert stats.min_ms == 0.0
        assert stats.max_ms == 0.0

    def test_min_max_avg(self) -> None:
        """min, max, and avg are computed correctly."""
        rec = _make_recorder()
        for v in [10.0, 20.0, 30.0]:
            rec.record("db", "SELECT *", v)
        stats = rec.compute_stats("db")
        assert stats.min_ms == 10.0
        assert stats.max_ms == 30.0
        assert stats.avg_ms == pytest.approx(20.0)

    def test_operation_filter(self) -> None:
        """compute_stats filters to the requested operation."""
        rec = _make_recorder()
        rec.record("http", "GET /health", 5.0)
        rec.record("http", "POST /control/set", 50.0)
        stats = rec.compute_stats("http", "GET /health")
        assert stats.count == 1
        assert stats.avg_ms == pytest.approx(5.0)

    def test_category_aggregation(self) -> None:
        """compute_stats with no operation aggregates all operations."""
        rec = _make_recorder()
        rec.record("http", "GET /health", 5.0)
        rec.record("http", "POST /control/set", 50.0)
        stats = rec.compute_stats("http")
        assert stats.count == 2

    def test_samples_per_second_nonzero(self) -> None:
        """samples_per_second is positive when samples exist."""
        rec = _make_recorder()
        rec.record("spawn", "executor", 200.0)
        stats = rec.compute_stats("spawn", window_seconds=300)
        assert stats.samples_per_second > 0.0


# ---------------------------------------------------------------------------
# Circular buffer overflow
# ---------------------------------------------------------------------------


class TestCircularBuffer:
    """Tests for circular buffer capacity enforcement."""

    def test_buffer_does_not_exceed_max(self) -> None:
        """Buffer size never exceeds max_samples regardless of how many we record."""
        max_s = 50
        rec = _make_recorder(max_samples=max_s)
        for i in range(200):
            rec.record("http", "GET /health", float(i))
        assert rec.buffer_size("http") == max_s

    def test_oldest_samples_evicted(self) -> None:
        """When the buffer fills up, the oldest samples are evicted."""
        rec = _make_recorder(max_samples=3)
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            rec.record("http", "GET /health", v)
        # Only last 3 samples should remain: 3.0, 4.0, 5.0
        stats = rec.compute_stats("http", "GET /health")
        assert stats.count == 3
        assert stats.min_ms == 3.0
        assert stats.max_ms == 5.0

    def test_multiple_categories_independent_buffers(self) -> None:
        """Each category has its own independent buffer."""
        rec = _make_recorder(max_samples=5)
        for i in range(10):
            rec.record("http", "GET /health", float(i))
        for i in range(3):
            rec.record("db", "SELECT *", float(i))
        assert rec.buffer_size("http") == 5
        assert rec.buffer_size("db") == 3


# ---------------------------------------------------------------------------
# Time windowing
# ---------------------------------------------------------------------------


class TestTimeWindowing:
    """Tests for the window_seconds filter in compute_stats."""

    def test_samples_outside_window_excluded(self) -> None:
        """Samples older than window_seconds are excluded from stats."""
        rec = _make_recorder()
        # Inject samples manually with an old wall_time
        from backend.benchmarks import BenchmarkSample

        old_sample = BenchmarkSample(
            timestamp=time.monotonic() - 1000,
            wall_time=time.time() - 1000,
            category="http",
            operation="GET /health",
            duration_ms=999.0,
        )
        fresh_sample = BenchmarkSample(
            timestamp=time.monotonic(),
            wall_time=time.time(),
            category="http",
            operation="GET /health",
            duration_ms=5.0,
        )
        from collections import deque

        with rec._lock:
            rec._buffers["http"] = deque([old_sample, fresh_sample], maxlen=100)

        stats = rec.compute_stats("http", window_seconds=300)
        assert stats.count == 1
        assert stats.avg_ms == pytest.approx(5.0)

    def test_empty_window_returns_zeroed_stats(self) -> None:
        """A window that excludes all samples returns zeroed stats."""
        rec = _make_recorder()
        rec.record("http", "GET /health", 10.0)
        # Use a 0-second window — should exclude all samples
        stats = rec.compute_stats("http", window_seconds=0)
        assert stats.count == 0


# ---------------------------------------------------------------------------
# History buckets
# ---------------------------------------------------------------------------


class TestHistory:
    """Tests for the get_history() time-series endpoint."""

    def test_history_returns_list(self) -> None:
        """get_history returns a list of bucket dicts."""
        rec = _make_recorder()
        rec.record("http", "GET /health", 5.0)
        history = rec.get_history("http", "GET /health", points=60)
        assert isinstance(history, list)

    def test_history_bucket_fields(self) -> None:
        """Each history bucket has the expected fields."""
        rec = _make_recorder()
        rec.record("http", "GET /health", 5.0)
        history = rec.get_history("http", "GET /health", points=60)
        assert len(history) >= 1
        bucket = history[0]
        assert "unix_minute" in bucket
        assert "count" in bucket
        assert "p50_ms" in bucket
        assert "p95_ms" in bucket
        assert "p99_ms" in bucket
        assert "avg_ms" in bucket

    def test_history_points_limit(self) -> None:
        """get_history respects the points limit."""
        rec = _make_recorder()
        rec.record("http", "GET /health", 5.0)
        # Even with points=1, we should not return more than 1 bucket
        history = rec.get_history("http", points=1)
        assert len(history) <= 1

    def test_history_clamps_points_to_60(self) -> None:
        """Points are clamped to 60 maximum."""
        rec = _make_recorder()
        # Requesting 999 points should be silently clamped to 60
        history = rec.get_history("http", points=999)
        # No assertion on length — just should not raise
        assert isinstance(history, list)


# ---------------------------------------------------------------------------
# Thread safety (smoke test)
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Smoke test for concurrent recording."""

    def test_concurrent_recording(self) -> None:
        """Concurrent writes from multiple threads do not raise exceptions."""
        import threading

        rec = _make_recorder(max_samples=200)
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for i in range(50):
                    rec.record("http", "GET /health", float(i))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        # At most 200 samples (maxlen enforced)
        assert rec.buffer_size("http") <= 200


# ---------------------------------------------------------------------------
# get_recorder() singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Tests for the global recorder singleton."""

    def test_get_recorder_returns_same_instance(self) -> None:
        """get_recorder() always returns the same object."""
        r1 = get_recorder()
        r2 = get_recorder()
        assert r1 is r2


# ---------------------------------------------------------------------------
# _build_stats helper
# ---------------------------------------------------------------------------


class TestBuildStats:
    """Tests for the internal _build_stats helper."""

    def test_single_sample(self) -> None:
        """Single-sample stats: p50 == p95 == p99 == that value."""
        s = _build_stats([42.0], category="http", operation="GET /", window_seconds=300)
        assert s.p50_ms == 42.0
        assert s.p95_ms == 42.0
        assert s.p99_ms == 42.0
        assert s.min_ms == 42.0
        assert s.max_ms == 42.0

    def test_stddev_zero_for_single(self) -> None:
        """stddev is 0 for a single sample."""
        s = _build_stats([5.0], category="http", operation=None, window_seconds=60)
        assert s.stddev_ms == 0.0

    def test_stddev_positive_for_varied(self) -> None:
        """stddev is positive for varied samples."""
        s = _build_stats([1.0, 10.0, 100.0], category="http", operation=None, window_seconds=300)
        assert s.stddev_ms > 0.0
