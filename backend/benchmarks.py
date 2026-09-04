"""Performance benchmarking module — rolling percentile tracking.

Provides a lightweight, always-on PerformanceRecorder that captures timing
samples in thread-safe circular buffers and computes rolling p50/p95/p99
statistics. Uses only stdlib (time, statistics, threading, collections).

Usage (library):
    from backend.benchmarks import get_recorder

    rec = get_recorder()
    rec.record("http", "GET /health", 12.5, metadata={"status_code": 200})
    stats = rec.compute_stats("http")
    print(stats.p50_ms, stats.p95_ms, stats.p99_ms)

Usage (CLI):
    python backend/benchmarks.py stats
    python backend/benchmarks.py record-http --count 100
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

# Allow running as a script from repo root: `python backend/benchmarks.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {"http", "event_bus", "spawn", "db"}


@dataclass
class BenchmarkSample:
    """A single timing observation."""

    timestamp: float  # time.monotonic() epoch-relative
    wall_time: float  # time.time() — for windowing
    category: str
    operation: str
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkStats:
    """Computed percentile statistics over a time window."""

    category: str
    operation: str | None
    window_seconds: int
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    avg_ms: float
    stddev_ms: float
    samples_per_second: float


@dataclass
class _MinuteBucket:
    """One-minute aggregate for history endpoint."""

    unix_minute: int  # floor(time.time() / 60)
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    samples: list[float] = field(default_factory=list)

    def add(self, duration_ms: float) -> None:
        """Accumulate one sample into this bucket."""
        self.count += 1
        self.total_ms += duration_ms
        if duration_ms < self.min_ms:
            self.min_ms = duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms
        self.samples.append(duration_ms)

    def to_stats(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary of the bucket."""
        if not self.samples:
            return {
                "unix_minute": self.unix_minute,
                "count": 0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "min_ms": 0.0,
                "max_ms": 0.0,
                "avg_ms": 0.0,
            }
        sorted_s = sorted(self.samples)
        n = len(sorted_s)

        def _pct(p: float) -> float:
            idx = max(0, min(n - 1, int(n * p)))
            return sorted_s[idx]

        return {
            "unix_minute": self.unix_minute,
            "count": self.count,
            "p50_ms": round(_pct(0.50), 3),
            "p95_ms": round(_pct(0.95), 3),
            "p99_ms": round(_pct(0.99), 3),
            "min_ms": round(self.min_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "avg_ms": round(self.total_ms / self.count, 3),
        }


# ---------------------------------------------------------------------------
# PerformanceRecorder
# ---------------------------------------------------------------------------


class PerformanceRecorder:
    """Thread-safe circular-buffer performance recorder.

    Captures timing samples per category in a deque(maxlen=10000).  All
    write operations hold a single lock for thread safety; reads copy the
    deque snapshot to minimise lock contention.
    """

    def __init__(self, max_samples: int = 10_000) -> None:
        """Initialise recorder with the given per-category buffer capacity."""
        self._max_samples = max_samples
        self._lock = threading.Lock()
        # Per-category sample buffers: {category: deque[BenchmarkSample]}
        self._buffers: dict[str, deque[BenchmarkSample]] = {}
        # Per-category+operation 1-minute history buckets
        # {(category, operation): {unix_minute: _MinuteBucket}}
        self._history: dict[tuple[str, str], dict[int, _MinuteBucket]] = {}
        # Bucket eviction: keep at most 60 minutes of history
        self._history_max_minutes = 60

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        category: str,
        operation: str,
        duration_ms: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a single timing sample.

        Args:
            category: One of "http", "event_bus", "spawn", "db".
            operation: Descriptive label (e.g. "GET /health").
            duration_ms: Elapsed time in milliseconds.
            metadata: Optional dict with extra context (status_code, etc.).
        """
        sample = BenchmarkSample(
            timestamp=time.monotonic(),
            wall_time=time.time(),
            category=category,
            operation=operation,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        key = (category, operation)
        unix_minute = int(sample.wall_time // 60)

        with self._lock:
            # Circular buffer — deque enforces maxlen automatically.
            if category not in self._buffers:
                self._buffers[category] = deque(maxlen=self._max_samples)
            self._buffers[category].append(sample)

            # History buckets
            if key not in self._history:
                self._history[key] = {}
            buckets = self._history[key]
            if unix_minute not in buckets:
                buckets[unix_minute] = _MinuteBucket(unix_minute=unix_minute)
                # Evict old buckets
                cutoff = unix_minute - self._history_max_minutes
                old_keys = [k for k in buckets if k < cutoff]
                for ok in old_keys:
                    del buckets[ok]
            buckets[unix_minute].add(duration_ms)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def compute_stats(
        self,
        category: str,
        operation: str | None = None,
        window_seconds: int = 300,
    ) -> BenchmarkStats:
        """Compute rolling percentile statistics.

        Args:
            category: Category to filter on.
            operation: Optional operation to further filter.  If None,
                all operations within the category are included.
            window_seconds: How far back to look (default 300 = 5 minutes).

        Returns:
            BenchmarkStats with p50/p95/p99/min/max/avg/stddev/rate.
            All duration fields are 0.0 when no samples exist in the window.
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            buf = self._buffers.get(category)
            if buf is None:
                samples: list[float] = []
            else:
                samples = [
                    s.duration_ms
                    for s in buf
                    if s.wall_time >= cutoff
                    and (operation is None or s.operation == operation)
                ]

        return _build_stats(
            samples=samples,
            category=category,
            operation=operation,
            window_seconds=window_seconds,
        )

    def get_all_stats(self, window_seconds: int = 300) -> list[BenchmarkStats]:
        """Return stats for every (category, operation) pair seen.

        Empty categories are included with zeroed stats so callers always
        see a complete picture.
        """
        result: list[BenchmarkStats] = []
        with self._lock:
            # snapshot keys
            categories = list(self._buffers.keys())

        for cat in categories:
            # collect distinct operations within this category
            with self._lock:
                buf = self._buffers.get(cat, deque())
                ops: set[str] = {s.operation for s in buf}
            for op in ops:
                result.append(self.compute_stats(cat, op, window_seconds))
        return result

    def get_history(
        self,
        category: str,
        operation: str | None = None,
        points: int = 60,
    ) -> list[dict[str, Any]]:
        """Return the last *points* one-minute buckets as a time series.

        Args:
            category: Category to query.
            operation: Optional operation filter.
            points: How many 1-minute buckets to return (max 60).

        Returns:
            List of bucket dicts ordered oldest-first.  Missing buckets
            (no activity that minute) are omitted from the output.
        """
        points = min(points, 60)
        now_minute = int(time.time() // 60)
        cutoff_minute = now_minute - points

        with self._lock:
            # Collect all matching buckets
            relevant: dict[int, _MinuteBucket] = {}
            for (cat, op), buckets in self._history.items():
                if cat != category:
                    continue
                if operation is not None and op != operation:
                    continue
                for minute, bucket in buckets.items():
                    if minute < cutoff_minute:
                        continue
                    if minute not in relevant:
                        relevant[minute] = _MinuteBucket(unix_minute=minute)
                    # merge bucket data
                    existing = relevant[minute]
                    existing.count += bucket.count
                    existing.total_ms += bucket.total_ms
                    if bucket.min_ms < existing.min_ms:
                        existing.min_ms = bucket.min_ms
                    if bucket.max_ms > existing.max_ms:
                        existing.max_ms = bucket.max_ms
                    existing.samples.extend(bucket.samples)

        return [
            b.to_stats()
            for _, b in sorted(relevant.items())
            if b.count > 0
        ]

    def get_categories(self) -> list[str]:
        """Return all categories that have at least one recorded sample."""
        with self._lock:
            return list(self._buffers.keys())

    def get_operations(self, category: str) -> list[str]:
        """Return all operation names seen for a given category."""
        with self._lock:
            buf = self._buffers.get(category, deque())
            return sorted({s.operation for s in buf})

    def buffer_size(self, category: str) -> int:
        """Return how many samples are currently held for a category."""
        with self._lock:
            buf = self._buffers.get(category)
            return len(buf) if buf else 0


# ---------------------------------------------------------------------------
# Percentile helpers
# ---------------------------------------------------------------------------


def _build_stats(
    samples: list[float],
    category: str,
    operation: str | None,
    window_seconds: int,
) -> BenchmarkStats:
    """Build a BenchmarkStats from a list of duration_ms values."""
    if not samples:
        return BenchmarkStats(
            category=category,
            operation=operation,
            window_seconds=window_seconds,
            count=0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            min_ms=0.0,
            max_ms=0.0,
            avg_ms=0.0,
            stddev_ms=0.0,
            samples_per_second=0.0,
        )

    sorted_s = sorted(samples)
    n = len(sorted_s)

    def _pct(p: float) -> float:
        # For small samples, avoid misleading precision on high percentiles.
        if n < 10 and p >= 0.95:
            return sorted_s[-1]
        idx = max(0, min(n - 1, int(n * p)))
        return sorted_s[idx]

    avg = statistics.mean(samples)
    stddev = statistics.stdev(samples) if n > 1 else 0.0
    rate = n / window_seconds if window_seconds > 0 else 0.0

    return BenchmarkStats(
        category=category,
        operation=operation,
        window_seconds=window_seconds,
        count=n,
        p50_ms=round(_pct(0.50), 3),
        p95_ms=round(_pct(0.95), 3),
        p99_ms=round(_pct(0.99), 3),
        min_ms=round(sorted_s[0], 3),
        max_ms=round(sorted_s[-1], 3),
        avg_ms=round(avg, 3),
        stddev_ms=round(stddev, 3),
        samples_per_second=round(rate, 6),
    )


def _stats_to_dict(stats: BenchmarkStats) -> dict[str, Any]:
    """Convert BenchmarkStats to a JSON-serialisable dict."""
    return {
        "category": stats.category,
        "operation": stats.operation,
        "window_seconds": stats.window_seconds,
        "count": stats.count,
        "p50_ms": stats.p50_ms,
        "p95_ms": stats.p95_ms,
        "p99_ms": stats.p99_ms,
        "min_ms": stats.min_ms,
        "max_ms": stats.max_ms,
        "avg_ms": stats.avg_ms,
        "stddev_ms": stats.stddev_ms,
        "samples_per_second": stats.samples_per_second,
    }


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_recorder: PerformanceRecorder | None = None
_recorder_lock = threading.Lock()


def get_recorder() -> PerformanceRecorder:
    """Return the process-global PerformanceRecorder, creating it if needed."""
    global _recorder  # noqa: PLW0603
    if _recorder is None:
        with _recorder_lock:
            if _recorder is None:
                _recorder = PerformanceRecorder()
    return _recorder


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_stats(_args: argparse.Namespace) -> None:
    """Print current rolling stats table to stdout."""
    rec = get_recorder()
    all_stats = rec.get_all_stats(window_seconds=300)
    if not all_stats:
        print("No samples recorded yet.")
        return

    header = (
        f"{'Category':<12} {'Operation':<30} {'Count':>6} "
        f"{'p50(ms)':>9} {'p95(ms)':>9} {'p99(ms)':>9} {'avg(ms)':>9}"
    )
    print(header)
    print("-" * len(header))
    for s in sorted(all_stats, key=lambda x: (x.category, x.operation or "")):
        op = (s.operation or "all")[:30]
        print(
            f"{s.category:<12} {op:<30} {s.count:>6} "
            f"{s.p50_ms:>9.1f} {s.p95_ms:>9.1f} {s.p99_ms:>9.1f} {s.avg_ms:>9.1f}"
        )


def _cmd_record_http(args: argparse.Namespace) -> None:
    """Run N GET /health requests against local API and display results."""
    port = getattr(args, "port", 18099)
    count = getattr(args, "count", 100)
    url = f"http://127.0.0.1:{port}/health"
    rec = get_recorder()

    print(f"Sending {count} requests to {url} ...")
    errors = 0
    for _ in range(count):
        t0 = time.monotonic()
        try:
            with urlopen(url, timeout=5) as resp:  # noqa: S310
                status = resp.status
                body = resp.read()
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        elapsed = (time.monotonic() - t0) * 1000.0
        rec.record(
            "http",
            "GET /health",
            elapsed,
            metadata={"status_code": status, "response_size_bytes": len(body)},
        )

    print(f"Done. {count - errors} succeeded, {errors} errored.")
    stats = rec.compute_stats("http", "GET /health")
    print(
        f"p50={stats.p50_ms:.1f}ms  p95={stats.p95_ms:.1f}ms  "
        f"p99={stats.p99_ms:.1f}ms  avg={stats.avg_ms:.1f}ms  "
        f"min={stats.min_ms:.1f}ms  max={stats.max_ms:.1f}ms"
    )


def main() -> None:
    """Entry point for the benchmarks CLI."""
    parser = argparse.ArgumentParser(
        prog="python backend/benchmarks.py",
        description="Performance benchmark recorder CLI",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("stats", help="Print current rolling stats table")

    rh = sub.add_parser("record-http", help="Run N requests against local API")
    rh.add_argument("--count", type=int, default=100, help="Number of requests")
    rh.add_argument("--port", type=int, default=18099, help="API port")

    args = parser.parse_args()
    if args.command == "stats":
        _cmd_stats(args)
    elif args.command == "record-http":
        _cmd_record_http(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
