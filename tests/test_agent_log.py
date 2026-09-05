"""
Tests for backend/agent_log.py — log_event() and truncate_if_needed().

All tests use tmp_path — no writes to the real .autonomous-team/ directory.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agent_log import log_event, truncate_if_needed, MAX_LINES, KEEP_LINES


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------

def test_log_event_creates_file(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    log_event("agent-1", "executor", "spawn", "starting", feed_path=feed)
    assert Path(feed).exists()


def test_log_event_appends_valid_json(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    log_event("agent-1", "executor", "spawn", "starting", feed_path=feed)
    lines = Path(feed).read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["agent"] == "agent-1"
    assert record["role"] == "executor"
    assert record["event"] == "spawn"
    assert record["detail"] == "starting"
    assert "ts" in record


def test_log_event_includes_discussion_when_provided(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    log_event("agent-2", "code-reviewer", "review", "checking", discussion=42, feed_path=feed)
    record = json.loads(Path(feed).read_text().splitlines()[0])
    assert record["discussion"] == 42


def test_log_event_omits_discussion_when_none(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    log_event("agent-3", "executor", "done", "finished", discussion=None, feed_path=feed)
    record = json.loads(Path(feed).read_text().splitlines()[0])
    assert "discussion" not in record


def test_log_event_truncates_detail_at_200_chars(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    long_detail = "x" * 300
    log_event("agent-4", "executor", "message", long_detail, feed_path=feed)
    record = json.loads(Path(feed).read_text().splitlines()[0])
    assert len(record["detail"]) == 200


def test_log_event_multiple_appends(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    for i in range(5):
        log_event("agent-1", "executor", "step", f"step {i}", feed_path=feed)
    lines = Path(feed).read_text().splitlines()
    assert len(lines) == 5


def test_log_event_creates_parent_dirs(tmp_path):
    feed = str(tmp_path / "deep" / "nested" / "feed.jsonl")
    log_event("agent-1", "executor", "spawn", "start", feed_path=feed)
    assert Path(feed).exists()


def test_log_event_timestamp_is_utc_iso(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    log_event("agent-1", "executor", "done", "ok", feed_path=feed)
    record = json.loads(Path(feed).read_text().splitlines()[0])
    ts = record["ts"]
    assert ts.endswith("Z"), f"Expected UTC 'Z' suffix, got: {ts!r}"
    # Should be parseable
    from datetime import datetime
    datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# truncate_if_needed
# ---------------------------------------------------------------------------

def test_truncate_no_op_when_file_missing(tmp_path):
    feed = str(tmp_path / "nonexistent.jsonl")
    # Should not raise
    truncate_if_needed(feed_path=feed)


def test_truncate_no_op_when_under_limit(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    lines = [json.dumps({"n": i}) + "\n" for i in range(MAX_LINES - 1)]
    Path(feed).write_text("".join(lines))
    truncate_if_needed(feed_path=feed)
    result = Path(feed).read_text().splitlines()
    assert len(result) == MAX_LINES - 1


def test_truncate_cuts_to_keep_lines_when_over_limit(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    n = MAX_LINES + 50  # 550 lines
    lines = [json.dumps({"n": i}) + "\n" for i in range(n)]
    Path(feed).write_text("".join(lines))
    truncate_if_needed(feed_path=feed)
    result = Path(feed).read_text().splitlines()
    assert len(result) == KEEP_LINES


def test_truncate_keeps_most_recent_lines(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    n = MAX_LINES + 10
    lines = [json.dumps({"n": i}) + "\n" for i in range(n)]
    Path(feed).write_text("".join(lines))
    truncate_if_needed(feed_path=feed)
    remaining = Path(feed).read_text().splitlines()
    # First remaining line should be from the newer half
    first_record = json.loads(remaining[0])
    assert first_record["n"] == n - KEEP_LINES


def test_truncate_exactly_at_limit_does_nothing(tmp_path):
    feed = str(tmp_path / "feed.jsonl")
    lines = [json.dumps({"n": i}) + "\n" for i in range(MAX_LINES)]
    Path(feed).write_text("".join(lines))
    truncate_if_needed(feed_path=feed)
    result = Path(feed).read_text().splitlines()
    assert len(result) == MAX_LINES
