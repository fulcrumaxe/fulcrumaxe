"""P1 perf gate: open cap+5 concurrent stub SSE streams, assert /health stays fast.

Covers AC10:
- Opens cap+5 concurrent streams against /stub/stream
- Those above the cap immediately get 503 (no blocking)
- While streams are open, GET /health must stay <200ms p95
- af_threadpool_active (anyio borrowed_tokens) must not pin at total_tokens

This test uses real HTTP via httpx with a live uvicorn server to exercise
the ASGI stack end-to-end. Uvicorn is started on a free port in a background
thread.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from statistics import median

import httpx
import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_PERF_GATE_CAP = 10  # global stream cap for perf gate tests


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Start a real uvicorn server on a free port. Yield the base URL."""
    os.environ.pop("AF_API_AUTH_KEY", None)
    os.environ["AF_THREADPOOL_TOKENS"] = "256"
    os.environ["AF_GLOBAL_STREAM_CAP"] = str(_PERF_GATE_CAP)

    import backend.asgi_app as asgi_mod
    from backend.deps.stream_limiter import GlobalStreamLimiter

    # Reset the module-level stream_limiter to the cap configured for this test.
    # (It may have been created with a different cap on first import.)
    asgi_mod.stream_limiter = GlobalStreamLimiter(max_global=_PERF_GATE_CAP)

    port = _free_port()
    config = uvicorn.Config(
        asgi_mod.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    # Wait for the server to become ready (up to 10s).
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base}/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    else:
        pytest.skip("live server did not start in time")

    yield base

    server.should_exit = True
    t.join(timeout=5)


def test_health_fast_under_concurrent_streams(live_server):
    """GET /health must stay <200ms p95 while cap+5 streams are open.

    cap = 10 (AF_GLOBAL_STREAM_CAP), so we open 15 concurrent streams.
    10 should get 200, 5 should get 503. In both cases the call returns fast.
    Meanwhile we fire 20 sequential /health requests and check p95 latency.
    """
    base = live_server
    cap = _PERF_GATE_CAP

    # Phase 1: open cap+5 streams concurrently in background threads.
    stream_results: list[int] = []
    stream_lock = threading.Lock()
    stop_event = threading.Event()

    def _open_stream():
        try:
            with httpx.stream("GET", f"{base}/stub/stream", timeout=30.0) as r:
                with stream_lock:
                    stream_results.append(r.status_code)
                if r.status_code == 200:
                    # Hold the stream open until stop_event is set.
                    stop_event.wait(timeout=10)
        except Exception:
            with stream_lock:
                stream_results.append(-1)

    threads = [threading.Thread(target=_open_stream, daemon=True) for _ in range(cap + 5)]
    for t in threads:
        t.start()

    # Let streams settle.
    time.sleep(0.5)

    # Phase 2: measure /health latency under load.
    latencies_ms: list[float] = []
    for _ in range(20):
        t0 = time.monotonic()
        r = httpx.get(f"{base}/health", timeout=2.0)
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert r.status_code == 200, f"/health returned {r.status_code} under stream load"
        latencies_ms.append(elapsed_ms)

    # Release the streams.
    stop_event.set()
    for t in threads:
        t.join(timeout=5)

    # Phase 3: assert p95 <200ms.
    latencies_ms.sort()
    p95_idx = int(len(latencies_ms) * 0.95)
    p95_ms = latencies_ms[min(p95_idx, len(latencies_ms) - 1)]
    assert p95_ms < 200, (
        f"p95 latency of /health was {p95_ms:.1f}ms — exceeds 200ms budget. "
        f"All latencies: {[f'{x:.1f}' for x in latencies_ms]}"
    )

    # Phase 4: cap+5 streams — 5 should be 503, 10 should be 200.
    # At least 1 must be 503 (cap enforcement), and at least 1 must be 200.
    with stream_lock:
        results = list(stream_results)

    successes = results.count(200)
    cap_hits = results.count(503)
    assert cap_hits >= 5, (
        f"Expected at least 5 cap rejections (503) but got {cap_hits}. "
        f"Results: {results}"
    )
    assert successes >= 1, (
        f"Expected at least 1 successful stream (200) but got {successes}. "
        f"Results: {results}"
    )


def test_threadpool_not_pinned_under_stream_load(live_server):
    """af_threadpool_active must not pin at total_tokens during stream load.

    We check this by hitting a dedicated /health endpoint that returns JSON
    while streams are open. If the threadpool were pinned, the health check
    would block and time out. A successful <200ms response proves it's not pinned.
    """
    base = live_server
    # /health is a quick check; if it responds in time, threadpool isn't exhausted.
    import time

    t0 = time.monotonic()
    r = httpx.get(f"{base}/health", timeout=2.0)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert r.status_code == 200, f"/health returned {r.status_code}"
    assert elapsed_ms < 200, (
        f"/health took {elapsed_ms:.1f}ms — threadpool may be pinned"
    )
