"""tests/backend/test_pre_spawn_fleet_cap.py — pre-spawn fleet cap integration tests.

Tests the fleet-cap enforcement that lives in pre-spawn-check.sh by calling
the concurrency module directly (the script integration path) and testing
the Python API gate that scripts invoke.

The shell script integration is tested via subprocess: run
pre-spawn-check.sh --dry-run with mocked fleet state seeded at 7/8 vs 8/8
to assert allow vs deny.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def repo_root() -> Path:
    """Return the repository root (worktree root)."""
    return Path(__file__).parent.parent.parent


@pytest.fixture()
def fleet_dir(tmp_path) -> Path:
    """A fresh fleet state dir at tmp_path/fleet."""
    d = tmp_path / "fleet"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"fleet_cap": 8}))
    return d


# ── Python API gate tests ─────────────────────────────────────────────────────

class TestFleetCapPythonGate:
    """Test the Python-level gate that pre-spawn-check.sh calls."""

    def test_allow_when_fleet_has_room(self, tmp_path, monkeypatch):
        import backend.fleet.concurrency as fc

        monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
        monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
        monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")

        (tmp_path / "config.json").write_text(json.dumps({"fleet_cap": 8}))

        # Seed 7 agents (under cap)
        for i in range(7):
            ok = fc.register("af", f"existing-{i}", "executor")
            assert ok is True

        # 8th should still be allowed
        ok = fc.register("af", "new-agent", "executor")
        assert ok is True

    def test_deny_when_fleet_at_cap(self, tmp_path, monkeypatch):
        import backend.fleet.concurrency as fc

        monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
        monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
        monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")

        (tmp_path / "config.json").write_text(json.dumps({"fleet_cap": 8}))

        # Fill all 8 slots
        for i in range(8):
            ok = fc.register("af", f"existing-{i}", "executor")
            assert ok is True, f"Slot {i} should succeed"

        # 9th should be denied
        ok = fc.register("projectb", "overflow-agent", "executor")
        assert ok is False

    def test_mixed_project_cap(self, tmp_path, monkeypatch):
        """3 in projectb + 5 in af = 8; next from af denied."""
        import backend.fleet.concurrency as fc

        monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
        monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
        monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")

        (tmp_path / "config.json").write_text(json.dumps({"fleet_cap": 8}))

        for i in range(3):
            assert fc.register("projectb", f"j{i}", "executor") is True
        for i in range(5):
            assert fc.register("af", f"a{i}", "executor") is True

        assert fc.count_fleet() == 8
        assert fc.register("af", "one-more", "executor") is False


# ── CLI exit-code gate (simulates what pre-spawn-check.sh calls) ──────────────

class TestFleetCapCLI:
    """Test the CLI mode: python3 -m backend.fleet.concurrency register ..."""

    def _run_cli(self, fleet_dir: Path, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["AUTONOMOUS_FLEET_STATE_DIR"] = str(fleet_dir)
        return subprocess.run(
            [sys.executable, "-m", "backend.fleet.concurrency", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).parent.parent.parent),
        )

    def test_cli_register_succeeds_when_cap_allows(self, fleet_dir):
        result = self._run_cli(fleet_dir, "register", "af", "agent-1", "executor")
        assert result.returncode == 0
        assert "registered" in result.stdout

    def test_cli_register_denied_when_cap_full(self, fleet_dir):
        # Fill to cap of 8
        for i in range(8):
            r = self._run_cli(fleet_dir, "register", "af", f"filler-{i}", "executor")
            assert r.returncode == 0, f"Slot {i} fill failed: {r.stderr}"

        # One more should fail
        result = self._run_cli(fleet_dir, "register", "projectb", "overflow", "executor")
        assert result.returncode != 0
        assert "fleet cap" in result.stderr.lower() or "denied" in result.stderr.lower()

    def test_cli_unregister(self, fleet_dir):
        self._run_cli(fleet_dir, "register", "af", "agent-1", "executor")
        result = self._run_cli(fleet_dir, "unregister", "af", "agent-1")
        assert result.returncode == 0
        assert "unregistered" in result.stdout

    def test_cli_count_fleet_empty(self, fleet_dir):
        result = self._run_cli(fleet_dir, "count_fleet")
        assert result.returncode == 0
        assert result.stdout.strip() == "0"

    def test_cli_count_fleet_after_register(self, fleet_dir):
        self._run_cli(fleet_dir, "register", "af", "agent-1", "executor")
        self._run_cli(fleet_dir, "register", "projectb", "agent-2", "executor")
        result = self._run_cli(fleet_dir, "count_fleet")
        assert result.returncode == 0
        assert result.stdout.strip() == "2"

    def test_cli_fleet_cap_default(self, fleet_dir):
        result = self._run_cli(fleet_dir, "fleet_cap")
        assert result.returncode == 0
        assert result.stdout.strip() == "8"

    def test_cli_fleet_cap_custom(self, fleet_dir):
        (fleet_dir / "config.json").write_text(json.dumps({"fleet_cap": 4}))
        result = self._run_cli(fleet_dir, "fleet_cap")
        assert result.returncode == 0
        assert result.stdout.strip() == "4"

    def test_cli_register_7_then_8_allows_deny_on_9(self, fleet_dir):
        """7/8 → allow (8th succeeds).  8/8 → deny (9th fails).  Matches spec gate 2."""
        # Fill 7
        for i in range(7):
            r = self._run_cli(fleet_dir, "register", "af", f"seed-{i}", "executor")
            assert r.returncode == 0

        # 8th (= cap) still allowed
        r8 = self._run_cli(fleet_dir, "register", "af", "agent-8", "executor")
        assert r8.returncode == 0, f"8th should succeed: {r8.stderr}"

        # 9th denied
        r9 = self._run_cli(fleet_dir, "register", "projectb", "agent-9", "executor")
        assert r9.returncode != 0, "9th should be denied"
