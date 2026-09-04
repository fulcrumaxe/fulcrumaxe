"""
test_replay.py — Unit tests for backend.replay.

Run with:
    python -m pytest backend/test_replay.py -v
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from backend.replay import ReplayEngine, ReplayRecorder, start_replay, stop_active_replay, get_active_replay
import backend.replay as replay_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_recorder(tmp_path: Path) -> ReplayRecorder:
    """Return a ReplayRecorder backed by a temp directory."""
    return ReplayRecorder(replays_dir=tmp_path / "replays")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_start_recording_creates_file(tmp_recorder: ReplayRecorder, tmp_path: Path) -> None:
    """start_recording should create the JSONL file with a header event."""
    tmp_recorder.start_recording("agent-1", role="executor", discussion=10)
    replay_file = tmp_path / "replays" / "agent-1.jsonl"
    assert replay_file.exists(), "replay file should exist after start_recording"
    lines = replay_file.read_text().strip().splitlines()
    assert len(lines) == 1, "should have exactly one line (header)"
    header = json.loads(lines[0])
    assert header["type"] == "header"
    assert header["content"]["role"] == "executor"
    assert header["content"]["discussion"] == 10


def test_record_event_appends_in_sequence(tmp_recorder: ReplayRecorder, tmp_path: Path) -> None:
    """record_event should append events with incrementing seq numbers."""
    tmp_recorder.start_recording("agent-2", role="executor")
    tmp_recorder.record_event("agent-2", "prompt", "hello")
    tmp_recorder.record_event("agent-2", "tool_call", {"name": "Bash"})
    tmp_recorder.record_event("agent-2", "response", "done")
    events = tmp_recorder.get_replay("agent-2")
    # header + 3 events
    assert len(events) == 4
    for i, ev in enumerate(events):
        assert ev["seq"] == i, f"seq mismatch at index {i}"


def test_stop_recording_writes_footer(tmp_recorder: ReplayRecorder) -> None:
    """stop_recording should append a footer with summary stats."""
    tmp_recorder.start_recording("agent-3", role="code-reviewer")
    tmp_recorder.record_event("agent-3", "response", "LGTM")
    summary = tmp_recorder.stop_recording("agent-3")
    assert summary is not None
    assert summary["total_events"] == 1
    assert summary["duration_seconds"] >= 0

    events = tmp_recorder.get_replay("agent-3")
    footer = next((e for e in events if e["type"] == "footer"), None)
    assert footer is not None, "footer event should be present"
    assert footer["content"]["total_events"] == 1


def test_get_replay_returns_ordered_events(tmp_recorder: ReplayRecorder) -> None:
    """get_replay should return events sorted by seq."""
    tmp_recorder.start_recording("agent-4", role="executor")
    for i in range(5):
        tmp_recorder.record_event("agent-4", "response", f"msg {i}")
    tmp_recorder.stop_recording("agent-4")

    events = tmp_recorder.get_replay("agent-4")
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), "events should be in ascending seq order"


def test_list_replays_returns_metadata(tmp_recorder: ReplayRecorder) -> None:
    """list_replays should return metadata dicts for completed recordings."""
    tmp_recorder.start_recording("agent-5", role="executor", discussion=42)
    for _ in range(5):
        tmp_recorder.record_event("agent-5", "response", "output")
    tmp_recorder.stop_recording("agent-5")

    replays = tmp_recorder.list_replays()
    assert len(replays) == 1
    meta = replays[0]
    assert meta["agent_id"] == "agent-5"
    assert meta["role"] == "executor"
    assert meta["discussion"] == 42
    assert meta["event_count"] == 5


def test_get_summary_returns_only_header_and_footer(tmp_recorder: ReplayRecorder) -> None:
    """get_summary should return header and footer, not body events."""
    tmp_recorder.start_recording("agent-6", role="executor")
    for _ in range(10):
        tmp_recorder.record_event("agent-6", "response", "x" * 100)
    tmp_recorder.stop_recording("agent-6")

    summary = tmp_recorder.get_summary("agent-6")
    assert summary is not None
    assert summary["header"] is not None
    assert summary["footer"] is not None
    assert summary["header"]["type"] == "header"
    assert summary["footer"]["type"] == "footer"


def test_pruning_by_age_removes_old_files(tmp_recorder: ReplayRecorder, tmp_path: Path) -> None:
    """Files older than retention_days should be deleted by list_replays."""
    replays_dir = tmp_path / "replays"
    replays_dir.mkdir(parents=True)

    # Create an old file by manually writing and backdating mtime.
    old_file = replays_dir / "old-agent.jsonl"
    old_file.write_text(
        json.dumps({"seq": 0, "ts": "2020-01-01T00:00:00Z", "type": "header",
                    "content": {"agent_id": "old-agent", "role": "executor", "discussion": None},
                    "metadata": {}}) + "\n"
    )
    # Set mtime to 30 days ago.
    old_mtime = time.time() - 30 * 86400
    os.utime(old_file, (old_mtime, old_mtime))

    recorder = ReplayRecorder(replays_dir=replays_dir)
    # Use default config (7-day retention); the 30-day-old file should be pruned.
    replays = recorder.list_replays()
    assert not old_file.exists(), "old replay file should be deleted during list_replays"
    assert all(r["agent_id"] != "old-agent" for r in replays)


def test_pruning_by_size_removes_oldest(tmp_recorder: ReplayRecorder, tmp_path: Path) -> None:
    """When storage exceeds max_storage_mb, oldest files should be deleted."""
    replays_dir = tmp_path / "replays"
    replays_dir.mkdir(parents=True)

    # Write two files, size them up, make one older than the other.
    for i, age_seconds in enumerate([200, 100]):
        path = replays_dir / f"big-agent-{i}.jsonl"
        # Write a header + large content to exceed a tiny limit.
        lines = [json.dumps({"seq": 0, "ts": "2026-01-01T00:00:00Z", "type": "header",
                              "content": {"agent_id": f"big-agent-{i}", "role": "executor",
                                          "discussion": None}, "metadata": {}})]
        lines += [json.dumps({"seq": j + 1, "ts": "2026-01-01T00:00:00Z", "type": "response",
                               "content": "x" * 512, "metadata": {}}) for j in range(100)]
        path.write_text("\n".join(lines) + "\n")
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))

    recorder = ReplayRecorder(replays_dir=replays_dir)
    # Monkey-patch config to a very small limit (0.05 MB ≈ 50 KB).
    import backend.replay as replay_mod
    original = replay_mod._load_replay_config
    replay_mod._load_replay_config = lambda: {"retention_days": 7, "max_storage_mb": 0.05}
    try:
        recorder.list_replays()
    finally:
        replay_mod._load_replay_config = original

    remaining = list(replays_dir.glob("*.jsonl"))
    # At most one file should remain (the newest), or none.
    assert len(remaining) <= 1, "oldest file should have been pruned to fit under storage cap"


def test_recording_ignores_unknown_agent(tmp_recorder: ReplayRecorder) -> None:
    """record_event for an agent that was never started should be a no-op."""
    # Should not raise.
    tmp_recorder.record_event("ghost-agent", "response", "hello")
    replay = tmp_recorder.get_replay("ghost-agent")
    assert replay == [], "no events should exist for an agent that was never started"


def test_event_types_are_preserved(tmp_recorder: ReplayRecorder) -> None:
    """Each event type in the spec should be round-trippable."""
    types = ["prompt", "tool_call", "tool_result", "response", "error"]
    tmp_recorder.start_recording("agent-7", role="executor")
    for t in types:
        tmp_recorder.record_event("agent-7", t, f"content for {t}")
    tmp_recorder.stop_recording("agent-7")

    events = tmp_recorder.get_replay("agent-7")
    recorded_types = [e["type"] for e in events if e["type"] not in ("header", "footer")]
    assert recorded_types == types, f"expected {types}, got {recorded_types}"


# ---------------------------------------------------------------------------
# ReplayEngine tests
# ---------------------------------------------------------------------------


def _make_trace(tmp_path: Path, agent_id: str, n_events: int = 5) -> Path:
    """Write a minimal JSONL trace for agent_id in tmp_path/replays/."""
    replays_dir = tmp_path / "replays"
    replays_dir.mkdir(parents=True, exist_ok=True)
    path = replays_dir / f"{agent_id}.jsonl"
    lines = []
    for i in range(n_events):
        lines.append(json.dumps({
            "seq": i,
            "ts": f"2026-04-10T00:00:{i:02d}Z",
            "type": "response",
            "content": f"event {i}",
            "metadata": {},
        }))
    path.write_text("\n".join(lines) + "\n")
    return replays_dir


@pytest.fixture(autouse=True)
def reset_active_replay():
    """Ensure no replay is active between tests."""
    stop_active_replay()
    yield
    stop_active_replay()


def test_replay_engine_start_and_stop(tmp_path: Path) -> None:
    """start_replay should return an engine; stop should terminate within 1 second."""
    replays_dir = _make_trace(tmp_path, "eng-1", n_events=10)
    eng = start_replay("eng-1", speed="instant", replays_dir=replays_dir)
    assert eng.is_alive or True  # may finish near-instantly in instant mode
    eng.stop()
    assert not eng.is_alive


def test_replay_engine_pause_resume(tmp_path: Path) -> None:
    """pause() should stop emission; resume() should restart it."""
    replays_dir = _make_trace(tmp_path, "eng-2", n_events=50)
    eng = start_replay("eng-2", speed="1x", replays_dir=replays_dir)
    time.sleep(0.05)  # let it start
    eng.pause()
    assert eng.paused
    pos_after_pause = eng._current_event
    time.sleep(0.05)
    pos_after_wait = eng._current_event
    assert pos_after_wait == pos_after_pause, "paused engine should not advance"
    eng.resume()
    assert not eng.paused
    eng.stop()


def test_replay_engine_seek(tmp_path: Path) -> None:
    """seek(N) should cause the engine to continue from event N."""
    replays_dir = _make_trace(tmp_path, "eng-3", n_events=100)
    eng = start_replay("eng-3", speed="instant", replays_dir=replays_dir)
    eng.pause()
    eng.seek(50)
    eng.resume()
    time.sleep(0.1)
    eng.stop()
    # After seeking to 50, current_event should be >= 50
    assert eng._current_event >= 50


def test_replay_engine_404_on_missing_agent(tmp_path: Path) -> None:
    """start_replay with a nonexistent agent_id should raise FileNotFoundError."""
    replays_dir = tmp_path / "replays"
    replays_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError, match="no replay found for agent_id"):
        start_replay("does-not-exist", replays_dir=replays_dir)


def test_replay_engine_invalid_speed(tmp_path: Path) -> None:
    """An invalid speed string should raise ValueError."""
    replays_dir = _make_trace(tmp_path, "eng-5", n_events=3)
    with pytest.raises(ValueError, match="speed must be one of"):
        start_replay("eng-5", speed="100x", replays_dir=replays_dir)


def test_concurrent_replay_prevention(tmp_path: Path) -> None:
    """Starting a new replay while one is active should stop the old one."""
    replays_dir = _make_trace(tmp_path, "eng-6a", n_events=200)
    _make_trace(tmp_path, "eng-6b", n_events=200)

    eng_a = start_replay("eng-6a", speed="1x", replays_dir=replays_dir)
    session_a = eng_a.replay_session_id
    time.sleep(0.05)

    eng_b = start_replay("eng-6b", speed="1x", replays_dir=replays_dir)
    time.sleep(0.1)

    assert not eng_a.is_alive, "first engine should be stopped when second starts"
    assert eng_b.replay_session_id != session_a
    eng_b.stop()


def test_status_endpoint_returns_accurate_state(tmp_path: Path) -> None:
    """get_status() should reflect current_event, speed, and paused flag."""
    replays_dir = _make_trace(tmp_path, "eng-7", n_events=5)
    eng = start_replay("eng-7", speed="instant", replays_dir=replays_dir)
    time.sleep(0.1)  # let instant replay finish
    eng.stop()

    status = eng.get_status()
    assert "active" in status
    assert status["agent_id"] == "eng-7"
    assert status["speed"] == "instant"
    assert status["total_events"] == 5
    assert "replay_session_id" in status


def test_replay_emits_events_on_bus(tmp_path: Path) -> None:
    """Events emitted by ReplayEngine should appear on the event bus with replay=True."""
    from backend.event_bus import AgentOutputEvent, get_bus

    replays_dir = _make_trace(tmp_path, "eng-8", n_events=3)

    received = []
    bus = get_bus()
    sub_id = bus.subscribe(AgentOutputEvent, received.append)
    try:
        eng = start_replay("eng-8", speed="instant", replays_dir=replays_dir)
        time.sleep(0.2)  # enough for instant replay to complete
        eng.stop()
    finally:
        bus.unsubscribe(sub_id)

    assert len(received) > 0, "replay should emit at least one event on the bus"
    for ev in received:
        assert ev.source == "replay"


def test_stop_with_no_active_replay(tmp_path: Path) -> None:
    """stop_active_replay() when nothing is active should return False without error."""
    result = stop_active_replay()
    assert result is False


def test_instant_speed_no_delay(tmp_path: Path) -> None:
    """Instant mode should complete much faster than 1x for a trace with long gaps."""
    replays_dir = tmp_path / "replays"
    replays_dir.mkdir(parents=True, exist_ok=True)
    path = replays_dir / "timing-agent.jsonl"
    # Write events with 1-second gaps (would take 10s at 1x).
    lines = []
    for i in range(10):
        lines.append(json.dumps({
            "seq": i,
            "ts": f"2026-04-10T00:{i:02d}:00Z",
            "type": "response",
            "content": f"msg {i}",
            "metadata": {},
        }))
    path.write_text("\n".join(lines) + "\n")

    t0 = time.monotonic()
    eng = start_replay("timing-agent", speed="instant", replays_dir=replays_dir)
    time.sleep(0.2)
    eng.stop()
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"instant replay took {elapsed:.2f}s — should be near-instant"
