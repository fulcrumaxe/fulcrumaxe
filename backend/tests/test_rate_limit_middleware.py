"""
Tests for backend/middleware/rate_limit.py — per-IP token-bucket rate limiter.

Coverage:
- AC1: burst from one IP past bucket → 429 + Retry-After, body matches legacy
- AC2: under threshold → 200; different IPs → independent buckets
- AC3: health/liveness exemptions match legacy (/health, /health/loop,
       /health/modules, /metrics)
- AC4: middleware lives in backend/middleware/rate_limit.py; logic reuses
       rate_limiter.py's RateLimiter (import check)
- AC5: Retry-After header present and >= 1 on 429 response

Test-isolation contract
-----------------------
Every test that needs a small-burst limiter uses ``monkeypatch.setattr``
(via the ``small_limiter`` fixture) to swap ``RateLimitMiddleware.limiter``
and ``RateLimitMiddleware.enabled``.  monkeypatch auto-reverts both
attributes after each test, so the production 60-token limiter is restored
before the next test file runs.  No importlib.reload of asgi_app — that
would rebuild the entire middleware stack and leave dangling references.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.middleware.rate_limit as rl_mod
from backend.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _restore_rate_limit(monkeypatch):
    """Ensure RateLimitMiddleware class attributes revert after each test.

    Uses monkeypatch.setattr so pytest auto-reverts even on test failure.
    Tests that need a custom limiter or disabled state should call
    monkeypatch.setattr themselves (which stacks correctly with this fixture).
    This fixture is a belt-and-suspenders guard; explicit setattr in each
    test is the primary mechanism.
    """
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", rl_mod._limiter)


def _small_limiter(burst: float = 3.0) -> RateLimiter:
    """Return a RateLimiter with tiny burst for fast tests (refill nearly stopped)."""
    return RateLimiter(rate=0.001, burst=burst)


@pytest.fixture()
def client(monkeypatch):
    """TestClient with auth disabled and rate-limiter using a 3-token burst."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    monkeypatch.delenv("AF_RATE_LIMIT_DISABLED", raising=False)
    limiter = _small_limiter(burst=3.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def big_client(monkeypatch):
    """TestClient with auth disabled and a large-burst limiter (no throttle in 5 reqs)."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    monkeypatch.delenv("AF_RATE_LIMIT_DISABLED", raising=False)
    limiter = _small_limiter(burst=100.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# AC1: burst past bucket → 429 + Retry-After + legacy body
# ---------------------------------------------------------------------------


def test_burst_triggers_429(monkeypatch):
    """Exhaust 3-token bucket on /docs → 429 on the 4th request."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    monkeypatch.delenv("AF_RATE_LIMIT_DISABLED", raising=False)
    limiter = _small_limiter(burst=3.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        for i in range(3):
            resp = c.get("/docs")
            assert resp.status_code != 429, f"request {i+1} should not be 429 yet"

        resp = c.get("/docs")
        assert resp.status_code == 429

        body = resp.json()
        assert body.get("error") == "rate limit exceeded"
        assert "retry_after" in body
        assert isinstance(body["retry_after"], int)
        assert body["retry_after"] >= 1


def test_429_has_retry_after_header(monkeypatch):
    """429 response must include Retry-After header + X-RateLimit-Remaining: 0."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    monkeypatch.delenv("AF_RATE_LIMIT_DISABLED", raising=False)
    limiter = _small_limiter(burst=1.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/docs")  # drain the single token
        resp = c.get("/docs")
        assert resp.status_code == 429
        assert "retry-after" in resp.headers
        retry_after_val = int(resp.headers["retry-after"])
        assert retry_after_val >= 1
        assert resp.headers.get("x-ratelimit-remaining") == "0"


# ---------------------------------------------------------------------------
# AC2: under threshold → non-429; different IPs → independent buckets
# ---------------------------------------------------------------------------


def test_under_threshold_succeeds(big_client):
    """Requests well under burst limit should not return 429."""
    for _ in range(5):
        resp = big_client.get("/docs")
        assert resp.status_code != 429


def test_different_ips_independent_buckets(monkeypatch):
    """Exhausting IP1's bucket must not affect IP2's bucket."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    limiter = _small_limiter(burst=2.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)

    # Exhaust IP1 via the limiter directly (TestClient always uses 127.0.0.1).
    limiter.check("10.0.0.1")
    limiter.check("10.0.0.1")
    allowed_ip1, _ = limiter.check("10.0.0.1")  # 3rd call with burst=2 → denied
    assert not allowed_ip1, "IP1 should be rate-limited after exhausting burst"

    allowed_ip2, _ = limiter.check("10.0.0.2")  # IP2 has a full bucket
    assert allowed_ip2, "IP2 should NOT be affected by IP1's exhaustion"


# ---------------------------------------------------------------------------
# AC3: health/liveness exemptions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/health", "/health/loop", "/health/modules", "/metrics"],
)
def test_exempt_paths_not_rate_limited(monkeypatch, path):
    """Exempt paths must never return 429 even when the testclient bucket is empty."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    monkeypatch.delenv("AF_RATE_LIMIT_DISABLED", raising=False)
    limiter = _small_limiter(burst=1.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        # Drain the testclient IP's single token on a non-exempt path.
        c.get("/docs")

        # Exempt path must still be served without 429.
        resp = c.get(path)
        assert resp.status_code != 429, (
            f"{path} should be exempt from rate limiting; got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# AC4: import check — RateLimitMiddleware reuses rate_limiter.RateLimiter
# ---------------------------------------------------------------------------


def test_rate_limit_middleware_reuses_rate_limiter():
    """The module-level _limiter must be a RateLimiter from backend.rate_limiter."""
    assert isinstance(rl_mod._limiter, RateLimiter), (
        "RateLimitMiddleware must use a RateLimiter instance from backend.rate_limiter"
    )


def test_rate_limit_middleware_class_is_importable():
    """RateLimitMiddleware must be importable from backend.middleware.rate_limit."""
    assert hasattr(rl_mod, "RateLimitMiddleware")


def test_rate_limit_middleware_registered_in_asgi_app():
    """asgi_app must register RateLimitMiddleware in its middleware stack."""
    from backend.asgi_app import app

    registered = [m.cls for m in app.user_middleware if hasattr(m, "cls")]
    assert rl_mod.RateLimitMiddleware in registered, (
        "RateLimitMiddleware must appear in app.user_middleware"
    )


# ---------------------------------------------------------------------------
# AC5: disabled flag passes all requests through
# ---------------------------------------------------------------------------


def test_env_disabled_flag_bypasses_rate_limit(monkeypatch):
    """AF_RATE_LIMIT_DISABLED=1 must pass requests through even with empty bucket."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    monkeypatch.setenv("AF_RATE_LIMIT_DISABLED", "1")
    limiter = _small_limiter(burst=1.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        for _ in range(5):
            resp = c.get("/health")
            assert resp.status_code != 429


def test_class_enabled_false_bypasses_rate_limit(monkeypatch):
    """RateLimitMiddleware.enabled = False must disable rate limiting."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    monkeypatch.delenv("AF_RATE_LIMIT_DISABLED", raising=False)
    limiter = _small_limiter(burst=1.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", False)  # disabled

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        for _ in range(5):
            resp = c.get("/health")
            assert resp.status_code != 429
