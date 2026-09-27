"""Tests for backend/agent_retros.py's write-target resolution and failure handling (D#2532).

Covers:
- RETROS_FILE default resolves via backend.state_paths.AGENT_RETROS
  ($AUTONOMOUS_TEAM_STATE_DIR/agent-retros.jsonl), not a path templated by
  this module itself.
- AF_RETROS_FILE keeps overriding the default (same precedent as
  STATS_DB_PATH in backend/state_paths.py).
- append_retro() raises RetroWriteError (not a silent False) when the
  underlying write fails — distinct from a dedup skip, which is a normal
  outcome.
- cmd_append() records a countable skip metric (backend/stats/retro_skip.py)
  when the write fails, so the failure is visible outside the calling
  agent's own AGENT_OUTPUT envelope.
- load_retros() merges the legacy in-repo location with the current one, so
  rows written before this fix are not silently dropped.
- append_retro()'s existing dedup behaviour survives the refactor.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import state_paths  # noqa: E402
import backend.agent_retros as ar  # noqa: E402


# ---------------------------------------------------------------------------
# Write-target resolution
# ---------------------------------------------------------------------------


def test_default_retros_file_uses_state_paths(monkeypatch, tmp_path):
    """With no AF_RETROS_FILE override, the default comes from state_paths —
    not a path this module templates itself."""
    monkeypatch.delenv("AF_RETROS_FILE", raising=False)
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    assert ar._default_retros_file() == tmp_path / "agent-retros.jsonl"


def test_af_retros_file_override_wins(monkeypatch, tmp_path):
    override = tmp_path / "custom-retros.jsonl"
    monkeypatch.setenv("AF_RETROS_FILE", str(override))
    assert ar._default_retros_file() == override


def test_state_paths_agent_retros_resolves_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    assert state_paths.AGENT_RETROS == tmp_path / "agent-retros.jsonl"


# ---------------------------------------------------------------------------
# append_retro() / dedup
# ---------------------------------------------------------------------------


def test_append_retro_writes_and_dedups(tmp_path, monkeypatch):
    target = tmp_path / "agent-retros.jsonl"
    monkeypatch.setattr(ar, "RETROS_FILE", target)
    entry = {
        "ts": "2026-01-01T00:00:00Z",
        "agent_id": "a1",
        "classifier": "c1",
        "turn_idx": 1,
    }
    assert ar.append_retro(dict(entry)) is True
    assert ar.append_retro(dict(entry)) is False  # dedup, not a failure
    lines = target.read_text().strip().splitlines()
    assert len(lines) == 1


def test_append_retro_raises_on_write_failure(tmp_path, monkeypatch):
    """A blocked/failed write is a RetroWriteError, not a silent False —
    False is reserved for the normal dedup-skip outcome."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")  # a file where append_retro expects a directory
    bad_path = blocker / "agent-retros.jsonl"
    monkeypatch.setattr(ar, "RETROS_FILE", bad_path)

    with pytest.raises(ar.RetroWriteError):
        ar.append_retro({"agent_id": "a", "classifier": "c", "turn_idx": 0})


# ---------------------------------------------------------------------------
# cmd_append() — the skip is countable (item 5/6)
# ---------------------------------------------------------------------------


def _append_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        agent_id="agent-1",
        role="executor",
        classifier="test_classifier",
        trigger="t",
        why="w",
        future_fix="f",
        work_corrected=False,
        shadow_mode=False,
        turn_idx=0,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_append_records_skip_metric_on_write_failure(tmp_path, monkeypatch):
    """This is the mutation-sensitive test for item 6: remove the
    _record_skip() call in cmd_append's except branch and this goes red,
    because `recorded` never gets populated."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    bad_path = blocker / "agent-retros.jsonl"
    monkeypatch.setattr(ar, "RETROS_FILE", bad_path)

    recorded: dict = {}

    def fake_record_retro_skip(reason, role=None):
        recorded["reason"] = reason
        recorded["role"] = role

    monkeypatch.setattr(
        "backend.stats.retro_skip.record_retro_skip", fake_record_retro_skip
    )

    rc = ar.cmd_append(_append_args(role="executor"))

    assert rc == 1
    assert recorded == {"reason": "retro_write_failed", "role": "executor"}


def test_cmd_append_skip_metric_failure_does_not_crash_cli(tmp_path, monkeypatch):
    """_record_skip() is best-effort: if the metric write itself blows up
    (e.g. duckdb unavailable), cmd_append must still return its own error
    code rather than raising."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    bad_path = blocker / "agent-retros.jsonl"
    monkeypatch.setattr(ar, "RETROS_FILE", bad_path)

    def boom(reason, role=None):
        raise RuntimeError("duckdb not installed")

    monkeypatch.setattr("backend.stats.retro_skip.record_retro_skip", boom)

    rc = ar.cmd_append(_append_args())
    assert rc == 1


def test_cmd_append_success_does_not_record_skip(tmp_path, monkeypatch):
    target = tmp_path / "agent-retros.jsonl"
    monkeypatch.setattr(ar, "RETROS_FILE", target)

    calls = []
    monkeypatch.setattr(
        "backend.stats.retro_skip.record_retro_skip",
        lambda reason, role=None: calls.append(reason),
    )

    rc = ar.cmd_append(_append_args())
    assert rc == 0
    assert calls == []


# ---------------------------------------------------------------------------
# load_retros() — dual-location read (item 7)
# ---------------------------------------------------------------------------


def test_load_retros_merges_legacy_and_current_locations(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy" / "agent-retros.jsonl"
    current = tmp_path / "current" / "agent-retros.jsonl"
    legacy.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {"ts": "2026-01-01T00:00:00Z", "agent_id": "old1", "classifier": "c_old"}
        )
        + "\n"
    )
    current.write_text(
        json.dumps(
            {"ts": "2026-02-01T00:00:00Z", "agent_id": "new1", "classifier": "c_new"}
        )
        + "\n"
    )
    monkeypatch.setattr(ar, "LEGACY_RETROS_FILE", legacy)
    monkeypatch.setattr(ar, "RETROS_FILE", current)

    entries = ar.load_retros()
    assert [e["agent_id"] for e in entries] == ["old1", "new1"]


def test_load_retros_does_not_double_read_when_paths_are_equal(tmp_path, monkeypatch):
    same = tmp_path / "agent-retros.jsonl"
    same.write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "agent_id": "x", "classifier": "c"})
        + "\n"
    )
    monkeypatch.setattr(ar, "LEGACY_RETROS_FILE", same)
    monkeypatch.setattr(ar, "RETROS_FILE", same)

    entries = ar.load_retros()
    assert len(entries) == 1


def test_load_retros_missing_files_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "LEGACY_RETROS_FILE", tmp_path / "nope-1.jsonl")
    monkeypatch.setattr(ar, "RETROS_FILE", tmp_path / "nope-2.jsonl")
    assert ar.load_retros() == []
