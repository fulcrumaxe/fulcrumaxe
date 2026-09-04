"""Fixture tests for last_5h_reset() — 5-scenario coverage.

Each scenario exercises one code path through the resolution chain:
  1. Blackboard hit — stored timestamp within (now-5h, now] → return it
  2. Blackboard stale — stored timestamp older than now-5h → fall through to scan
  3. Blackboard miss + transcript hit → scan finds earliest token, pins, returns
  4. Idle — no stored value, no tokens in last 5h → return (now-5h)
  5. weekly_usage() unaffected — last_5h_reset is only wired into current_usage

The synthetic fixture writes JSONL entries via tmp files; no real transcripts
are read. Blackboard calls are patched via monkeypatch.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend import subscription_usage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc


def _dt(hours_ago: float, now: datetime) -> datetime:
    """Return a UTC datetime that is `hours_ago` hours before `now`."""
    return now - timedelta(hours=hours_ago)


def _make_jsonl_file(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a temporary JSONL file and return its path."""
    f = tmp_path / "session.jsonl"
    lines = [json.dumps(e) for e in entries]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _token_entry(ts: datetime, input_tokens: int = 100, output_tokens: int = 50) -> dict:
    """Minimal JSONL entry with a timestamp and token counts."""
    return {
        "timestamp": ts.isoformat(),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Scenario 1 — blackboard hit
# ---------------------------------------------------------------------------

class TestScenario1BlackboardHit:
    """Stored timestamp is within (now-5h, now] → return it without scanning transcripts."""

    def test_returns_stored_timestamp(self, monkeypatch):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)
        # Stored 2h ago — well within the 5h window
        stored_dt = _dt(2.0, now)

        mock_bb = MagicMock()
        mock_bb.read.return_value = stored_dt.isoformat()

        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [],  # should NOT be called
        )

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.last_5h_reset(now=now)

        assert result == stored_dt
        # Transcript scan must not happen (no files provided, but function shouldn't call it)
        mock_bb.write.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 2 — blackboard stale
# ---------------------------------------------------------------------------

class TestScenario2BlackboardStale:
    """Stored timestamp is older than now-5h → discard and fall through to transcript scan."""

    def test_falls_through_when_stored_is_stale(self, monkeypatch, tmp_path):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)
        # Stored 6h ago — outside the 5h window
        stale_dt = _dt(6.0, now)

        # One token entry 3h ago — within the 5h window
        fresh_entry_dt = _dt(3.0, now)
        jsonl_file = _make_jsonl_file(tmp_path, [_token_entry(fresh_entry_dt)])

        mock_bb = MagicMock()
        mock_bb.read.return_value = stale_dt.isoformat()

        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [jsonl_file],
        )

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.last_5h_reset(now=now)

        # Should return the fresh transcript entry, not the stale stored one
        assert result == fresh_entry_dt
        # Should pin the new boundary
        mock_bb.write.assert_called_once()
        _key, _val, *_ = mock_bb.write.call_args[0]
        assert _key == "subscription/last_5h_reset"


# ---------------------------------------------------------------------------
# Scenario 3 — blackboard miss + transcript hit
# ---------------------------------------------------------------------------

class TestScenario3BlackboardMissTranscriptHit:
    """No stored value; earliest token entry in last 5h is found and pinned."""

    def test_scans_and_pins(self, monkeypatch, tmp_path):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)

        # Two token entries: 4h ago and 2h ago — earliest within 5h is 4h ago
        entry_4h = _token_entry(_dt(4.0, now))
        entry_2h = _token_entry(_dt(2.0, now))
        jsonl_file = _make_jsonl_file(tmp_path, [entry_2h, entry_4h])  # unsorted on disk

        mock_bb = MagicMock()
        mock_bb.read.return_value = None  # no stored value

        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [jsonl_file],
        )

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.last_5h_reset(now=now)

        expected = _dt(4.0, now)
        # Must return earliest, not latest
        assert abs((result - expected).total_seconds()) < 1
        # Must pin
        mock_bb.write.assert_called_once()

    def test_8h_synthetic_fixture(self, monkeypatch, tmp_path):
        """8h fixture: entries at -7h, -4h, -1h. Window is 5h so -7h is excluded."""
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)

        entry_7h = _token_entry(_dt(7.0, now))  # outside 5h window
        entry_4h = _token_entry(_dt(4.0, now))  # inside 5h window — earliest
        entry_1h = _token_entry(_dt(1.0, now))  # inside 5h window

        jsonl_file = _make_jsonl_file(tmp_path, [entry_7h, entry_4h, entry_1h])

        mock_bb = MagicMock()
        mock_bb.read.return_value = None

        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [jsonl_file],
        )

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.last_5h_reset(now=now)

        expected = _dt(4.0, now)
        assert abs((result - expected).total_seconds()) < 1


# ---------------------------------------------------------------------------
# Scenario 4 — idle (no tokens in last 5h)
# ---------------------------------------------------------------------------

class TestScenario4Idle:
    """No stored value and no token entries in the last 5h → return now-5h without pinning."""

    def test_returns_floor_without_pinning(self, monkeypatch, tmp_path):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)

        # Only entry is 7h ago — outside the 5h window
        old_entry = _token_entry(_dt(7.0, now))
        jsonl_file = _make_jsonl_file(tmp_path, [old_entry])

        mock_bb = MagicMock()
        mock_bb.read.return_value = None

        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [jsonl_file],
        )

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.last_5h_reset(now=now)

        expected_floor = now - timedelta(hours=5)
        assert result == expected_floor
        # Idle: do NOT pin
        mock_bb.write.assert_not_called()

    def test_no_files_returns_floor(self, monkeypatch):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)

        mock_bb = MagicMock()
        mock_bb.read.return_value = None

        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [],
        )

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.last_5h_reset(now=now)

        assert result == now - timedelta(hours=5)
        mock_bb.write.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 5 — current_usage() and weekly_usage() integration
# ---------------------------------------------------------------------------

class TestScenario5Integration:
    """current_usage() uses last_5h_reset; weekly_usage() is unaffected."""

    def test_current_usage_window_start_from_last_5h_reset(self, monkeypatch, tmp_path):
        """current_usage() window_start must equal what last_5h_reset() returns."""
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)
        # Blackboard says reset was 1.5h ago
        reset_dt = _dt(1.5, now)

        mock_bb = MagicMock()
        mock_bb.read.return_value = reset_dt.isoformat()

        # One token entry 1h ago (within the window)
        entry = _token_entry(_dt(1.0, now), input_tokens=200, output_tokens=100)
        jsonl_file = _make_jsonl_file(tmp_path, [entry])

        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [jsonl_file],
        )
        monkeypatch.setattr(subscription_usage, "_load_config", lambda: {})

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.current_usage(now=now)

        # window_start in the result should match the reset boundary
        ws = datetime.fromisoformat(result["window_start"])
        if ws.tzinfo is None:
            ws = ws.replace(tzinfo=_UTC)
        assert abs((ws - reset_dt).total_seconds()) < 1

    def test_weekly_usage_unaffected(self, monkeypatch, tmp_path):
        """weekly_usage() does not call last_5h_reset — it uses its own weekly window."""
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)

        mock_bb = MagicMock()

        monkeypatch.setattr(subscription_usage, "_load_config", lambda: {})
        monkeypatch.setattr(
            subscription_usage,
            "_find_jsonl_files",
            lambda _: [],
        )

        with patch("backend.subscription_usage._get_blackboard", return_value=mock_bb):
            result = subscription_usage.weekly_usage(now=now)

        # Blackboard must not be touched by weekly_usage
        mock_bb.read.assert_not_called()
        mock_bb.write.assert_not_called()

        # Result must have weekly-specific keys
        assert "weekly_pct_sonnet" in result
        assert "weekly_pct_opus" in result
        assert "time_to_reset" in result
