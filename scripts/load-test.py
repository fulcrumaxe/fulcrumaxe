#!/usr/bin/env python3
"""Load testing script for autonomous-forever API.

Uses only stdlib (concurrent.futures + urllib). No external dependencies.

Usage:
    python3 scripts/load-test.py [--workers N] [--requests N] [--base-url URL]

Exit code 1 if error rate > 5% or p99 latency > 2 seconds.
"""

import argparse
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple


ENDPOINTS = [
    "/health",
    "/budget/status",
    "/registry",
    "/registry/stats",
    "/control",
    "/metrics",
    "/cost/summary",
]


class Result(NamedTuple):
    endpoint: str
    status: int
    elapsed_ms: float
    error: str | None


def hit(base_url: str, endpoint: str, timeout: int = 10) -> Result:
    """Make a single GET request and return a Result."""
    url = base_url.rstrip("/") + endpoint
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read()
            elapsed = (time.monotonic() - start) * 1000
            return Result(endpoint, resp.status, elapsed, None)
    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - start) * 1000
        return Result(endpoint, e.code, elapsed, str(e))
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        return Result(endpoint, 0, elapsed, str(e))


def percentile(data: list[float], p: float) -> float:
    """Compute the p-th percentile of data (0–100)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = (p / 100) * (len(sorted_data) - 1)
    lower = int(idx)
    upper = min(lower + 1, len(sorted_data) - 1)
    frac = idx - lower
    return sorted_data[lower] * (1 - frac) + sorted_data[upper] * frac


def fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms/1000:.2f}s"
    return f"{ms:.1f}ms"


def run_load_test(base_url: str, workers: int, total_requests: int) -> int:
    """Run load test and return exit code (0=pass, 1=fail)."""
    print(f"Load test: {base_url}")
    print(f"Workers: {workers}  |  Total requests: {total_requests}")
    print(f"Endpoints: {', '.join(ENDPOINTS)}")
    print()

    # Build task list: distribute requests across endpoints
    tasks: list[tuple[str, str]] = []
    for i in range(total_requests):
        endpoint = ENDPOINTS[i % len(ENDPOINTS)]
        tasks.append((base_url, endpoint))

    results: list[Result] = []
    start_wall = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(hit, base_url, endpoint) for (base_url, endpoint) in tasks]
        for fut in as_completed(futures):
            results.append(fut.result())

    total_wall_ms = (time.monotonic() - start_wall) * 1000

    # Aggregate
    all_latencies = [r.elapsed_ms for r in results]
    errors = [r for r in results if r.error is not None or r.status >= 500 or r.status == 0]
    successes = [r for r in results if r.error is None and r.status < 500 and r.status != 0]
    error_rate = len(errors) / len(results) * 100 if results else 100

    rps = len(results) / (total_wall_ms / 1000) if total_wall_ms > 0 else 0

    p50 = percentile(all_latencies, 50)
    p95 = percentile(all_latencies, 95)
    p99 = percentile(all_latencies, 99)

    # Per-endpoint breakdown
    by_endpoint: dict[str, list[Result]] = {}
    for r in results:
        by_endpoint.setdefault(r.endpoint, []).append(r)

    # Print summary
    print("=== Summary ===")
    print(f"Total requests : {len(results)}")
    print(f"Successes      : {len(successes)}")
    print(f"Errors         : {len(errors)} ({error_rate:.1f}%)")
    print(f"Total time     : {fmt_ms(total_wall_ms)}")
    print(f"Throughput     : {rps:.1f} req/s")
    print()
    print("=== Latency ===")
    print(f"p50 : {fmt_ms(p50)}")
    print(f"p95 : {fmt_ms(p95)}")
    print(f"p99 : {fmt_ms(p99)}")
    if all_latencies:
        print(f"min : {fmt_ms(min(all_latencies))}")
        print(f"max : {fmt_ms(max(all_latencies))}")
    print()

    print("=== Per-endpoint breakdown ===")
    col_w = max(len(ep) for ep in ENDPOINTS)
    print(f"{'Endpoint':<{col_w}}  {'Reqs':>6}  {'Errs':>6}  {'p50':>8}  {'p95':>8}  {'p99':>8}")
    print("-" * (col_w + 44))
    for ep in ENDPOINTS:
        ep_results = by_endpoint.get(ep, [])
        if not ep_results:
            continue
        ep_lat = [r.elapsed_ms for r in ep_results]
        ep_errs = sum(1 for r in ep_results if r.error or r.status >= 500 or r.status == 0)
        print(
            f"{ep:<{col_w}}  {len(ep_results):>6}  {ep_errs:>6}"
            f"  {fmt_ms(percentile(ep_lat, 50)):>8}"
            f"  {fmt_ms(percentile(ep_lat, 95)):>8}"
            f"  {fmt_ms(percentile(ep_lat, 99)):>8}"
        )
    print()

    # Gate checks
    passed = True
    if error_rate > 5.0:
        print(f"[FAIL] Error rate {error_rate:.1f}% exceeds 5% threshold")
        passed = False
    if p99 > 2000:
        print(f"[FAIL] p99 latency {fmt_ms(p99)} exceeds 2s threshold")
        passed = False
    if passed:
        print("[PASS] All thresholds met")

    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Load test for autonomous-forever API")
    parser.add_argument("--workers", type=int, default=10, help="Number of concurrent workers (default: 10)")
    parser.add_argument("--requests", type=int, default=100, help="Total number of requests (default: 100)")
    parser.add_argument("--base-url", default="http://localhost:18099", help="API base URL (default: http://localhost:18099)")
    args = parser.parse_args()

    exit_code = run_load_test(args.base_url, args.workers, args.requests)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
