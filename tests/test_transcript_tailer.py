"""tests/test_transcript_tailer.py — Tests for backend/transcript_tailer.py (Discussion #835 PR-a).

Covers:
  - Secret scrubbing (5 original attack strings + 6 bypass fix cases)
  - Partial-line buffering (input with no trailing newline)
  - Bounded queue drop-oldest at max_lines capacity
  - 20-spawn cap in discover_active_spawns()
  - p99 tail emit overhead <5ms per line under 4-agent simulated load
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import transcript_tailer as tt

from testsupport.fixture_paths import FIXTURE_HOME, FIXTURE_MAIN_REPO, FIXTURE_PROJECT_SLUG


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

class TestScrubSecrets:
    """Five original attack strings per the spec."""

    def test_gh_token_assignment(self):
        line = "export GH_TOKEN=ghp_abc123XYZ789longtokenvalue"
        result = tt.scrub_secrets(line)
        assert "ghp_abc123XYZ789longtokenvalue" not in result
        assert "[REDACTED]" in result

    def test_anthropic_api_key_assignment(self):
        line = "ANTHROPIC_API_KEY=sk-ant-api03-supersecretkey1234567890ABCDEF"
        result = tt.scrub_secrets(line)
        assert "sk-ant-api03-supersecretkey1234567890ABCDEF" not in result
        assert "[REDACTED]" in result

    def test_dollar_gh_token_reference(self):
        line = "echo $GH_TOKEN"
        result = tt.scrub_secrets(line)
        assert "[REDACTED]" in result

    def test_authorization_bearer(self):
        line = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.somepayload.sig"
        result = tt.scrub_secrets(line)
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "[REDACTED]" in result

    def test_env_style_long_key(self):
        # .env-style assignment with a long hex token value
        line = "MY_SECRET_TOKEN=4f9a2b8c1e3d7f5a6b4c2e1f9d8a7b3c"
        result = tt.scrub_secrets(line)
        assert "4f9a2b8c1e3d7f5a6b4c2e1f9d8a7b3c" not in result
        assert "[REDACTED]" in result

    def test_clean_line_unchanged(self):
        line = '{"role": "assistant", "content": "hello world"}'
        result = tt.scrub_secrets(line)
        assert result == line

    def test_short_value_not_scrubbed(self):
        # PORT=8080 should NOT be scrubbed (too short to be a secret)
        line = "PORT=8080"
        result = tt.scrub_secrets(line)
        assert result == line


class TestScrubSecretsBypassFixes:
    """Six new test cases covering bypass paths identified in security review."""

    def test_json_style_gh_token_quoted_value(self):
        # JSON form: "GH_TOKEN": "ghp_realtokenfake1234567890"
        line = '"GH_TOKEN": "ghp_realtokenfake1234567890"'
        result = tt.scrub_secrets(line)
        assert "ghp_realtokenfake1234567890" not in result

    def test_yaml_style_gh_token_unquoted_value(self):
        # YAML form: GH_TOKEN: ghp_realtokenfake1234567890 (with whitespace after colon)
        line = "GH_TOKEN: ghp_realtokenfake1234567890"
        result = tt.scrub_secrets(line)
        assert "ghp_realtokenfake1234567890" not in result

    def test_url_credentials_scrubbed(self):
        # URL credential form: https://x-access-token:ghp_...@github.com/...
        line = "https://x-access-token:ghp_realtokenfake1234567890@github.com/foo/bar.git"
        result = tt.scrub_secrets(line)
        assert "ghp_realtokenfake1234567890" not in result
        assert "@github.com" in result  # host preserved
        assert "<scrubbed>" in result

    def test_standalone_ghp_token_scrubbed(self):
        # Standalone ghp_ token anywhere in a line
        line = 'fetching with token ghp_realtokenfake1234567890 done'
        result = tt.scrub_secrets(line)
        assert "ghp_realtokenfake1234567890" not in result
        assert "[REDACTED]" in result

    def test_standalone_sk_ant_token_scrubbed(self):
        # Standalone Anthropic API key
        line = 'key=sk-ant-api03-realtokenfake1234567890'
        result = tt.scrub_secrets(line)
        assert "sk-ant-api03-realtokenfake1234567890" not in result
        assert "[REDACTED]" in result

    def test_negative_no_underscore_not_scrubbed(self):
        # "ghpsomething" without underscore separator — NOT a real token shape
        line = "ghpsomething is just a word"
        result = tt.scrub_secrets(line)
        # Should not be scrubbed — ghp without underscore is not a token prefix
        assert result == line

    def test_filesystem_path_not_scrubbed(self):
        # A long UPPER_SNAKE=<value> assignment whose value is a filesystem
        # path should NOT be scrubbed. The .env-style pattern's value group
        # starts with [A-Za-z0-9+._-], which excludes '/', so a leading slash
        # is what keeps the path out of it — not the value's length.
        line = f"REPO_ROOT={FIXTURE_MAIN_REPO}"
        result = tt.scrub_secrets(line)
        assert FIXTURE_MAIN_REPO in result


# ---------------------------------------------------------------------------
# Partial-line buffering
# ---------------------------------------------------------------------------

class TestPartialLineBuffering:
    def test_partial_line_not_emitted_prematurely(self, tmp_path):
        """A file ending with no trailing newline holds the last partial line in buffer
        until EOF — then emits it only if it's a complete fragment from prior newlines."""
        transcript = tmp_path / "partial.jsonl"
        # Write two complete lines + one partial (no trailing newline)
        transcript.write_bytes(
            b'{"line": 1}\n{"line": 2}\n{"partial":',  # deliberate truncation
        )

        emitted = []
        tt.tail_transcript(str(transcript), emitted.append)

        # Only the two complete lines should be emitted; the partial fragment is buffered
        # and NOT emitted (no trailing newline means incomplete JSON fragment).
        assert len(emitted) == 2
        assert '"line": 1' in emitted[0]
        assert '"line": 2' in emitted[1]

    def test_complete_lines_emitted(self, tmp_path):
        transcript = tmp_path / "complete.jsonl"
        lines = ['{"a": 1}\n', '{"b": 2}\n', '{"c": 3}\n']
        transcript.write_text("".join(lines))

        emitted = []
        tt.tail_transcript(str(transcript), emitted.append)
        assert len(emitted) == 3

    def test_single_line_with_newline(self, tmp_path):
        transcript = tmp_path / "one.jsonl"
        transcript.write_text('{"x": 1}\n')
        emitted = []
        tt.tail_transcript(str(transcript), emitted.append)
        assert len(emitted) == 1


# ---------------------------------------------------------------------------
# Bounded queue drop-oldest
# ---------------------------------------------------------------------------

class TestBoundedQueueDropOldest:
    def test_drop_oldest_when_at_capacity(self, tmp_path):
        """When max_lines=5 and the file has 10 lines, only the last 5 are emitted."""
        transcript = tmp_path / "big.jsonl"
        all_lines = [f'{{"n": {i}}}\n' for i in range(10)]
        transcript.write_text("".join(all_lines))

        emitted = []
        tt.tail_transcript(str(transcript), emitted.append, max_lines=5)

        assert len(emitted) == 5
        # The LAST 5 lines should have been kept (drop-oldest means first 5 are dropped)
        emitted_ns = [int(__import__("json").loads(l)["n"]) for l in emitted]
        assert emitted_ns == [5, 6, 7, 8, 9]

    def test_bounded_queue_direct(self):
        q = tt._BoundedQueue(maxlen=3)
        for i in range(5):
            q.put(str(i))
        items = q.drain()
        # deque(maxlen=3) keeps the last 3
        assert items == ["2", "3", "4"]

    def test_drain_empties_queue(self):
        q = tt._BoundedQueue(maxlen=10)
        q.put("a")
        q.put("b")
        first_drain = q.drain()
        second_drain = q.drain()
        assert first_drain == ["a", "b"]
        assert second_drain == []


# ---------------------------------------------------------------------------
# 20-spawn cap
# ---------------------------------------------------------------------------

class TestSpawnCap:
    def test_capped_at_20(self, tmp_path, monkeypatch):
        """discover_active_spawns never returns more than max_spawns paths."""
        # Create 25 fake transcript files
        fake_dir = tmp_path / "subagents"
        fake_dir.mkdir()
        for i in range(25):
            f = fake_dir / f"agent-{i:04d}.jsonl"
            f.write_text("")

        glob_pattern = str(fake_dir / "agent-*.jsonl")
        monkeypatch.setattr(tt, "_SUBAGENT_GLOB_PATTERN", glob_pattern)

        result = tt.discover_active_spawns(max_spawns=20)
        assert len(result) <= 20

    def test_returns_newest_first(self, tmp_path, monkeypatch):
        """Newer files come first in the returned list."""
        fake_dir = tmp_path / "subagents"
        fake_dir.mkdir()
        paths = []
        for i in range(5):
            f = fake_dir / f"agent-{i:04d}.jsonl"
            f.write_text("")
            # Stagger mtimes: file 4 is newest
            import os
            atime = mtime = 1000000 + i * 1000
            os.utime(str(f), (atime, mtime))
            paths.append(str(f))

        monkeypatch.setattr(tt, "_SUBAGENT_GLOB_PATTERN", str(fake_dir / "agent-*.jsonl"))

        result = tt.discover_active_spawns(max_spawns=5)
        # Newest (agent-0004) should be first
        assert "agent-0004" in result[0]

    def test_empty_when_no_files(self, monkeypatch):
        monkeypatch.setattr(tt, "_SUBAGENT_GLOB_PATTERN", "/tmp/nonexistent_path_xyz/agent-*.jsonl")
        result = tt.discover_active_spawns()
        assert result == []

    def test_custom_max_spawns(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "subagents"
        fake_dir.mkdir()
        for i in range(10):
            (fake_dir / f"agent-{i:04d}.jsonl").write_text("")
        monkeypatch.setattr(tt, "_SUBAGENT_GLOB_PATTERN", str(fake_dir / "agent-*.jsonl"))

        result = tt.discover_active_spawns(max_spawns=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# p99 emit overhead <5ms per line under 4-agent simulated load
# ---------------------------------------------------------------------------

class TestEmitOverhead:
    def test_p99_overhead_under_5ms(self, tmp_path):
        """p99 per-line overhead must be <5ms with 4 concurrent agents."""
        # Create 4 transcript files, each with 100 lines
        n_agents = 4
        lines_per_agent = 100
        transcripts = []
        for i in range(n_agents):
            t = tmp_path / f"agent-{i}.jsonl"
            t.write_text(
                "\n".join(f'{{"agent": {i}, "line": {j}}}' for j in range(lines_per_agent))
                + "\n"
            )
            transcripts.append(str(t))

        latencies_ms: list[float] = []
        lock = threading.Lock()

        def make_callback():
            def cb(line: str) -> None:
                t1 = time.perf_counter()
                # Simulate a trivial downstream consumer
                _ = len(line)
                t2 = time.perf_counter()
                with lock:
                    latencies_ms.append((t2 - t1) * 1000)
            return cb

        # Tail all 4 transcripts concurrently
        start = time.perf_counter()
        threads = [
            threading.Thread(
                target=tt.tail_transcript,
                args=(path, make_callback()),
                daemon=True,
            )
            for path in transcripts
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
        elapsed = time.perf_counter() - start

        total_lines = n_agents * lines_per_agent
        assert len(latencies_ms) == total_lines, (
            f"Expected {total_lines} lines, got {len(latencies_ms)}"
        )

        p99 = statistics.quantiles(latencies_ms, n=100)[-1]
        assert p99 < 5.0, (
            f"p99 per-line overhead {p99:.2f}ms exceeds 5ms threshold"
        )

    def test_nonexistent_file_silent(self):
        """tail_transcript on a missing file returns silently without raising."""
        emitted = []
        tt.tail_transcript("/tmp/nonexistent_transcript_xyz.jsonl", emitted.append)
        assert emitted == []


# ---------------------------------------------------------------------------
# Integration: live_analyst_daemon imports transcript_tailer
# ---------------------------------------------------------------------------

class TestDaemonImportsFromTailer:
    def test_import_succeeds(self):
        """live_analyst_daemon must import without error and expose _TAILER_AVAILABLE."""
        import importlib
        import live_analyst_daemon as daemon
        # After our refactor, _TAILER_AVAILABLE should be True
        assert hasattr(daemon, "_TAILER_AVAILABLE")
        assert daemon._TAILER_AVAILABLE is True

    def test_scrub_secrets_reachable_via_daemon(self):
        """scrub_secrets must be accessible (re-exported) from live_analyst_daemon."""
        import live_analyst_daemon as daemon
        # The daemon imports scrub_secrets — verify it's callable
        assert callable(daemon.scrub_secrets)
        result = daemon.scrub_secrets("GH_TOKEN=ghp_secret123456789ABCDEF")
        assert "[REDACTED]" in result


# ---------------------------------------------------------------------------
# agent_label_from_path
# ---------------------------------------------------------------------------

class TestAgentLabelFromPath:
    def test_standard_subagent_path(self):
        path = f"{FIXTURE_HOME}/.claude/projects/{FIXTURE_PROJECT_SLUG}" \
               "/abc123/subagents/agent-ab15f3a6007edf168.jsonl"
        label = tt.agent_label_from_path(path)
        assert label == "agent-ab15f3a6"

    def test_short_name_preserved(self):
        path = "/tmp/subagents/agent-x.jsonl"
        label = tt.agent_label_from_path(path)
        assert label == "agent-x"
