"""Tests for backend/deps/auth.py — auth dependency and default-deny middleware.

Covers:
- AC4: public routes pass without token
- AC5: route-introspection — every non-public route in app carries require_auth
- AC6: hmac.compare_digest, 401 for missing token, 403 for wrong token
- AC9: XFF spoofing guard (DefaultDenyMiddleware never trusts X-Forwarded-For)
"""

from __future__ import annotations

import hmac
import inspect
import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.deps.auth import PUBLIC_ROUTES, DefaultDenyMiddleware, require_auth, _is_public


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_with_auth(token: str | None = "secret"):
    """Return a minimal FastAPI app with DefaultDenyMiddleware and one guarded route."""
    env_patch = {}
    if token is not None:
        os.environ["AF_API_AUTH_KEY"] = token
    else:
        os.environ.pop("AF_API_AUTH_KEY", None)

    test_app = FastAPI()
    test_app.add_middleware(DefaultDenyMiddleware)

    @test_app.get("/health")
    async def _health():
        return {"ok": True}

    @test_app.get("/secret", dependencies=[Depends(require_auth)])
    async def _secret():
        return {"data": "sensitive"}

    return test_app


# ---------------------------------------------------------------------------
# AC4: public route passes without token
# ---------------------------------------------------------------------------


def test_public_health_no_token(monkeypatch):
    monkeypatch.setenv("AF_API_AUTH_KEY", "my-key")
    from backend.deps.auth import DefaultDenyMiddleware

    app = FastAPI()
    app.add_middleware(DefaultDenyMiddleware)

    @app.get("/health")
    async def health():
        return {"ok": True}

    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# AC6: 401 for missing token, 403 for wrong token
# ---------------------------------------------------------------------------


def test_missing_token_returns_401(monkeypatch):
    monkeypatch.setenv("AF_API_AUTH_KEY", "my-key")
    from backend.deps.auth import DefaultDenyMiddleware

    app = FastAPI()
    app.add_middleware(DefaultDenyMiddleware)

    @app.get("/private", dependencies=[Depends(require_auth)])
    async def private():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/private")
    assert r.status_code == 401


def test_wrong_token_returns_403(monkeypatch):
    monkeypatch.setenv("AF_API_AUTH_KEY", "correct-key")
    from backend.deps.auth import DefaultDenyMiddleware

    app = FastAPI()
    app.add_middleware(DefaultDenyMiddleware)

    @app.get("/private", dependencies=[Depends(require_auth)])
    async def private():
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/private", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 403


def test_correct_token_returns_200(monkeypatch):
    monkeypatch.setenv("AF_API_AUTH_KEY", "correct-key")
    from backend.deps.auth import DefaultDenyMiddleware

    app = FastAPI()
    app.add_middleware(DefaultDenyMiddleware)

    @app.get("/private", dependencies=[Depends(require_auth)])
    async def private():
        return {"ok": True}

    with TestClient(app) as client:
        r = client.get("/private", headers={"Authorization": "Bearer correct-key"})
    assert r.status_code == 200


def test_auth_disabled_when_no_key(monkeypatch):
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.deps.auth import DefaultDenyMiddleware

    app = FastAPI()
    app.add_middleware(DefaultDenyMiddleware)

    @app.get("/private")
    async def private():
        return {"ok": True}

    with TestClient(app) as client:
        r = client.get("/private")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# AC6: require_auth uses hmac.compare_digest (not ==)
# ---------------------------------------------------------------------------


def test_require_auth_uses_hmac_compare_digest(monkeypatch):
    """require_auth must call hmac.compare_digest, not == or string comparison."""
    source = inspect.getsource(require_auth)
    assert "hmac.compare_digest" in source, (
        "require_auth must use hmac.compare_digest for constant-time comparison"
    )


# ---------------------------------------------------------------------------
# AC5: route-introspection — every non-public route carries require_auth
# ---------------------------------------------------------------------------


def test_route_introspection_all_non_public_routes_have_require_auth():
    """Every app route NOT in the public set must carry require_auth as a dependency.

    This test also acts as a tripwire: adding a new route without auth AND
    without allowlisting it (in PUBLIC_ROUTES or PUBLIC_PREFIXES) will make
    this test fail.

    Uses _is_public() so prefix-matched public routes (e.g. /api/projects/*)
    are recognised without requiring an exact entry in PUBLIC_ROUTES.
    """
    from backend.asgi_app import app
    from fastapi.routing import APIRoute

    violations: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # Skip Mount, WebSocketRoute, etc.
        path = route.path
        if _is_public(path):
            continue
        # Check whether require_auth appears in the route's dependencies.
        has_auth = any(
            dep.dependency is require_auth
            for dep in route.dependencies
        )
        if not has_auth:
            violations.append(path)

    assert not violations, (
        f"These routes are not public and lack require_auth: {violations}"
    )


def test_adding_unauthenticated_non_public_route_fails_introspection():
    """Adding an unauthed non-allowlisted route makes the introspection gate fail."""
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    bad_app = FastAPI()

    # This route has NO auth and is NOT in PUBLIC_ROUTES.
    @bad_app.get("/sneaky-no-auth")
    async def _sneaky():
        return {}

    violations: list[str] = []
    for route in bad_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if _is_public(route.path):
            continue
        has_auth = any(dep.dependency is require_auth for dep in route.dependencies)
        if not has_auth:
            violations.append(route.path)

    assert "/sneaky-no-auth" in violations, (
        "Expected /sneaky-no-auth to appear as a violation"
    )


# ---------------------------------------------------------------------------
# AC9: XFF spoofing guard — middleware never trusts X-Forwarded-For
# ---------------------------------------------------------------------------


def test_xff_spoofed_from_nonloopback_is_still_denied(monkeypatch):
    """Spoofing X-Forwarded-For: 127.0.0.1 must NOT bypass the auth gate.

    A non-loopback caller that adds XFF claiming to be localhost should still
    be rejected with 401 when they lack a valid token.
    """
    monkeypatch.setenv("AF_API_AUTH_KEY", "real-secret")
    from backend.deps.auth import DefaultDenyMiddleware

    app = FastAPI()
    app.add_middleware(DefaultDenyMiddleware)

    @app.get("/loopback-only", dependencies=[Depends(require_auth)])
    async def loopback_only(request: "Request"):  # type: ignore[name-defined]
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get(
            "/loopback-only",
            headers={
                # Spoof loopback — middleware must NOT trust this.
                "X-Forwarded-For": "127.0.0.1",
                # No real Authorization header.
            },
        )
    assert r.status_code == 401, (
        f"Expected 401 but got {r.status_code} — XFF spoofing is not being rejected"
    )
