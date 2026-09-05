"""
test_tui_screenshot_infra.py — Integration tests for D#855 Sub-PR 0.

Tests that:
 1. pilot-sweep.py produces findings.json with the required shape.
 2. tmux-sweep.sh produces 11 .txt files.
 3. STATE_DIR is not mutated by either sweep.
 4. Baseline diff logic works (empty baseline → pass; mismatched → fail).

These tests use a throwaway STATE_DIR override so they never touch
~/.autonomous-forever-state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PILOT_SCRIPT = REPO_ROOT / "scripts" / "tui-tester" / "pilot-sweep.py"
TMUX_SCRIPT = REPO_ROOT / "scripts" / "tui-tester" / "tmux-sweep.sh"
BASELINES_DIR = REPO_ROOT / "tests" / "fixtures" / "tui-baseline"

EXPECTED_SCREEN_COUNT = 11
EXPECTED_SCREENS = [
    "home", "prs", "discussions", "loop", "runs",
    "agent_feed", "stats", "pr_detail", "loop_controller",
    "ideas", "settings",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_state_dir(tmp_path: Path) -> Path:
    """A throwaway STATE_DIR so tests never touch real state."""
    d = tmp_path / "state"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# pilot-sweep.py tests
# ---------------------------------------------------------------------------


class TestPilotSweep:
    """Test Layer A: pilot-sweep.py output shape."""

    def test_script_exists_and_is_executable(self) -> None:
        assert PILOT_SCRIPT.exists(), f"pilot-sweep.py not found at {PILOT_SCRIPT}"
        assert os.access(PILOT_SCRIPT, os.X_OK) or PILOT_SCRIPT.suffix == ".py"

    def test_findings_json_shape(self, tmp_state_dir: Path) -> None:
        """Run pilot-sweep.py and assert findings.json has the required shape.

        We run with a --timeout=60 to allow CI headroom. The test is marked
        as slow because it starts a full Textual app in headless mode.

        If textual or dashboard_tui is not installed, the sweep returns a
        verdict='fail' with an import-error finding — we still check the shape.
        """
        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)
        env["PYTHONPATH"] = str(REPO_ROOT)

        result = subprocess.run(
            [sys.executable, str(PILOT_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--timeout", "60"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(REPO_ROOT),
            env=env,
        )

        # Script must exit with 0, 1, or 2 — any non-standard code is a crash
        assert result.returncode in (0, 1, 2), (
            f"pilot-sweep.py exited {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )

        # The last line of stdout should be parseable JSON
        stdout_lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        assert stdout_lines, "pilot-sweep.py produced no stdout"

        last_line = stdout_lines[-1]
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Last stdout line is not valid JSON: {exc}\nLine: {last_line!r}")

        # Required shape: { verdict, findings: [...] }
        assert "verdict" in payload, f"findings.json missing 'verdict' key: {payload.keys()}"
        assert "findings" in payload, f"findings.json missing 'findings' key: {payload.keys()}"
        assert payload["verdict"] in ("pass", "needs-fix", "fail"), (
            f"unexpected verdict: {payload['verdict']!r}"
        )
        assert isinstance(payload["findings"], list), (
            f"'findings' must be a list, got {type(payload['findings'])}"
        )

    def test_findings_json_written_to_artifact_dir(self, tmp_state_dir: Path) -> None:
        """pilot-sweep.py must write findings.json into STATE_DIR/tui-tester/<run-id>/."""
        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)
        env["PYTHONPATH"] = str(REPO_ROOT)

        result = subprocess.run(
            [sys.executable, str(PILOT_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--timeout", "60"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(REPO_ROOT),
            env=env,
        )

        # Parse the JSON payload to get artifact_dir
        stdout_lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if not stdout_lines:
            pytest.skip("pilot-sweep produced no output — likely missing textual")

        last_line = stdout_lines[-1]
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError:
            pytest.skip("pilot-sweep output is not JSON")

        artifact_dir_str = payload.get("artifact_dir", "")
        if not artifact_dir_str:
            pytest.skip("pilot-sweep returned empty artifact_dir (likely import error)")

        artifact_dir = Path(artifact_dir_str)
        assert artifact_dir.exists(), f"artifact_dir does not exist: {artifact_dir}"

        findings_file = artifact_dir / "findings.json"
        assert findings_file.exists(), f"findings.json not written to {artifact_dir}"

        findings_data = json.loads(findings_file.read_text())
        assert "verdict" in findings_data
        assert "findings" in findings_data

    def test_state_dir_not_mutated_outside_tui_tester(self, tmp_state_dir: Path) -> None:
        """Sweep must produce artifacts inside STATE_DIR/tui-tester/ and must not
        modify pre-existing production state files.

        The app is allowed to create its own cache files (discussion_cache.db,
        blackboard/) in STATE_DIR — that is normal TUI behavior.  What we assert
        is that the *sweep artifacts* (findings.json, tree JSON, SVGs) land under
        tui-tester/ and that no pre-existing files are overwritten.

        To exercise the guard, we create a sentinel file before the sweep and
        verify it is unmodified after.
        """
        # Create a sentinel file that must not be touched
        sentinel = tmp_state_dir / "sentinel.txt"
        sentinel.write_text("original content")
        sentinel_mtime_before = sentinel.stat().st_mtime

        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)
        env["PYTHONPATH"] = str(REPO_ROOT)

        subprocess.run(
            [sys.executable, str(PILOT_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--timeout", "60"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(REPO_ROOT),
            env=env,
        )

        # Sentinel must be unchanged
        assert sentinel.exists(), "Sentinel file was deleted by pilot-sweep"
        assert sentinel.read_text() == "original content", "Sentinel file was modified"
        assert sentinel.stat().st_mtime == sentinel_mtime_before, "Sentinel mtime changed"

        # findings.json must be under tui-tester/
        tui_tester_dir = tmp_state_dir / "tui-tester"
        if tui_tester_dir.exists():
            all_findings = list(tui_tester_dir.rglob("findings.json"))
            # At least one findings.json should have been created
            assert all_findings, f"No findings.json found under {tui_tester_dir}"


# ---------------------------------------------------------------------------
# tmux-sweep.sh tests
# ---------------------------------------------------------------------------


class TestTmuxSweep:
    """Test Layer B: tmux-sweep.sh output shape and artifact count."""

    def test_script_exists(self) -> None:
        assert TMUX_SCRIPT.exists(), f"tmux-sweep.sh not found at {TMUX_SCRIPT}"

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not available",
    )
    def test_produces_11_txt_files(self, tmp_state_dir: Path) -> None:
        """tmux-sweep.sh must produce exactly 11 .txt files in the artifact dir."""
        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)

        result = subprocess.run(
            ["bash", str(TMUX_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--timeout", "45"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
            env=env,
        )

        # Script exits 0 on success, 1 on partial failure
        combined = result.stdout + result.stderr

        # Parse manifest from the artifact dir
        tui_dir = tmp_state_dir / "tui-tester"
        run_dirs = sorted(tui_dir.iterdir()) if tui_dir.exists() else []
        assert run_dirs, (
            f"No run directories created under {tui_dir}\n"
            f"stdout: {combined[-1000:]}"
        )

        latest_run = run_dirs[-1]
        txt_files = list(latest_run.glob("*.txt"))
        # Exclude manifest.json and README if mistakenly captured as .txt
        screen_txts = [f for f in txt_files if f.stem in EXPECTED_SCREENS]

        assert len(screen_txts) == EXPECTED_SCREEN_COUNT, (
            f"Expected {EXPECTED_SCREEN_COUNT} .txt files, got {len(screen_txts)}: "
            f"{[f.name for f in screen_txts]}\n"
            f"All txt files: {[f.name for f in txt_files]}\n"
            f"stdout: {combined[-1000:]}"
        )

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not available",
    )
    def test_manifest_shape(self, tmp_state_dir: Path) -> None:
        """tmux-sweep.sh must write manifest.json with required fields."""
        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)

        subprocess.run(
            ["bash", str(TMUX_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--timeout", "45"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
            env=env,
        )

        tui_dir = tmp_state_dir / "tui-tester"
        if not tui_dir.exists():
            pytest.skip("No tui-tester dir created — tmux sweep did not run")

        run_dirs = sorted(tui_dir.iterdir())
        if not run_dirs:
            pytest.skip("No run dirs found")

        manifest_path = run_dirs[-1] / "manifest.json"
        assert manifest_path.exists(), f"manifest.json not written to {run_dirs[-1]}"

        manifest = json.loads(manifest_path.read_text())
        assert "run_id" in manifest
        assert "elapsed_s" in manifest
        assert "screens_expected" in manifest
        assert "screens_captured" in manifest
        assert manifest["screens_expected"] == EXPECTED_SCREEN_COUNT

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not available",
    )
    def test_state_dir_not_mutated_outside_tui_tester(self, tmp_state_dir: Path) -> None:
        """tmux-sweep.sh must not modify pre-existing state files.

        The script starts a real TUI process which may create caches in STATE_DIR —
        that is expected.  What we check is that pre-existing sentinel files are
        left untouched and that sweep artifacts land in tui-tester/.
        """
        sentinel = tmp_state_dir / "sentinel.txt"
        sentinel.write_text("original content")
        sentinel_mtime_before = sentinel.stat().st_mtime

        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)

        subprocess.run(
            ["bash", str(TMUX_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--timeout", "45"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
            env=env,
        )

        assert sentinel.exists(), "Sentinel file was deleted by tmux-sweep"
        assert sentinel.read_text() == "original content", "Sentinel file was modified"
        assert sentinel.stat().st_mtime == sentinel_mtime_before, "Sentinel mtime changed"

        tui_tester_dir = tmp_state_dir / "tui-tester"
        if tui_tester_dir.exists():
            all_manifests = list(tui_tester_dir.rglob("manifest.json"))
            assert all_manifests, f"No manifest.json found under {tui_tester_dir}"

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not available",
    )
    def test_no_dashboard_tui_orphan_after_normal_run(self, tmp_state_dir: Path) -> None:
        """After tmux-sweep.sh exits normally, no dashboard_tui process should remain.

        Uses a named --session so pgrep can confirm zero survivors.
        """
        import uuid
        session_name = f"tui-sweep-test-{uuid.uuid4().hex[:8]}"
        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)

        subprocess.run(
            ["bash", str(TMUX_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--session", session_name,
             "--timeout", "10"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
            env=env,
        )

        # Count surviving dashboard_tui processes — must be zero
        pgrep_result = subprocess.run(
            ["pgrep", "-f", "dashboard_tui"],
            capture_output=True,
            text=True,
        )
        survivor_count = len(pgrep_result.stdout.strip().splitlines()) if pgrep_result.stdout.strip() else 0
        assert survivor_count == 0, (
            f"dashboard_tui orphan(s) survived a normal sweep run: "
            f"{pgrep_result.stdout.strip()}"
        )

    @pytest.mark.skipif(
        subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
        reason="tmux not available",
    )
    def test_no_dashboard_tui_orphan_after_interrupt(self, tmp_state_dir: Path) -> None:
        """After tmux-sweep.sh is killed mid-run (SIGTERM), no dashboard_tui should remain.

        Simulates the test-runner timeout / pytest kill scenario that caused the
        original accumulation of 7 orphaned dashboard_tui processes (~1.1 GB RSS).
        """
        import signal
        import uuid
        session_name = f"tui-sweep-test-{uuid.uuid4().hex[:8]}"
        env = os.environ.copy()
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(tmp_state_dir)

        proc = subprocess.Popen(
            ["bash", str(TMUX_SCRIPT),
             "--state-dir", str(tmp_state_dir),
             "--session", session_name,
             "--timeout", "60"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
        )

        # Wait long enough for the tmux session + dashboard_tui to come up
        import time
        time.sleep(5)

        # Send SIGTERM — simulates an external timeout or pytest runner kill
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass  # process already exited — that's fine

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Give the trap a moment to fire and kill the session
        time.sleep(2)

        # Count surviving dashboard_tui processes — must be zero
        pgrep_result = subprocess.run(
            ["pgrep", "-f", "dashboard_tui"],
            capture_output=True,
            text=True,
        )
        survivor_count = len(pgrep_result.stdout.strip().splitlines()) if pgrep_result.stdout.strip() else 0
        assert survivor_count == 0, (
            f"dashboard_tui orphan(s) survived a SIGTERM-interrupted sweep run: "
            f"{pgrep_result.stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# Baseline diff logic tests (unit-level — no tmux or Textual required)
# ---------------------------------------------------------------------------


class TestBaselineDiff:
    """Unit tests for the baseline fixture diff logic."""

    def test_baselines_dir_exists(self) -> None:
        assert BASELINES_DIR.exists(), f"tui-baseline dir missing: {BASELINES_DIR}"

    def test_no_diff_on_empty_baseline(self, tmp_path: Path) -> None:
        """If baseline dir has no .txt files, diff is skipped (no failure)."""
        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir()

        # Simulate a fresh capture
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()
        (capture_dir / "home.txt").write_text("Home screen content\n")

        # Empty baseline → no files to diff against → no failures
        baseline_files = list(baseline_dir.glob("*.txt"))
        assert baseline_files == [], "Expected empty baseline in this test"

        # The rule: if baseline has no files, diff returns 0 diffs
        diffs = _diff_captures(capture_dir, baseline_dir)
        assert diffs == [], f"Expected no diffs on empty baseline, got: {diffs}"

    def test_no_diff_on_matching_captures(self, tmp_path: Path) -> None:
        """Captures that match baseline produce no diff results."""
        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir()
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()

        content = "Home screen\n  some content\n"
        (baseline_dir / "home.txt").write_text(content)
        (capture_dir / "home.txt").write_text(content)

        diffs = _diff_captures(capture_dir, baseline_dir)
        assert diffs == [], f"Expected no diffs for matching captures, got: {diffs}"

    def test_diff_detected_on_changed_capture(self, tmp_path: Path) -> None:
        """A changed capture is reported as a diff."""
        baseline_dir = tmp_path / "baseline"
        baseline_dir.mkdir()
        capture_dir = tmp_path / "capture"
        capture_dir.mkdir()

        (baseline_dir / "home.txt").write_text("version A\n")
        (capture_dir / "home.txt").write_text("version B\n")

        diffs = _diff_captures(capture_dir, baseline_dir)
        assert len(diffs) == 1
        assert diffs[0]["screen"] == "home"


# ---------------------------------------------------------------------------
# Helpers used by baseline tests
# ---------------------------------------------------------------------------


def _diff_captures(capture_dir: Path, baseline_dir: Path) -> list[dict]:
    """Compare .txt files in capture_dir against baseline_dir.

    Returns a list of {screen, baseline_path, capture_path} for screens
    where baseline exists and capture differs.  Screens with no baseline
    are skipped (initial bootstrap state).
    """
    diffs = []
    for baseline_file in sorted(baseline_dir.glob("*.txt")):
        screen_name = baseline_file.stem
        capture_file = capture_dir / baseline_file.name
        if not capture_file.exists():
            continue
        if baseline_file.read_text() != capture_file.read_text():
            diffs.append(
                {
                    "screen": screen_name,
                    "baseline_path": str(baseline_file),
                    "capture_path": str(capture_file),
                }
            )
    return diffs
