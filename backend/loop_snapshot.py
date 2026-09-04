"""loop_snapshot.py — loader for the canonical loop snapshot with staleness detection.

The path comes from backend/snapshot_path.py; it is not spelled out here.

Public API:
    load(path, max_age_seconds) -> dict   # raises SnapshotStale if stale
    age_seconds(snapshot) -> float        # seconds since snapshot was generated
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

try:  # package import (`from backend.loop_snapshot import load`)
    from backend.snapshot_path import MAX_AGE_SECONDS, SNAPSHOT_PATH
except ImportError:  # flat import (callers that put backend/ on sys.path)
    from snapshot_path import MAX_AGE_SECONDS, SNAPSHOT_PATH

DEFAULT_SNAPSHOT_PATH = str(SNAPSHOT_PATH)
DEFAULT_MAX_AGE_SECONDS = MAX_AGE_SECONDS


class SnapshotStale(Exception):
    """Raised when the snapshot is missing, lacks generated_at, or is too old."""


def age_seconds(snapshot: dict) -> float:
    """Return the age in seconds of a snapshot dict.

    Raises SnapshotStale if generated_at is missing or unparseable.
    """
    raw = snapshot.get("generated_at")
    if not raw:
        raise SnapshotStale("Snapshot is missing 'generated_at' field")
    try:
        generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise SnapshotStale(f"Cannot parse generated_at={raw!r}: {exc}") from exc
    now = datetime.now(timezone.utc)
    return (now - generated).total_seconds()


def load(
    path: str = DEFAULT_SNAPSHOT_PATH,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict:
    """Load the snapshot JSON and validate its freshness.

    Parameters
    ----------
    path:
        Path to the snapshot JSON file. Defaults to the canonical path from
        backend/snapshot_path.py (``$AUTONOMOUS_TEAM_STATE_DIR/loop-snapshot.json``).
    max_age_seconds:
        Maximum allowed age in seconds. Default 600 (10 minutes).

    Returns
    -------
    dict
        The parsed snapshot.

    Raises
    ------
    SnapshotStale
        If the file does not exist, is missing ``generated_at``, or is older
        than *max_age_seconds*.
    """
    p = Path(path)
    if not p.exists():
        raise SnapshotStale(f"Snapshot file not found: {path}")

    try:
        snapshot = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SnapshotStale(f"Cannot read/parse snapshot at {path}: {exc}") from exc

    age = age_seconds(snapshot)
    if age > max_age_seconds:
        raise SnapshotStale(
            f"Snapshot is {age:.0f}s old (max {max_age_seconds}s): {path}"
        )

    return snapshot
