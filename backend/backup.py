"""
Backup and restore for .autonomous-team/ state directory.

Creates timestamped tar.gz snapshots, lists them, restores from any snapshot,
and prunes old snapshots to keep disk usage bounded. All stdlib — no extra deps.

Usage:
    from backend.backup import create_backup, list_backups, restore_backup, prune_backups

    info = create_backup()          # {"filename": "...", "size_bytes": N, "created_at": "..."}
    entries = list_backups()        # newest-first list of same dicts
    result = restore_backup("backup-2026-04-10T12:00:00.123456Z.tar.gz")
    prune_backups(keep=20)          # delete oldest beyond the keep count
"""

from __future__ import annotations

import tarfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _REPO_ROOT / ".autonomous-team"
BACKUP_DIR = _STATE_DIR / "backups"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return current UTC time as a filename-safe ISO 8601 string.

    Includes microseconds so two backups created within the same second
    produce distinct filenames.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _entry_for(path: Path) -> dict:
    """Build a metadata dict for a backup file."""
    stat = path.stat()
    # Derive created_at from filename (backup-{ISO8601}.tar.gz).
    # path.name = "backup-2026-04-10T12:00:00.123456Z.tar.gz"
    # Strip both suffixes to get the stem without ".tar".
    try:
        bare = path.name
        for suffix in (".tar.gz", ".gz"):
            if bare.endswith(suffix):
                bare = bare[: -len(suffix)]
                break
        ts_part = bare[len("backup-"):]  # e.g. "2026-04-10T12:00:00.123456Z"
        ts_clean = ts_part.rstrip("Z")   # strip trailing Z before parsing
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(ts_clean, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unrecognised timestamp: {ts_part!r}")
        created_at = dt.isoformat()
    except (ValueError, IndexError):
        # Fall back to file mtime so the function always returns a valid dict.
        created_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_backup() -> dict:
    """Create a tar.gz snapshot of .autonomous-team/ (excluding backups/ and __pycache__).

    Returns:
        {"filename": str, "size_bytes": int, "created_at": str}
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    filename = f"backup-{_now_iso()}.tar.gz"
    dest = BACKUP_DIR / filename

    with tarfile.open(dest, "w:gz") as tar:
        for item in sorted(_STATE_DIR.rglob("*")):
            # Skip the backups subdirectory itself (no recursive nesting).
            try:
                item.relative_to(BACKUP_DIR)
                continue  # item is inside BACKUP_DIR — skip
            except ValueError:
                pass
            # Skip __pycache__ directories and their contents.
            if "__pycache__" in item.parts:
                continue
            arcname = item.relative_to(_STATE_DIR.parent)
            tar.add(item, arcname=str(arcname), recursive=False)

    return _entry_for(dest)


def list_backups() -> list[dict]:
    """Return metadata for all backups, sorted newest-first.

    Returns:
        List of {"filename": str, "size_bytes": int, "created_at": str}
    """
    if not BACKUP_DIR.exists():
        return []
    entries = [
        _entry_for(p)
        for p in BACKUP_DIR.iterdir()
        if p.is_file() and p.name.endswith(".tar.gz")
    ]
    # Sort by created_at descending (ISO strings sort lexicographically).
    entries.sort(key=lambda e: e["created_at"], reverse=True)
    return entries


def restore_backup(filename: str) -> dict:
    """Restore .autonomous-team/ from a named snapshot.

    Creates a pre-restore safety backup before overwriting current state.

    Args:
        filename: Bare filename (no path) of the backup to restore from.

    Returns:
        {"restored_from": str, "restored_at": str, "safety_backup": str}

    Raises:
        FileNotFoundError: If *filename* does not exist in the backup directory.
        ValueError: If *filename* contains path separators (security check).
    """
    if "/" in filename or "\\" in filename:
        raise ValueError(f"filename must not contain path separators: {filename!r}")

    src = BACKUP_DIR / filename
    if not src.exists():
        raise FileNotFoundError(f"backup not found: {filename}")

    # Create a safety backup before we overwrite anything.
    safety = create_backup()

    # Extract the archive over the existing state directory.
    # filter='data' applies safe extraction defaults (no absolute paths, no .. traversal).
    with tarfile.open(src, "r:gz") as tar:
        tar.extractall(path=_REPO_ROOT, filter="data")  # archive paths are relative to repo root

    restored_at = _now_iso()
    return {
        "restored_from": filename,
        "restored_at": restored_at,
        "safety_backup": safety["filename"],
    }


def prune_backups(keep: int = 20) -> int:
    """Delete oldest backups beyond the *keep* count.

    Args:
        keep: Maximum number of backups to retain (newest are kept).

    Returns:
        Number of backup files deleted.
    """
    entries = list_backups()  # already sorted newest-first
    to_delete = entries[keep:]
    deleted = 0
    for entry in to_delete:
        path = BACKUP_DIR / entry["filename"]
        try:
            path.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted
