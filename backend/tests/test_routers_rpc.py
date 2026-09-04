"""
Tests for backend/routers/rpc.py — POST /rpc JSON-RPC dispatch.

Covers:
  - Dispatch parity: read-only methods return correct result envelope
  - Envelope/error codes: unknown method → -32601; handler exception → -32000
    (or exc.rpc_code); invalid JSON → HTTP 400 jsonrpc shape
  - HTTP status: 200 for all RPC-level errors; 400 for invalid JSON
  - Auth: Bearer token → 200; ?token= query param → 200; no token → 401;
          wrong token → 401; no token file → always passes (open)
  - Spawn-guard: HeadlessChrome UA + loop.start → -32000 spawn_blocked_test_origin
                 Allowed-env bypass works; non-spawn methods are unguarded
  - Import safety: importing backend.server does NOT start a server
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Rate-limit bypass — prevents token-bucket exhaustion in combined runs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    monkeypatch.setenv("AF_RATE_LIMIT_DISABLED", "1")


def _make_client(
    *,
    rpc_token: str | None = "test-rpc-token",
    rest_key: str | None = None,
    extra_headers: dict | None = None,
) -> TestClient:
    """Build a TestClient with the RPC token pre-set on the token file path
    and optional REST auth key.

    Pass ``rpc_token=None`` to simulate a missing token file (open access).
    """
    from backend.asgi_app import app

    headers: dict[str, str] = {}
    if rest_key is not None:
        headers["Authorization"] = f"Bearer {rest_key}"
    if extra_headers:
        headers.update(extra_headers)
    return TestClient(app, headers=headers, raise_server_exceptions=False)


@pytest.fixture()
def rpc_token(tmp_path):
    """Patch the RPC token file path to a temp file with a known token."""
    token_file = tmp_path / "dashboard-token"
    token_file.write_text("test-rpc-token", encoding="utf-8")
    with patch("backend.routers.rpc._TOKEN_PATH", token_file):
        yield "test-rpc-token"


@pytest.fixture()
def no_token_file():
    """Patch the RPC token file path to a nonexistent file."""
    with patch("backend.routers.rpc._TOKEN_PATH", Path("/nonexistent/dashboard-token")):
        yield


@pytest.fixture()
def client(rpc_token):
    """TestClient with REST auth disabled, RPC token file patched."""
    os.environ.pop("AF_API_AUTH_KEY", None)
    return _make_client()


@pytest.fixture()
def authed_client(rpc_token):
    """TestClient with the correct Bearer token in all requests."""
    os.environ.pop("AF_API_AUTH_KEY", None)
    return _make_client(extra_headers={"Authorization": f"Bearer {rpc_token}"})


# ---------------------------------------------------------------------------
# Import safety — importing backend.server must not start a server
# ---------------------------------------------------------------------------


def test_import_server_does_not_start(monkeypatch):
    """Importing backend.server must not call HttpAdapter.start() or listen()."""
    # If server starts it would open a socket and/or call sys.exit — neither
    # should happen on a bare import.  We verify by importing and checking
    # that _RPC_METHODS is a non-empty dict (registry populated by decorators).
    import backend.server as srv  # noqa: PLC0415

    assert isinstance(srv._RPC_METHODS, dict)
    assert len(srv._RPC_METHODS) > 0, "Expected at least one registered RPC method"


# ---------------------------------------------------------------------------
# Dispatch parity — read-only methods
# ---------------------------------------------------------------------------


def _mock_loop_list_handler(params: dict) -> dict:
    return {"loops": []}


def _mock_gates_handler(params: dict) -> dict:
    return {"gates": {"loop_start": True}}


class TestDispatchParity:
    """Verify that the FastAPI /rpc route dispatches and returns the correct envelope."""

    def test_loop_list_returns_result_envelope(self, authed_client, monkeypatch):
        """loop.list → {"jsonrpc":"2.0","id":..., "result":{...}}"""
        monkeypatch.setattr(
            "backend.server._RPC_METHODS",
            {"loop.list": _mock_loop_list_handler},
        )
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert "result" in body
        assert body["result"] == {"loops": []}

    def test_gates_snapshot_returns_result_envelope(self, authed_client, monkeypatch):
        """dashboard.gates_snapshot → result envelope with gates dict."""
        monkeypatch.setattr(
            "backend.server._RPC_METHODS",
            {"dashboard.gates_snapshot": _mock_gates_handler},
        )
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": "abc", "method": "dashboard.gates_snapshot", "params": {}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "abc"
        assert "result" in body
        assert body["result"]["gates"]["loop_start"] is True

    def test_null_id_preserved(self, authed_client, monkeypatch):
        """id:null is preserved in the response envelope."""
        monkeypatch.setattr(
            "backend.server._RPC_METHODS",
            {"loop.list": _mock_loop_list_handler},
        )
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": None, "method": "loop.list", "params": {}},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] is None

    def test_missing_params_defaults_to_empty_dict(self, authed_client, monkeypatch):
        """Omitted params defaults to empty dict (handler receives {})."""
        received: list[dict] = []

        def capture(params: dict) -> dict:
            received.append(params)
            return {"ok": True}

        monkeypatch.setattr("backend.server._RPC_METHODS", {"test.method": capture})
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "test.method"},
        )
        assert resp.status_code == 200
        assert received == [{}]


# ---------------------------------------------------------------------------
# Envelope / error codes
# ---------------------------------------------------------------------------


class TestEnvelopeErrors:
    """Verify error envelopes match legacy exactly."""

    def test_unknown_method_returns_32601(self, authed_client, monkeypatch):
        """Unknown method → HTTP 200, error code -32601."""
        monkeypatch.setattr("backend.server._RPC_METHODS", {})
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 7, "method": "no.such.method", "params": {}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32601
        assert "no.such.method" in body["error"]["message"]
        assert body["id"] == 7

    def test_handler_exception_returns_minus_32000(self, authed_client, monkeypatch):
        """Handler that raises plain ValueError → HTTP 200, error code -32000."""

        def bad_handler(params: dict) -> dict:
            raise ValueError("something went wrong")

        monkeypatch.setattr("backend.server._RPC_METHODS", {"test.bad": bad_handler})
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 3, "method": "test.bad", "params": {}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32000
        assert "something went wrong" in body["error"]["message"]

    def test_handler_exception_custom_rpc_code(self, authed_client, monkeypatch):
        """Handler that raises exc with rpc_code attribute → that code is used."""

        class CustomRpcError(Exception):
            rpc_code = -32602

        def handler_with_code(params: dict) -> dict:
            raise CustomRpcError("invalid params")

        monkeypatch.setattr("backend.server._RPC_METHODS", {"test.coded": handler_with_code})
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 9, "method": "test.coded", "params": {}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32602

    def test_invalid_json_returns_400(self, authed_client, monkeypatch):
        """Invalid JSON body → HTTP 400 with jsonrpc error envelope."""
        monkeypatch.setattr("backend.server._RPC_METHODS", {})
        resp = authed_client.post(
            "/rpc",
            content=b"not-json{{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert body.get("jsonrpc") == "2.0"
        assert body["id"] is None

    def test_rpc_level_errors_are_200(self, authed_client, monkeypatch):
        """Unknown method is RPC-level — should be HTTP 200 not 4xx."""
        monkeypatch.setattr("backend.server._RPC_METHODS", {})
        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "missing", "params": {}},
        )
        assert resp.status_code == 200

    def test_non_object_json_body_returns_400(self, authed_client, monkeypatch):
        """A JSON array body (not an object) → HTTP 400."""
        monkeypatch.setattr("backend.server._RPC_METHODS", {})
        resp = authed_client.post(
            "/rpc",
            content=b"[1, 2, 3]",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    """Verify auth gate: Bearer + ?token=; 401 for missing/wrong."""

    def test_no_token_returns_401(self, rpc_token):
        """Request with no auth → 401."""
        os.environ.pop("AF_API_AUTH_KEY", None)
        client = _make_client()  # no Authorization header
        with patch("backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}):
            resp = client.post(
                "/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
            )
        assert resp.status_code == 401

    def test_wrong_bearer_returns_401(self, rpc_token):
        """Wrong Bearer token → 401."""
        os.environ.pop("AF_API_AUTH_KEY", None)
        client = _make_client(extra_headers={"Authorization": "Bearer wrong-token"})
        with patch("backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}):
            resp = client.post(
                "/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
            )
        assert resp.status_code == 401

    def test_correct_bearer_returns_200(self, rpc_token):
        """Correct Bearer token → 200."""
        os.environ.pop("AF_API_AUTH_KEY", None)
        client = _make_client(extra_headers={"Authorization": f"Bearer {rpc_token}"})
        with patch("backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}):
            resp = client.post(
                "/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
            )
        assert resp.status_code == 200

    def test_query_param_token_accepted(self, rpc_token):
        """?token= query param → 200 (parity with legacy SSE/EventSource auth)."""
        os.environ.pop("AF_API_AUTH_KEY", None)
        client = _make_client()  # no Authorization header
        with patch("backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}):
            resp = client.post(
                f"/rpc?token={rpc_token}",
                json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
            )
        assert resp.status_code == 200

    def test_query_param_wrong_token_returns_401(self, rpc_token):
        """?token=wrong → 401."""
        os.environ.pop("AF_API_AUTH_KEY", None)
        client = _make_client()
        with patch("backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}):
            resp = client.post(
                "/rpc?token=bad-token",
                json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
            )
        assert resp.status_code == 401

    def test_no_token_file_rejects_with_401(self, no_token_file):
        """When the token file is absent, ALL requests are rejected with 401 (fail-closed).

        A missing token configuration must never silently open /rpc — consistent
        with the default-deny principle established in P1.
        """
        os.environ.pop("AF_API_AUTH_KEY", None)
        client = _make_client()  # no Authorization header
        with patch("backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}):
            resp = client.post(
                "/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
            )
        # Missing token file → rpc_token is "" → fail-closed → 401
        assert resp.status_code == 401

    def test_no_token_file_rejects_even_with_bearer(self, no_token_file):
        """Even a Bearer token can't get in when no token is configured — fail-closed."""
        os.environ.pop("AF_API_AUTH_KEY", None)
        client = _make_client(extra_headers={"Authorization": "Bearer some-token"})
        with patch("backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}):
            resp = client.post(
                "/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": "loop.list", "params": {}},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Spawn-guard
# ---------------------------------------------------------------------------


class TestSpawnGuard:
    """Verify loop.start is blocked for test origins."""

    def test_headlesschrome_ua_blocked(self, authed_client, monkeypatch):
        """HeadlessChrome UA + loop.start → -32000 spawn_blocked_test_origin."""
        # We patch loop.start to a NOOP so it would succeed if the guard didn't fire.
        def noop_start(params: dict) -> dict:
            return {"loop_id": "fake"}

        monkeypatch.setattr(
            "backend.server._RPC_METHODS", {"loop.start": noop_start}
        )
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)

        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": "loop.start", "params": {}},
            headers={"User-Agent": "Mozilla/5.0 HeadlessChrome/114.0"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32000
        assert body["error"]["message"] == "spawn_blocked_test_origin"

    def test_puppeteer_ua_blocked(self, authed_client, monkeypatch):
        """Puppeteer UA → blocked."""

        def noop_start(params: dict) -> dict:
            return {}

        monkeypatch.setattr("backend.server._RPC_METHODS", {"loop.start": noop_start})
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)

        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 2, "method": "loop.start", "params": {}},
            headers={"User-Agent": "Puppeteer/21.0"},
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["message"] == "spawn_blocked_test_origin"

    def test_playwright_ua_blocked(self, authed_client, monkeypatch):
        """playwright UA → blocked (case-insensitive)."""

        def noop_start(params: dict) -> dict:
            return {}

        monkeypatch.setattr("backend.server._RPC_METHODS", {"loop.start": noop_start})
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)

        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 3, "method": "loop.start", "params": {}},
            headers={"User-Agent": "playwright/1.40"},
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["message"] == "spawn_blocked_test_origin"

    def test_test_origin_blocked(self, authed_client, monkeypatch):
        """localhost:5173 Origin → blocked for loop.start."""

        def noop_start(params: dict) -> dict:
            return {}

        monkeypatch.setattr("backend.server._RPC_METHODS", {"loop.start": noop_start})
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)

        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 4, "method": "loop.start", "params": {}},
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["message"] == "spawn_blocked_test_origin"

    def test_allow_env_bypasses_guard(self, authed_client, monkeypatch):
        """AF_ALLOW_TEST_ORIGIN_SPAWNS=1 lets test-origin spawn through."""

        def noop_start(params: dict) -> dict:
            return {"loop_id": "allowed"}

        monkeypatch.setattr("backend.server._RPC_METHODS", {"loop.start": noop_start})
        monkeypatch.setenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", "1")

        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 5, "method": "loop.start", "params": {}},
            headers={"User-Agent": "HeadlessChrome/114.0"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "result" in body
        assert body["result"]["loop_id"] == "allowed"

    def test_non_spawn_method_unguarded(self, authed_client, monkeypatch):
        """loop.list is NOT in _SPAWN_METHODS — test-UA has no effect."""
        monkeypatch.setattr(
            "backend.server._RPC_METHODS", {"loop.list": _mock_loop_list_handler}
        )
        monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)

        resp = authed_client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": 6, "method": "loop.list", "params": {}},
            headers={"User-Agent": "HeadlessChrome/114.0"},
        )
        assert resp.status_code == 200
        assert "result" in resp.json()


# ---------------------------------------------------------------------------
# Route registration — /rpc appears in the FastAPI app
# ---------------------------------------------------------------------------


def test_rpc_route_registered():
    """POST /rpc must appear in the app's route table."""
    from backend.asgi_app import app

    routes = {r.path for r in app.routes}  # type: ignore[union-attr]
    assert "/rpc" in routes
