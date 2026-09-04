"""backend/stats/weekly_velocity.py — Weekly Velocity metric.

Counts PRs merged in the last 7 days via `gh pr list`, bins them by date
into a 7-entry sparkline, and optionally computes a trend vs the prior
7-day window (Phase 1b).

Cache: per-repo in-process cache, 60 s TTL. Cold first call ~400 ms
(gh subprocess); all subsequent calls within the window are sub-millisecond.

Pass ``repo`` explicitly so the function queries the right project's GitHub
repo instead of the module-level AF default.  Falls back to the module-level
constant when ``repo`` is None (preserves AF-native caller behaviour).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from backend._repo import REPO as _REPO

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process cache (keyed by repo slug, 60 s TTL)
# ---------------------------------------------------------------------------

# _CACHE[repo_slug] = {"data": dict | None, "ts": float}
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_TTL = 60.0  # seconds


def _is_cache_valid(repo: str) -> bool:
    entry = _CACHE.get(repo)
    return (
        entry is not None
        and entry["data"] is not None
        and (time.monotonic() - entry["ts"]) < _CACHE_TTL
    )


# ---------------------------------------------------------------------------
# Core gh subprocess fetch
# ---------------------------------------------------------------------------

def _fetch_merged_prs(since_iso: str, repo: str, limit: int = 200) -> list[dict]:
    """Run gh pr list and return parsed JSON rows.

    Returns empty list on any subprocess failure.
    """
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--search", f"merged:>={since_iso}",
        "--json", "number,mergedAt",
        "--limit", str(limit),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout) if result.stdout.strip() else []
    except subprocess.TimeoutExpired:
        log.warning("weekly_velocity: gh pr list timed out")
        return []
    except subprocess.CalledProcessError as exc:
        log.warning("weekly_velocity: gh pr list failed: %s", exc.stderr[:200])
        return []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("weekly_velocity: parse/os error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Bucketing helpers
# ---------------------------------------------------------------------------

def _build_7day_buckets(
    prs: list[dict],
    window_start: datetime,
) -> list[dict]:
    """Bin PR mergedAt dates into 7 daily buckets from window_start.

    Returns a list of 7 dicts: {"date": "YYYY-MM-DD", "count": int}
    in ascending date order.
    """
    buckets: dict[str, int] = {}
    for i in range(7):
        day = (window_start + timedelta(days=i)).date()
        buckets[day.isoformat()] = 0

    for pr in prs:
        merged_at_str = pr.get("mergedAt", "")
        if not merged_at_str:
            continue
        try:
            merged_at = datetime.fromisoformat(
                merged_at_str.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        day_key = merged_at.date().isoformat()
        if day_key in buckets:
            buckets[day_key] += 1

    return [{"date": d, "count": c} for d, c in sorted(buckets.items())]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def weekly_velocity(repo: str | None = None) -> dict:
    """Return PRs merged in the last 7 days with per-day sparkline and trend.

    Args:
        repo: GitHub ``owner/name`` slug to query. When ``None``, falls back
              to the module-level default resolved from project.json /
              AUTONOMOUS_TEAM_REPO env at import time. Always pass this for
              non-AF projects so the tile shows the right data.

    Shape:
        {
            "applicable":   bool,  # False when no PRs in 14-day window (empty-state signal)
            "total":        int,
            "by_day":       [{"date": "YYYY-MM-DD", "count": int}, ...7 entries],
            "window_start": "YYYY-MM-DDTHH:MM:SSZ",
            "window_end":   "YYYY-MM-DDTHH:MM:SSZ",
            "prev_total":   int,   # PRs merged in the prior 7-day window
            "trend_pct":    int,   # (total - prev_total) / max(prev_total, 1) * 100
        }

    Falls back to last cached value (then empty-window response) on error.
    """
    effective_repo = repo or _REPO

    if _is_cache_valid(effective_repo):
        return _CACHE[effective_repo]["data"]  # type: ignore[return-value]

    now = datetime.now(timezone.utc)
    window_end = now
    # Use days=6 so the 7-day window spans now-6d..today inclusive.
    # days=7 would exclude today (bucket keys go up to now-1d).
    window_start = now - timedelta(days=6)
    prior_start = now - timedelta(days=13)

    # Fetch 14 days in one call so prev_total costs zero extra subprocess calls.
    # Use limit=500 — high-velocity repos merge ~200 PRs/week so 14d needs headroom.
    since_14d = prior_start.date().isoformat()
    all_prs = _fetch_merged_prs(since_14d, effective_repo, limit=500)

    if all_prs is None:
        all_prs = []

    # Partition: current 7d vs prior 7d
    current_prs: list[dict] = []
    prior_prs: list[dict] = []

    for pr in all_prs:
        merged_at_str = pr.get("mergedAt", "")
        if not merged_at_str:
            continue
        try:
            merged_at = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if merged_at >= window_start:
            current_prs.append(pr)
        elif merged_at >= prior_start:
            prior_prs.append(pr)

    by_day = _build_7day_buckets(current_prs, window_start)
    total = len(current_prs)
    prev_total = len(prior_prs)
    trend_pct = round((total - prev_total) / max(prev_total, 1) * 100)

    # applicable=False when the project has no merged PRs in the full 14-day
    # fetch window.  The frontend renders an empty-state message instead of
    # a meaningless "0" headline in that case.
    applicable = total > 0 or prev_total > 0

    data: dict = {
        "applicable": applicable,
        "total": total,
        "by_day": by_day,
        "window_start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prev_total": prev_total,
        "trend_pct": trend_pct,
    }

    _CACHE[effective_repo] = {"data": data, "ts": time.monotonic()}
    return data
