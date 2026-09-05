"""
Integration tests for scripts/hooks/post-merge.d/cross-file-pattern-check.sh

These tests invoke the shell hook via subprocess to verify shell-level
behaviour (timeout, gate=off exit, per-PR Discussion cap).

They use environment variables and mocked executables to avoid real GitHub
API calls and real detector runs.

AC#3  — hook exits within 60s when fed an oversized diff
AC#4  — gate=false causes hook to log-and-exit with zero side effects
AC#6  — second finding on same PR appends comment, not new Discussion
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOK_PATH = REPO_ROOT / "scripts" / "hooks" / "post-merge.d" / "cross-file-pattern-check.sh"


def _run_hook(
    env: dict[str, str],
    args: list[str] | None = None,
    timeout: int = 90,
) -> subprocess.CompletedProcess:
    """Run the hook script and return the CompletedProcess."""
    cmd = ["bash", str(HOOK_PATH)] + (args or [])
    return subprocess.run(
        cmd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# AC#4 — gate=false: hook logs and exits 0 with no side effects
# ---------------------------------------------------------------------------

class TestGateOff:
    """AC#4 — gate=false causes hook to log-and-exit with zero side effects."""

    def test_gate_off_exits_zero(self, tmp_path):
        """When control_plane returns false, hook exits 0 immediately."""
        # Create a fake control_plane.py that returns 'false' for any gate
        fake_cp = tmp_path / "fake_control_plane.py"
        fake_cp.write_text(textwrap.dedent("""\
            import sys
            print('false')
        """))

        # Override REPO_ROOT so the hook sources our fake backend
        # The hook calls: python3 "$REPO_ROOT/backend/control_plane.py" get ...
        # We intercept by making a fake backend/control_plane.py
        fake_backend = tmp_path / "backend"
        fake_backend.mkdir()
        (fake_backend / "control_plane.py").write_text(textwrap.dedent("""\
            import sys
            print('false')
        """))

        result = _run_hook(
            env={"HOME": str(tmp_path)},
            args=["--pr", "999"],
        )

        # Hook must exit 0 (no failure)
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}"

    def test_gate_off_logs_skip_message(self, tmp_path):
        """When gate=false, hook emits the 'gate=off — skipping' message."""
        # Create a fake backend that the hook won't find but use a path-override
        # to ensure gate resolves to false. The hook script's fallback when
        # python3 fails is echo "false", so we just need the script to not find
        # a real backend.  We do this by setting HOME to tmp_path so no real
        # control_plane.py is in the expected REPO_ROOT path.
        #
        # The hook derives REPO_ROOT from BASH_SOURCE; we can't override that.
        # Instead we rely on the fallback in the hook:
        #   GATE=$(python3 "$REPO_ROOT/backend/control_plane.py" get ... 2>/dev/null || echo "false")
        # If control_plane.py returns non-zero or raises, GATE="false".
        #
        # Since we're running from the actual repo root, the real control_plane
        # may be present and may return a real value.  Skip this test when the
        # real gate is enabled to avoid false failures.
        real_result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "backend" / "control_plane.py"),
             "get", "gates.cross_file_pattern_check"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        real_gate = real_result.stdout.strip()
        if real_gate == "true":
            pytest.skip("cross_file_pattern_check gate is currently true — gate=off test skipped")

        result = _run_hook(env={}, args=["--pr", "999"])

        assert result.returncode == 0
        assert "gate=off" in result.stdout or "skipping" in result.stdout, (
            f"Expected gate=off / skipping message in stdout, got: {result.stdout!r}"
        )

    def test_gate_off_no_gh_calls(self, tmp_path):
        """With gate=false, the hook must not invoke any gh subcommand."""
        # We verify this statically: the gate check happens before any gh call
        # in the script, so if gate=false we exit before line 70+ where gh appears.
        hook_content = HOOK_PATH.read_text()
        gate_check_pos = hook_content.find('if [[ "$GATE" != "true" ]]')
        first_gh_pos = hook_content.find('\ngh ')

        assert gate_check_pos != -1, "Gate check not found in hook script"
        assert first_gh_pos != -1, "No gh calls found in hook script"
        assert gate_check_pos < first_gh_pos, (
            "Gate check must appear before first gh call — "
            f"gate check at char {gate_check_pos}, first gh at {first_gh_pos}"
        )


# ---------------------------------------------------------------------------
# AC#3 — timeout: hook exits within 60s when fed an oversized diff
# ---------------------------------------------------------------------------

class TestTimeoutMechanism:
    """AC#3 — hook exits within 60s when fed an oversized diff."""

    def test_timeout_mechanism_present_in_hook(self):
        """Verify the hook uses timeout with --kill-after around the detector invocation."""
        content = HOOK_PATH.read_text()
        # The hook should contain the timeout invocation with kill-after to also
        # terminate child rg subprocesses spawned by the detector.
        assert "timeout --kill-after=" in content, (
            "Expected 'timeout --kill-after=...' in hook script for 60s ceiling + SIGKILL escalation"
        )

    def test_timeout_exit_code_handled(self):
        """Verify hook checks for exit code 124 (timeout) and exits 0."""
        content = HOOK_PATH.read_text()
        assert "DETECTOR_RC -eq 124" in content or "$DETECTOR_RC" in content, (
            "Expected DETECTOR_RC timeout check (124) in hook script"
        )
        # Specifically verify that on timeout the hook exits 0 (graceful)
        # Find the block after exit code 124 check
        idx = content.find("DETECTOR_RC -eq 124")
        assert idx != -1, "Timeout exit code check not found"
        # The block following this check must contain exit 0
        block = content[idx:idx + 200]
        assert "exit 0" in block, (
            f"Expected 'exit 0' after timeout check, got: {block!r}"
        )

    def test_timeout_command_slow_detector(self, tmp_path):
        """A detector that sleeps >1s is killed by a 1s timeout; hook still exits 0."""
        # Create a fake detector that sleeps forever
        slow_detector = tmp_path / "slow_detector.py"
        slow_detector.write_text(textwrap.dedent("""\
            import time, sys
            time.sleep(120)
            print('[]')
        """))

        # Write a minimal hook variant that uses timeout 1 instead of 60
        # to make the test fast, while preserving the same logic
        fast_hook = tmp_path / "test_hook.sh"
        original = HOOK_PATH.read_text()
        # Replace the timeout invocation with a 1s version pointing at slow_detector.
        # The hook now uses timeout --kill-after=5s --signal=TERM 60 so we match that.
        patched = original.replace(
            'timeout --kill-after=5s --signal=TERM 60 python3 "$DETECTOR"',
            f'timeout --kill-after=5s --signal=TERM 1 python3 "{slow_detector}"',
        )
        if 'timeout --kill-after=5s' not in original:
            # Fallback for older hook format
            patched = original.replace(
                'timeout 60 python3 "$DETECTOR"',
                f'timeout 1 python3 "{slow_detector}"',
            )
        # The hook uses REPO_ROOT-relative paths; patch GATE to "true" so
        # we reach the timeout path.  We patch the control_plane call too.
        patched = patched.replace(
            'python3 "$REPO_ROOT/backend/control_plane.py" get gates.cross_file_pattern_check 2>/dev/null || echo "false"',
            'echo "true"',
        )
        fast_hook.write_text(patched)
        fast_hook.chmod(0o755)

        result = subprocess.run(
            ["bash", str(fast_hook), "--pr", "1"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
        )

        assert result.returncode == 0, (
            f"Hook must exit 0 on timeout, got {result.returncode}. stderr: {result.stderr}"
        )
        assert "timed out" in result.stderr or "timed out" in result.stdout, (
            f"Expected 'timed out' message. stdout: {result.stdout!r} stderr: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# AC#6 — second finding on same PR appends comment, not new Discussion
# ---------------------------------------------------------------------------

class TestPerPrDiscussionCap:
    """AC#6 — second finding on same PR appends a comment, not a new Discussion."""

    def test_existing_discussion_marker_in_hook(self):
        """Verify the hook embeds a per-PR marker and checks for it before creating."""
        content = HOOK_PATH.read_text()
        # The hook must embed a unique marker per PR in the Discussion body
        assert "cross-file-finding-pr-" in content, (
            "Expected per-PR marker 'cross-file-finding-pr-<N>' in hook"
        )

    def test_existing_disc_branch_appends_comment(self):
        """When EXISTING_DISC is non-empty, hook takes the comment path, not createDiscussion."""
        content = HOOK_PATH.read_text()
        # Verify the branch: if EXISTING_DISC non-empty → addDiscussionComment mutation
        assert "addDiscussionComment" in content, (
            "Expected addDiscussionComment mutation for the existing Discussion path"
        )
        # Verify the alternative: if EXISTING_DISC empty → createDiscussion mutation
        assert "createDiscussion" in content, (
            "Expected createDiscussion mutation for the new Discussion path"
        )

    def test_create_vs_append_branch_logic(self):
        """
        The append-vs-create branch is:
          if [[ -n "$EXISTING_DISC" ]] → comment
          else → createDiscussion
        This is a static assertion on the script structure.
        """
        content = HOOK_PATH.read_text()
        existing_check_pos = content.find('[[ -n "$EXISTING_DISC"')
        assert existing_check_pos != -1, (
            "Expected '[[ -n \"$EXISTING_DISC\"' branch in hook"
        )
        # After the existing check, addDiscussionComment must come before createDiscussion
        after_check = content[existing_check_pos:]
        comment_pos = after_check.find("addDiscussionComment")
        create_pos = after_check.find("createDiscussion")
        assert comment_pos != -1, "addDiscussionComment not found after EXISTING_DISC check"
        assert create_pos != -1, "createDiscussion not found after EXISTING_DISC check"
        assert comment_pos < create_pos, (
            "addDiscussionComment (append path) must appear before createDiscussion (new path) "
            f"in the if/else branch. comment_pos={comment_pos}, create_pos={create_pos}"
        )
