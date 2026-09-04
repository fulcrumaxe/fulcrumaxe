"""
Global SSE/WS concurrent stream limiter.

Thread-safe cap on the total number of open streams across all IPs and
connections.  When the cap is reached, ``acquire()`` returns False and the
caller should return HTTP 503.

Usage::

    from backend.deps.stream_limiter import GlobalStreamLimiter

    _limiter = GlobalStreamLimiter(max_global=20)

    if not _limiter.acquire():
        return Response(status_code=503)
    try:
        yield  # stream data
    finally:
        _limiter.release()
"""

from __future__ import annotations

import threading


class GlobalStreamLimiter:
    """Thread-safe global concurrent SSE/WS stream limiter.

    Tracks the total number of open streams across all IPs and connections.
    When the global cap is reached, ``acquire()`` returns False and callers
    should return HTTP 503.
    """

    def __init__(self, max_global: int) -> None:
        self._max = max_global
        self._count: int = 0
        self._lock = threading.Lock()

    @property
    def max_global(self) -> int:
        return self._max

    @property
    def active(self) -> int:
        with self._lock:
            return self._count

    def acquire(self) -> bool:
        with self._lock:
            if self._count >= self._max:
                return False
            self._count += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._count > 0:
                self._count -= 1
