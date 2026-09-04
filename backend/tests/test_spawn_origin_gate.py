"""Tests for _reject_test_origin_spawn — the spawn-protection gate in backend/api.py.

Covers two requirements:
1. Real dashboard browser usage (localhost:5173 origin + real browser UA) is NOT blocked.
2. Headless/test-runner requests (HeadlessChrome/Puppeteer/Playwright UA) ARE blocked.

Run with:
    python -m pytest backend/tests/test_spawn_origin_gate.py -v
"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.api import _reject_test_origin_spawn  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHeaders(dict):
    """Minimal headers mapping that also supports .get() with a default."""

    def get(self, key, default=""):  # type: ignore[override]
        return super().get(key, default)


def _make_handler(user_agent: str = "", origin: str = "") -> MagicMock:
    """Build a mock BaseHTTPRequestHandler with the given headers."""
    handler = MagicMock()
    handler.headers = _FakeHeaders({
        "User-Agent": user_agent,
        "Origin": origin,
    })
    # Capture calls to send_response / send_header / end_headers / wfile.write
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.wfile.write = MagicMock()
    return handler


# ---------------------------------------------------------------------------
# Real dashboard usage — must NOT be blocked
# ---------------------------------------------------------------------------


class TestDashboardNotBlocked:
    """Requests from a real browser at localhost:5173 must pass through."""

    def test_normal_chrome_from_dashboard(self):
        """Chrome UA + localhost:5173 origin → allowed (this is the Innovate loop scenario)."""
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        handler = _make_handler(user_agent=ua, origin="http://localhost:5173")
        result = _reject_test_origin_spawn(handler)
        assert result is False, "Real Chrome UA from dashboard must not be blocked"
        handler.send_response.assert_not_called()

    def test_normal_firefox_from_dashboard(self):
        """Firefox UA + localhost:5173 origin → allowed."""
        ua = "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"
        handler = _make_handler(user_agent=ua, origin="http://localhost:5173")
        result = _reject_test_origin_spawn(handler)
        assert result is False
        handler.send_response.assert_not_called()

    def test_localhost_5173_without_ua(self):
        """Origin localhost:5173 with no UA at all → allowed (cron or internal caller)."""
        handler = _make_handler(user_agent="", origin="http://localhost:5173")
        result = _reject_test_origin_spawn(handler)
        assert result is False
        handler.send_response.assert_not_called()

    def test_127_0_0_1_5173_real_browser(self):
        """127.0.0.1:5173 origin with real browser UA → allowed."""
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0"
        handler = _make_handler(user_agent=ua, origin="http://127.0.0.1:5173")
        result = _reject_test_origin_spawn(handler)
        assert result is False
        handler.send_response.assert_not_called()

    def test_no_origin_no_ua(self):
        """No Origin + no UA (server-side / curl call) → allowed."""
        handler = _make_handler(user_agent="", origin="")
        result = _reject_test_origin_spawn(handler)
        assert result is False
        handler.send_response.assert_not_called()

    def test_safari_from_dashboard(self):
        """Safari UA + localhost:5173 → allowed."""
        ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15"
        )
        handler = _make_handler(user_agent=ua, origin="http://localhost:5173")
        result = _reject_test_origin_spawn(handler)
        assert result is False
        handler.send_response.assert_not_called()


# ---------------------------------------------------------------------------
# Test runners — must be blocked
# ---------------------------------------------------------------------------


class TestTestRunnerBlocked:
    """Headless / automated test UAs must be rejected with 403."""

    def _assert_blocked(self, handler: MagicMock) -> None:
        result = _reject_test_origin_spawn(handler)
        assert result is True
        handler.send_response.assert_called_once_with(403)
        # The error body must be present
        written = b"".join(
            call.args[0] for call in handler.wfile.write.call_args_list
        )
        assert b"spawn_blocked_test_origin" in written

    def test_headlesschrome_ua_blocked(self):
        """HeadlessChrome in UA → blocked regardless of origin."""
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) HeadlessChrome/124.0.0.0 Safari/537.36"
        )
        handler = _make_handler(user_agent=ua, origin="")
        self._assert_blocked(handler)

    def test_headlesschrome_from_dashboard_origin_blocked(self):
        """HeadlessChrome UA + localhost:5173 origin → still blocked (test runner scenario)."""
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) HeadlessChrome/124.0.0.0 Safari/537.36"
        )
        handler = _make_handler(user_agent=ua, origin="http://localhost:5173")
        self._assert_blocked(handler)

    def test_puppeteer_ua_blocked(self):
        """Puppeteer in UA → blocked."""
        handler = _make_handler(user_agent="Puppeteer/21.0.0", origin="")
        self._assert_blocked(handler)

    def test_playwright_ua_blocked(self):
        """Playwright in UA → blocked."""
        handler = _make_handler(user_agent="playwright/1.44.0", origin="")
        self._assert_blocked(handler)

    def test_playwright_mixed_case_blocked(self):
        """Playwright UA is case-insensitive match."""
        handler = _make_handler(user_agent="PLAYWRIGHT/1.44.0", origin="")
        self._assert_blocked(handler)

    def test_puppeteer_embedded_in_longer_string(self):
        """Puppeteer within a longer UA string → blocked."""
        ua = "Mozilla/5.0 (compatible; Puppeteer/21.0) Chrome/124"
        handler = _make_handler(user_agent=ua, origin="")
        self._assert_blocked(handler)


# ---------------------------------------------------------------------------
# Env-var bypass
# ---------------------------------------------------------------------------


class TestEnvVarBypass:
    """AF_ALLOW_TEST_ORIGIN_SPAWNS and AF_MCP_TEST_ORIGIN skip the gate."""

    def test_af_allow_test_origin_spawns_bypasses_headless(self):
        """AF_ALLOW_TEST_ORIGIN_SPAWNS=1 lets HeadlessChrome through."""
        handler = _make_handler(user_agent="HeadlessChrome/124")
        with patch.dict("os.environ", {"AF_ALLOW_TEST_ORIGIN_SPAWNS": "1"}):
            result = _reject_test_origin_spawn(handler)
        assert result is False
        handler.send_response.assert_not_called()

    def test_af_mcp_test_origin_bypasses_headless(self):
        """AF_MCP_TEST_ORIGIN=1 lets HeadlessChrome through."""
        handler = _make_handler(user_agent="HeadlessChrome/124")
        with patch.dict("os.environ", {"AF_MCP_TEST_ORIGIN": "1"}):
            result = _reject_test_origin_spawn(handler)
        assert result is False
        handler.send_response.assert_not_called()

    def test_bypass_vars_not_set_by_default_blocks_headless(self):
        """Without bypass vars, HeadlessChrome is still blocked."""
        handler = _make_handler(user_agent="HeadlessChrome/124")
        env_without_bypass = {
            k: v for k, v in __import__("os").environ.items()
            if k not in ("AF_ALLOW_TEST_ORIGIN_SPAWNS", "AF_MCP_TEST_ORIGIN")
        }
        with patch.dict("os.environ", env_without_bypass, clear=True):
            result = _reject_test_origin_spawn(handler)
        assert result is True
