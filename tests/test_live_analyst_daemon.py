"""tests/test_live_analyst_daemon.py

Tests for the live-analyst daemon (Discussion #574 PR-b).

Covers:
  - Happy path: classifier fires → intervention written to FIFO
  - Gate-off path: daemon refuses to start when gate is disabled
  - Per-agent cap enforcement: stops after max_per_agent interventions
  - Allowlist filter: non-allowlisted categories do not trigger interventions
  - FIFO write failure is handled gracefully (no crash)
  - agent_id_from_path extracts stable IDs from transcript paths
  - Dry-run mode: counts interventions but does not write FIFO or log
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow imports from repo root and backend/
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import live_analyst_daemon as daemon  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_rm_transcript(path: Path, n_turns: int = 3) -> None:
    """Write a minimal JSONL transcript with git rm usage."""
    turns = []
    for i in range(n_turns):
        turns.append(json.dumps({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "id": f"tool_{i:04x}",
                    "input": {"command": f"git rm some-file-{i}.py"},
                }
            ],
        }))
    path.write_text("\n".join(turns) + "\n", encoding="utf-8")


def _tool_output_ignored_transcript(path: Path, streak: int = 4) -> None:
    """Write a transcript with repeated error tool results and no pivot."""
    lines = []
    for i in range(streak):
        lines.append(json.dumps({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "id": f"tid_{i:04x}",
                    "input": {"command": "bad_command"},
                }
            ],
        }))
        lines.append(json.dumps({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"tid_{i:04x}",
                    "is_error": True,
                    "content": "command not found: bad_command",
                }
            ],
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _minimal_library() -> dict:
    return {
        "git_rm_usage": {
            "message_template": "HARD RULE: no git rm. Use git mv to archive/.",
            "max_per_agent": 2,
            "severity": "error",
        },
        "tool_output_ignored": {
            "message_template": "WARNING: stop ignoring errors.",
            "max_per_agent": 3,
            "severity": "high",
        },
    }


# ---------------------------------------------------------------------------
# agent_id_from_path
# ---------------------------------------------------------------------------

class TestAgentIdFromPath:
    def test_standard_layout(self):
        path = "/tmp/claude-abc123/-home-agent-autonomous-forever/some-session/tasks/run.output"
        result = daemon.agent_id_from_path(path)
        assert result == "some-session"

    def test_short_layout(self):
        path = "/tmp/claude-abc/myagent/tasks/run.output"
        result = daemon.agent_id_from_path(path)
        assert result == "myagent"

    def test_no_tasks_dir(self):
        path = "/tmp/whatever/foo.output"
        result = daemon.agent_id_from_path(path)
        # Falls back to parent dir name
        assert result == "whatever"


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------

class TestGateCheck:
    def test_gate_on(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="true\n", stderr="")
            assert daemon.check_gate() is True

    def test_gate_off(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="false\n", stderr="")
            assert daemon.check_gate() is False

    def test_gate_subprocess_error(self):
        with patch("subprocess.run", side_effect=Exception("no control_plane")):
            assert daemon.check_gate() is False


# ---------------------------------------------------------------------------
# Intervention library
# ---------------------------------------------------------------------------

class TestInterventionLibrary:
    def test_load_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "INTERVENTION_LIBRARY", tmp_path / "nonexistent.json")
        lib = daemon.load_intervention_library()
        assert lib == {}

    def test_load_valid(self, tmp_path, monkeypatch):
        lib_file = tmp_path / "intervention-library.json"
        lib_file.write_text(json.dumps({
            "classifiers": {
                "git_rm_usage": {
                    "message_template": "no git rm",
                    "max_per_agent": 2,
                }
            }
        }))
        monkeypatch.setattr(daemon, "INTERVENTION_LIBRARY", lib_file)
        lib = daemon.load_intervention_library()
        assert "git_rm_usage" in lib
        assert lib["git_rm_usage"]["message_template"] == "no git rm"

    def test_load_corrupted(self, tmp_path, monkeypatch):
        lib_file = tmp_path / "intervention-library.json"
        lib_file.write_text("not valid json {{{{")
        monkeypatch.setattr(daemon, "INTERVENTION_LIBRARY", lib_file)
        lib = daemon.load_intervention_library()
        assert lib == {}


# ---------------------------------------------------------------------------
# Intervention log
# ---------------------------------------------------------------------------

class TestInterventionLog:
    def test_count_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", tmp_path / "log.jsonl")
        assert daemon.count_agent_interventions("agent-123") == 0

    def test_count_with_entries(self, tmp_path, monkeypatch):
        log_file = tmp_path / "log.jsonl"
        records = [
            {"agent_id": "agent-123", "classifier": "git_rm_usage"},
            {"agent_id": "agent-123", "classifier": "git_rm_usage"},
            {"agent_id": "agent-999", "classifier": "git_rm_usage"},
        ]
        log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", log_file)
        assert daemon.count_agent_interventions("agent-123") == 2
        assert daemon.count_agent_interventions("agent-999") == 1
        assert daemon.count_agent_interventions("agent-000") == 0

    def test_append(self, tmp_path, monkeypatch):
        log_file = tmp_path / "log.jsonl"
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", log_file)
        daemon.append_intervention_log({"agent_id": "x", "classifier": "test"})
        lines = [l for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["agent_id"] == "x"


# ---------------------------------------------------------------------------
# FIFO write
# ---------------------------------------------------------------------------

class TestFifoWrite:
    def test_write_to_real_fifo(self, tmp_path):
        fifo_path = str(tmp_path / "test.fifo")
        os.mkfifo(fifo_path)

        received: list[str] = []

        def reader():
            with open(fifo_path, "r") as f:
                received.append(f.readline())

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.05)  # let reader open

        result = daemon.write_intervention(fifo_path, "test message")
        t.join(timeout=2)

        assert result is True
        assert len(received) == 1
        payload = json.loads(received[0])
        assert payload["prompt"] == "test message"

    def test_write_to_nonexistent_fifo(self):
        # Should return False without raising
        result = daemon.write_intervention("/tmp/nonexistent-fifo-xyz", "test")
        assert result is False

    def test_write_to_regular_file_fails_gracefully(self, tmp_path):
        # Regular files are not FIFOs — open with O_NONBLOCK should behave differently
        # Just verify no exception is raised
        regular = str(tmp_path / "regular.txt")
        Path(regular).write_text("existing content")
        # This will fail (not a FIFO) but must not crash
        result = daemon.write_intervention(regular, "test")
        # Either True or False — just must not raise
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# process_transcript — happy path
# ---------------------------------------------------------------------------

class TestProcessTranscript:
    def test_happy_path_git_rm(self, tmp_path, monkeypatch):
        """git_rm classifier fires → intervention written to FIFO."""
        transcript = tmp_path / "run.output"
        _git_rm_transcript(transcript)

        fifo_path = str(tmp_path / "agent.fifo")
        os.mkfifo(fifo_path)

        intervention_log = tmp_path / "intervention-log.jsonl"
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", intervention_log)
        monkeypatch.setattr(daemon, "find_agent_fifo", lambda _: fifo_path)
        monkeypatch.setattr(daemon, "post_team_log", lambda _: None)

        library = _minimal_library()
        state = daemon.FileState(str(transcript))

        received: list[str] = []

        def reader():
            with open(fifo_path, "r") as f:
                data = f.readline()
                if data:
                    received.append(data)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.05)

        daemon.process_transcript(state, library, dry_run=False)
        t.join(timeout=5)

        assert len(received) == 1
        payload = json.loads(received[0])
        assert "git rm" in payload["prompt"].lower() or "archive" in payload["prompt"].lower()

        # Intervention log written
        log_lines = [l for l in intervention_log.read_text().splitlines() if l.strip()]
        assert len(log_lines) >= 1
        record = json.loads(log_lines[0])
        assert record["classifier"] == "git_rm_usage"
        assert record["agent_id"] == agent_id_from_path_helper(str(transcript))

    def test_dry_run_no_fifo_write(self, tmp_path, monkeypatch):
        """dry_run=True: classifier fires but FIFO write is skipped."""
        transcript = tmp_path / "run.output"
        _git_rm_transcript(transcript)

        write_called = []

        def fake_write(fifo, msg):
            write_called.append((fifo, msg))
            return True

        intervention_log = tmp_path / "intervention-log.jsonl"
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", intervention_log)
        monkeypatch.setattr(daemon, "find_agent_fifo", lambda _: "/tmp/fake.fifo")
        monkeypatch.setattr(daemon, "write_intervention", fake_write)
        monkeypatch.setattr(daemon, "post_team_log", lambda _: None)

        library = _minimal_library()
        state = daemon.FileState(str(transcript))
        daemon.process_transcript(state, library, dry_run=True)

        # FIFO write NOT called in dry_run mode
        assert write_called == []
        # Intervention log NOT written in dry_run mode
        assert not intervention_log.exists()

    def test_no_findings_no_intervention(self, tmp_path, monkeypatch):
        """Empty transcript → no interventions."""
        transcript = tmp_path / "run.output"
        transcript.write_text(
            json.dumps({"role": "assistant", "content": "hello"}) + "\n"
        )

        write_called = []
        monkeypatch.setattr(daemon, "find_agent_fifo", lambda _: None)
        monkeypatch.setattr(daemon, "write_intervention", lambda *a: write_called.append(a))
        monkeypatch.setattr(daemon, "post_team_log", lambda _: None)

        intervention_log = tmp_path / "log.jsonl"
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", intervention_log)

        library = _minimal_library()
        state = daemon.FileState(str(transcript))
        daemon.process_transcript(state, library)

        assert write_called == []

    def test_cap_enforcement(self, tmp_path, monkeypatch):
        """Per-agent cap: after max_per_agent interventions, no more writes."""
        transcript = tmp_path / "run.output"
        _git_rm_transcript(transcript)

        intervention_log = tmp_path / "intervention-log.jsonl"
        # Pre-populate log to simulate cap already reached (max_per_agent=2)
        intervention_log.write_text(
            json.dumps({"agent_id": transcript.parent.name, "classifier": "git_rm_usage"}) + "\n" +
            json.dumps({"agent_id": transcript.parent.name, "classifier": "git_rm_usage"}) + "\n"
        )
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", intervention_log)

        write_called = []
        monkeypatch.setattr(daemon, "find_agent_fifo", lambda _: "/tmp/fake.fifo")
        monkeypatch.setattr(daemon, "write_intervention", lambda *a: write_called.append(a) or True)
        monkeypatch.setattr(daemon, "post_team_log", lambda _: None)

        library = _minimal_library()
        state = daemon.FileState(str(transcript))
        daemon.process_transcript(state, library, dry_run=False)

        # Cap was already reached — no new writes
        assert write_called == []

    def test_allowlist_filter(self, tmp_path, monkeypatch):
        """Category not in library → no intervention."""
        transcript = tmp_path / "run.output"
        # A finding that would fire for git_rm_usage but library has no entry for it
        _git_rm_transcript(transcript)

        write_called = []
        monkeypatch.setattr(daemon, "find_agent_fifo", lambda _: "/tmp/fake.fifo")
        monkeypatch.setattr(daemon, "write_intervention", lambda *a: write_called.append(a) or True)
        monkeypatch.setattr(daemon, "post_team_log", lambda _: None)
        intervention_log = tmp_path / "log.jsonl"
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", intervention_log)

        # Library with only a different category
        library = {
            "wrong_premise_retries": {
                "message_template": "stop retrying",
                "max_per_agent": 3,
            }
        }

        state = daemon.FileState(str(transcript))
        daemon.process_transcript(state, library)

        # git_rm_usage finding → but not in library → no write
        assert write_called == []

    def test_byte_offset_advances(self, tmp_path, monkeypatch):
        """Byte offset is updated after processing so incremental reads work."""
        transcript = tmp_path / "run.output"
        _git_rm_transcript(transcript, n_turns=2)

        monkeypatch.setattr(daemon, "find_agent_fifo", lambda _: None)
        monkeypatch.setattr(daemon, "post_team_log", lambda _: None)
        intervention_log = tmp_path / "log.jsonl"
        monkeypatch.setattr(daemon, "INTERVENTION_LOG", intervention_log)

        library = _minimal_library()
        state = daemon.FileState(str(transcript))
        assert state.byte_offset == 0

        daemon.process_transcript(state, library, dry_run=True)
        # Offset should have advanced past the initial 0
        assert state.byte_offset > 0

    def test_missing_file_is_safe(self, tmp_path, monkeypatch):
        """process_transcript on a nonexistent file does not raise."""
        state = daemon.FileState(str(tmp_path / "nonexistent.output"))
        library = _minimal_library()
        daemon.process_transcript(state, library)  # must not raise


# ---------------------------------------------------------------------------
# Gate-off path: process_transcript still works (gate is checked at start)
# ---------------------------------------------------------------------------

class TestGateOffPath:
    def test_daemon_main_gate_off(self, monkeypatch):
        """main() returns 1 when gate is off."""
        monkeypatch.setattr(daemon, "check_gate", lambda: False)
        # main() checks gate and exits 1
        import io
        captured = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(captured):
            # Patch sys.argv to avoid argparse complaining
            monkeypatch.setattr(sys, "argv", ["live_analyst_daemon.py"])
            result = daemon.main()
        assert result == 1
        assert "gates.live_run_analyst" in captured.getvalue()


# ---------------------------------------------------------------------------
# discover_transcripts
# ---------------------------------------------------------------------------

class TestDiscoverTranscripts:
    def test_returns_list(self, tmp_path, monkeypatch):
        """discover_transcripts always returns a list (even when empty)."""
        monkeypatch.setattr(daemon, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(daemon, "TRANSCRIPT_GLOB_ALT", str(tmp_path / "*.output"))
        result = daemon.discover_transcripts()
        assert isinstance(result, list)

    def test_deduplicates(self, tmp_path, monkeypatch):
        """Same path returned by both globs is deduplicated."""
        f = tmp_path / "a.output"
        f.write_text("{}")
        glob_pattern = str(tmp_path / "*.output")
        monkeypatch.setattr(daemon, "TRANSCRIPT_GLOB", glob_pattern)
        monkeypatch.setattr(daemon, "TRANSCRIPT_GLOB_ALT", glob_pattern)
        result = daemon.discover_transcripts()
        assert result.count(str(f)) == 1


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def agent_id_from_path_helper(path: str) -> str:
    """Mirror daemon.agent_id_from_path for assertion use in tests."""
    return daemon.agent_id_from_path(path)
