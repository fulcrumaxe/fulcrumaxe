"""Tests for backend/rate_limiter.py — token bucket rate limiting."""

from __future__ import annotations

import time
import unittest

from backend.rate_limiter import RateLimiter, SSEConnectionTracker, TokenBucket


class TestTokenBucket(unittest.TestCase):
    """Unit tests for the TokenBucket primitive."""

    def test_new_bucket_starts_full(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=10.0)
        self.assertAlmostEqual(bucket.tokens_remaining(), 10.0, places=1)

    def test_consume_decrements_tokens(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=10.0)
        for _ in range(5):
            self.assertTrue(bucket.consume())
        self.assertAlmostEqual(bucket.tokens_remaining(), 5.0, places=0)

    def test_consume_returns_false_when_empty(self) -> None:
        bucket = TokenBucket(rate=0.01, burst=3.0)
        for _ in range(3):
            self.assertTrue(bucket.consume())
        self.assertFalse(bucket.consume())

    def test_burst_capacity_respected(self) -> None:
        """Tokens must never exceed burst even after a long wait."""
        bucket = TokenBucket(rate=10.0, burst=5.0)
        time.sleep(0.5)  # would add 5 tokens at rate=10
        self.assertLessEqual(bucket.tokens_remaining(), 5.0)

    def test_refill_over_time(self) -> None:
        """Tokens refill at the configured rate."""
        bucket = TokenBucket(rate=10.0, burst=10.0)
        # Drain completely
        for _ in range(10):
            bucket.consume()
        self.assertFalse(bucket.consume())
        time.sleep(0.2)  # should add ~2 tokens at rate=10
        self.assertGreater(bucket.tokens_remaining(), 1.0)

    def test_retry_after_zero_when_tokens_available(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=5.0)
        self.assertEqual(bucket.retry_after(), 0.0)

    def test_retry_after_positive_when_empty(self) -> None:
        bucket = TokenBucket(rate=1.0, burst=1.0)
        bucket.consume()
        ra = bucket.retry_after()
        self.assertGreater(ra, 0.0)

    def test_invalid_rate_raises(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucket(rate=0, burst=10)

    def test_invalid_burst_raises(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucket(rate=1.0, burst=0)


class TestRateLimiter(unittest.TestCase):
    """Integration tests for the RateLimiter."""

    def _limiter(self, rate: float = 1.0, burst: float = 5.0) -> RateLimiter:
        # Use very long cleanup interval so tests don't interfere.
        return RateLimiter(rate=rate, burst=burst, cleanup_interval=9999.0, stale_after=9999.0)

    def test_allows_requests_within_burst(self) -> None:
        rl = self._limiter(burst=5.0)
        for _ in range(5):
            allowed, _ = rl.check("10.0.0.1")
            self.assertTrue(allowed)

    def test_rejects_request_beyond_burst(self) -> None:
        rl = self._limiter(burst=3.0, rate=0.01)
        for _ in range(3):
            rl.check("10.0.0.1")
        allowed, _ = rl.check("10.0.0.1")
        self.assertFalse(allowed)

    def test_per_ip_isolation(self) -> None:
        """Different IPs have independent buckets."""
        rl = self._limiter(burst=2.0, rate=0.01)
        for _ in range(2):
            rl.check("1.1.1.1")
        allowed_1, _ = rl.check("1.1.1.1")
        allowed_2, _ = rl.check("2.2.2.2")
        self.assertFalse(allowed_1)
        self.assertTrue(allowed_2)

    def test_remaining_decrements(self) -> None:
        rl = self._limiter(burst=10.0)
        _, r1 = rl.check("10.0.0.1")
        _, r2 = rl.check("10.0.0.1")
        self.assertLess(r2, r1)

    def test_429_response_format_via_retry_after(self) -> None:
        """retry_after returns a positive integer when denied."""
        rl = self._limiter(burst=1.0, rate=0.01)
        rl.check("10.0.0.1")
        ra = rl.retry_after("10.0.0.1")
        self.assertIsInstance(ra, int)
        self.assertGreater(ra, 0)

    def test_stale_cleanup_removes_old_buckets(self) -> None:
        """force_cleanup removes buckets idle longer than stale_after."""
        rl = RateLimiter(rate=1.0, burst=5.0, cleanup_interval=0.0, stale_after=0.01)
        rl.check("10.0.0.1")
        self.assertEqual(rl.bucket_count(), 1)
        time.sleep(0.05)  # wait past stale_after
        rl.force_cleanup()
        self.assertEqual(rl.bucket_count(), 0)

    def test_active_bucket_not_cleaned(self) -> None:
        """A bucket accessed recently is not pruned."""
        rl = RateLimiter(rate=1.0, burst=5.0, cleanup_interval=0.0, stale_after=60.0)
        rl.check("10.0.0.1")
        purged = rl.force_cleanup()
        self.assertEqual(purged, 0)
        self.assertEqual(rl.bucket_count(), 1)

    def test_retry_after_unknown_ip_returns_zero(self) -> None:
        rl = self._limiter()
        self.assertEqual(rl.retry_after("unknown.ip"), 0)


class TestSSEConnectionTracker(unittest.TestCase):
    """Tests for per-IP SSE connection limiting."""

    def test_allows_connections_within_limit(self) -> None:
        tracker = SSEConnectionTracker(max_per_ip=3)
        for _ in range(3):
            self.assertTrue(tracker.acquire("10.0.0.1"))

    def test_rejects_connection_at_limit(self) -> None:
        tracker = SSEConnectionTracker(max_per_ip=2)
        tracker.acquire("10.0.0.1")
        tracker.acquire("10.0.0.1")
        self.assertFalse(tracker.acquire("10.0.0.1"))

    def test_release_allows_new_connection(self) -> None:
        tracker = SSEConnectionTracker(max_per_ip=1)
        self.assertTrue(tracker.acquire("10.0.0.1"))
        self.assertFalse(tracker.acquire("10.0.0.1"))
        tracker.release("10.0.0.1")
        self.assertTrue(tracker.acquire("10.0.0.1"))

    def test_sse_per_ip_isolation(self) -> None:
        """SSE limit is enforced per-IP, not globally."""
        tracker = SSEConnectionTracker(max_per_ip=1)
        self.assertTrue(tracker.acquire("1.1.1.1"))
        self.assertTrue(tracker.acquire("2.2.2.2"))

    def test_connections_counter(self) -> None:
        tracker = SSEConnectionTracker(max_per_ip=5)
        tracker.acquire("10.0.0.1")
        tracker.acquire("10.0.0.1")
        self.assertEqual(tracker.connections("10.0.0.1"), 2)

    def test_release_cleans_zero_count(self) -> None:
        tracker = SSEConnectionTracker(max_per_ip=5)
        tracker.acquire("10.0.0.1")
        tracker.release("10.0.0.1")
        self.assertEqual(tracker.connections("10.0.0.1"), 0)

    def test_invalid_max_raises(self) -> None:
        with self.assertRaises(ValueError):
            SSEConnectionTracker(max_per_ip=0)


if __name__ == "__main__":
    unittest.main()
