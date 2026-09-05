"""test_render_safe.py — tests for dashboard_tui/render_safe.py.

Tests (per AC5, AC6):
  - Truncates at 200 chars
  - Strips ANSI escapes
  - Strips control chars
  - Redacts known secret patterns (regression-tested with known tokens)
  - Non-string input is converted
  - Empty string returns empty string
  - String exactly at max_len is not truncated
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# dashboard_tui/ is not present in every tree that runs this suite (an adopter
# clone legitimately has no TUI). Skip rather than raise at collection time: an
# uncaught ImportError here aborts the whole run for every other test file too.
pytest.importorskip(
    "dashboard_tui.render_safe",
    reason="dashboard_tui/ not present in this tree",
)

from dashboard_tui.render_safe import render_safe  # noqa: E402


class TestRenderSafe:
    def test_truncates_long_string(self):
        s = "x" * 250
        result = render_safe(s)
        assert len(result) <= 200
        assert result.endswith("…")

    def test_exactly_max_len_not_truncated(self):
        s = "a" * 200
        result = render_safe(s, max_len=200)
        assert len(result) == 200
        assert not result.endswith("…")

    def test_strips_ansi_escapes(self):
        s = "\x1b[31mred text\x1b[0m"
        result = render_safe(s)
        assert "\x1b" not in result
        assert "red text" in result

    def test_strips_control_chars(self):
        s = "hello\x00\x01\x07world"
        result = render_safe(s)
        assert "\x00" not in result
        assert "\x01" not in result
        assert "\x07" not in result
        assert "hello" in result
        assert "world" in result

    def test_redacts_github_pat(self):
        """AC6: known GitHub PAT is redacted."""
        token = "github_pat_" + "A" * 36
        result = render_safe(f"token={token}")
        assert token not in result
        assert "[REDACTED]" in result

    def test_redacts_ghp_token(self):
        token = "ghp_" + "B" * 36
        result = render_safe(f"key={token}")
        assert token not in result
        assert "[REDACTED]" in result

    def test_redacts_anthropic_key(self):
        token = "sk-ant-" + "C" * 20
        result = render_safe(token)
        assert token not in result
        assert "[REDACTED]" in result

    def test_redacts_ghs_token(self):
        token = "ghs_" + "D" * 36
        result = render_safe(f"authorization: {token}")
        assert token not in result
        assert "[REDACTED]" in result

    def test_non_string_input(self):
        result = render_safe(42)
        assert result == "42"

    def test_none_input(self):
        result = render_safe(None)
        assert result == "None"

    def test_empty_string(self):
        result = render_safe("")
        assert result == ""

    def test_plain_string_unchanged(self):
        s = "loop_iteration_duration_seconds"
        result = render_safe(s)
        assert result == s

    def test_custom_max_len(self):
        s = "hello world"
        result = render_safe(s, max_len=5)
        assert len(result) <= 5
        assert result.endswith("…")
