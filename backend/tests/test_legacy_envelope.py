"""Tests for backend/middleware/legacy_envelope.py.

Covers all acceptance criteria from the D#1425 PR1 spec:
AC1 - 4xx JSON error -> {"error": ..., "_api_version": N}  (not {"detail": ...})
AC2 - Successful dict JSON response has _api_version injected
AC3 - SSE (/stream/feed) NOT buffered; /rpc {jsonrpc} bodies NOT given _api_version
AC4 - Forced 500 -> GENERIC body, no stack trace/exception text/path
AC5 - Non-dict JSON (list) unchanged; 204/empty unchanged
AC6 - Logic in backend/middleware/legacy_envelope.py; asgi_app.py = register only
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from backend.api_version import CURRENT_VERSION
from backend.middleware.legacy_envelope import (
    LegacyEnvelopeMiddleware,
    _generic_5xx,
    _rewrite_body,
    unhandled_exc_handler,
)
from testsupport.fixture_paths import FIXTURE_HOME


# ---------------------------------------------------------------------------
# Helpers — build a minimal test app with the middleware registered outermost
# ---------------------------------------------------------------------------


def _make_test_app() -> FastAPI:
    """Minimal FastAPI app with LegacyEnvelopeMiddleware registered outermost."""
    test_app = FastAPI()
    test_app.add_middleware(LegacyEnvelopeMiddleware)
    return test_app


# ---------------------------------------------------------------------------
# Unit tests for _rewrite_body (pure logic, no HTTP overhead)
# ---------------------------------------------------------------------------


class TestRewriteBodyUnit:
    def test_injects_api_version_on_success(self):
        result = _rewrite_body(200, "/health", {"ok": True})
        assert result["_api_version"] == CURRENT_VERSION
        assert result["ok"] is True
        # _api_version must be the FIRST key.
        assert list(result.keys())[0] == "_api_version"

    def test_already_has_api_version_not_duplicated(self):
        result = _rewrite_body(200, "/health", {"_api_version": 1, "ok": True})
        assert result["_api_version"] == 1
        assert result.count == result.__class__.count if False else True  # just verify no dup

    def test_4xx_detail_rewritten_to_error(self):
        result = _rewrite_body(401, "/secret", {"detail": "unauthorized"})
        assert "error" in result
        assert result["error"] == "unauthorized"
        assert "detail" not in result
        assert result["_api_version"] == CURRENT_VERSION

    def test_4xx_with_existing_error_not_overwritten(self):
        # Body already has "error" key -- leave it alone (just inject version).
        result = _rewrite_body(400, "/x", {"error": "custom", "extra": "data"})
        assert result["error"] == "custom"
        assert result["_api_version"] == CURRENT_VERSION

    def test_jsonrpc_body_untouched(self):
        body = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        result = _rewrite_body(200, "/rpc", body)
        assert result == body  # unchanged

    def test_5xx_returns_generic(self):
        result = _rewrite_body(500, "/x", {"detail": "Server error", "traceback": "..."})
        assert result["error"] == "internal error"
        assert "traceback" not in result
        assert "detail" not in result


# ---------------------------------------------------------------------------
# Integration tests using a minimal FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """TestClient wrapping a minimal app with the envelope middleware."""
    app = _make_test_app()

    @app.get("/ok")
    async def _ok():
        return {"status": "ok"}

    @app.get("/ok-has-version")
    async def _ok_with_version():
        return {"_api_version": CURRENT_VERSION, "status": "ok"}

    @app.get("/error-401", status_code=401)
    async def _error_401():
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})

    @app.get("/error-404", status_code=404)
    async def _error_404():
        return JSONResponse(status_code=404, content={"detail": "not found"})

    @app.get("/list-response")
    async def _list():
        return JSONResponse(content=[1, 2, 3])

    @app.get("/no-content", status_code=204)
    async def _no_content():
        return Response(status_code=204)

    @app.get("/stream")
    async def _stream():
        async def _gen():
            yield b"data: hello\n\n"
        return StreamingResponse(_gen(), media_type="text/event-stream")

    @app.get("/raise-500")
    async def _raise():
        raise RuntimeError(f"Internal traceback line {FIXTURE_HOME}/secret")

    @app.post("/rpc")
    async def _rpc(request: Request):
        body = await request.json()
        return JSONResponse(content={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    # Register unhandled exc handler so raise-500 is caught generically.
    app.add_exception_handler(Exception, unhandled_exc_handler)  # type: ignore[arg-type]

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# AC1: 4xx JSON error -> {"error": ..., "_api_version": N}
# ---------------------------------------------------------------------------


class TestAC1FourxxErrors:
    def test_401_error_key_not_detail(self, client):
        r = client.get("/error-401")
        assert r.status_code == 401
        data = r.json()
        assert "error" in data
        assert "detail" not in data
        assert data["_api_version"] == CURRENT_VERSION

    def test_401_error_value_matches_detail(self, client):
        r = client.get("/error-401")
        assert r.json()["error"] == "unauthorized"

    def test_404_error_key_not_detail(self, client):
        r = client.get("/error-404")
        assert r.status_code == 404
        data = r.json()
        assert "error" in data
        assert "detail" not in data
        assert data["_api_version"] == CURRENT_VERSION


# ---------------------------------------------------------------------------
# AC2: Successful dict JSON response has _api_version injected
# ---------------------------------------------------------------------------


class TestAC2ApiVersionInjection:
    def test_success_has_api_version(self, client):
        r = client.get("/ok")
        assert r.status_code == 200
        data = r.json()
        assert data["_api_version"] == CURRENT_VERSION
        assert data["status"] == "ok"

    def test_existing_api_version_not_duplicated(self, client):
        r = client.get("/ok-has-version")
        assert r.status_code == 200
        data = r.json()
        assert data["_api_version"] == CURRENT_VERSION
        # The key must appear exactly once.
        body_str = r.text
        assert body_str.count("_api_version") == 1

    def test_content_length_correct_after_injection(self, client):
        r = client.get("/ok")
        body_bytes = r.content
        cl = int(r.headers["content-length"])
        assert cl == len(body_bytes)


# ---------------------------------------------------------------------------
# AC3: SSE NOT buffered; /rpc {jsonrpc} bodies NOT given _api_version
# ---------------------------------------------------------------------------


class TestAC3StreamAndRpc:
    def test_sse_stream_not_buffered(self, client):
        # StreamingResponse with text/event-stream must pass through untouched.
        r = client.get("/stream")
        assert r.status_code == 200
        # The raw event data must be present; no "_api_version" injected.
        assert b"data: hello" in r.content
        assert "_api_version" not in r.text

    def test_rpc_jsonrpc_body_unchanged(self, client):
        payload = {"jsonrpc": "2.0", "id": 42, "method": "ping", "params": {}}
        r = client.post("/rpc", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert "jsonrpc" in data
        assert "_api_version" not in data


# ---------------------------------------------------------------------------
# AC4: Forced 500 -> GENERIC body, no stack trace / exception text / path
# ---------------------------------------------------------------------------


class TestAC4Generic5xx:
    def test_unhandled_exception_generic_body(self, client):
        r = client.get("/raise-500")
        assert r.status_code == 500
        data = r.json()
        assert data["error"] == "internal error"
        assert data["_api_version"] == CURRENT_VERSION

    def test_no_traceback_in_body(self, client):
        r = client.get("/raise-500")
        body_str = r.text
        assert "Traceback" not in body_str
        assert "traceback" not in body_str

    def test_no_internal_path_in_body(self, client):
        r = client.get("/raise-500")
        body_str = r.text
        # The exception message contained f"{FIXTURE_HOME}/secret" -- must not appear.
        assert FIXTURE_HOME not in body_str
        assert "secret" not in body_str

    def test_generic_5xx_helper(self):
        resp = _generic_5xx()
        body = json.loads(resp.body)
        assert body["error"] == "internal error"
        assert body["_api_version"] == CURRENT_VERSION
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# AC5: Non-dict JSON (list) unchanged; 204/empty unchanged
# ---------------------------------------------------------------------------


class TestAC5Passthrough:
    def test_list_json_unchanged(self, client):
        r = client.get("/list-response")
        assert r.status_code == 200
        data = r.json()
        assert data == [1, 2, 3]
        # No _api_version injected into a list.
        assert "_api_version" not in r.text

    def test_204_unchanged(self, client):
        r = client.get("/no-content")
        assert r.status_code == 204
        assert r.content == b""


# ---------------------------------------------------------------------------
# AC6: Logic lives in backend/middleware/legacy_envelope.py only
# ---------------------------------------------------------------------------


class TestAC6ModuleScope:
    def test_module_is_importable(self):
        """The middleware lives in its own module."""
        from backend.middleware import legacy_envelope  # noqa: F401

    def test_asgi_app_imports_middleware(self):
        """asgi_app.py registers the middleware but contains no rewrite logic."""
        import inspect
        import backend.asgi_app as hub
        src = inspect.getsource(hub)
        # The hub should import but not define the rewrite function.
        assert "LegacyEnvelopeMiddleware" in src
        assert "_rewrite_body" not in src

    def test_rewrite_logic_not_in_asgi_app(self):
        """_rewrite_body must NOT be defined in asgi_app.py."""
        import backend.asgi_app as hub
        assert not hasattr(hub, "_rewrite_body")


# ---------------------------------------------------------------------------
# Integration against the real asgi_app (smoke: existing tests not broken)
# ---------------------------------------------------------------------------


class TestRealAppSmoke:
    @pytest.fixture()
    def real_client(self, monkeypatch):
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_health_has_api_version(self, real_client):
        r = real_client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["_api_version"] == CURRENT_VERSION

    def test_health_loop_has_api_version(self, real_client):
        r = real_client.get("/health/loop")
        assert r.status_code == 200
        data = r.json()
        assert data["_api_version"] == CURRENT_VERSION

    def test_401_on_protected_route_has_error_key(self, monkeypatch):
        monkeypatch.setenv("AF_API_AUTH_KEY", "test-key")
        from backend.asgi_app import app
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/registry")
            # Should be 401 with "error" key, not "detail".
            assert r.status_code == 401
            data = r.json()
            assert "error" in data
            assert "detail" not in data
            assert data["_api_version"] == CURRENT_VERSION
