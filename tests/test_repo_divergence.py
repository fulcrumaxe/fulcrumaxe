"""tests/test_repo_divergence.py

Tests for backend/repo_divergence.py (D#1763): the working-tree-vs-origin/main
content divergence check.

Every test runs against scratch git repos created inside the test — never
against the live checkout. Each scratch repo has a bare "remote" (so
origin/main is a real remote-tracking ref, not a fabricated string) and a
clone that acts as the working tree under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from backend import repo_divergence  # noqa: E402


def _git(cwd: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> Path:
    """A clone of a bare 'remote' repo, seeded with one baseline commit.

    Baseline commit includes files under paths used by the tests: a critical
    path (hooks/), a non-critical tracked path (docs/), and an excluded path
    (.autonomous-team/). origin/main is a real remote-tracking ref pointing
    at that commit.
    """
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"

    _git(tmp_path, ["init", "--bare", "-b", "main", str(remote)])
    _git(tmp_path, ["clone", str(remote), str(work)])
    _git(work, ["config", "user.email", "test@example.com"])
    _git(work, ["config", "user.name", "Test"])

    (work / "hooks").mkdir()
    (work / "hooks" / "sandbox_rules.py").write_text("SAFE = True\n", encoding="utf-8")
    (work / "docs").mkdir()
    (work / "docs" / "readme.md").write_text("hello\n", encoding="utf-8")
    (work / ".autonomous-team").mkdir()
    (work / ".autonomous-team" / "config.json").write_text("{}\n", encoding="utf-8")

    _git(work, ["add", "-A"])
    _git(work, ["commit", "-m", "baseline"])
    _git(work, ["push", "origin", "main"])
    _git(work, ["fetch", "origin", "main"])

    return work


def test_clean_tree_reports_clean(scratch_repo: Path) -> None:
    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] == "clean"
    assert report["ok"] is True
    assert report["alarm_files"] == []
    assert report["info_files"] == []
    assert report["blind_spots"] == 0


def test_excluded_path_stays_excluded_but_counted(scratch_repo: Path) -> None:
    """Acceptance #5: modifying a tracked file under .autonomous-team/ stays
    tier 'clean' and increments blind_spots — the residual is documented,
    not accidental."""
    (scratch_repo / ".autonomous-team" / "config.json").write_text('{"changed": true}\n', encoding="utf-8")

    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] == "clean"
    assert report["ok"] is True
    assert report["alarm_files"] == []
    assert report["info_files"] == []
    assert report["blind_spots"] == 1


def test_critical_path_uncommitted_drift_is_alarm(scratch_repo: Path) -> None:
    """Acceptance #6a: an uncommitted revert of a hooks/ file — the D#1759
    signature — yields tier 'alarm' and ok=False."""
    (scratch_repo / "hooks" / "sandbox_rules.py").write_text("SAFE = False  # reverted\n", encoding="utf-8")

    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] == "alarm"
    assert report["ok"] is False
    assert "hooks/sandbox_rules.py" in report["alarm_files"]


def test_noncritical_path_uncommitted_drift_is_info(scratch_repo: Path) -> None:
    """Acceptance #6b: an uncommitted edit to a non-critical tracked file
    (e.g. under docs/) yields tier 'info' and ok=True."""
    (scratch_repo / "docs" / "readme.md").write_text("changed\n", encoding="utf-8")

    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] == "info"
    assert report["ok"] is True
    assert "docs/readme.md" in report["info_files"]
    assert report["alarm_files"] == []


def test_alarm_survives_stale_origin_ref(scratch_repo: Path, tmp_path: Path) -> None:
    """Acceptance #7: an uncommitted revert still yields tier 'alarm' even
    when the local origin/main ref is stale (never re-fetched). This is the
    criterion that fails if the implementer diffs only against origin/main —
    build_report never calls `git fetch`, so this holds by construction, but
    the test proves it rather than assuming it."""
    # Advance the remote past what `work`'s local origin/main ref knows about,
    # without fetching in `work` — origin/main in `work` is now stale.
    other_clone = tmp_path / "other"
    _git(tmp_path, ["clone", str(tmp_path / "remote.git"), str(other_clone)])
    _git(other_clone, ["config", "user.email", "test@example.com"])
    _git(other_clone, ["config", "user.name", "Test"])
    (other_clone / "unrelated.txt").write_text("new commit on remote\n", encoding="utf-8")
    _git(other_clone, ["add", "-A"])
    _git(other_clone, ["commit", "-m", "advance remote"])
    _git(other_clone, ["push", "origin", "main"])

    # work's origin/main ref is now behind the real remote HEAD — do not fetch.
    (scratch_repo / "hooks" / "sandbox_rules.py").write_text("SAFE = False  # reverted\n", encoding="utf-8")

    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] == "alarm"
    assert report["ok"] is False
    assert "hooks/sandbox_rules.py" in report["alarm_files"]


def _push_hooks_change_from_a_second_clone(remote_path: Path, tmp_path: Path, tag: str) -> None:
    """Advance the shared remote's main with a commit touching hooks/,
    from an independent clone — without touching `scratch_repo`'s HEAD or
    fetching in it. Mirrors the pattern already used by
    test_alarm_survives_stale_origin_ref for advancing the remote."""
    other_clone = tmp_path / f"other-{tag}"
    _git(tmp_path, ["clone", str(remote_path), str(other_clone)])
    _git(other_clone, ["config", "user.email", "test@example.com"])
    _git(other_clone, ["config", "user.name", "Test"])
    (other_clone / "hooks" / "sandbox_rules.py").write_text(f"SAFE = True  # {tag}\n", encoding="utf-8")
    _git(other_clone, ["add", "-A"])
    _git(other_clone, ["commit", "-m", f"advance hooks on remote ({tag})"])
    _git(other_clone, ["push", "origin", "main"])


def test_behind_in_critical_path_on_main_is_stale(scratch_repo: Path, tmp_path: Path) -> None:
    """Acceptance #3: on `main`, behind origin/main by a commit touching
    hooks/ — the D#1912 gap — yields tier 'stale', ok=False, the path named
    under stale_files, and a non-zero exit from the CLI."""
    _push_hooks_change_from_a_second_clone(tmp_path / "remote.git", tmp_path, "stale3")

    # Update the local origin/main ref without moving this checkout's HEAD —
    # that is what "behind" means. `work`'s branch is `main` (the bare
    # remote was init'd with -b main and the clone tracks it).
    _git(scratch_repo, ["fetch", "origin", "main"])

    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] == "stale"
    assert report["ok"] is False
    assert report["behind_count"] == 1
    assert "hooks/sandbox_rules.py" in report["stale_files"]
    # Split out of info_files — not double-reported as merely "info".
    assert "hooks/sandbox_rules.py" not in report["info_files"]

    result = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "backend", "repo_divergence.py"),
         "check", "--repo-root", str(scratch_repo)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert '"tier": "stale"' in result.stdout


def test_behind_on_non_main_branch_is_not_stale(scratch_repo: Path, tmp_path: Path) -> None:
    """Acceptance #4: the over-blocking guard. The same behind-in-hooks/
    scenario on a feature branch must not be failed — an un-rebased
    worktree is normal, not an incident."""
    _push_hooks_change_from_a_second_clone(tmp_path / "remote.git", tmp_path, "stale4")

    _git(scratch_repo, ["checkout", "-b", "feature-x"])
    _git(scratch_repo, ["fetch", "origin", "main"])

    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] != "stale"
    assert report["ok"] is True
    assert report["behind_count"] == 1  # still measured — just not gated to fail here


def test_stale_origin_ref_does_not_manufacture_stale(scratch_repo: Path) -> None:
    """Acceptance #8: origin/main pointing at a commit *older* than HEAD
    (a local commit ahead of the last-fetched origin/main ref) must report
    behind_count == 0 and a tier other than 'stale' — a directional bug
    (diffing origin/main..HEAD instead of HEAD..origin/main) would fail
    this the same way it would fail the existing ahead-only test."""
    (scratch_repo / "hooks" / "sandbox_rules.py").write_text("SAFE = True  # local, ahead\n", encoding="utf-8")
    _git(scratch_repo, ["add", "-A"])
    _git(scratch_repo, ["commit", "-m", "local commit ahead of stale origin/main ref"])

    report = repo_divergence.build_report(scratch_repo)
    assert report["behind_count"] == 0
    assert report["tier"] != "stale"


def test_committed_but_unpushed_is_info_not_alarm(scratch_repo: Path) -> None:
    """Committed-but-unpushed work (HEAD ahead of origin/main) is reported,
    never alarming — the ALARM tier is reserved for uncommitted drift."""
    (scratch_repo / "docs" / "readme.md").write_text("committed change\n", encoding="utf-8")
    _git(scratch_repo, ["add", "-A"])
    _git(scratch_repo, ["commit", "-m", "local work, not pushed"])

    report = repo_divergence.build_report(scratch_repo)
    assert report["tier"] == "info"
    assert report["ok"] is True
    assert "docs/readme.md" in report["info_files"]
    assert report["alarm_files"] == []
    assert report["origin_sha"] is not None


def test_format_file_list_caps_at_20() -> None:
    files = [f"file{i}.py" for i in range(25)]
    text = repo_divergence.format_file_list(files, cap=20)
    assert text.count(",") == 19 + 1  # 20 items joined + the "and N more" tail
    assert "… and 5 more" in text


def test_format_file_list_empty() -> None:
    assert repo_divergence.format_file_list([]) == ""


def test_force_tier_alarm_exits_nonzero() -> None:
    result = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "backend", "repo_divergence.py"), "check", "--force-tier", "alarm"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert '"tier": "alarm"' in result.stdout


def test_force_tier_info_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "backend", "repo_divergence.py"), "check", "--force-tier", "info"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert '"tier": "info"' in result.stdout


def test_force_tier_clean_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, os.path.join(_REPO_ROOT, "backend", "repo_divergence.py"), "check", "--force-tier", "clean"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert '"tier": "clean"' in result.stdout
