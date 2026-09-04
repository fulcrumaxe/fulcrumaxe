"""
Token bucket rate limiter for the REST API.

Provides per-IP rate limiting with configurable rate and burst, an SSE
connection tracker, and stale bucket cleanup — all using the Python standard
library only.

Usage::

    from backend.rate_limiter import RateLimiter, SSEConnectionTracker

    _limiter = RateLimiter(rate=1.0, burst=60)          # 60 req/min
    _sse_tracker = SSEConnectionTracker(max_per_ip=5)

    # In a request handler:
    allowed, remaining = _limiter.check("192.168.1.1")
    if not allowed:
        retry_after = _limiter.retry_after("192.168.1.1")
        # return 429 ...
"""

from __future__ import annotations

import threading
import time
from typing import Dict


class TokenBucket:
    """Standard token bucket supporting configurable rate and burst.

    Tokens refill continuously at *rate* tokens per second up to a maximum
    of *burst* tokens.  A new bucket starts full.
    """

    def __init__(self, rate: float, burst: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self._rate = rate
        self._burst = burst
        self._tokens: float = burst
        self._last_refill: float = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers (caller must hold any external lock)
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def consume(self) -> bool:
        """Attempt to consume one token.  Returns True if allowed."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def tokens_remaining(self) -> float:
        """Return the current token count (after refill)."""
        self._refill()
        return self._tokens

    def retry_after(self) -> float:
        """Seconds until at least one token is available."""
        self._refill()
        if self._tokens >= 1.0:
            return 0.0
        deficit = 1.0 - self._tokens
        return deficit / self._rate

    @property
    def last_seen(self) -> float:
        """Monotonic timestamp of the last refill (proxy for last access)."""
        return self._last_refill


class RateLimiter:
    """Thread-safe per-IP rate limiter backed by TokenBucket instances.

    Parameters
    ----------
    rate:
        Tokens refilled per second (e.g. 1.0 for 60 req/min).
    burst:
        Maximum token capacity (controls burst allowance).
    cleanup_interval:
        How often (seconds) to purge buckets idle for more than
        *stale_after* seconds.
    stale_after:
        Seconds of inactivity before a bucket is considered stale.
    """

    def __init__(
        self,
        rate: float = 1.0,
        burst: float = 60.0,
        cleanup_interval: float = 60.0,
        stale_after: float = 600.0,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._stale_after = stale_after
        self._cleanup_interval = cleanup_interval
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, ip: str) -> TokenBucket:
        """Return existing bucket or create a new one (lock must be held)."""
        if ip not in self._buckets:
            self._buckets[ip] = TokenBucket(self._rate, self._burst)
        return self._buckets[ip]

    def _maybe_cleanup(self) -> None:
        """Purge stale buckets if cleanup interval has elapsed (lock held)."""
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        cutoff = now - self._stale_after
        stale = [ip for ip, b in self._buckets.items() if b.last_seen < cutoff]
        for ip in stale:
            del self._buckets[ip]
        self._last_cleanup = now

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check(self, ip: str) -> tuple[bool, float]:
        """Check and consume one request for *ip*.

        Returns ``(allowed, remaining)`` where *remaining* is the token count
        after this request (0.0 when denied).
        """
        with self._lock:
            self._maybe_cleanup()
            bucket = self._get_or_create(ip)
            allowed = bucket.consume()
            remaining = bucket.tokens_remaining()
        return allowed, remaining

    def retry_after(self, ip: str) -> int:
        """Seconds until the next request from *ip* would be allowed."""
        with self._lock:
            if ip not in self._buckets:
                return 0
            secs = self._buckets[ip].retry_after()
        return max(1, int(secs) + 1)

    def bucket_count(self) -> int:
        """Return the number of active IP buckets (for tests/diagnostics)."""
        with self._lock:
            return len(self._buckets)

    def force_cleanup(self) -> int:
        """Force a cleanup pass regardless of interval. Returns purge count."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self._stale_after
            stale = [ip for ip, b in self._buckets.items() if b.last_seen < cutoff]
            for ip in stale:
                del self._buckets[ip]
            self._last_cleanup = now
            return len(stale)


class SSEConnectionTracker:
    """Track the number of active SSE connections per IP address.

    Enforces a maximum number of concurrent SSE connections per client to
    prevent resource exhaustion on long-lived streaming endpoints.
    """

    def __init__(self, max_per_ip: int = 5) -> None:
        if max_per_ip <= 0:
            raise ValueError("max_per_ip must be positive")
        self._max = max_per_ip
        self._counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, ip: str) -> bool:
        """Attempt to register a new SSE connection for *ip*.

        Returns True if the connection is allowed, False if the per-IP limit
        has been reached.  Callers MUST call :meth:`release` when the
        connection closes (use try/finally).
        """
        with self._lock:
            current = self._counts.get(ip, 0)
            if current >= self._max:
                return False
            self._counts[ip] = current + 1
            return True

    def release(self, ip: str) -> None:
        """Decrement the connection count for *ip* when a connection closes."""
        with self._lock:
            count = self._counts.get(ip, 0)
            if count <= 1:
                self._counts.pop(ip, None)
            else:
                self._counts[ip] = count - 1

    def connections(self, ip: str) -> int:
        """Return the current open connection count for *ip*."""
        with self._lock:
            return self._counts.get(ip, 0)
