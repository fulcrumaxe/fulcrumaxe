"""
Tests for backend.backup — create, list, restore, prune, and exclusion behaviour.

Run with:  python3 -m pytest backend/test_backup.py -v
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary .autonomous-team/ layout and point backup module at it."""
    state = tmp_path / ".autonomous-team"
    state.mkdir()

    # A few realistic files
    (state / "config.json").write_text('{"version": 1}')
    (state / "now.md").write_text("# Now\nActive.")
    (state / "session.json").write_text('{"session_id": "abc"}')

    import backend.backup as bk

    monkeypatch.setattr(bk, "_STATE_DIR", state)
    monkeypatch.setattr(bk, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(bk, "BACKUP_DIR", state / "backups")

    return state


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------


def test_create_backup_produces_tar_gz(state_dir: Path) -> None:
    import backend.backup as bk

    info = bk.create_backup()

    assert info["filename"].startswith("backup-")
    assert info["filename"].endswith(".tar.gz")
    assert info["size_bytes"] > 0

    dest = bk.BACKUP_DIR / info["filename"]
    assert dest.exists()
    assert tarfile.is_tarfile(dest)


def test_create_backup_returns_metadata_keys(state_dir: Path) -> None:
    import backend.backup as bk

    info = bk.create_backup()
    assert {"filename", "size_bytes", "created_at"} <= info.keys()


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


def test_list_backups_empty_when_no_backups(state_dir: Path) -> None:
    import backend.backup as bk

    assert bk.list_backups() == []


def test_list_backups_sorted_newest_first(state_dir: Path) -> None:
    import backend.backup as bk

    first = bk.create_backup()
    second = bk.create_backup()

    entries = bk.list_backups()
    assert len(entries) == 2
    # Newest (second) should be at index 0
    assert entries[0]["filename"] == second["filename"]
    assert entries[1]["filename"] == first["filename"]


# ---------------------------------------------------------------------------
# restore_backup
# ---------------------------------------------------------------------------


def test_restore_backup_overwrites_current_state(state_dir: Path) -> None:
    import backend.backup as bk

    # Snapshot original content
    original_text = (state_dir / "config.json").read_text()

    info = bk.create_backup()

    # Mutate current state
    (state_dir / "config.json").write_text('{"version": 99}')

    bk.restore_backup(info["filename"])

    # State should be back to original
    restored_text = (state_dir / "config.json").read_text()
    assert restored_text == original_text


def test_restore_backup_returns_metadata(state_dir: Path) -> None:
    import backend.backup as bk

    snap = bk.create_backup()
    result = bk.restore_backup(snap["filename"])

    assert result["restored_from"] == snap["filename"]
    assert "restored_at" in result
    assert "safety_backup" in result


def test_restore_backup_creates_safety_backup(state_dir: Path) -> None:
    import backend.backup as bk

    snap = bk.create_backup()
    bk.restore_backup(snap["filename"])

    # There should now be two backups: the original + the pre-restore safety
    entries = bk.list_backups()
    assert len(entries) >= 2


def test_restore_backup_raises_for_missing_file(state_dir: Path) -> None:
    import backend.backup as bk

    with pytest.raises(FileNotFoundError):
        bk.restore_backup("does-not-exist.tar.gz")


def test_restore_backup_rejects_path_traversal(state_dir: Path) -> None:
    import backend.backup as bk

    with pytest.raises(ValueError):
        bk.restore_backup("../../../etc/passwd")


# ---------------------------------------------------------------------------
# prune_backups
# ---------------------------------------------------------------------------


def test_prune_backups_keeps_specified_count(state_dir: Path) -> None:
    import backend.backup as bk

    for _ in range(5):
        bk.create_backup()

    deleted = bk.prune_backups(keep=3)
    assert deleted == 2
    assert len(bk.list_backups()) == 3


def test_prune_backups_keeps_newest(state_dir: Path) -> None:
    import backend.backup as bk

    created = [bk.create_backup()["filename"] for _ in range(4)]
    bk.prune_backups(keep=2)

    remaining = {e["filename"] for e in bk.list_backups()}
    # The two newest should survive
    assert created[-1] in remaining
    assert created[-2] in remaining
    assert created[0] not in remaining


# ---------------------------------------------------------------------------
# Exclusion: backups/ subdirectory not included recursively
# ---------------------------------------------------------------------------


def test_backup_excludes_backups_subdirectory(state_dir: Path) -> None:
    import backend.backup as bk

    snap = bk.create_backup()
    archive_path = bk.BACKUP_DIR / snap["filename"]

    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()

    # None of the archive members should be inside the backups/ subdirectory
    assert not any("backups/" in name or name.endswith("/backups") for name in names), (
        f"backups/ directory found in archive members: {[n for n in names if 'backups' in n]}"
    )
