"""Tests for backend/agent_run.py — one per acceptance criterion."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure repo root is on path so backend.agent_feed resolves
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.agent_feed as agent_feed
import backend.agent_run as agent_run


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    # Run 1: executor for Discussion 364 / PR 353
    {
        "ts": "2026-05-09T10:00:00Z",
        "event_type": "agent_start",
        "role": "executor",
        "message": "executor started",
        "discussion": 364,
        "pr": 353,
        "model": "claude-sonnet",
    },
    {
        "ts": "2026-05-09T10:01:00Z",
        "event_type": "log",
        "role": "executor",
        "message": "writing files",
        "discussion": 364,
        "pr": 353,
        "extra": {"tool": "Write", "target": "backend/agent_run.py", "ok": True},
    },
    {
        "ts": "2026-05-09T10:05:00Z",
        "event_type": "agent_end",
        "role": "executor",
        "message": "executor done",
        "discussion": 364,
        "pr": 353,
        "verdict": "done",
        "tokens": {"input": 10000, "output": 1200},
        "model": "claude-sonnet",
        "files": ["backend/agent_run.py", "backend/tests/test_agent_run.py"],
    },
    # Run 2: executor for Discussion 364 / PR 353
    {
        "ts": "2026-05-09T10:10:00Z",
        "event_type": "agent_start",
        "role": "executor",
        "message": "executor started",
        "discussion": 364,
        "pr": 353,
    },
    {
        "ts": "2026-05-09T10:20:00Z",
        "event_type": "agent_end",
        "role": "executor",
        "message": "executor done",
        "discussion": 364,
        "pr": 353,
        "verdict": "done",
        "tokens": {"input": 5000, "output": 800},
    },
    # Run 3: code-reviewer for a different PR
    {
        "ts": "2026-05-09T11:00:00Z",
        "event_type": "agent_start",
        "role": "code-reviewer",
        "message": "reviewer started",
        "discussion": 100,
        "pr": 400,
    },
    {
        "ts": "2026-05-09T11:10:00Z",
        "event_type": "agent_end",
        "role": "code-reviewer",
        "message": "reviewer done",
        "discussion": 100,
        "pr": 400,
        "verdict": "pass",
        "tokens": {"input": 3000, "output": 500},
    },
]


def _write_feed(tmp_path: Path, events: list[dict]) -> Path:
    """Write a list of event dicts to a temp JSONL file."""
    feed = tmp_path / "agent-feed.jsonl"
    with open(feed, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return feed


# ---------------------------------------------------------------------------
# AC 1: --pr N prints at least one run header with pr=N and exits 0
# ---------------------------------------------------------------------------


def test_ac1_pr_filter_prints_and_exits_0(tmp_path, monkeypatch):
    """AC1: --pr 353 prints at least one run header and exits 0."""
    feed_path = _write_feed(tmp_path, SAMPLE_EVENTS)
    monkeypatch.setattr(agent_feed, "_FEED_PATH", feed_path)

    captured = []

    def fake_print(*args, **kwargs):
        captured.append(" ".join(str(a) for a in args))

    monkeypatch.setattr("builtins.print", fake_print)

    rc = agent_run.main(["--pr", "353"])
    assert rc == 0

    # At least one line should mention pr=353
    assert any("pr=353" in line for line in captured), (
        f"Expected 'pr=353' in output, got: {captured}"
    )


# ---------------------------------------------------------------------------
# AC 2: --pr N --role X --json emits parseable JSON with correct fields
# ---------------------------------------------------------------------------


def test_ac2_json_output_executor(tmp_path, monkeypatch, capsys):
    """AC2: --pr 353 --role executor --json emits valid JSON with correct fields."""
    feed_path = _write_feed(tmp_path, SAMPLE_EVENTS)
    monkeypatch.setattr(agent_feed, "_FEED_PATH", feed_path)

    rc = agent_run.main(["--pr", "353", "--role", "executor", "--json"])
    assert rc == 0

    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) >= 1
    for item in data:
        assert item["role"] == "executor"
        assert item["pr"] == 353


# ---------------------------------------------------------------------------
# AC 3: no-match exits 1 with "no matching runs" on stderr
# ---------------------------------------------------------------------------


def test_ac3_no_match_exits_1(tmp_path, monkeypatch, capsys):
    """AC3: --discussion 99999 --role nobody exits 1 with message on stderr."""
    feed_path = _write_feed(tmp_path, SAMPLE_EVENTS)
    monkeypatch.setattr(agent_feed, "_FEED_PATH", feed_path)

    rc = agent_run.main(["--discussion", "99999", "--role", "nobody"])
    assert rc == 1

    err = capsys.readouterr().err
    assert "no matching runs" in err


# ---------------------------------------------------------------------------
# AC 4: --show-prompt prints transcript or fallback, never raises
# ---------------------------------------------------------------------------


def test_ac4_show_prompt_never_raises(tmp_path, monkeypatch, capsys):
    """AC4: --show-prompt prints transcript contents OR fallback string; no exception."""
    feed_path = _write_feed(tmp_path, SAMPLE_EVENTS)
    monkeypatch.setattr(agent_feed, "_FEED_PATH", feed_path)

    # Patch glob to return no files (simulates /tmp not available)
    monkeypatch.setattr("glob.glob", lambda *a, **kw: [])

    rc = agent_run.main(["--pr", "353", "--show-prompt"])
    assert rc == 0

    out = capsys.readouterr().out
    # Each run should say either transcript content or the fallback
    assert "(transcript not retained)" in out


# ---------------------------------------------------------------------------
# AC 5: tool timeline for run with no tool events shows "(no tool events recorded)"
# ---------------------------------------------------------------------------


def test_ac5_no_tool_events_message(tmp_path, monkeypatch, capsys):
    """AC5: a run with no tool-event logs prints '(no tool events recorded)'."""
    # Build a feed with only start+end, no log/tool events
    events = [
        {
            "ts": "2026-05-09T10:10:00Z",
            "event_type": "agent_start",
            "role": "executor",
            "message": "started",
            "discussion": 364,
            "pr": 353,
        },
        {
            "ts": "2026-05-09T10:20:00Z",
            "event_type": "agent_end",
            "role": "executor",
            "message": "done",
            "discussion": 364,
            "pr": 353,
            "verdict": "done",
            "tokens": {"input": 100, "output": 10},
        },
    ]
    feed_path = _write_feed(tmp_path, events)
    monkeypatch.setattr(agent_feed, "_FEED_PATH", feed_path)

    rc = agent_run.main(["--pr", "353", "--role", "executor"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "(no tool events recorded)" in out


# ---------------------------------------------------------------------------
# AC 6: corrupt JSONL line is tolerated; valid run still printed
# ---------------------------------------------------------------------------


def test_ac6_corrupt_jsonl_tolerated(tmp_path, monkeypatch, capsys):
    """AC6: a corrupt JSONL line is skipped; valid events still produce a run."""
    feed_path = tmp_path / "agent-feed.jsonl"
    valid_events = [
        {
            "ts": "2026-05-09T12:00:00Z",
            "event_type": "agent_start",
            "role": "executor",
            "message": "started",
            "discussion": 1,
            "pr": 99,
        },
        {
            "ts": "2026-05-09T12:05:00Z",
            "event_type": "agent_end",
            "role": "executor",
            "message": "done",
            "discussion": 1,
            "pr": 99,
            "verdict": "done",
            "tokens": {"input": 500, "output": 50},
        },
    ]
    with open(feed_path, "w", encoding="utf-8") as fh:
        # Insert corrupt line FIRST
        fh.write("{garbage\n")
        for ev in valid_events:
            fh.write(json.dumps(ev) + "\n")

    monkeypatch.setattr(agent_feed, "_FEED_PATH", feed_path)

    rc = agent_run.main(["--pr", "99"])
    assert rc == 0

    out = capsys.readouterr().out
    err = capsys.readouterr().err  # should be empty — no traceback
    assert "pr=99" in out
    assert "Traceback" not in out
    assert "Traceback" not in err
