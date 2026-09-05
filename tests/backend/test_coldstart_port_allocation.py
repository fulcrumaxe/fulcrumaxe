"""Tests for backend/fleet/port_claim.py — scan-and-claim port allocation.

Acceptance Criteria:
1. claim_port picks 5100 for the first project (no existing state dirs).
2. claim_port picks 5101 for a second project when alpha already has 5100.
3. Idempotent re-run: claim_port returns the same port when project.json already has one.
4. Exhaustion: claim_port raises RuntimeError when all ports 5100..5999 are taken.
5. Lock path is created in ~/.autonomous-fleet-state/coldstart.lock.
6. Corrupt project.json in other state dirs does not crash the scan (skips it).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.fleet import port_claim
from backend.fleet.port_claim import (
    PORT_MIN,
    PORT_MAX,
    claim_port,
    _read_existing_port,
    _scan_taken_ports,
    _pick_free_port,
    _write_port,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_dir_with_port(tmp_path: Path, name: str, port: int) -> Path:
    sd = tmp_path / f".{name}-state"
    sd.mkdir(parents=True, exist_ok=True)
    pj = sd / "project.json"
    pj.write_text(json.dumps({"project_name": name, "dashboard_port": port, "version": 1}))
    return sd


# ---------------------------------------------------------------------------
# AC1: First project gets 5100
# ---------------------------------------------------------------------------


def test_first_project_gets_5100(tmp_path, monkeypatch):
    """With no existing state dirs, claim_port returns PORT_MIN (5100)."""
    state_dir = tmp_path / ".alpha-state"
    state_dir.mkdir()
    project_json = state_dir / "project.json"
    project_json.write_text(json.dumps({"project_name": "alpha", "version": 1}))

    # No other projects on disk
    monkeypatch.setattr(port_claim, "_LOCK_PATH", tmp_path / ".fleet-lock" / "coldstart.lock")
    (tmp_path / ".fleet-lock").mkdir(parents=True, exist_ok=True)

    with patch("glob.glob", return_value=[]):  # no other state dirs
        port = claim_port("alpha", state_dir)

    assert port == PORT_MIN
    data = json.loads(project_json.read_text())
    assert data["dashboard_port"] == PORT_MIN


# ---------------------------------------------------------------------------
# AC2: Second project gets 5101 when alpha has 5100
# ---------------------------------------------------------------------------


def test_second_project_gets_5101(tmp_path, monkeypatch):
    alpha_dir = _state_dir_with_port(tmp_path, "alpha", PORT_MIN)
    beta_dir = tmp_path / ".beta-state"
    beta_dir.mkdir()
    (beta_dir / "project.json").write_text(json.dumps({"project_name": "beta", "version": 1}))

    monkeypatch.setattr(port_claim, "_LOCK_PATH", tmp_path / ".fleet-lock" / "coldstart.lock")
    (tmp_path / ".fleet-lock").mkdir(parents=True, exist_ok=True)

    with patch("glob.glob", return_value=[str(alpha_dir / "project.json")]):
        port = claim_port("beta", beta_dir)

    assert port == PORT_MIN + 1
    data = json.loads((beta_dir / "project.json").read_text())
    assert data["dashboard_port"] == PORT_MIN + 1


# ---------------------------------------------------------------------------
# AC3: Idempotent re-run preserves existing port
# ---------------------------------------------------------------------------


def test_idempotent_rerun_preserves_port(tmp_path, monkeypatch):
    """Re-running claim_port on a project that already has a port returns same port."""
    alpha_dir = _state_dir_with_port(tmp_path, "alpha", 5150)

    monkeypatch.setattr(port_claim, "_LOCK_PATH", tmp_path / ".fleet-lock" / "coldstart.lock")
    (tmp_path / ".fleet-lock").mkdir(parents=True, exist_ok=True)

    # claim_port should return 5150 without modifying anything
    with patch("glob.glob", return_value=[]):
        port = claim_port("alpha", alpha_dir)

    assert port == 5150
    data = json.loads((alpha_dir / "project.json").read_text())
    assert data["dashboard_port"] == 5150


def test_idempotent_rerun_does_not_lock(tmp_path, monkeypatch):
    """Idempotent path returns early without acquiring the lock."""
    alpha_dir = _state_dir_with_port(tmp_path, "alpha", 5100)

    lock_acquired = []
    real_acquire = port_claim._acquire_lock

    def spy_acquire():
        lock_acquired.append(True)
        return real_acquire()

    monkeypatch.setattr(port_claim, "_acquire_lock", spy_acquire)
    monkeypatch.setattr(port_claim, "_LOCK_PATH", tmp_path / ".fleet-lock" / "coldstart.lock")
    (tmp_path / ".fleet-lock").mkdir(parents=True, exist_ok=True)

    with patch("glob.glob", return_value=[]):
        port_claim.claim_port("alpha", alpha_dir)

    # Lock should NOT have been acquired on idempotent path
    assert not lock_acquired


# ---------------------------------------------------------------------------
# AC4: Exhaustion raises RuntimeError
# ---------------------------------------------------------------------------


def test_exhaustion_raises_runtime_error():
    all_ports = set(range(PORT_MIN, PORT_MAX + 1))
    with pytest.raises(RuntimeError, match="All dashboard ports"):
        _pick_free_port(all_ports)


# ---------------------------------------------------------------------------
# AC6: Corrupt state dirs are skipped gracefully
# ---------------------------------------------------------------------------


def test_corrupt_state_dir_skipped_in_scan(tmp_path):
    """_scan_taken_ports skips corrupted project.json files without crashing."""
    bad_state = tmp_path / ".bad-state"
    bad_state.mkdir()
    (bad_state / "project.json").write_text("NOT JSON {{{{")

    good_state = tmp_path / ".good-state"
    good_state.mkdir()
    (good_state / "project.json").write_text(json.dumps({"dashboard_port": 5200}))

    glob_results = [
        str(bad_state / "project.json"),
        str(good_state / "project.json"),
    ]

    with patch("glob.glob", return_value=glob_results):
        taken = _scan_taken_ports()

    assert 5200 in taken
    # Corrupt file does not cause a crash; just omitted


# ---------------------------------------------------------------------------
# _read_existing_port
# ---------------------------------------------------------------------------


def test_read_existing_port_returns_none_when_missing(tmp_path):
    assert _read_existing_port(tmp_path / "nonexistent.json") is None


def test_read_existing_port_returns_none_for_corrupt_file(tmp_path):
    p = tmp_path / "project.json"
    p.write_text("NOT JSON")
    assert _read_existing_port(p) is None


def test_read_existing_port_returns_none_for_out_of_range(tmp_path):
    p = tmp_path / "project.json"
    p.write_text(json.dumps({"dashboard_port": 80}))  # out of range
    assert _read_existing_port(p) is None


def test_read_existing_port_returns_valid_port(tmp_path):
    p = tmp_path / "project.json"
    p.write_text(json.dumps({"dashboard_port": 5123}))
    assert _read_existing_port(p) == 5123


# ---------------------------------------------------------------------------
# _write_port
# ---------------------------------------------------------------------------


def test_write_port_creates_file_if_missing(tmp_path):
    p = tmp_path / "project.json"
    _write_port(p, 5100)
    data = json.loads(p.read_text())
    assert data["dashboard_port"] == 5100


def test_write_port_merges_with_existing(tmp_path):
    p = tmp_path / "project.json"
    p.write_text(json.dumps({"project_name": "alpha", "version": 1}))
    _write_port(p, 5100)
    data = json.loads(p.read_text())
    assert data["dashboard_port"] == 5100
    assert data["project_name"] == "alpha"  # preserved
    assert data["version"] == 1  # preserved


# ---------------------------------------------------------------------------
# _pick_free_port
# ---------------------------------------------------------------------------


def test_pick_free_port_returns_min_when_empty():
    assert _pick_free_port(set()) == PORT_MIN


def test_pick_free_port_skips_taken():
    taken = {PORT_MIN, PORT_MIN + 1, PORT_MIN + 2}
    assert _pick_free_port(taken) == PORT_MIN + 3


def test_pick_free_port_handles_sparse_gaps():
    # Take all ports except 5250
    taken = set(range(PORT_MIN, 5250)) | set(range(5251, PORT_MAX + 1))
    assert _pick_free_port(taken) == 5250
