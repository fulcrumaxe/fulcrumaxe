"""Targeted tests for the git-aware orphan_worktrees check.

These tests cover the acceptance criteria from Discussion #1208:
  AC1: attached + locked + old → NOT flagged
  AC2: attached + branch + old → NOT flagged
  AC3: attached + detached + old → NOT flagged
  AC4: absent from list + old → FLAGGED
  AC5: absent from list + young → NOT flagged (under 4h threshold)
  AC6: all present or young → ok:True with 0 orphans
  AC7: worktrees dir absent → ok:True unchanged
  AC8: git failure → mtime-only fallback with visible suffix
  AC9: detail lists up to 5 offenders + total count

Tests monkeypatch ``backend.health_report._git_worktree_paths`` and
``backend.health_report._WORKTREES_DIR`` so no real subprocess or filesystem
mutation is needed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

import backend.health_report as hr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = time.time()
_OLD = _NOW - 10 * 3600   # 10 hours ago — above the 4h threshold
_YOUNG = _NOW - 1 * 3600  # 1 hour ago — below the 4h threshold


def _make_dir(tmp_path: Path, name: str, mtime: float) -> Path:
    """Create a directory under tmp_path and stamp its mtime."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    os.utime(str(d), (mtime, mtime))
    return d


def _run(worktrees_dir: Path, live_paths: Optional[set[Path]]) -> dict:
    """Patch globals and run check_orphan_worktrees."""
    with patch.object(hr, "_WORKTREES_DIR", worktrees_dir):
        with patch.object(hr, "_git_worktree_paths", return_value=live_paths):
            return hr.check_orphan_worktrees()


# ---------------------------------------------------------------------------
# AC1: attached + locked + old → NOT flagged
# ---------------------------------------------------------------------------

def test_ac1_locked_old_not_flagged(tmp_path):
    d = _make_dir(tmp_path, "agent-locked", _OLD)
    result = _run(tmp_path, {d.resolve()})
    assert result["ok"] is True
    assert "agent-locked" not in result.get("detail", "")


# ---------------------------------------------------------------------------
# AC2: attached + branch (no locked line) + old → NOT flagged
# ---------------------------------------------------------------------------

def test_ac2_branch_old_not_flagged(tmp_path):
    d = _make_dir(tmp_path, "agent-branch", _OLD)
    # Presence in live set is all that matters; "locked" / "branch" are parsing details
    result = _run(tmp_path, {d.resolve()})
    assert result["ok"] is True
    assert "agent-branch" not in result.get("detail", "")


# ---------------------------------------------------------------------------
# AC3: attached + detached + old → NOT flagged
# ---------------------------------------------------------------------------

def test_ac3_detached_old_not_flagged(tmp_path):
    d = _make_dir(tmp_path, "agent-detached", _OLD)
    result = _run(tmp_path, {d.resolve()})
    assert result["ok"] is True
    assert "agent-detached" not in result.get("detail", "")


# ---------------------------------------------------------------------------
# AC4: absent from list + old → FLAGGED
# ---------------------------------------------------------------------------

def test_ac4_absent_old_flagged(tmp_path):
    _make_dir(tmp_path, "agent-stale", _OLD)
    # Live set is empty — nothing is registered
    result = _run(tmp_path, set())
    assert result["ok"] is False
    assert "agent-stale" in result["detail"]


# ---------------------------------------------------------------------------
# AC5: absent from list + young → NOT flagged
# ---------------------------------------------------------------------------

def test_ac5_absent_young_not_flagged(tmp_path):
    _make_dir(tmp_path, "agent-new", _YOUNG)
    result = _run(tmp_path, set())
    assert result["ok"] is True
    assert "agent-new" not in result.get("detail", "")


# ---------------------------------------------------------------------------
# AC6: all present or young → ok:True, 0 orphans
# ---------------------------------------------------------------------------

def test_ac6_all_present_or_young(tmp_path):
    old_attached = _make_dir(tmp_path, "agent-old-but-live", _OLD)
    _make_dir(tmp_path, "agent-young-absent", _YOUNG)
    result = _run(tmp_path, {old_attached.resolve()})
    assert result["ok"] is True
    assert "0 orphan" in result["detail"]


# ---------------------------------------------------------------------------
# AC7: worktrees dir absent → ok:True (unchanged behavior)
# ---------------------------------------------------------------------------

def test_ac7_worktrees_dir_absent(tmp_path):
    missing = tmp_path / "no_such_dir"
    with patch.object(hr, "_WORKTREES_DIR", missing):
        result = hr.check_orphan_worktrees()
    assert result["ok"] is True
    assert result["detail"] == "worktrees dir absent (clean)"


# ---------------------------------------------------------------------------
# AC8: git failure → mtime-only fallback with visible suffix
# ---------------------------------------------------------------------------

def test_ac8_git_failure_fallback_all_young(tmp_path):
    """Git unavailable + only young dirs → ok:True with fallback suffix."""
    _make_dir(tmp_path, "agent-young", _YOUNG)
    result = _run(tmp_path, None)  # None signals git unavailable
    assert result["ok"] is True
    assert "git unavailable — mtime-only fallback" in result["detail"]


def test_ac8_git_failure_fallback_old_dir_flagged(tmp_path):
    """Git unavailable + old dir → ok:False with fallback suffix."""
    _make_dir(tmp_path, "agent-old", _OLD)
    result = _run(tmp_path, None)
    assert result["ok"] is False
    assert "agent-old" in result["detail"]
    assert "git unavailable — mtime-only fallback" in result["detail"]


# ---------------------------------------------------------------------------
# AC9: detail lists up to 5 offenders + total count
# ---------------------------------------------------------------------------

def test_ac9_detail_shape(tmp_path):
    for i in range(7):
        _make_dir(tmp_path, f"agent-old-{i}", _OLD)
    result = _run(tmp_path, set())
    assert result["ok"] is False
    detail = result["detail"]
    # Must report the count of has-content orphans (7 non-git dirs → all has-content)
    assert "7 orphan" in detail
    # At most 5 names listed
    name_count = sum(1 for i in range(7) if f"agent-old-{i}" in detail)
    assert name_count <= 5


# ---------------------------------------------------------------------------
# AC10 (read-only / no-reaping): verify no subprocess.run calls for writes
# ---------------------------------------------------------------------------

def test_no_reaping(tmp_path):
    """check_orphan_worktrees must not call git worktree remove or rmdir."""
    _make_dir(tmp_path, "agent-orphan", _OLD)
    import subprocess as sp
    original_run = sp.run
    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return original_run(cmd, **kwargs)

    with patch("backend.health_report.subprocess.run", side_effect=mock_run):
        _run(tmp_path, set())

    for call in calls:
        cmd_str = " ".join(str(c) for c in call) if isinstance(call, list) else str(call)
        assert "remove" not in cmd_str
        assert "prune" not in cmd_str
        assert "rmdir" not in cmd_str


# ---------------------------------------------------------------------------
# Classification tests (D#1249): disposable vs has-content
# ---------------------------------------------------------------------------


def _run_with_classify(worktrees_dir: Path, live_paths: set, porcelain_output: str) -> dict:
    """Run check_orphan_worktrees with mocked _git_worktree_paths and _classify_orphan's
    underlying subprocess so we can inject arbitrary git status output."""
    import subprocess
    import backend.health_report as _hr

    mock_result = type("R", (), {"returncode": 0, "stdout": porcelain_output, "stderr": ""})()

    with patch.object(_hr, "_WORKTREES_DIR", worktrees_dir), \
         patch.object(_hr, "_git_worktree_paths", return_value=live_paths), \
         patch("backend.health_report.subprocess.run", return_value=mock_result):
        return _hr.check_orphan_worktrees()


def test_classify_disposable_only_untracked_ok(tmp_path):
    """An old orphan whose only untracked files are all disposable → ok=True."""
    orphan = _make_dir(tmp_path, "agent-stale", _OLD)
    # Simulate git status showing only allowlisted untracked paths
    porcelain = (
        "?? .autonomous-team/loop.log\n"
        "?? .autonomous-team/agent-feed.jsonl\n"
        "?? node_modules/some-dep/index.js\n"
        "?? kpi.json\n"
        "?? dial-registry.json\n"
    )
    result = _run_with_classify(tmp_path, set(), porcelain)
    assert result["ok"] is True, f"Expected ok=True, got detail: {result.get('detail')}"
    assert "non-blocking" in result["detail"]
    assert "agent-stale" not in result["detail"]


def test_classify_tracked_change_has_content(tmp_path):
    """An old orphan with a tracked modification → has-content → ok=False."""
    orphan = _make_dir(tmp_path, "agent-with-work", _OLD)
    # M in the first column = staged modification (tracked change)
    porcelain = "M  backend/server.py\n"
    result = _run_with_classify(tmp_path, set(), porcelain)
    assert result["ok"] is False, f"Expected ok=False, got detail: {result.get('detail')}"
    assert "agent-with-work" in result["detail"]


def test_classify_non_allowlisted_untracked_has_content(tmp_path):
    """An old orphan with an untracked feature.py → has-content → ok=False."""
    orphan = _make_dir(tmp_path, "agent-dev-work", _OLD)
    porcelain = (
        "?? .autonomous-team/loop.log\n"
        "?? feature.py\n"  # not in allowlist
    )
    result = _run_with_classify(tmp_path, set(), porcelain)
    assert result["ok"] is False, f"Expected ok=False, got detail: {result.get('detail')}"
    assert "agent-dev-work" in result["detail"]


def test_classify_mixed_disposable_and_content(tmp_path):
    """Two orphans: one disposable + one with real work → ok=False, only non-disposable listed."""
    _make_dir(tmp_path, "agent-scratch", _OLD)
    _make_dir(tmp_path, "agent-real", _OLD)

    import backend.health_report as _hr
    import subprocess

    def mock_run(cmd, **kwargs):
        # Identify which worktree is being inspected by looking at -C argument
        c_idx = cmd.index("-C") if "-C" in cmd else -1
        path_arg = cmd[c_idx + 1] if c_idx >= 0 else ""
        if "agent-scratch" in path_arg:
            out = "?? .autonomous-team/now.md\n"
        else:
            out = "?? new-feature.py\n"
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    with patch.object(_hr, "_WORKTREES_DIR", tmp_path), \
         patch.object(_hr, "_git_worktree_paths", return_value=set()), \
         patch("backend.health_report.subprocess.run", side_effect=mock_run):
        result = _hr.check_orphan_worktrees()

    assert result["ok"] is False
    assert "agent-real" in result["detail"]
    # The disposable one should be mentioned as non-blocking
    assert "1 disposable-only" in result["detail"] or "non-blocking" in result["detail"]


# ---------------------------------------------------------------------------
# FIX 1: _pr-in-basename pattern removed — must not classify real source files
# ---------------------------------------------------------------------------


def test_fix1_agent_profiler_py_not_disposable(tmp_path):
    """agent_profiler.py must NOT be classified disposable (FIX 1: _pr pattern removed)."""
    orphan = _make_dir(tmp_path, "agent-dev", _OLD)
    porcelain = "?? agent_profiler.py\n"
    result = _run_with_classify(tmp_path, set(), porcelain)
    assert result["ok"] is False, (
        "agent_profiler.py (contains '_pr') was wrongly classified disposable — "
        "FIX 1 check failed"
    )
    assert "agent-dev" in result["detail"]


def test_fix1_feature_pr_py_not_disposable(tmp_path):
    """feature_pr.py must NOT be classified disposable (FIX 1: _pr pattern removed)."""
    orphan = _make_dir(tmp_path, "agent-dev2", _OLD)
    porcelain = "?? feature_pr.py\n"
    result = _run_with_classify(tmp_path, set(), porcelain)
    assert result["ok"] is False, (
        "feature_pr.py (contains '_pr', ends in .py) was wrongly classified disposable — "
        "FIX 1 check failed"
    )
    assert "agent-dev2" in result["detail"]


# ---------------------------------------------------------------------------
# FIX 2: bare config.json removed from _DISPOSABLE_NAMES
# ---------------------------------------------------------------------------


def test_fix2_config_json_not_disposable_by_basename():
    """config.json must NOT appear in _DISPOSABLE_NAMES (bare basename match removed)."""
    from backend.health_report import _DISPOSABLE_NAMES
    assert "config.json" not in _DISPOSABLE_NAMES, (
        "config.json should be removed from _DISPOSABLE_NAMES to prevent "
        "over-broad basename matching at any depth"
    )


# ---------------------------------------------------------------------------
# FIX 4: tracked changes in _TRACKED_DISPOSABLE → disposable
# ---------------------------------------------------------------------------


def test_fix4_tracked_generated_files_only_is_disposable(tmp_path):
    """An orphan whose ONLY tracked changes are in _TRACKED_DISPOSABLE → disposable → ok=True."""
    orphan = _make_dir(tmp_path, "agent-generated", _OLD)
    # All tracked changes are generated/runtime files in the closed allowlist
    porcelain = (
        " M .autonomous-team/now.md\n"
        " M .autonomous-team/config.json\n"
        " M wiki/Project-Status.md\n"
        "?? .autonomous-team/agent-feed.jsonl\n"
        "?? .autonomous-team/.last-jsonl-sweep\n"
    )
    result = _run_with_classify(tmp_path, set(), porcelain)
    assert result["ok"] is True, (
        f"Orphan with only generated-file tracked drift should be disposable, "
        f"got detail: {result.get('detail')}"
    )
    assert "non-blocking" in result["detail"]


def test_fix4_tracked_backend_file_is_has_content(tmp_path):
    """An orphan with backend/anything.py as tracked change → has-content → ok=False."""
    orphan = _make_dir(tmp_path, "agent-backend-work", _OLD)
    porcelain = (
        " M .autonomous-team/now.md\n"
        " M backend/health_report.py\n"  # NOT in _TRACKED_DISPOSABLE
    )
    result = _run_with_classify(tmp_path, set(), porcelain)
    assert result["ok"] is False, (
        "Orphan with tracked backend/health_report.py should be has-content"
    )
    assert "agent-backend-work" in result["detail"]


def test_fix4_git_unavailable_is_has_content(tmp_path):
    """When git is unavailable for classification, result is has-content (safe)."""
    orphan = _make_dir(tmp_path, "agent-no-git", _OLD)
    # git_worktree_paths returns a set (git IS available for the worktree list),
    # but _classify_orphan will get a non-zero returncode simulating git failure
    import backend.health_report as _hr

    fail_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    list_result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    call_count = [0]

    def mock_run(cmd, **kwargs):
        call_count[0] += 1
        # First call is _git_worktree_paths (worktree list --porcelain) — return empty
        if "worktree" in cmd:
            return list_result
        # Subsequent calls are git status for classification — simulate failure
        return fail_result

    with patch.object(_hr, "_WORKTREES_DIR", tmp_path), \
         patch("backend.health_report.subprocess.run", side_effect=mock_run):
        result = _hr.check_orphan_worktrees()

    # git failure in classify → has-content → ok=False
    assert result["ok"] is False, (
        "git failure during classification should produce has-content (safe default)"
    )
