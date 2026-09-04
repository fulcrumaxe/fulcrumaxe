"""
Tests for backend/replay.py

ReplayRecorder and ReplayEngine — all file I/O uses tmp_path,
threading is mocked or controlled via stop flags.

Run with:
    pytest backend/tests/test_replay.py -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.replay import ReplayEngine, ReplayRecorder, get_recorder, start_replay


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def recorder(tmp_path: Path) -> ReplayRecorder:
    return ReplayRecorder(replays_dir=tmp_path)


# ---------------------------------------------------------------------------
# ReplayRecorder.start_recording
# ---------------------------------------------------------------------------


class TestStartRecording:
    def test_creates_jsonl_file(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("agent-1", role="executor", discussion=10)
        assert (tmp_path / "agent-1.jsonl").exists()

    def test_header_event_written(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("agent-2", role="code-reviewer", discussion=5)
        lines = (tmp_path / "agent-2.jsonl").read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["type"] == "header"
        assert obj["content"]["agent_id"] == "agent-2"
        assert obj["content"]["role"] == "code-reviewer"
        assert obj["content"]["discussion"] == 5

    def test_agent_tracked_as_active(self, recorder: ReplayRecorder):
        recorder.start_recording("agent-3")
        assert "agent-3" in recorder._active


# ---------------------------------------------------------------------------
# ReplayRecorder.record_event
# ---------------------------------------------------------------------------


class TestRecordEvent:
    def test_event_appended(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ag", role="executor")
        recorder.record_event("ag", "prompt", "Hello world")
        lines = (tmp_path / "ag.jsonl").read_text().splitlines()
        assert len(lines) == 2  # header + event
        obj = json.loads(lines[1])
        assert obj["type"] == "prompt"
        assert obj["content"] == "Hello world"

    def test_sequence_increments(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ag2", role="executor")
        recorder.record_event("ag2", "prompt", "first")
        recorder.record_event("ag2", "response", "second")
        lines = (tmp_path / "ag2.jsonl").read_text().splitlines()
        seqs = [json.loads(l)["seq"] for l in lines]
        assert seqs == [0, 1, 2]

    def test_silently_ignores_unknown_agent(self, recorder: ReplayRecorder, tmp_path: Path):
        # Should not raise and should not create any file
        recorder.record_event("nobody", "prompt", "ignored")
        assert not (tmp_path / "nobody.jsonl").exists()

    def test_metadata_token_tracking(self, recorder: ReplayRecorder):
        recorder.start_recording("ag3")
        recorder.record_event("ag3", "response", "text", metadata={"input_tokens": 100, "output_tokens": 50})
        state = recorder._active["ag3"]
        assert state["input_tokens"] == 100
        assert state["output_tokens"] == 50

    def test_timestamp_added(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ag4")
        recorder.record_event("ag4", "response", "hello")
        lines = (tmp_path / "ag4.jsonl").read_text().splitlines()
        obj = json.loads(lines[1])
        assert "ts" in obj
        assert obj["ts"].endswith("Z")


# ---------------------------------------------------------------------------
# ReplayRecorder.stop_recording
# ---------------------------------------------------------------------------


class TestStopRecording:
    def test_returns_summary(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ag5")
        recorder.record_event("ag5", "response", "hello")
        summary = recorder.stop_recording("ag5")
        assert summary is not None
        assert summary["total_events"] == 1
        assert "duration_seconds" in summary

    def test_footer_written(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ag6")
        recorder.stop_recording("ag6")
        lines = (tmp_path / "ag6.jsonl").read_text().splitlines()
        types = [json.loads(l)["type"] for l in lines]
        assert "footer" in types

    def test_agent_removed_from_active(self, recorder: ReplayRecorder):
        recorder.start_recording("ag7")
        recorder.stop_recording("ag7")
        assert "ag7" not in recorder._active

    def test_stop_nonexistent_returns_none(self, recorder: ReplayRecorder):
        result = recorder.stop_recording("ghost")
        assert result is None

    def test_further_events_ignored_after_stop(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ag8")
        recorder.stop_recording("ag8")
        recorder.record_event("ag8", "response", "should be ignored")
        lines = (tmp_path / "ag8.jsonl").read_text().splitlines()
        types = [json.loads(l)["type"] for l in lines]
        # Only header + footer — no extra event
        assert types.count("response") == 0


# ---------------------------------------------------------------------------
# ReplayRecorder.list_replays
# ---------------------------------------------------------------------------


class TestListReplays:
    def test_returns_completed_replays(self, recorder: ReplayRecorder, tmp_path: Path):
        for i in range(3):
            recorder.start_recording(f"agent-{i}", role="executor")
            recorder.stop_recording(f"agent-{i}")
        results = recorder.list_replays()
        assert len(results) == 3

    def test_result_has_metadata_fields(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("meta-agent", role="code-reviewer", discussion=99)
        recorder.stop_recording("meta-agent")
        results = recorder.list_replays()
        assert len(results) == 1
        m = results[0]
        assert m["agent_id"] == "meta-agent"
        assert m["role"] == "code-reviewer"
        assert m["discussion"] == 99

    def test_empty_dir_returns_empty_list(self, recorder: ReplayRecorder):
        assert recorder.list_replays() == []


# ---------------------------------------------------------------------------
# ReplayRecorder.get_replay
# ---------------------------------------------------------------------------


class TestGetReplay:
    def test_returns_events_in_order(self, recorder: ReplayRecorder):
        recorder.start_recording("rep1")
        recorder.record_event("rep1", "prompt", "input")
        recorder.record_event("rep1", "response", "output")
        recorder.stop_recording("rep1")
        events = recorder.get_replay("rep1")
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        types = [e["type"] for e in events]
        assert types[0] == "header"
        assert types[-1] == "footer"

    def test_nonexistent_returns_empty(self, recorder: ReplayRecorder):
        result = recorder.get_replay("no-such-agent")
        assert result == []


# ---------------------------------------------------------------------------
# ReplayRecorder.get_summary
# ---------------------------------------------------------------------------


class TestGetSummary:
    def test_returns_header_and_footer(self, recorder: ReplayRecorder):
        recorder.start_recording("sum1")
        recorder.record_event("sum1", "response", "hello")
        recorder.stop_recording("sum1")
        summary = recorder.get_summary("sum1")
        assert summary is not None
        assert summary["header"]["type"] == "header"
        assert summary["footer"]["type"] == "footer"

    def test_nonexistent_returns_none(self, recorder: ReplayRecorder):
        assert recorder.get_summary("ghost") is None


# ---------------------------------------------------------------------------
# ReplayRecorder.handle_agent_output_event
# ---------------------------------------------------------------------------


class TestHandleAgentOutputEvent:
    def test_records_event_for_active_agent(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ev-agent", role="executor")
        event = MagicMock()
        event.agent_id = "ev-agent"
        event.event_subtype = "content"
        event.content = "Tool result text"
        event.agent_role = "executor"
        recorder.handle_agent_output_event(event)
        lines = (tmp_path / "ev-agent.jsonl").read_text().splitlines()
        assert len(lines) == 2  # header + event

    def test_ignores_unknown_agent(self, recorder: ReplayRecorder):
        event = MagicMock()
        event.agent_id = "not-recording"
        event.event_subtype = "content"
        event.content = "hello"
        event.agent_role = "executor"
        # Should not raise
        recorder.handle_agent_output_event(event)

    def test_ignores_event_with_no_agent_id(self, recorder: ReplayRecorder):
        event = MagicMock()
        event.agent_id = None
        event.event_subtype = "content"
        event.content = "hello"
        event.agent_role = "executor"
        recorder.handle_agent_output_event(event)

    def test_subtype_mapping(self, recorder: ReplayRecorder, tmp_path: Path):
        recorder.start_recording("ev2", role="executor")
        event = MagicMock()
        event.agent_id = "ev2"
        event.event_subtype = "thinking"
        event.content = "I think..."
        event.agent_role = "executor"
        recorder.handle_agent_output_event(event)
        lines = (tmp_path / "ev2.jsonl").read_text().splitlines()
        obj = json.loads(lines[1])
        assert obj["type"] == "prompt"  # "thinking" maps to "prompt"


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------


def _make_events(n: int) -> list[dict]:
    """Build n fake replay events with sequential timestamps."""
    base_ts = "2026-01-01T00:00:00Z"
    return [
        {
            "seq": i,
            "ts": f"2026-01-01T00:00:{i:02d}Z",
            "type": "response",
            "content": f"event {i}",
            "metadata": {},
        }
        for i in range(n)
    ]


class TestReplayEngine:
    def test_start_and_is_alive(self):
        events = _make_events(0)
        engine = ReplayEngine("test-agent", events, speed="instant")
        assert not engine.is_alive
        with patch("backend.event_bus.get_bus"):
            engine.start()
        # Thread may finish instantly for 0 events; just check no error
        engine.stop()

    def test_pause_resume(self):
        engine = ReplayEngine("test-agent", _make_events(0), speed="instant")
        assert not engine.paused
        engine.pause()
        assert engine.paused
        engine.resume()
        assert not engine.paused

    def test_stop_terminates_thread(self):
        events = _make_events(5)
        engine = ReplayEngine("test-agent", events, speed="instant")
        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            engine.start()
            engine.stop()
        # After stop, thread should be joined within 1 second
        assert not engine.is_alive

    def test_seek_clamps_to_bounds(self):
        events = _make_events(5)
        engine = ReplayEngine("test-agent", events, speed="instant")
        engine.seek(100)  # out of range — should clamp to last index
        assert engine._seek_to == 4
        engine.seek(-5)  # negative — should clamp to 0
        assert engine._seek_to == 0

    def test_get_status_fields(self):
        events = _make_events(3)
        engine = ReplayEngine("test-agent", events, speed="5x")
        status = engine.get_status()
        assert status["agent_id"] == "test-agent"
        assert status["speed"] == "5x"
        assert status["total_events"] == 3
        assert "current_event" in status
        assert "paused" in status
        assert "active" in status

    def test_invalid_speed_raises(self):
        with pytest.raises(ValueError, match="speed must be one of"):
            ReplayEngine("agent", [], speed="99x")

    def test_start_already_running_then_stop(self):
        events = _make_events(2)
        engine = ReplayEngine("test-agent", events, speed="instant")
        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            engine.start()
            # stop waits for thread
            engine.stop()
        assert not engine.is_alive

    def test_instant_speed_emits_all_events(self):
        events = _make_events(5)
        emitted = []

        def fake_publish(ev):
            emitted.append(ev)

        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_bus.publish.side_effect = fake_publish
            mock_get_bus.return_value = mock_bus
            engine = ReplayEngine("test-agent", events, speed="instant")
            engine.start()
            engine._thread.join(timeout=5)

        assert len(emitted) == 5


# ---------------------------------------------------------------------------
# start_replay helper
# ---------------------------------------------------------------------------


class TestStartReplay:
    def test_raises_if_no_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            start_replay("nonexistent-agent", replays_dir=tmp_path)

    def test_returns_engine_and_starts(self, tmp_path: Path):
        recorder = ReplayRecorder(replays_dir=tmp_path)
        recorder.start_recording("started-agent", role="executor")
        recorder.record_event("started-agent", "response", "hello")
        recorder.stop_recording("started-agent")

        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            engine = start_replay("started-agent", speed="instant", replays_dir=tmp_path)

        assert isinstance(engine, ReplayEngine)
        engine.stop()
