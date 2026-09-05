"""Tests for backend/agent_feed.py"""
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_feed(tmp_path, monkeypatch):
    """Redirect agent_feed to a temp directory."""
    team_dir = tmp_path / ".autonomous-team"
    team_dir.mkdir()
    archive_dir = tmp_path / "archive" / "agent-feed"
    archive_dir.mkdir(parents=True)

    import backend.agent_feed as af
    monkeypatch.setattr(af, "_TEAM_DIR", team_dir)
    monkeypatch.setattr(af, "_FEED_PATH", team_dir / "agent-feed.jsonl")
    monkeypatch.setattr(af, "_ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(af, "_REPO_ROOT", tmp_path)
    return af


# ---------------------------------------------------------------------------
# append() tests
# ---------------------------------------------------------------------------

class TestAppend:
    def test_appends_valid_event(self, tmp_feed):
        tmp_feed.append({"event_type": "log", "role": "test", "message": "hello"})
        events = tmp_feed.tail(10)
        assert len(events) == 1
        assert events[0]["role"] == "test"
        assert events[0]["message"] == "hello"
        assert "ts" in events[0]

    def test_adds_ts_if_missing(self, tmp_feed):
        tmp_feed.append({"event_type": "log", "role": "test", "message": "no-ts"})
        events = tmp_feed.tail(1)
        assert "ts" in events[0]
        # Should be a valid ISO8601 string
        ts = events[0]["ts"]
        assert "T" in ts and "Z" in ts

    def test_preserves_existing_ts(self, tmp_feed):
        fixed_ts = "2026-01-01T00:00:00Z"
        tmp_feed.append({"ts": fixed_ts, "event_type": "log", "role": "test", "message": "ts-test"})
        events = tmp_feed.tail(1)
        assert events[0]["ts"] == fixed_ts

    def test_rejects_missing_role(self, tmp_feed):
        with pytest.raises(ValueError, match="role"):
            tmp_feed.append({"event_type": "log", "message": "no role"})

    def test_rejects_missing_event_type(self, tmp_feed):
        with pytest.raises(ValueError, match="event_type"):
            tmp_feed.append({"role": "test", "message": "no event_type"})

    def test_rejects_missing_message(self, tmp_feed):
        with pytest.raises(ValueError, match="message"):
            tmp_feed.append({"event_type": "log", "role": "test"})

    def test_rejects_message_too_long(self, tmp_feed):
        with pytest.raises(ValueError, match="280"):
            tmp_feed.append({"event_type": "log", "role": "test", "message": "x" * 281})

    def test_coerces_discussion_to_int(self, tmp_feed):
        tmp_feed.append({"event_type": "log", "role": "test", "message": "coerce", "discussion": "42"})
        events = tmp_feed.tail(1)
        assert events[0]["discussion"] == 42
        assert isinstance(events[0]["discussion"], int)

    def test_optional_fields_preserved(self, tmp_feed):
        tmp_feed.append({
            "event_type": "agent_end",
            "role": "executor",
            "message": "done",
            "discussion": 100,
            "pr": 200,
            "verdict": "pass",
            "tokens": {"input": 1000, "output": 500},
            "files": ["a.py", "b.py"],
            "model": "claude-sonnet-4-20250514",
        })
        events = tmp_feed.tail(1)
        e = events[0]
        assert e["discussion"] == 100
        assert e["pr"] == 200
        assert e["verdict"] == "pass"
        assert e["tokens"] == {"input": 1000, "output": 500}
        assert e["files"] == ["a.py", "b.py"]
        assert e["model"] == "claude-sonnet-4-20250514"

    def test_empty_string_pr_normalizes_to_null(self, tmp_feed):
        """Appending with pr='' must store null, not empty string (D#854)."""
        tmp_feed.append({"event_type": "log", "role": "test", "message": "no-pr", "pr": ""})
        events = tmp_feed.tail(1)
        e = events[0]
        assert "pr" in e, "pr key should be present (as null)"
        assert e["pr"] is None, f"expected null, got {e['pr']!r}"

    def test_empty_string_verdict_normalizes_to_null(self, tmp_feed):
        """Appending with verdict='' must store null, not empty string (D#854)."""
        tmp_feed.append({"event_type": "log", "role": "test", "message": "no-verdict", "verdict": ""})
        events = tmp_feed.tail(1)
        e = events[0]
        assert "verdict" in e, "verdict key should be present (as null)"
        assert e["verdict"] is None, f"expected null, got {e['verdict']!r}"

    def test_absent_pr_verdict_not_in_output(self, tmp_feed):
        """When pr and verdict are not supplied, they should be absent from the stored event."""
        tmp_feed.append({"event_type": "log", "role": "test", "message": "minimal"})
        events = tmp_feed.tail(1)
        e = events[0]
        assert "pr" not in e
        assert "verdict" not in e

    def test_concurrent_writes_no_corruption(self, tmp_feed, tmp_path):
        """Multiple parallel appends should produce N valid JSONL lines."""
        import threading
        N = 20
        errors = []

        def write_event(i):
            try:
                tmp_feed.append({"event_type": "log", "role": "test", "message": f"event-{i}"})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_event, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised: {errors}"
        events = tmp_feed.tail(N + 10)
        assert len(events) == N, f"Expected {N} events, got {len(events)}"
        # All must be valid JSON (tail() already parsed them)
        messages = {e["message"] for e in events}
        assert len(messages) == N


# ---------------------------------------------------------------------------
# tail() tests
# ---------------------------------------------------------------------------

class TestTail:
    def test_empty_feed_returns_empty(self, tmp_feed):
        assert tmp_feed.tail(50) == []

    def test_returns_last_n(self, tmp_feed):
        for i in range(10):
            tmp_feed.append({"event_type": "log", "role": "test", "message": f"msg-{i}"})
        events = tmp_feed.tail(3)
        assert len(events) == 3
        assert events[-1]["message"] == "msg-9"
        assert events[0]["message"] == "msg-7"

    def test_returns_all_if_fewer_than_n(self, tmp_feed):
        for i in range(5):
            tmp_feed.append({"event_type": "log", "role": "test", "message": f"m-{i}"})
        events = tmp_feed.tail(50)
        assert len(events) == 5


# ---------------------------------------------------------------------------
# filter() tests
# ---------------------------------------------------------------------------

class TestFilter:
    def test_filters_by_role(self, tmp_feed):
        tmp_feed.append({"event_type": "log", "role": "executor", "message": "exec"})
        tmp_feed.append({"event_type": "log", "role": "reviewer", "message": "review"})
        results = list(tmp_feed.filter(lambda e: e.get("role") == "executor"))
        assert len(results) == 1
        assert results[0]["role"] == "executor"

    def test_filters_by_since(self, tmp_feed):
        old_ts = "2020-01-01T00:00:00Z"
        new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp_feed.append({"ts": old_ts, "event_type": "log", "role": "test", "message": "old"})
        tmp_feed.append({"ts": new_ts, "event_type": "log", "role": "test", "message": "new"})

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        results = list(tmp_feed.filter(lambda e: True, since=cutoff))
        assert len(results) == 1
        assert results[0]["message"] == "new"

    def test_empty_feed_yields_nothing(self, tmp_feed):
        results = list(tmp_feed.filter(lambda e: True))
        assert results == []


# ---------------------------------------------------------------------------
# rotate() tests
# ---------------------------------------------------------------------------

class TestRotate:
    def test_no_op_if_feed_not_found(self, tmp_feed):
        result = tmp_feed.rotate()
        assert result["skipped"] == "feed_not_found"

    def test_splits_old_events_to_gz(self, tmp_feed):
        # Write one old event (yesterday) and one today
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        import backend.agent_feed as af
        # Write directly to feed file to bypass validation path injection
        feed = af._FEED_PATH
        feed.write_text(
            json.dumps({"ts": yesterday, "event_type": "log", "role": "test", "message": "old"}) + "\n" +
            json.dumps({"ts": today_ts, "event_type": "log", "role": "test", "message": "new"}) + "\n",
            encoding="utf-8"
        )

        result = tmp_feed.rotate()
        assert len(result["rotated_dates"]) == 1

        # Active feed should only contain today's event
        remaining = feed.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 1
        evt = json.loads(remaining[0])
        assert evt["message"] == "new"

        # Gz file should contain yesterday's event
        import gzip
        gz_files = list(af._TEAM_DIR.glob("agent-feed-*.jsonl.gz"))
        assert len(gz_files) == 1
        lines = gzip.open(gz_files[0], "rt", encoding="utf-8").read().strip().splitlines()
        assert len(lines) == 1
        old_evt = json.loads(lines[0])
        assert old_evt["message"] == "old"
