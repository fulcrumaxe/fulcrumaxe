"""Tests for the test-origin spawn rejection guard in backend/api.py.

Verifies that _reject_test_origin_spawn() blocks requests from Puppeteer /
HeadlessChrome user-agents and localhost:5173 origins before any state
mutation occurs, regardless of the gates.idea_generation setting.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Unit tests for _reject_test_origin_spawn directly
# ---------------------------------------------------------------------------


class TestRejectTestOriginSpawn(unittest.TestCase):
    """Unit tests for the _reject_test_origin_spawn helper."""

    def _make_handler(self, ua: str = "", origin: str = "") -> MagicMock:
        """Build a mock handler with the given headers and a recording wfile."""
        headers_dict: dict[str, str] = {}
        if ua:
            headers_dict["User-Agent"] = ua
        if origin:
            headers_dict["Origin"] = origin

        class FakeHeaders:
            def get(self, k: str, d: str = "") -> str:
                return headers_dict.get(k, d)

        handler = MagicMock()
        handler.headers = FakeHeaders()
        return handler

    def _call(self, ua: str = "", origin: str = "", allow_env: str = "") -> tuple[bool, int]:
        """Call _reject_test_origin_spawn and return (rejected, status_code)."""
        from backend.api import _reject_test_origin_spawn

        handler = self._make_handler(ua, origin)

        # Capture send_response calls
        sent_status = []
        handler.send_response = lambda code: sent_status.append(code)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()

        env_patch = {"AF_ALLOW_TEST_ORIGIN_SPAWNS": allow_env} if allow_env else {}
        with patch.dict(os.environ, env_patch, clear=False):
            # Remove the key if allow_env is empty so we test without it
            if not allow_env:
                os.environ.pop("AF_ALLOW_TEST_ORIGIN_SPAWNS", None)
            rejected = _reject_test_origin_spawn(handler)

        status = sent_status[0] if sent_status else 0
        return rejected, status

    # -- Puppeteer user-agent variants --

    def test_headless_chrome_ua_is_rejected(self) -> None:
        rejected, status = self._call(ua="Mozilla/5.0 HeadlessChrome/120.0")
        self.assertTrue(rejected)
        self.assertEqual(status, 403)

    def test_puppeteer_ua_is_rejected(self) -> None:
        rejected, status = self._call(ua="Puppeteer/21.0")
        self.assertTrue(rejected)
        self.assertEqual(status, 403)

    def test_playwright_ua_is_rejected(self) -> None:
        rejected, status = self._call(ua="Mozilla/5.0 playwright/1.42")
        self.assertTrue(rejected)
        self.assertEqual(status, 403)

    def test_case_insensitive_ua_match(self) -> None:
        rejected, status = self._call(ua="headlesschrome/120")
        self.assertTrue(rejected)
        self.assertEqual(status, 403)

    # -- Localhost origin variants --

    def test_localhost_5173_origin_is_rejected(self) -> None:
        rejected, status = self._call(origin="http://localhost:5173")
        self.assertTrue(rejected)
        self.assertEqual(status, 403)

    def test_127_0_0_1_5173_origin_is_rejected(self) -> None:
        rejected, status = self._call(origin="http://127.0.0.1:5173")
        self.assertTrue(rejected)
        self.assertEqual(status, 403)

    def test_other_origin_is_allowed(self) -> None:
        rejected, status = self._call(origin="https://example.com")
        self.assertFalse(rejected)
        self.assertEqual(status, 0)

    def test_normal_ua_is_allowed(self) -> None:
        rejected, status = self._call(ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
        self.assertFalse(rejected)
        self.assertEqual(status, 0)

    def test_empty_ua_and_origin_is_allowed(self) -> None:
        rejected, status = self._call()
        self.assertFalse(rejected)
        self.assertEqual(status, 0)

    # -- Escape hatch --

    def test_allow_env_bypasses_headless_ua(self) -> None:
        rejected, status = self._call(ua="HeadlessChrome/120.0", allow_env="1")
        self.assertFalse(rejected)
        self.assertEqual(status, 0)

    def test_allow_env_bypasses_localhost_origin(self) -> None:
        rejected, status = self._call(origin="http://localhost:5173", allow_env="1")
        self.assertFalse(rejected)
        self.assertEqual(status, 0)

    def test_response_body_is_json_error(self) -> None:
        """The 403 body must be valid JSON with an 'error' key."""
        from backend.api import _reject_test_origin_spawn

        handler = MagicMock()
        headers_dict = {"User-Agent": "HeadlessChrome/120"}
        handler.headers = type("FH", (), {"get": lambda self, k, d="": headers_dict.get(k, d)})()
        sent_bodies: list[bytes] = []
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.wfile = type("W", (), {"write": lambda self, b: sent_bodies.append(b)})()

        os.environ.pop("AF_ALLOW_TEST_ORIGIN_SPAWNS", None)
        _reject_test_origin_spawn(handler)

        self.assertTrue(sent_bodies)
        body = json.loads(sent_bodies[0].decode())
        self.assertIn("error", body)
        self.assertEqual(body["error"], "spawn_blocked_test_origin")


# ---------------------------------------------------------------------------
# Integration tests against a live _Handler instance
# ---------------------------------------------------------------------------


class TestInnovateTickGuardIntegration(unittest.TestCase):
    """Integration tests hitting a real _Handler via HTTP."""

    @classmethod
    def setUpClass(cls) -> None:
        # Start a minimal api server on a random port
        from backend.api import _Handler

        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def _post(self, path: str, *, ua: str = "", origin: str = "", body: bytes = b"{}") -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers: dict[str, str] = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if ua:
            headers["User-Agent"] = ua
        if origin:
            headers["Origin"] = origin
        conn.request("POST", path, body=body, headers=headers)
        return conn.getresponse()

    def test_innovate_tick_blocked_by_headless_ua(self) -> None:
        os.environ.pop("AF_ALLOW_TEST_ORIGIN_SPAWNS", None)
        resp = self._post("/api/innovate/tick", ua="HeadlessChrome/120")
        self.assertEqual(resp.status, 403)
        data = json.loads(resp.read())
        self.assertEqual(data.get("error"), "spawn_blocked_test_origin")

    def test_innovate_tick_blocked_by_localhost_origin(self) -> None:
        os.environ.pop("AF_ALLOW_TEST_ORIGIN_SPAWNS", None)
        resp = self._post("/api/innovate/tick", origin="http://localhost:5173")
        self.assertEqual(resp.status, 403)

    def test_innovate_tick_blocked_regardless_of_idea_generation_gate(self) -> None:
        """Rejection must happen before any gate check — idea_generation is irrelevant."""
        os.environ.pop("AF_ALLOW_TEST_ORIGIN_SPAWNS", None)
        # Even if idea_generation gate were true, block should fire first
        with patch("backend.api._innovate_tick") as mock_tick:
            resp = self._post("/api/innovate/tick", ua="Puppeteer/21.0")
        mock_tick.assert_not_called()
        self.assertEqual(resp.status, 403)

    def test_innovate_tick_allowed_with_escape_hatch(self) -> None:
        """AF_ALLOW_TEST_ORIGIN_SPAWNS=1 must let the request past the guard."""
        with patch.dict(os.environ, {"AF_ALLOW_TEST_ORIGIN_SPAWNS": "1"}):
            # Will likely fail with 503 (no AF_API_AUTH_KEY set) — but NOT 403
            resp = self._post("/api/innovate/tick", ua="HeadlessChrome/120")
        # 403 would mean guard fired — any other status means guard was bypassed
        self.assertNotEqual(resp.status, 403)

    def test_loop_run_blocked_by_headless_ua(self) -> None:
        os.environ.pop("AF_ALLOW_TEST_ORIGIN_SPAWNS", None)
        resp = self._post("/api/loop/run", ua="HeadlessChrome/120")
        self.assertEqual(resp.status, 403)

    def test_project_loop_run_blocked_by_headless_ua(self) -> None:
        os.environ.pop("AF_ALLOW_TEST_ORIGIN_SPAWNS", None)
        resp = self._post("/api/projects/autonomous-forever/loop/run", ua="HeadlessChrome/120")
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
