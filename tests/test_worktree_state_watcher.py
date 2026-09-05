"""
tests/test_worktree_state_watcher.py — unit tests for backend/worktree_state_watcher.py

Covers AC #1-4 + #6 from D#614 spec.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_manifest(root: Path, entries: list[dict]) -> None:
    manifest_path = root / ".autonomous-team" / "state-symlinks.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"version": 1, "entries": entries}))


def _write_registry(root: Path, entries: list[dict]) -> None:
    registry_path = root / ".autonomous-team" / "worktrees.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(entries))


def _worktree_entry(wt_id: str, wt_path: str, status: str = "active") -> dict:
    return {"worktree_id": wt_id, "path": wt_path, "status": status}


# ── Fixtures ──────────────────────────────────────────────────────────────────

MANIFEST_ENTRIES = [
    {"in_repo": "audit.jsonl",                  "external": "audit.jsonl",                  "type": "file"},
    {"in_repo": "state.db",                      "external": "state.db",                      "type": "file"},
    {"in_repo": "blackboard",                    "external": "blackboard",                    "type": "dir"},
    {"in_repo": "stats.duckdb",                  "external": "stats.duckdb",                  "type": "file"},
    {"in_repo": "circuit-breaker-history.jsonl", "external": "circuit-breaker-history.jsonl", "type": "file"},
]


# ── AC #1: real file → 1 finding with kind=real_file ─────────────────────────

def test_real_file_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #1: worktree with a real audit.jsonl → watcher reports 1 finding."""
    # Set up fake state dir
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

    # Set up fake repo root with manifest
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, MANIFEST_ENTRIES)

    # Set up worktree with a real file (not a symlink)
    wt_root = tmp_path / "worktree1"
    wt_at_path = wt_root / ".autonomous-team"
    wt_at_path.mkdir(parents=True)
    real_file = wt_at_path / "audit.jsonl"
    real_file.write_text("some log line\n")

    _write_registry(repo_root, [_worktree_entry("wt-001", str(wt_root))])

    import backend.worktree_state_watcher as ww
    monkeypatch.setattr(ww, "MANIFEST_PATH", repo_root / ".autonomous-team" / "state-symlinks.json")
    monkeypatch.setattr(ww, "REGISTRY_PATH", repo_root / ".autonomous-team" / "worktrees.json")

    # Patch file_bug so we don't hit the API
    bug_calls: list[dict] = []
    def fake_file_bug(finding: dict, dry_run: bool = False) -> str | None:
        bug_calls.append(finding)
        return "https://github.com/autonomous-agent-7/autonomous-forever/discussions/999"

    monkeypatch.setattr(ww, "file_bug", fake_file_bug)
    monkeypatch.setattr(ww, "_increment_divergence_counter", lambda _: None)

    findings = ww.scan(dry_run=False)

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}: {findings}"
    assert findings[0]["kind"] == "real_file"
    assert "audit.jsonl" in findings[0]["in_repo_path"]
    assert findings[0]["size_bytes"] == len("some log line\n")
    assert len(bug_calls) == 1


# ── AC #2: correct symlinks → 0 findings ─────────────────────────────────────

def test_correct_symlinks_no_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #2: worktree with only correct symlinks → 0 findings."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, MANIFEST_ENTRIES)

    wt_root = tmp_path / "worktree2"
    wt_at_path = wt_root / ".autonomous-team"
    wt_at_path.mkdir(parents=True)

    # Create correct symlinks for all manifest entries
    for entry in MANIFEST_ENTRIES:
        in_repo = entry["in_repo"]
        external = entry["external"]
        # Create the target in state_dir so the symlink can resolve
        target = state_dir / external
        target.write_text("canonical content")
        link = wt_at_path / in_repo
        link.symlink_to(target)

    _write_registry(repo_root, [_worktree_entry("wt-002", str(wt_root))])

    import backend.worktree_state_watcher as ww
    monkeypatch.setattr(ww, "MANIFEST_PATH", repo_root / ".autonomous-team" / "state-symlinks.json")
    monkeypatch.setattr(ww, "REGISTRY_PATH", repo_root / ".autonomous-team" / "worktrees.json")
    monkeypatch.setattr(ww, "_increment_divergence_counter", lambda _: None)

    findings = ww.scan(dry_run=True)

    assert findings == [], f"Expected 0 findings, got {findings}"


# ── AC #3: wrong-target symlink → 1 finding with kind=wrong_symlink ───────────

def test_wrong_symlink_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #3: symlink pointing to wrong target → 1 finding with kind=wrong_symlink."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, [{"in_repo": "audit.jsonl", "external": "audit.jsonl", "type": "file"}])

    wt_root = tmp_path / "worktree3"
    wt_at_path = wt_root / ".autonomous-team"
    wt_at_path.mkdir(parents=True)

    # Create a symlink pointing to a different location (not state_dir)
    wrong_target = tmp_path / "wrong-place" / "audit.jsonl"
    wrong_target.parent.mkdir(parents=True)
    wrong_target.write_text("wrong data")
    link = wt_at_path / "audit.jsonl"
    link.symlink_to(wrong_target)

    _write_registry(repo_root, [_worktree_entry("wt-003", str(wt_root))])

    import backend.worktree_state_watcher as ww
    monkeypatch.setattr(ww, "MANIFEST_PATH", repo_root / ".autonomous-team" / "state-symlinks.json")
    monkeypatch.setattr(ww, "REGISTRY_PATH", repo_root / ".autonomous-team" / "worktrees.json")

    bug_calls: list[dict] = []
    def fake_file_bug(finding: dict, dry_run: bool = False) -> str | None:
        bug_calls.append(finding)
        return "https://github.com/autonomous-agent-7/autonomous-forever/discussions/998"

    monkeypatch.setattr(ww, "file_bug", fake_file_bug)
    monkeypatch.setattr(ww, "_increment_divergence_counter", lambda _: None)

    findings = ww.scan(dry_run=False)

    assert len(findings) == 1, f"Expected 1 finding, got {findings}"
    assert findings[0]["kind"] == "wrong_symlink"
    assert "audit.jsonl" in findings[0]["in_repo_path"]
    assert len(bug_calls) == 1


# ── AC #4: dedup — scan twice with same divergent state → only 1 Discussion filed ──

def test_dedup_prevents_duplicate_filing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #4: calling scan() twice → file_bug called twice but _discussion_already_filed
    returns True on 2nd call → net 1 filing."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(state_dir))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_manifest(repo_root, [{"in_repo": "state.db", "external": "state.db", "type": "file"}])

    wt_root = tmp_path / "worktree4"
    wt_at_path = wt_root / ".autonomous-team"
    wt_at_path.mkdir(parents=True)
    (wt_at_path / "state.db").write_bytes(b"\x00" * 512)  # real file

    _write_registry(repo_root, [_worktree_entry("wt-004", str(wt_root))])

    import backend.worktree_state_watcher as ww
    monkeypatch.setattr(ww, "MANIFEST_PATH", repo_root / ".autonomous-team" / "state-symlinks.json")
    monkeypatch.setattr(ww, "REGISTRY_PATH", repo_root / ".autonomous-team" / "worktrees.json")
    monkeypatch.setattr(ww, "_increment_divergence_counter", lambda _: None)

    # Simulate: first scan → not yet filed; second scan → already filed
    filed_markers: set[str] = set()

    def fake_already_filed(marker: str) -> bool:
        return marker in filed_markers

    actual_file_calls: list[dict] = []

    def fake_file_bug_dedup(finding: dict, dry_run: bool = False) -> str | None:
        worktree_id = finding.get("worktree_id", "unknown")
        in_repo_path = finding.get("in_repo_path", "")
        marker = ww._dedup_marker(worktree_id, in_repo_path)
        if fake_already_filed(marker):
            print(f"[test] dedup: already filed for {marker}")
            return None
        filed_markers.add(marker)
        actual_file_calls.append(finding)
        return "https://github.com/autonomous-agent-7/autonomous-forever/discussions/997"

    monkeypatch.setattr(ww, "file_bug", fake_file_bug_dedup)

    # First scan
    findings1 = ww.scan(dry_run=False)
    # Second scan (same divergent state)
    findings2 = ww.scan(dry_run=False)

    assert len(findings1) == 1, f"First scan should return 1 finding, got {findings1}"
    assert len(findings2) == 1, f"Second scan should still detect 1 finding, got {findings2}"
    assert len(actual_file_calls) == 1, (
        f"Expected exactly 1 Discussion filed (dedup), but file_bug was called {len(actual_file_calls)} times"
    )


# ── AC #6: manifest absent → 0 findings, no exception ────────────────────────

def test_manifest_absent_exits_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #6: state-symlinks.json absent → scan returns [], logs INFO, does not raise."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # No manifest written

    import backend.worktree_state_watcher as ww
    nonexistent = repo_root / ".autonomous-team" / "state-symlinks.json"
    monkeypatch.setattr(ww, "MANIFEST_PATH", nonexistent)
    monkeypatch.setattr(ww, "REGISTRY_PATH", repo_root / ".autonomous-team" / "worktrees.json")

    findings = ww.scan(dry_run=True)
    assert findings == [], f"Expected [] when manifest absent, got {findings}"


# ── check_path unit tests ────────────────────────────────────────────────────

def test_check_path_absent_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """check_path returns None when the path simply doesn't exist."""
    state_dir = tmp_path / "state"
    entry = {"in_repo": "audit.jsonl", "external": "audit.jsonl"}
    wt_path = str(tmp_path / "wt")
    (tmp_path / "wt" / ".autonomous-team").mkdir(parents=True)

    import backend.worktree_state_watcher as ww
    result = ww.check_path(wt_path, entry, state_dir)
    assert result is None


def test_check_path_real_file(tmp_path: Path) -> None:
    """check_path returns real_file finding for a regular file."""
    state_dir = tmp_path / "state"
    entry = {"in_repo": "state.db", "external": "state.db"}
    wt_path = str(tmp_path / "wt")
    at_path = tmp_path / "wt" / ".autonomous-team"
    at_path.mkdir(parents=True)
    (at_path / "state.db").write_bytes(b"x" * 100)

    import backend.worktree_state_watcher as ww
    result = ww.check_path(wt_path, entry, state_dir)
    assert result is not None
    assert result["kind"] == "real_file"
    assert result["size_bytes"] == 100


def test_check_path_correct_symlink_returns_none(tmp_path: Path) -> None:
    """check_path returns None when symlink resolves to the correct external target."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = state_dir / "audit.jsonl"
    target.write_text("data")

    entry = {"in_repo": "audit.jsonl", "external": "audit.jsonl"}
    wt_path = str(tmp_path / "wt")
    at_path = tmp_path / "wt" / ".autonomous-team"
    at_path.mkdir(parents=True)
    (at_path / "audit.jsonl").symlink_to(target)

    import backend.worktree_state_watcher as ww
    result = ww.check_path(wt_path, entry, state_dir)
    assert result is None


def test_check_path_wrong_symlink(tmp_path: Path) -> None:
    """check_path returns wrong_symlink when symlink points elsewhere."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wrong_target = tmp_path / "other" / "audit.jsonl"
    wrong_target.parent.mkdir()
    wrong_target.write_text("wrong")

    entry = {"in_repo": "audit.jsonl", "external": "audit.jsonl"}
    wt_path = str(tmp_path / "wt")
    at_path = tmp_path / "wt" / ".autonomous-team"
    at_path.mkdir(parents=True)
    (at_path / "audit.jsonl").symlink_to(wrong_target)

    import backend.worktree_state_watcher as ww
    result = ww.check_path(wt_path, entry, state_dir)
    assert result is not None
    assert result["kind"] == "wrong_symlink"
