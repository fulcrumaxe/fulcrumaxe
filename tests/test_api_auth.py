"""
Tests for API key authentication in backend/api.py.

Uses http.server's test client pattern -- spins up a real ThreadingHTTPServer
on a random port for each test, which avoids mocking the HTTP stack.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.api import _Handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_server(auth_key: str | None) -> tuple[ThreadingHTTPServer, int]:
    """Spin up a test server on a random OS-assigned port."""
    _Handler.auth_key = auth_key
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _get(port: int, path: str, headers: dict | None = None) -> tuple[int, dict]:
    """Perform a GET request and return (status_code, response_json)."""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ---------------------------------------------------------------------------
# No-auth mode (AF_API_AUTH_KEY unset)
# ---------------------------------------------------------------------------


class TestNoAuth:
    def setup_method(self):
        self.server, self.port = _start_server(auth_key=None)

    def teardown_method(self):
        self.server.shutdown()

    def test_health_returns_200(self):
        status, body = _get(self.port, "/health")
        assert status == 200
        assert body["ok"] is True

    def test_budget_status_accessible_without_header(self):
        # No auth header -- should still work when auth is disabled.
        status, body = _get(self.port, "/budget/status")
        # 200 or 500 (if BudgetTracker fails due to missing files) but NOT 401/403.
        assert status not in (401, 403)

    def test_agents_accessible_without_header(self):
        status, body = _get(self.port, "/agents")
        assert status not in (401, 403)


# ---------------------------------------------------------------------------
# Auth enabled (AF_API_AUTH_KEY=secret123)
# ---------------------------------------------------------------------------


class TestAuthEnabled:
    SECRET = "secret123"

    def setup_method(self):
        self.server, self.port = _start_server(auth_key=self.SECRET)

    def teardown_method(self):
        self.server.shutdown()

    # /health is always bypassed -------------------------------------------

    def test_health_bypass_no_header(self):
        status, body = _get(self.port, "/health")
        assert status == 200
        assert body["ok"] is True

    def test_health_bypass_wrong_token(self):
        status, body = _get(self.port, "/health", {"Authorization": "Bearer wrong"})
        assert status == 200

    # Missing header returns 401 -------------------------------------------

    def test_missing_header_returns_401(self):
        status, body = _get(self.port, "/budget/status")
        assert status == 401
        assert body["error"] == "unauthorized"

    def test_malformed_header_not_bearer_returns_401(self):
        status, body = _get(self.port, "/budget/status", {"Authorization": "Basic abc"})
        assert status == 401
        assert body["error"] == "unauthorized"

    def test_bearer_with_no_token_returns_401(self):
        # "Bearer" with no trailing token -- split gives only one part.
        status, body = _get(self.port, "/budget/status", {"Authorization": "Bearer"})
        assert status == 401

    # Wrong token returns 403 -----------------------------------------------

    def test_wrong_token_returns_403(self):
        status, body = _get(self.port, "/budget/status", {"Authorization": "Bearer wrong"})
        assert status == 403
        assert body["error"] == "forbidden"

    def test_empty_token_returns_403(self):
        status, body = _get(self.port, "/budget/status", {"Authorization": "Bearer "})
        assert status == 403

    # Correct token returns non-4xx ----------------------------------------

    def test_valid_token_passes(self):
        status, body = _get(
            self.port, "/agents", {"Authorization": f"Bearer {self.SECRET}"}
        )
        # 200 means auth passed (content may vary based on backend state).
        assert status == 200

    def test_valid_token_on_budget_endpoint(self):
        status, _body = _get(
            self.port, "/budget/status", {"Authorization": f"Bearer {self.SECRET}"}
        )
        assert status not in (401, 403)


# ---------------------------------------------------------------------------
# --require-auth CLI flag
# ---------------------------------------------------------------------------


def test_require_auth_exits_when_no_env_var(monkeypatch):
    """main() with --require-auth must exit(1) if AF_API_AUTH_KEY is not set."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.api import main
    with pytest.raises(SystemExit) as exc_info:
        main(["--require-auth"])
    assert exc_info.value.code == 1
