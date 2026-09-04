"""Unit tests for backend/orchestrator/hook_runner.py.

Tests cover:
  - HookRunner construction (explicit repo_root vs auto-detect)
  - _run_script: missing script (skip/return False), success (return True),
    non-zero exit (return False), timeout (return False), OSError (return False)
  - post_agent: arg construction (with/without discussion and pr)
  - pre_spawn: arg construction (with/without discussion), return value mirrors
    script success

All subprocess calls are mocked — no real hook scripts are invoked.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.orchestrator.hook_runner import HookRunner


# ---------------------------------------------------------------------------
# Minimal RunResult stand-in (mirrors sdk_runner.RunResult fields used by
# HookRunner.post_agent — avoids importing the full SDK runner module and its
# heavyweight dependencies in unit tests).
# ---------------------------------------------------------------------------

@dataclass
class _RunResult:
    agent_id: str
    role: str
    verdict: str
    discussion: Optional[int] = None
    pr: Optional[int] = None
    final_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls_count: int = 0
    prompt_sha256: str = ""
    start_ts: str = ""
    end_ts: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _completed_process(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stderr = stderr
    cp.stdout = ""
    return cp


# ---------------------------------------------------------------------------
# Construction tests
# ---------------------------------------------------------------------------


class TestHookRunnerConstruction(unittest.TestCase):
    def test_explicit_repo_root(self):
        hr = HookRunner(repo_root="/some/path")
        self.assertEqual(hr._root, Path("/some/path"))

    def test_default_repo_root_is_repo_dir(self):
        hr = HookRunner()
        # hook_runner.py lives at backend/orchestrator/hook_runner.py
        # parent.parent.parent of hook_runner.py == repo root
        import backend.orchestrator.hook_runner as _hr_mod
        expected = Path(_hr_mod.__file__).resolve().parent.parent.parent
        self.assertEqual(hr._root, expected)

    def test_root_stored_as_path(self):
        hr = HookRunner(repo_root="/tmp/fakerepo")
        self.assertIsInstance(hr._root, Path)


# ---------------------------------------------------------------------------
# _run_script behaviour
# ---------------------------------------------------------------------------


class TestRunScript(unittest.TestCase):
    def setUp(self):
        self.hr = HookRunner(repo_root="/tmp/fakerepo")

    def test_missing_script_returns_false(self):
        missing = Path("/tmp/fakerepo/scripts/does-not-exist.sh")
        # Path.exists() → False: no subprocess call, returns False
        result = self.hr._run_script(missing, [])
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_success_returns_true(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            result = self.hr._run_script(script, ["--flag", "val"])
        self.assertTrue(result)

    @patch("subprocess.run")
    def test_nonzero_exit_returns_false(self, mock_run):
        mock_run.return_value = _completed_process(returncode=1, stderr="something broke")
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            result = self.hr._run_script(script, [])
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_timeout_returns_false(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="bash hook.sh", timeout=60)
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            result = self.hr._run_script(script, [])
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_oserror_returns_false(self, mock_run):
        mock_run.side_effect = OSError("permission denied")
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            result = self.hr._run_script(script, [])
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_subprocess_called_with_bash_and_args(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            self.hr._run_script(script, ["--role", "executor"])
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        self.assertEqual(cmd[0], "bash")
        self.assertEqual(cmd[1], str(script))
        self.assertIn("--role", cmd)
        self.assertIn("executor", cmd)

    @patch("subprocess.run")
    def test_subprocess_timeout_is_60(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            self.hr._run_script(script, [])
        kwargs = mock_run.call_args[1]
        self.assertEqual(kwargs["timeout"], 60)

    @patch("subprocess.run")
    def test_subprocess_cwd_is_repo_root(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            self.hr._run_script(script, [])
        kwargs = mock_run.call_args[1]
        self.assertEqual(kwargs["cwd"], "/tmp/fakerepo")

    @patch("subprocess.run")
    def test_capture_output_and_text_enabled(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        script = Path("/tmp/fakerepo/scripts/hook.sh")
        with patch.object(Path, "exists", return_value=True):
            self.hr._run_script(script, [])
        kwargs = mock_run.call_args[1]
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])


# ---------------------------------------------------------------------------
# post_agent: arg construction
# ---------------------------------------------------------------------------


class TestPostAgent(unittest.TestCase):
    def setUp(self):
        self.hr = HookRunner(repo_root="/tmp/fakerepo")

    def _make_result(self, **overrides):
        defaults = dict(
            agent_id="executor-42-abc123",
            role="executor",
            verdict="done",
            discussion=42,
            pr=101,
        )
        defaults.update(overrides)
        return _RunResult(**defaults)

    @patch("subprocess.run")
    def test_post_agent_calls_post_agent_hook_script(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result()
        with patch.object(Path, "exists", return_value=True):
            # Patch the import inside post_agent to return our local RunResult
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        self.assertIn("post-agent-hook.sh", cmd[1])

    @patch("subprocess.run")
    def test_post_agent_passes_event_id(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result(agent_id="exec-99-xyz")
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--event-id")
        self.assertEqual(cmd[idx + 1], "exec-99-xyz")

    @patch("subprocess.run")
    def test_post_agent_passes_verdict(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result(verdict="fail")
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--verdict")
        self.assertEqual(cmd[idx + 1], "fail")

    @patch("subprocess.run")
    def test_post_agent_passes_role(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result(role="code-reviewer")
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--role")
        self.assertEqual(cmd[idx + 1], "code-reviewer")

    @patch("subprocess.run")
    def test_post_agent_with_discussion_includes_discussion_arg(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result(discussion=42, pr=None)
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--discussion", cmd)
        idx = cmd.index("--discussion")
        self.assertEqual(cmd[idx + 1], "42")

    @patch("subprocess.run")
    def test_post_agent_without_discussion_omits_arg(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result(discussion=None, pr=None)
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--discussion", cmd)

    @patch("subprocess.run")
    def test_post_agent_with_pr_includes_pr_arg(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result(discussion=None, pr=101)
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--pr", cmd)
        idx = cmd.index("--pr")
        self.assertEqual(cmd[idx + 1], "101")

    @patch("subprocess.run")
    def test_post_agent_without_pr_omits_arg(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        result = self._make_result(discussion=None, pr=None)
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--pr", cmd)

    @patch("subprocess.run")
    def test_post_agent_hook_failure_is_non_fatal(self, mock_run):
        """post_agent must not raise even when the hook script fails."""
        mock_run.return_value = _completed_process(returncode=1, stderr="exploded")
        result = self._make_result()
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                # Should not raise
                self.hr.post_agent(result)

    @patch("subprocess.run")
    def test_post_agent_missing_script_is_non_fatal(self, mock_run):
        """post_agent must not raise when hook script is missing."""
        result = self._make_result()
        with patch.object(Path, "exists", return_value=False):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_post_agent_timeout_is_non_fatal(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="bash", timeout=60)
        result = self._make_result()
        with patch.object(Path, "exists", return_value=True):
            with patch("backend.orchestrator.sdk_runner.RunResult", _RunResult):
                self.hr.post_agent(result)
        # No exception means pass


# ---------------------------------------------------------------------------
# pre_spawn: arg construction and return value
# ---------------------------------------------------------------------------


class TestPreSpawn(unittest.TestCase):
    def setUp(self):
        self.hr = HookRunner(repo_root="/tmp/fakerepo")

    @patch("subprocess.run")
    def test_pre_spawn_calls_pre_spawn_check_script(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        with patch.object(Path, "exists", return_value=True):
            self.hr.pre_spawn("executor")
        cmd = mock_run.call_args[0][0]
        self.assertIn("pre-spawn-check.sh", cmd[1])

    @patch("subprocess.run")
    def test_pre_spawn_passes_role(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        with patch.object(Path, "exists", return_value=True):
            self.hr.pre_spawn("code-reviewer")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--role")
        self.assertEqual(cmd[idx + 1], "code-reviewer")

    @patch("subprocess.run")
    def test_pre_spawn_with_discussion_includes_discussion_arg(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        with patch.object(Path, "exists", return_value=True):
            self.hr.pre_spawn("executor", discussion=99)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--discussion", cmd)
        idx = cmd.index("--discussion")
        self.assertEqual(cmd[idx + 1], "99")

    @patch("subprocess.run")
    def test_pre_spawn_without_discussion_omits_arg(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        with patch.object(Path, "exists", return_value=True):
            self.hr.pre_spawn("executor")
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--discussion", cmd)

    @patch("subprocess.run")
    def test_pre_spawn_returns_true_on_script_success(self, mock_run):
        mock_run.return_value = _completed_process(returncode=0)
        with patch.object(Path, "exists", return_value=True):
            result = self.hr.pre_spawn("executor")
        self.assertTrue(result)

    @patch("subprocess.run")
    def test_pre_spawn_returns_false_when_script_blocked(self, mock_run):
        """pre-spawn-check returning non-0 means spawn is blocked → False."""
        mock_run.return_value = _completed_process(returncode=1, stderr="dial denied")
        with patch.object(Path, "exists", return_value=True):
            result = self.hr.pre_spawn("executor")
        self.assertFalse(result)

    def test_pre_spawn_returns_false_when_script_missing(self):
        """Missing pre-spawn-check.sh → fail-open returns False (spawn-blocked path)."""
        with patch.object(Path, "exists", return_value=False):
            result = self.hr.pre_spawn("executor")
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_pre_spawn_returns_false_on_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="bash", timeout=60)
        with patch.object(Path, "exists", return_value=True):
            result = self.hr.pre_spawn("executor")
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_pre_spawn_discussion_cast_to_string(self, mock_run):
        """discussion is an int; the CLI flag must receive it as a string."""
        mock_run.return_value = _completed_process(returncode=0)
        with patch.object(Path, "exists", return_value=True):
            self.hr.pre_spawn("executor", discussion=1302)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--discussion")
        self.assertIsInstance(cmd[idx + 1], str)
        self.assertEqual(cmd[idx + 1], "1302")


if __name__ == "__main__":
    unittest.main()
