"""tests/test_bash_retry_warn.py

Unit tests for hooks/bash_retry_warn.py.

Acceptance criteria:
  AC1 — transcript with cosmetic retry: hook emits warning to stderr
  AC2 — transcript with novel commands: hook is silent
  AC3 — live test: `false` followed by retry `false` emits warning
  AC4 — no regressions to sandbox.py (structural smoke test)
  AC5 — hook completes in <100ms

Run with:
    python3 -m pytest tests/test_bash_retry_warn.py -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hooks.bash_retry_warn import (
    _agent_id_from_cwd,
    _normalize,
    _parse_bash_history,
)
import hooks.bash_retry_warn as _mod
from testsupport.fixture_paths import FIXTURE_HOME, FIXTURE_MAIN_REPO

_WT_ROOT = str(_REPO / ".claude" / "worktrees")
_WT_ID = "agent-testabcd1234"
_WT_CWD = f"{_WT_ROOT}/{_WT_ID}"


def _bash_entry(tool_id: str, command: str) -> dict:
    return {
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": command},
                }
            ]
        }
    }


def _result_entry(tool_id: str, is_error: bool) -> dict:
    return {
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": is_error,
                }
            ]
        }
    }


def _make_transcript(entries: list) -> str:
    return "\n".join(json.dumps(e) for e in entries)


def _run_main(command: str, transcript_jsonl: str, cwd: str) -> tuple:
    """Run main() with a patched transcript, return (stderr_text, exit_code)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_id = Path(cwd).name
        transcript_path = Path(tmpdir) / f"{agent_id}.output"
        transcript_path.write_text(transcript_jsonl)

        orig_find = _mod._find_transcript
        orig_agent_id = _mod._agent_id_from_cwd
        _mod._find_transcript = lambda aid: str(transcript_path) if aid == agent_id else None
        _mod._agent_id_from_cwd = lambda c: agent_id

        payload = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
        )
        old_stdin, old_stderr = sys.stdin, sys.stderr
        stderr_cap = io.StringIO()
        sys.stdin = io.StringIO(payload)
        sys.stderr = stderr_cap

        exit_code = 0
        try:
            _mod.main()
        except SystemExit as e:
            exit_code = e.code or 0
        finally:
            sys.stdin = old_stdin
            sys.stderr = old_stderr
            _mod._find_transcript = orig_find
            _mod._agent_id_from_cwd = orig_agent_id

        return stderr_cap.getvalue(), exit_code


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_strips_cd_prefix(self):
        assert _normalize("cd /foo && git status") == _normalize("git status")

    def test_strips_redirect(self):
        assert _normalize("ls -la 2>&1") == _normalize("ls -la")

    def test_strips_pipe_head(self):
        assert _normalize("cat file | head -5") == _normalize("cat file")

    def test_strips_pipe_grep(self):
        assert _normalize("cat file | grep foo") == _normalize("cat file")

    def test_strips_quotes(self):
        assert _normalize('echo "hello"') == _normalize("echo hello")

    def test_lowercases(self):
        assert _normalize("GIT STATUS") == "git status"

    def test_distinct_commands_differ(self):
        assert _normalize("git commit -m fix") != _normalize("git push origin HEAD")


# ---------------------------------------------------------------------------
# _parse_bash_history
# ---------------------------------------------------------------------------


class TestParseBashHistory:
    def test_failed_detected(self):
        t = _make_transcript([_bash_entry("i1", "false"), _result_entry("i1", True)])
        history = _parse_bash_history(t)
        assert len(history) == 1
        assert history[0] == ("false", True)

    def test_success_not_failed(self):
        t = _make_transcript([_bash_entry("i1", "echo ok"), _result_entry("i1", False)])
        history = _parse_bash_history(t)
        assert history[0] == ("echo ok", False)

    def test_order_preserved(self):
        t = _make_transcript([
            _bash_entry("i1", "cmd1"), _result_entry("i1", False),
            _bash_entry("i2", "cmd2"), _result_entry("i2", True),
        ])
        history = _parse_bash_history(t)
        assert [c for c, _ in history] == ["cmd1", "cmd2"]

    def test_non_bash_ignored(self):
        obj = {"message": {"content": [{"type": "tool_use", "id": "i1", "name": "Read", "input": {"file_path": "/tmp/x"}}]}}
        history = _parse_bash_history(json.dumps(obj))
        assert history == []

    def test_malformed_lines_skipped(self):
        t = "not-json\n" + json.dumps(_bash_entry("i1", "echo hi"))
        history = _parse_bash_history(t)
        assert len(history) == 1


# ---------------------------------------------------------------------------
# AC1 — cosmetic retry → warning
# ---------------------------------------------------------------------------


class TestCosmeticRetry:
    def test_ac1_emits_warning(self):
        t = _make_transcript([_bash_entry("i1", "git log --oneline"), _result_entry("i1", True)])
        cwd = f"{_WT_ROOT}/agent-cosm1"
        stderr, code = _run_main("git log --oneline 2>&1", t, cwd)
        assert code == 0
        assert "[bash-retry-guard]" in stderr
        assert "cosmetic variant" in stderr

    def test_ac2_novel_command_silent(self):
        t = _make_transcript([_bash_entry("i1", "false"), _result_entry("i1", True)])
        cwd = f"{_WT_ROOT}/agent-novel1"
        stderr, code = _run_main("echo completely_different", t, cwd)
        assert code == 0
        assert stderr == ""

    def test_successful_prior_no_warning(self):
        t = _make_transcript([_bash_entry("i1", "ls -la"), _result_entry("i1", False)])
        cwd = f"{_WT_ROOT}/agent-succ1"
        stderr, code = _run_main("ls -la", t, cwd)
        assert code == 0
        assert stderr == ""

    def test_cd_prefix_stripped(self):
        t = _make_transcript([_bash_entry("i1", "npm run build"), _result_entry("i1", True)])
        cwd = f"{_WT_ROOT}/agent-cdpx1"
        stderr, code = _run_main(f"cd {FIXTURE_HOME} && npm run build", t, cwd)
        assert code == 0
        assert "[bash-retry-guard]" in stderr

    def test_team_lead_silent(self):
        orig = _mod._agent_id_from_cwd
        _mod._agent_id_from_cwd = lambda c: None
        t = _make_transcript([_bash_entry("i1", "false"), _result_entry("i1", True)])
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "false"}, "cwd": FIXTURE_MAIN_REPO})
        old_stdin, old_stderr = sys.stdin, sys.stderr
        stderr_cap = io.StringIO()
        sys.stdin = io.StringIO(payload)
        sys.stderr = stderr_cap
        exit_code = 0
        try:
            _mod.main()
        except SystemExit as e:
            exit_code = e.code or 0
        finally:
            sys.stdin = old_stdin
            sys.stderr = old_stderr
            _mod._agent_id_from_cwd = orig
        assert exit_code == 0
        assert stderr_cap.getvalue() == ""


# ---------------------------------------------------------------------------
# AC3 — live test: false; false emits warning
# ---------------------------------------------------------------------------


class TestLive:
    def test_ac3_false_false(self):
        t = _make_transcript([_bash_entry("i1", "false"), _result_entry("i1", True)])
        cwd = f"{_WT_ROOT}/agent-live99"
        stderr, code = _run_main("false", t, cwd)
        assert code == 0
        assert "[bash-retry-guard]" in stderr


# ---------------------------------------------------------------------------
# AC5 — performance <100ms
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_ac5_under_100ms(self):
        entries = []
        for i in range(250):
            entries.append(_bash_entry(f"i{i}", f"echo line {i}"))
            entries.append(_result_entry(f"i{i}", False))
        t = _make_transcript(entries)
        cwd = f"{_WT_ROOT}/agent-perf1"

        with tempfile.TemporaryDirectory() as tmpdir:
            agent_id = "agent-perf1"
            tp = Path(tmpdir) / f"{agent_id}.output"
            tp.write_text(t)

            orig_find = _mod._find_transcript
            orig_aid = _mod._agent_id_from_cwd
            _mod._find_transcript = lambda aid: str(tp)
            _mod._agent_id_from_cwd = lambda c: agent_id

            payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo new"}, "cwd": cwd})
            old_stdin, old_stderr = sys.stdin, sys.stderr
            sys.stdin = io.StringIO(payload)
            sys.stderr = io.StringIO()

            start = time.monotonic()
            exit_code = 0
            try:
                _mod.main()
            except SystemExit as e:
                exit_code = e.code or 0
            finally:
                elapsed = time.monotonic() - start
                sys.stdin = old_stdin
                sys.stderr = old_stderr
                _mod._find_transcript = orig_find
                _mod._agent_id_from_cwd = orig_aid

            assert elapsed < 0.100, f"Took {elapsed*1000:.1f}ms"
            assert exit_code == 0


# ---------------------------------------------------------------------------
# AC4 — sandbox regression
# ---------------------------------------------------------------------------


class TestSandboxRegression:
    def test_ac4_sandbox_importable(self):
        from hooks.sandbox import main as sandbox_main  # noqa: F401
        from hooks.sandbox_rules import classify_bash, is_worktree  # noqa: F401

    def test_ac4_sandbox_blocks_merge(self):
        from hooks.sandbox_rules import classify_bash
        d = classify_bash("gh pr merge 42", f"{_WT_ROOT}/agent-regtest")
        assert not d.allow
        assert "merge" in d.reason
