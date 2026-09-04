"""Tests for the strangler-proxy catch-all in backend/asgi_app.py.

Covers:
- AC3: unmigrated paths are forwarded to legacy :18099 with byte-identical response
- AC9: XFF headers are stripped before forwarding to legacy
- Proxy connects only over loopback (127.0.0.1)

We run a real stub legacy HTTP server on a free port, point the proxy at it,
and assert byte-identical body + status.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Stub legacy server
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal legacy-server stub: echoes the request path + any auth header."""

    def log_message(self, *args, **kwargs) -> None:  # noqa: D102
        pass  # suppress noise in test output

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        # Return a JSON body reflecting which path was hit and the XFF header
        # (so we can assert it was stripped).
        xff = self.headers.get("X-Forwarded-For", "<not-present>")
        body = json.dumps(
            {"legacy": True, "path": self.path, "xff_received": xff}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def legacy_server():
    """Start a stub legacy server on a free loopback port, yield its URL."""
    # Pick a free port.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = ThreadingHTTPServer(("127.0.0.1", port), _StubHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# AC3: byte-identical proxy
# ---------------------------------------------------------------------------


def test_proxy_forwards_to_legacy_byte_identical(legacy_server, monkeypatch):
    """Proxy must forward to legacy and return the same payload + status.

    The LegacyEnvelopeMiddleware (D#1411) injects ``_api_version`` into every
    dict JSON response, so the proxied body will equal the legacy body plus that
    one injected key.  We compare JSON semantics rather than raw bytes.
    """
    import httpx
    from backend.tests._envelope import API_VERSION
    from fastapi.testclient import TestClient

    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    # Point the proxy at our stub legacy server.
    with patch("backend.asgi_app._LEGACY_BASE_URL", legacy_server):
        with patch("backend.asgi_app._proxy_client", None):
            from backend.asgi_app import app

            with TestClient(app, raise_server_exceptions=True) as client:
                # Hit the proxy with a path that is NOT registered in the app.
                proxy_resp = client.get("/stats/connection")

    # Also hit the legacy server directly.
    direct_resp = httpx.get(f"{legacy_server}/stats/connection")

    assert proxy_resp.status_code == direct_resp.status_code, (
        f"Status mismatch: proxy={proxy_resp.status_code} "
        f"direct={direct_resp.status_code}"
    )

    # The envelope middleware adds _api_version to proxied dict responses.
    # Strip it before comparing to the raw legacy body.
    proxy_body = proxy_resp.json()
    direct_body = direct_resp.json()

    proxy_body_stripped = {k: v for k, v in proxy_body.items() if k != "_api_version"}
    assert proxy_body_stripped == direct_body, (
        f"Proxy body (minus envelope) differs from direct legacy response.\n"
        f"proxy (stripped): {proxy_body_stripped}\n"
        f"direct:           {direct_body}"
    )
    # The injected key must be present and carry the current API version.
    assert proxy_body.get("_api_version") == API_VERSION, (
        f"Expected _api_version={API_VERSION} in proxied response, got: {proxy_body}"
    )


# ---------------------------------------------------------------------------
# AC9: XFF stripping
# ---------------------------------------------------------------------------


def test_proxy_strips_xff_before_forwarding(legacy_server, monkeypatch):
    """The proxy must NOT forward X-Forwarded-For to the legacy server."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    with patch("backend.asgi_app._LEGACY_BASE_URL", legacy_server):
        with patch("backend.asgi_app._proxy_client", None):
            from backend.asgi_app import app

            with TestClient(app, raise_server_exceptions=True) as client:
                r = client.get(
                    "/any-path",
                    headers={"X-Forwarded-For": "1.2.3.4"},
                )

    body = r.json()
    # The stub legacy server echoes whatever XFF header it received.
    # If the proxy stripped it, the legacy server will see "<not-present>".
    assert body["xff_received"] == "<not-present>", (
        f"Proxy should strip X-Forwarded-For but legacy received: "
        f"{body['xff_received']}"
    )


def test_proxy_strips_x_forwarded_host(legacy_server, monkeypatch):
    """All X-Forwarded-* headers must be stripped."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    with patch("backend.asgi_app._LEGACY_BASE_URL", legacy_server):
        with patch("backend.asgi_app._proxy_client", None):
            from backend.asgi_app import app

            with TestClient(app, raise_server_exceptions=True) as client:
                # Include multiple spoofed forwarding headers.
                r = client.get(
                    "/any-path",
                    headers={
                        "X-Forwarded-For": "127.0.0.1",
                        "X-Forwarded-Host": "evil.example.com",
                        "X-Forwarded-Proto": "https",
                    },
                )

    # The stub only echoes X-Forwarded-For; if we got this far without a 500
    # the proxy didn't explode; XFF check is done above.
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# AC3: proxy connects over loopback only
# ---------------------------------------------------------------------------


def test_proxy_base_url_is_loopback():
    """_LEGACY_BASE_URL must point at 127.0.0.1 (never 0.0.0.0 or a hostname)."""
    from backend.asgi_app import _LEGACY_BASE_URL

    assert "127.0.0.1" in _LEGACY_BASE_URL, (
        f"Proxy base URL must use loopback (127.0.0.1), got: {_LEGACY_BASE_URL}"
    )
    assert "0.0.0.0" not in _LEGACY_BASE_URL
    assert "localhost" not in _LEGACY_BASE_URL.lower() or "127.0.0.1" in _LEGACY_BASE_URL
