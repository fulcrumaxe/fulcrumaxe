"""tests/test_live_analyst_daemon_cwd.py

Regression test for the cwd bug in live_analyst_daemon.process_transcript.

Root cause: subprocess.run([..., "backend/run_analyst.py", "--live", ...]) had
no cwd= argument and no PYTHONPATH in the child env, so the child Python could
not resolve `from backend._repo import ...` when the daemon was launched from
a temp dir (or any non-repo cwd).

Fix: pass cwd=str(REPO_ROOT) and env with PYTHONPATH=str(REPO_ROOT) to the
subprocess.run call so the child can always import backend.* regardless of
where the daemon was launched.

This test verifies that:
1. subprocess.run receives both cwd= and a PYTHONPATH env var.
2. Invoking the subprocess with cwd=REPO_ROOT and PYTHONPATH=REPO_ROOT
   produces zero ModuleNotFoundError, even when the test's own cwd is /tmp.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Allow imports from repo root and backend/
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import live_analyst_daemon as daemon  # noqa: E402


def _write_minimal_transcript(path: Path) -> None:
    """Write a minimal valid JSONL transcript."""
    turn = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    path.write_text(json.dumps(turn) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Core regression: cwd and PYTHONPATH are set on the subprocess call
# ---------------------------------------------------------------------------

class TestProcessTranscriptCwdFix:
    """Verify cwd=str(REPO_ROOT) and PYTHONPATH=str(REPO_ROOT) are passed to
    the subprocess so imports work regardless of the daemon's working directory."""

    def test_cwd_kwarg_present_in_subprocess_run(self, tmp_path, monkeypatch):
        """subprocess.run must receive cwd=REPO_ROOT so child can resolve imports."""
        transcript = tmp_path / "agent.output"
        _write_minimal_transcript(transcript)

        # Change the process cwd to simulate a daemon launched from /tmp
        monkeypatch.chdir(tmp_path)

        captured: list[dict] = []

        class FakeResult:
            returncode = 0
            stdout = json.dumps({
                "findings": [],
                "next_byte_offset": transcript.stat().st_size,
                "turns_read": 1,
                "classifiers_run": [],
            })
            stderr = ""

        def spy_run(cmd, **kwargs):
            captured.append(kwargs)
            return FakeResult()

        state = daemon.FileState(path=str(transcript))
        with patch("live_analyst_daemon.subprocess.run", side_effect=spy_run):
            daemon.process_transcript(state, library={}, dry_run=False)

        assert captured, "subprocess.run should have been called"
        kwargs = captured[0]

        assert "cwd" in kwargs, (
            "subprocess.run must receive cwd= so child can import backend.*"
        )
        assert kwargs["cwd"] == str(daemon.REPO_ROOT), (
            f"cwd should be REPO_ROOT ({daemon.REPO_ROOT!r}), got {kwargs['cwd']!r}"
        )

    def test_pythonpath_set_in_child_env(self, tmp_path, monkeypatch):
        """subprocess.run must pass PYTHONPATH=REPO_ROOT so `from backend.*` resolves."""
        transcript = tmp_path / "agent.output"
        _write_minimal_transcript(transcript)

        monkeypatch.chdir(tmp_path)

        captured: list[dict] = []

        class FakeResult:
            returncode = 0
            stdout = json.dumps({
                "findings": [],
                "next_byte_offset": transcript.stat().st_size,
                "turns_read": 1,
                "classifiers_run": [],
            })
            stderr = ""

        def spy_run(cmd, **kwargs):
            captured.append(kwargs)
            return FakeResult()

        state = daemon.FileState(path=str(transcript))
        with patch("live_analyst_daemon.subprocess.run", side_effect=spy_run):
            daemon.process_transcript(state, library={}, dry_run=False)

        assert captured
        kwargs = captured[0]
        child_env = kwargs.get("env", {})

        assert "PYTHONPATH" in child_env, (
            "subprocess.run must pass env with PYTHONPATH so backend.* imports work"
        )
        assert str(daemon.REPO_ROOT) in child_env["PYTHONPATH"], (
            f"PYTHONPATH must include REPO_ROOT ({daemon.REPO_ROOT}). "
            f"Got PYTHONPATH={child_env.get('PYTHONPATH')!r}"
        )

    def test_no_module_not_found_error_in_real_subprocess(self, tmp_path, monkeypatch):
        """End-to-end: invoke the subprocess with the fixed cwd+env and confirm
        the child exits 0 with no ModuleNotFoundError."""
        transcript = tmp_path / "agent.output"
        _write_minimal_transcript(transcript)

        # Change our cwd to /tmp to simulate the daemon being in a wrong dir
        monkeypatch.chdir(tmp_path)

        # Run exactly as the daemon now does after the fix
        child_env = {**os.environ, "PYTHONPATH": str(daemon.REPO_ROOT)}
        result = subprocess.run(
            [
                sys.executable,
                str(daemon.REPO_ROOT / "backend" / "run_analyst.py"),
                "--live",
                "--transcript", str(transcript),
            ],
            capture_output=True,
            text=True,
            cwd=str(daemon.REPO_ROOT),
            env=child_env,
        )

        assert result.returncode == 0, (
            f"run_analyst.py --live must exit 0 with cwd=REPO_ROOT + PYTHONPATH. "
            f"stderr: {result.stderr[:400]}"
        )
        assert "ModuleNotFoundError" not in result.stderr, (
            f"No ModuleNotFoundError expected. stderr: {result.stderr[:400]}"
        )
        assert "exited 1" not in result.stdout + result.stderr

    def test_process_transcript_full_round_trip_from_tmp_cwd(self, tmp_path, monkeypatch):
        """Full round-trip: process_transcript succeeds when daemon cwd is /tmp.

        This is the regression scenario from the bug — previously every call
        would log 'run_analyst.py --live exited 1' and return early.
        """
        transcript = tmp_path / "agent.output"
        _write_minimal_transcript(transcript)

        # Simulate daemon launched from /tmp (non-repo cwd)
        monkeypatch.chdir(tmp_path)

        state = daemon.FileState(path=str(transcript))
        # Capture any warning log calls that indicate subprocess failure
        warning_messages: list[str] = []

        original_warning = daemon.logger.warning

        def capture_warning(msg, *args):
            warning_messages.append(msg % args if args else msg)
            original_warning(msg, *args)

        monkeypatch.setattr(daemon.logger, "warning", capture_warning)

        # Call process_transcript for real — no mocking of subprocess
        daemon.process_transcript(state, library={}, dry_run=True)

        # Filter for the specific failure message that indicates the cwd bug
        module_errors = [m for m in warning_messages if "exited 1" in m or "ModuleNotFoundError" in m]
        assert not module_errors, (
            "process_transcript should not log subprocess failures. "
            f"Warnings seen: {module_errors}"
        )
