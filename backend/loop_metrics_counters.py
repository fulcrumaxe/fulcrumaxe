"""
loop_metrics_counters — compute real agents_spawned / prs_merged / discussions_scanned /
prs_scanned for a loop iteration window.

Used by:
  - scripts/append-loop-metrics.sh (step 7.5 writer, both cron and interactive)
  - scripts/team-lead-iteration.sh (cron-loop writer)
  - backend/api.py (_innovate_tick dashboard writer)
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from backend import agent_feed as _agent_feed
from backend._repo import CODE_REPO as _GH_CODE_REPO  # noqa: E402
from backend.snapshot_path import MAX_AGE_SECONDS, SNAPSHOT_PATH

# ---------------------------------------------------------------------------
# Snapshot-based counters
# ---------------------------------------------------------------------------

_SNAPSHOT_PATH = SNAPSHOT_PATH
_SNAPSHOT_MAX_AGE_SECONDS = MAX_AGE_SECONDS


def _load_snapshot() -> dict | None:
    """Return the loop snapshot, or None when it is missing, unreadable or stale.

    None and {} mean different things and the distinction is the whole point of
    this function. Before, any failure collapsed to {} and the counters below
    turned that into a 0 that a dashboard rendered as "we scanned nothing this
    iteration" — indistinguishable from a genuine idle loop. Worse, a snapshot
    that was days old was read without an age check at all and its counts
    presented as "last loop iteration".

    Returns
    -------
    dict
        The parsed snapshot, when it exists and is younger than
        ``_SNAPSHOT_MAX_AGE_SECONDS``.
    None
        Missing, unparseable, undatable, or past the age threshold.
    """
    try:
        if not _SNAPSHOT_PATH.exists():
            return None
        snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(snapshot, dict):
        return None

    generated = _parse_iso(snapshot.get("generated_at") or snapshot.get("snapshot_at") or "")
    if generated is None:
        return None
    age = (datetime.now(timezone.utc) - generated).total_seconds()
    if age > _SNAPSHOT_MAX_AGE_SECONDS:
        return None
    return snapshot


def _count_collection(key: str) -> int | None:
    """Return len(snapshot[key]), or None when that number would be a fiction."""
    try:
        snapshot = _load_snapshot()
        if snapshot is None:
            return None
        value = snapshot.get(key, None)
        if isinstance(value, (list, dict)):
            return len(value)
    except Exception:  # noqa: BLE001
        return None
    # Fresh snapshot but no such key: we cannot tell "zero" from "not recorded".
    return None


def count_discussions_scanned() -> int | None:
    """Number of distinct Discussions touched in the last loop iteration.

    Returns None — not 0 — when the snapshot is missing or stale, so callers can
    render "unknown" instead of a zero that reads as a real measurement.
    """
    return _count_collection("discussions")


def count_prs_scanned() -> int | None:
    """Number of distinct open PRs examined in the last loop iteration.

    Returns None — not 0 — when the snapshot is missing or stale.
    """
    return _count_collection("prs")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp string, return None on failure."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _count_spawns(start_dt: datetime, end_dt: datetime) -> int:
    """Count agent_feed entries with event_type in {spawn, spawn_attempt} in [start, end).

    Uses agent_feed.jsonl (not audit_trail) because spawn events are written there
    — audit_trail never receives spawn/agent_spawn actions in practice.
    """
    _SPAWN_TYPES = {"spawn", "spawn_attempt"}
    try:
        count = 0
        for entry in _agent_feed.filter(
            predicate=lambda e: e.get("event_type") in _SPAWN_TYPES,
            since=start_dt,
        ):
            ts_raw = entry.get("ts", "")
            entry_dt = _parse_iso(ts_raw)
            if entry_dt is None:
                continue
            if entry_dt < end_dt:
                count += 1
        return count
    except Exception:  # noqa: BLE001
        return 0


def _count_merged_prs(start_dt: datetime, end_dt: datetime) -> int:
    """Count PRs merged in [start, end) via gh pr list."""
    try:
        start_date = start_dt.strftime("%Y-%m-%d")
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", _GH_CODE_REPO,
                "--state", "merged",
                "--search", f"merged:>={start_date}",
                "--json", "number,mergedAt",
                "--limit", "200",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return 0

        prs = json.loads(result.stdout or "[]")
        count = 0
        for pr in prs:
            merged_at_raw = pr.get("mergedAt", "")
            merged_dt = _parse_iso(merged_at_raw)
            if merged_dt is None:
                continue
            if start_dt <= merged_dt < end_dt:
                count += 1
        return count
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_counters(iter_start_iso: str, iter_end_iso: str) -> dict:
    """
    Return {agents_spawned, prs_merged, discussions_scanned, prs_scanned} for the window.

    Parameters
    ----------
    iter_start_iso: ISO 8601 string for the start of the iteration (e.g. "2026-05-10T12:00:00Z")
    iter_end_iso:   ISO 8601 string for the end of the iteration

    Returns
    -------
    dict with keys ``agents_spawned``, ``prs_merged``, ``discussions_scanned``, and
    ``prs_scanned``.

    ``agents_spawned`` and ``prs_merged`` are always non-negative ints — they are
    computed from the feed and the GitHub API, not from the snapshot.

    ``discussions_scanned`` and ``prs_scanned`` are ``int | None``; they are
    ``None`` when the loop snapshot is missing or stale, because a 0 there would
    be indistinguishable from an iteration that genuinely scanned nothing.
    Callers that need an int must decide what to substitute and own that choice
    (``scripts/append-loop-metrics.sh`` does so explicitly with ``// 0``).

    Never raises.
    """
    _UNKNOWN = {
        "agents_spawned": 0,
        "prs_merged": 0,
        "discussions_scanned": None,
        "prs_scanned": None,
    }
    try:
        start_dt = _parse_iso(iter_start_iso)
        end_dt = _parse_iso(iter_end_iso)
        if start_dt is None or end_dt is None:
            return dict(_UNKNOWN)

        agents_spawned = _count_spawns(start_dt, end_dt)
        prs_merged = _count_merged_prs(start_dt, end_dt)
        discussions_scanned = count_discussions_scanned()
        prs_scanned = count_prs_scanned()
        return {
            "agents_spawned": max(0, agents_spawned),
            "prs_merged": max(0, prs_merged),
            "discussions_scanned": (
                None if discussions_scanned is None else max(0, discussions_scanned)
            ),
            "prs_scanned": None if prs_scanned is None else max(0, prs_scanned),
        }
    except Exception:  # noqa: BLE001
        return dict(_UNKNOWN)
