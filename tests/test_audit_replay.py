"""Tests for scripts/audit-replay.sh — dial-row chain verification.

Covers:
  - intact dial chain → exit 0, "OK"
  - tampered dial row → exit 1, names the broken row
  - mixed file with control-plane rows → exit 0 (the bug being fixed)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "audit-replay.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dial_row(prev_line_bytes: bytes, class_name: str = "agent.spawn", new_level: int = 4) -> dict:
    """Build a dial_change row whose prev_hash is the SHA-256 of prev_line_bytes."""
    return {
        "kind": "dial_change",
        "prev_hash": hashlib.sha256(prev_line_bytes).hexdigest(),
        "class": class_name,
        "prev_level": new_level,
        "new_level": new_level,
        "source": {"kind": "system"},
        "ttl_until": None,
        "timestamp": "2026-05-19T12:00:00+00:00",
    }


def _control_plane_row(seq: int = 1) -> dict:
    """Build a control-plane row (no prev_hash field)."""
    return {
        "ts": "2026-05-19T12:00:00Z",
        "source": "test",
        "action": "set",
        "key": "gates.lint_must_pass",
        "old": "true",
        "new": "false",
        "actor": "test",
        "seq": seq,
    }


def _write_audit(rows: list[dict], tmpdir: str) -> str:
    """Write rows as JSONL to a temp audit.jsonl; return the state-dir path."""
    audit_path = Path(tmpdir) / "audit.jsonl"
    lines = [json.dumps(r) + "\n" for r in rows]
    audit_path.write_text("".join(lines), encoding="utf-8")
    return tmpdir


def _run(state_dir: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["AUTONOMOUS_TEAM_STATE_DIR"] = state_dir
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_intact_dial_chain_exits_0():
    """An intact chain of dial rows reports OK and exits 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # First dial row uses "genesis" sentinel — written when file was empty
        row0 = {
            "kind": "dial_change",
            "prev_hash": "genesis",
            "class": "agent.spawn",
            "prev_level": 4,
            "new_level": 4,
            "source": {"kind": "system"},
            "ttl_until": None,
            "timestamp": "2026-05-19T10:00:00+00:00",
        }
        line0 = (json.dumps(row0) + "\n").encode()
        row1 = _dial_row(line0.rstrip(b"\n"))
        line1 = (json.dumps(row1) + "\n").encode()
        row2 = _dial_row(line1.rstrip(b"\n"))

        _write_audit([row0, row1, row2], tmpdir)
        result = _run(tmpdir)

        assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "OK" in result.stdout


def test_tampered_dial_row_exits_1():
    """A chain where one dial row has a wrong prev_hash exits 1 and names the row."""
    with tempfile.TemporaryDirectory() as tmpdir:
        row0 = {
            "kind": "dial_change",
            "prev_hash": "genesis",
            "class": "agent.spawn",
            "prev_level": 4,
            "new_level": 4,
            "source": {"kind": "system"},
            "ttl_until": None,
            "timestamp": "2026-05-19T10:00:00+00:00",
        }
        # Tamper: use a hash that doesn't point to any real line in the file
        row1_bad = {
            "kind": "dial_change",
            "prev_hash": "deadbeef" * 8,  # wrong — doesn't match sha256 of any row
            "class": "agent.spawn",
            "prev_level": 4,
            "new_level": 4,
            "source": {"kind": "system"},
            "ttl_until": None,
            "timestamp": "2026-05-19T11:00:00+00:00",
        }

        _write_audit([row0, row1_bad], tmpdir)
        result = _run(tmpdir)

        assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        # Should name "dial row 1"
        assert "BROKEN" in result.stdout
        assert "dial row 1" in result.stdout


def test_mixed_file_with_control_plane_rows_exits_0():
    """Mixed audit.jsonl with control-plane rows interspersed still passes — the original bug."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Build audit.jsonl that looks like the live file:
        # many control-plane rows, then some dial rows scattered in
        rows: list[dict] = []

        # 10 control-plane rows (no prev_hash)
        for i in range(10):
            rows.append(_control_plane_row(seq=i + 1))

        # First dial row — uses "genesis" sentinel since it was the first ever written
        row_d0 = {
            "kind": "dial_change",
            "prev_hash": "genesis",
            "class": "agent.spawn",
            "prev_level": 4,
            "new_level": 4,
            "source": {"kind": "system"},
            "ttl_until": None,
            "timestamp": "2026-05-19T10:00:00+00:00",
        }
        rows.append(row_d0)
        line_d0 = (json.dumps(row_d0) + "\n").encode()

        # 5 more control-plane rows between dial rows
        for i in range(5):
            rows.append(_control_plane_row(seq=100 + i))

        # Second dial row (chain link off first dial row)
        row_d1 = _dial_row(line_d0.rstrip(b"\n"))
        rows.append(row_d1)
        line_d1 = (json.dumps(row_d1) + "\n").encode()

        # 3 more control-plane rows
        for i in range(3):
            rows.append(_control_plane_row(seq=200 + i))

        # Third dial row
        row_d2 = _dial_row(line_d1.rstrip(b"\n"))
        rows.append(row_d2)

        _write_audit(rows, tmpdir)
        result = _run(tmpdir)

        assert result.returncode == 0, (
            f"Got non-zero exit on a valid mixed file (false-positive bug).\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert "OK" in result.stdout
        assert "3 dial rows" in result.stdout


def test_empty_audit_file_exits_0():
    """An empty audit file returns OK (nothing to verify)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_audit([], tmpdir)
        result = _run(tmpdir)
        assert result.returncode == 0
        assert "OK" in result.stdout


def test_no_dial_rows_exits_0():
    """Audit file with only control-plane rows (no dial rows) returns OK."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rows = [_control_plane_row(seq=i) for i in range(20)]
        _write_audit(rows, tmpdir)
        result = _run(tmpdir)
        assert result.returncode == 0, f"stdout={result.stdout!r}"
        assert "OK" in result.stdout
