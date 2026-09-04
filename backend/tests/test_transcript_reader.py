"""Unit tests for backend/transcript_reader.py — all 28 ACs from Discussion #1217.

Covers:
  A. _extract_content — content normalization (AC-A1 through AC-A7)
  B. iter_turns — turn extraction across both formats (AC-B1 through AC-B10)
  C. find_transcripts — discovery, dedup, recency filter (AC-C1 through AC-C3)
  D. detect_role — role extraction priority order (AC-D1 through AC-D7)
  E. agent_id_from_path — path.stem extraction (AC-E1)

All tests use pytest's tmp_path fixture for FS isolation. No live /tmp or archive
dependencies. _SKIP_STATS is reset per test class via setup_method where needed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure backend is importable
_BACKEND = Path(__file__).parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from testsupport.fixture_paths import FIXTURE_HOME, FIXTURE_PROJECT_SLUG

import backend.transcript_reader as tr
from backend.transcript_reader import (
    TranscriptTurn,
    _extract_content,
    agent_id_from_path,
    detect_role,
    find_transcripts,
    iter_turns,
)


# ---------------------------------------------------------------------------
# A. _extract_content — content normalization
# ---------------------------------------------------------------------------


class TestExtractContent:
    """AC-A1 through AC-A7: _extract_content normalizes content to (text, tool_calls, tool_results)."""

    def test_ac_a1_string_content(self):
        """AC-A1: string content returns (text, [], [])."""
        text, calls, results = _extract_content("hello")
        assert text == "hello"
        assert calls == []
        assert results == []

    def test_ac_a2_list_of_text_blocks(self):
        """AC-A2: list of text blocks space-joins text, empty tool lists."""
        content = [
            {"type": "text", "text": "foo"},
            {"type": "text", "text": "bar"},
        ]
        text, calls, results = _extract_content(content)
        assert text == "foo bar"
        assert calls == []
        assert results == []

    def test_ac_a2_order_preserved(self):
        """AC-A2: text blocks are joined in order."""
        content = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
            {"type": "text", "text": "third"},
        ]
        text, _, _ = _extract_content(content)
        assert text == "first second third"

    def test_ac_a3_tool_use_block(self):
        """AC-A3: tool_use block produces exactly one tool_calls entry."""
        content = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "ls"},
                "id": "tu_1",
            }
        ]
        text, calls, results = _extract_content(content)
        assert text == ""
        assert results == []
        assert len(calls) == 1
        assert calls[0] == {"name": "Bash", "input": {"command": "ls"}, "id": "tu_1"}

    def test_ac_a4_tool_result_block(self):
        """AC-A4: tool_result block produces one tool_results entry."""
        content = [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": "ok",
                "is_error": True,
            }
        ]
        text, calls, results = _extract_content(content)
        assert text == ""
        assert calls == []
        assert len(results) == 1
        assert results[0] == {"tool_use_id": "tu_1", "content": "ok", "is_error": True}

    def test_ac_a5_tool_result_nested_text_blocks(self):
        """AC-A5: tool_result with nested text blocks flattens to space-joined content."""
        content = [
            {
                "type": "tool_result",
                "tool_use_id": "tu_2",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b"},
                ],
                "is_error": False,
            }
        ]
        _, _, results = _extract_content(content)
        assert len(results) == 1
        assert results[0]["content"] == "a b"

    def test_ac_a6_non_list_non_string_returns_empty(self):
        """AC-A6: int content returns ('', [], [])."""
        text, calls, results = _extract_content(5)
        assert text == ""
        assert calls == []
        assert results == []

    def test_ac_a6_dict_content_returns_empty(self):
        """AC-A6: dict content (not list, not str) returns ('', [], [])."""
        text, calls, results = _extract_content({"something": "weird"})
        assert text == ""
        assert calls == []
        assert results == []

    def test_ac_a6_non_dict_block_inside_list_skipped(self):
        """AC-A6: non-dict blocks inside a content list are skipped without error."""
        content = [
            "raw string not a dict",
            42,
            {"type": "text", "text": "valid"},
        ]
        text, calls, results = _extract_content(content)
        assert text == "valid"
        assert calls == []
        assert results == []

    def test_ac_a7_tool_use_missing_fields_default(self):
        """AC-A7: tool_use block missing name/input/id defaults each to empty string / {}."""
        content = [{"type": "tool_use"}]
        _, calls, _ = _extract_content(content)
        assert len(calls) == 1
        assert calls[0]["name"] == ""
        assert calls[0]["input"] == {}
        assert calls[0]["id"] == ""

    def test_ac_a7_tool_result_missing_is_error_defaults_false(self):
        """AC-A7: tool_result missing is_error defaults to False."""
        content = [{"type": "tool_result", "tool_use_id": "tu_3", "content": "x"}]
        _, _, results = _extract_content(content)
        assert results[0]["is_error"] is False

    def test_ac_a7_tool_result_missing_content_defaults_empty(self):
        """AC-A7: tool_result missing content defaults to ''."""
        content = [{"type": "tool_result", "tool_use_id": "tu_4"}]
        _, _, results = _extract_content(content)
        assert results[0]["content"] == ""


# ---------------------------------------------------------------------------
# B. iter_turns — turn extraction across both formats
# ---------------------------------------------------------------------------


def _reset_skip_stats() -> None:
    tr._SKIP_STATS["skipped_non_jsonl"] = 0
    tr._SKIP_STATS["skipped_trailing_truncation"] = 0
    tr._SKIP_STATS["corrupt_midfile"] = 0


class TestIterTurns:
    """AC-B1 through AC-B10: iter_turns() yields TranscriptTurns from .jsonl and .output."""

    def setup_method(self):
        _reset_skip_stats()

    def test_ac_b1_jsonl_two_valid_lines(self, tmp_path: Path):
        """AC-B1: two valid .jsonl lines yield two turns with correct roles and text."""
        f = tmp_path / "test.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
            + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}}) + "\n",
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        assert len(turns) == 2
        assert turns[0].turn_idx == 0
        assert turns[1].turn_idx == 1
        assert turns[0].role == "user"
        assert turns[1].role == "assistant"
        assert turns[0].text == "hi"
        assert turns[1].text == "yo"

    def test_ac_b2_output_format_message_wrapper(self, tmp_path: Path):
        """AC-B2: .output top-level-message format yields a turn with correct role and text."""
        f = tmp_path / "task.output"
        f.write_text(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "x"}}) + "\n",
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        assert len(turns) == 1
        assert turns[0].role == "assistant"
        assert turns[0].text == "x"

    def test_ac_b3_string_message_does_not_raise(self, tmp_path: Path):
        """AC-B3: a record whose message is a string falls back to obj for role/content — yields one turn."""
        f = tmp_path / "system.jsonl"
        f.write_text(
            json.dumps({"type": "system", "message": "Loaded session abc"}) + "\n",
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        assert len(turns) == 1
        # role and text may be empty strings (no role/content on top-level obj)
        assert turns[0].role == ""
        assert turns[0].text == ""

    def test_ac_b4_raw_field_equals_parsed_dict(self, tmp_path: Path):
        """AC-B4: each turn's raw field equals the original parsed JSON dict."""
        record = {"type": "user", "message": {"role": "user", "content": "hello"}}
        f = tmp_path / "test.jsonl"
        f.write_text(json.dumps(record) + "\n", encoding="utf-8")
        turns = list(iter_turns(f))
        assert len(turns) == 1
        assert turns[0].raw == record

    def test_ac_b5_blank_lines_skipped(self, tmp_path: Path):
        """AC-B5: blank lines are skipped and do not consume turn_idx."""
        record = {"type": "user", "message": {"role": "user", "content": "hi"}}
        f = tmp_path / "test.jsonl"
        f.write_text(
            "\n" + json.dumps(record) + "\n\n" + json.dumps(record) + "\n",
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        assert len(turns) == 2
        assert turns[0].turn_idx == 0
        assert turns[1].turn_idx == 1

    def test_ac_b6_non_dict_json_line_not_yielded(self, tmp_path: Path):
        """AC-B6: a bare JSON array is classified as bad — not yielded."""
        record = {"type": "user", "message": {"role": "user", "content": "hi"}}
        f = tmp_path / "test.jsonl"
        # valid line first, then a trailing non-dict JSON line
        f.write_text(
            json.dumps(record) + "\n" + "[1, 2]\n",
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        # The valid leading turn is still yielded
        assert len(turns) == 1
        assert turns[0].role == "user"

    def test_ac_b7_trailing_truncation_yields_valid_turns_no_warning(self, tmp_path: Path, capsys):
        """AC-B7: valid + trailing unparseable → yields valid turn, increments trailing stat, no warning."""
        record = {"type": "user", "message": {"role": "user", "content": "hi"}}
        f = tmp_path / "partial.jsonl"
        f.write_text(
            json.dumps(record) + "\n" + '{"truncated":',
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        captured = capsys.readouterr()
        assert len(turns) == 1
        assert turns[0].role == "user"
        assert "skipping malformed JSONL" not in captured.err
        assert tr._SKIP_STATS["skipped_trailing_truncation"] == 1

    def test_ac_b8_midfile_corruption_warns_unconditionally(self, tmp_path: Path, capsys):
        """AC-B8: valid / garbage / valid → yields two valid turns, counts corrupt_midfile, always emits one per-file warning.

        Class 3 (genuine mid-file corruption) warns unconditionally — _VERBOSE no longer
        gates this.  The flood fix (#1199) targeted Class 2 trailing truncation, not Class 3.
        """
        record = {"type": "user", "message": {"role": "user", "content": "hi"}}
        f = tmp_path / "corrupt.jsonl"
        f.write_text(
            json.dumps(record) + "\n"
            + "NOT JSON AT ALL\n"
            + json.dumps(record) + "\n",
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        captured = capsys.readouterr()
        assert len(turns) == 2
        assert captured.err.count("skipping malformed JSONL") == 1
        assert tr._SKIP_STATS["corrupt_midfile"] == 1

    def test_ac_b8_multiple_corrupt_files_warns_per_file(self, tmp_path: Path, capsys):
        """AC-B8 (multiple): N corrupt files → N per-file warnings, count == N.

        Each Class-3 mid-file-corrupt file produces exactly one stderr warning.
        """
        record = {"type": "user", "message": {"role": "user", "content": "hi"}}
        for i in range(3):
            f = tmp_path / f"corrupt_{i}.jsonl"
            f.write_text(
                json.dumps(record) + "\n"
                + "GARBAGE\n"
                + json.dumps(record) + "\n",
                encoding="utf-8",
            )
            list(iter_turns(f))
        captured = capsys.readouterr()
        assert captured.err.count("skipping malformed JSONL") == 3
        assert tr._SKIP_STATS["corrupt_midfile"] == 3

    def test_ac_b8_inflight_trailing_truncation_zero_warnings(self, tmp_path: Path, capsys):
        """AC-B8 regression (#1199 flood fix): in-flight/trailing-truncation file → 0 per-file warnings.

        Class 2 (valid lines followed by a single truncated/partial trailing line) must
        remain silent.  This is the scenario that caused the warning flood in PR #835/#1199:
        live .output files have a partial last line while the agent is still writing.
        Only the aggregate atexit summary is emitted — never a per-file warning.
        """
        record = {"type": "user", "message": {"role": "user", "content": "hi"}}
        f = tmp_path / "inflight.output"
        # Simulate in-flight file: valid lines + a truncated last line (no closing brace)
        f.write_text(
            json.dumps(record) + "\n"
            + json.dumps(record) + "\n"
            + '{"truncated":',
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        captured = capsys.readouterr()
        # Valid turns are yielded
        assert len(turns) == 2
        # No per-file warning — Class 2 stays silent
        assert "skipping malformed JSONL" not in captured.err
        assert tr._SKIP_STATS["skipped_trailing_truncation"] == 1
        assert tr._SKIP_STATS["corrupt_midfile"] == 0

    def test_ac_b9_all_unparseable_yields_nothing_no_warning(self, tmp_path: Path, capsys):
        """AC-B9: only unparseable lines → yields nothing, increments skipped_non_jsonl, no per-file warning."""
        f = tmp_path / "shell.output"
        f.write_text(
            "echo hello\nsome shell output\ntraceback\n",
            encoding="utf-8",
        )
        turns = list(iter_turns(f))
        captured = capsys.readouterr()
        assert turns == []
        assert "skipping malformed JSONL" not in captured.err
        assert tr._SKIP_STATS["skipped_non_jsonl"] == 1

    def test_ac_b10_nonexistent_path_yields_nothing(self, tmp_path: Path):
        """AC-B10: nonexistent path yields nothing and does not raise."""
        f = tmp_path / "does_not_exist.jsonl"
        turns = list(iter_turns(f))
        assert turns == []


# ---------------------------------------------------------------------------
# C. find_transcripts — discovery, dedup, recency filter
# ---------------------------------------------------------------------------


class TestFindTranscripts:
    """AC-C1 through AC-C3: find_transcripts() returns sorted, deduplicated, filtered paths."""

    def _write_valid_output(self, path: Path) -> None:
        record = {"type": "message", "message": {"role": "assistant", "content": "hello"}}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def _write_valid_jsonl(self, path: Path) -> None:
        record = {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def _old_mtime(self) -> float:
        """Return an mtime well past IN_FLIGHT_SECONDS."""
        return time.time() - (tr.IN_FLIGHT_SECONDS + 60)

    def test_ac_c1_sorted_and_deduplicated(self, tmp_path: Path, monkeypatch):
        """AC-C1: find_transcripts returns a sorted, deduplicated list of Paths."""
        # Create two old .output files
        f1 = tmp_path / "b_task.output"
        f2 = tmp_path / "a_task.output"
        self._write_valid_output(f1)
        self._write_valid_output(f2)
        old = self._old_mtime()
        os.utime(f1, (old, old))
        os.utime(f2, (old, old))

        # Point both globs at the same directory — same file might match both
        monkeypatch.setattr(tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.output"))

        results = find_transcripts()
        strs = [str(p) for p in results]
        # Sorted
        assert strs == sorted(strs), f"Expected sorted results, got {strs}"
        # Deduplicated
        assert len(strs) == len(set(strs)), "Duplicate paths in results"
        # Both files present
        assert f1 in results
        assert f2 in results

    def test_ac_c2_since_seconds_filter_excludes_old(self, tmp_path: Path, monkeypatch):
        """AC-C2: since_seconds excludes files older than now - since_seconds."""
        old_file = tmp_path / "old.output"
        fresh_file = tmp_path / "fresh.output"
        self._write_valid_output(old_file)
        self._write_valid_output(fresh_file)

        now = time.time()
        # old file: 200 seconds ago
        old_mtime = now - 200
        os.utime(old_file, (old_mtime, old_mtime))
        # fresh file: 5 seconds ago (beyond IN_FLIGHT_SECONDS=10, so not in-flight, but within 100s)
        fresh_mtime = now - (tr.IN_FLIGHT_SECONDS + 5)
        os.utime(fresh_file, (fresh_mtime, fresh_mtime))

        monkeypatch.setattr(tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.never_matches"))

        results = find_transcripts(since_seconds=100)
        assert old_file not in results, "Old file must be excluded"
        assert fresh_file in results, "Fresh file must be included"

    def test_ac_c3_jsonl_never_recency_skipped(self, tmp_path: Path, monkeypatch):
        """AC-C3: .jsonl with mtime=now is included; .output with same fresh mtime is excluded."""
        jsonl_file = tmp_path / "agent-abc-executor.jsonl"
        output_file = tmp_path / "task.output"
        self._write_valid_jsonl(jsonl_file)
        self._write_valid_output(output_file)

        # Set both mtimes to now (in-flight window)
        now = time.time()
        os.utime(jsonl_file, (now, now))
        os.utime(output_file, (now, now))

        monkeypatch.setattr(tr, "TRANSCRIPT_GLOB", str(tmp_path / "*.output"))
        monkeypatch.setattr(tr, "JSONL_TRANSCRIPT_GLOB", str(tmp_path / "*.jsonl"))

        results = find_transcripts()
        assert jsonl_file in results, ".jsonl must never be excluded by in-flight recency filter"
        assert output_file not in results, ".output with mtime=now must be excluded as in-flight"


# ---------------------------------------------------------------------------
# D. detect_role — role extraction priority order
# ---------------------------------------------------------------------------


class TestDetectRole:
    """AC-D1 through AC-D7: detect_role reads signals in priority order."""

    def test_ac_d1_system_field_wins(self, tmp_path: Path):
        """AC-D1: first parseable record with top-level system field returns that role."""
        f = tmp_path / "transcript.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}, "system": "executor"}) + "\n",
            encoding="utf-8",
        )
        assert detect_role(f) == "executor"

    def test_ac_d2_system_field_wins_over_sidecar_and_filename(self, tmp_path: Path):
        """AC-D2: system field beats sidecar and filename when all three signals present."""
        # Filename has role = "code-reviewer" via pattern
        f = tmp_path / "agent-abc123-code-reviewer.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}, "system": "executor"}) + "\n",
            encoding="utf-8",
        )
        # Sidecar says "security-reviewer"
        sidecar = Path(str(f) + ".role")
        sidecar.write_text("security-reviewer", encoding="utf-8")
        try:
            result = detect_role(f)
            assert result == "executor", f"system field must win, got {result!r}"
        finally:
            sidecar.unlink(missing_ok=True)

    def test_ac_d3_sidecar_wins_over_filename(self, tmp_path: Path):
        """AC-D3: no system field but .role sidecar → returns sidecar role."""
        # Filename has no role pattern
        f = tmp_path / "some-transcript.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8",
        )
        sidecar = Path(str(f) + ".role")
        sidecar.write_text("code-reviewer\n", encoding="utf-8")
        try:
            assert detect_role(f) == "code-reviewer"
        finally:
            sidecar.unlink(missing_ok=True)

    def test_ac_d4_filename_pattern_signal(self, tmp_path: Path):
        """AC-D4: no system field, no sidecar — filename pattern returns role."""
        f = tmp_path / "agent-abc123-executor.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8",
        )
        assert detect_role(f) == "executor"

    def test_ac_d5_fallback_unknown(self, tmp_path: Path):
        """AC-D5: no signals present → 'unknown'."""
        f = tmp_path / "generic-transcript.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8",
        )
        assert detect_role(f) == "unknown"

    def test_ac_d6_output_file_skips_signal1(self, tmp_path: Path):
        """AC-D6: .output file skips Signal 1 (no file read for system field), falls through to filename/unknown."""
        # Even if the file has a system field, .output suffix bypasses Signal 1
        f = tmp_path / "task.output"
        f.write_text(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "x"}, "system": "executor"}) + "\n",
            encoding="utf-8",
        )
        # No sidecar, filename doesn't match agent-<id>-<role>.jsonl pattern
        result = detect_role(f)
        assert result == "unknown", f"Expected 'unknown' for .output file, got {result!r}"

    def test_ac_d7_empty_system_field_not_honored(self, tmp_path: Path):
        """AC-D7: empty/whitespace system value does not satisfy Signal 1."""
        f = tmp_path / "agent-abc-executor.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}, "system": "   "}) + "\n",
            encoding="utf-8",
        )
        # Falls through to filename pattern
        result = detect_role(f)
        assert result == "executor", f"Expected filename signal 'executor', got {result!r}"

    def test_ac_d7_non_string_system_field_not_honored(self, tmp_path: Path):
        """AC-D7: non-string system field does not satisfy Signal 1."""
        f = tmp_path / "agent-abc-executor.jsonl"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}, "system": 42}) + "\n",
            encoding="utf-8",
        )
        # Falls through to filename pattern
        result = detect_role(f)
        assert result == "executor", f"Expected filename signal 'executor', got {result!r}"

    def test_ac_d7_only_first_parseable_record_checked(self, tmp_path: Path):
        """AC-D7: only the first parseable record is checked for system field."""
        f = tmp_path / "agent-abc-executor.jsonl"
        # First record: no system field; second record: system="code-reviewer"
        f.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
            + json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}, "system": "code-reviewer"}) + "\n",
            encoding="utf-8",
        )
        # Signal 1 from first record fails (no system); falls through to filename
        result = detect_role(f)
        assert result == "executor", f"Expected filename signal 'executor', got {result!r}"


# ---------------------------------------------------------------------------
# E. agent_id_from_path
# ---------------------------------------------------------------------------


class TestAgentIdFromPath:
    """AC-E1: agent_id_from_path returns path.stem."""

    def test_ac_e1_output_stem(self):
        """AC-E1: .output path returns stem (filename without extension)."""
        p = Path(".../tasks/abc123.output")
        assert agent_id_from_path(p) == "abc123"

    def test_ac_e1_jsonl_stem(self):
        """AC-E1: .jsonl path returns stem."""
        p = Path(".../uuid-1234.jsonl")
        assert agent_id_from_path(p) == "uuid-1234"

    def test_ac_e1_deep_path(self):
        """AC-E1: deep absolute path returns just the stem."""
        p = Path(f"/tmp/claude-abc/{FIXTURE_PROJECT_SLUG}/uuid/tasks/agent-123.output")
        assert agent_id_from_path(p) == "agent-123"

    def test_ac_e1_jsonl_uuid(self):
        """AC-E1: jsonl uuid path returns uuid stem."""
        p = Path(f"{FIXTURE_HOME}/.claude/projects/{FIXTURE_PROJECT_SLUG}/abc-123.jsonl")
        assert agent_id_from_path(p) == "abc-123"
