"""backend/stats/cosmetic_blocks.py — cosmetic_blocks_per_hour metric.

Reads the per-day cosmetic-blocks JSONL files from
.autonomous-team/hook-events/ and returns hourly block counts for the
last 7 days.

Follows the stats/ module-per-feature pattern: pure data reader, no
side effects, importable by RPC handlers and tests.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Default hook-events dir — overridable via AF_HOOK_EVENTS_DIR for tests.
# Uses for_project("fulcrumaxe") to avoid a hardcoded AF path.
def _default_hook_events_dir() -> Path:
    env = os.environ.get("AF_HOOK_EVENTS_DIR")
    if env:
        return Path(env)
    from backend.state_paths import for_project as _fp  # noqa: PLC0415
    return _fp("fulcrumaxe").state_dir / "hook-events"

_RETENTION_DAYS = 7


def _hook_events_dir_for_project(project: "str | None") -> Path:
    """Return the hook-events directory for *project*, or the AF default."""
    if not project:
        return _default_hook_events_dir()
    from backend.state_paths import for_project as _fp  # noqa: PLC0415
    return _fp(project).state_dir / "hook-events"


def blocks_per_hour(
    hook_events_dir: Path | None = None,
    since_days: int = _RETENTION_DAYS,
    project: "str | None" = None,
) -> list[dict]:
    """Return hourly cosmetic block counts for the last since_days days.

    Each entry:
        hour_iso   — str  ISO-8601 hour bucket, e.g. "2026-05-14T09:00:00Z"
        count      — int  number of block events in that hour

    Returns only hours that have at least one block. Returns [] if no log
    files exist or no blocks have occurred.
    """
    events_dir = hook_events_dir if hook_events_dir is not None else _hook_events_dir_for_project(project)
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    hourly: defaultdict[str, int] = defaultdict(int)

    for day_offset in range(since_days + 1):
        day = (datetime.now(timezone.utc) - timedelta(days=day_offset)).date()
        log_file = events_dir / f"cosmetic-blocks-{day.isoformat()}.jsonl"
        if not log_file.exists():
            continue
        try:
            with log_file.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = entry.get("ts", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    # Bucket to hour
                    hour_bucket = ts.replace(minute=0, second=0, microsecond=0)
                    hour_key = hour_bucket.strftime("%Y-%m-%dT%H:00:00Z")
                    hourly[hour_key] += 1
        except OSError:
            continue

    return [
        {"hour_iso": hour, "count": count}
        for hour, count in sorted(hourly.items())
    ]


def total_blocks_24h(hook_events_dir: Path | None = None, project: "str | None" = None) -> int:
    """Return total block count in the last 24 hours."""
    events_dir = hook_events_dir if hook_events_dir is not None else _hook_events_dir_for_project(project)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    total = 0

    for day_offset in range(2):  # today and yesterday
        day = (datetime.now(timezone.utc) - timedelta(days=day_offset)).date()
        log_file = events_dir / f"cosmetic-blocks-{day.isoformat()}.jsonl"
        if not log_file.exists():
            continue
        try:
            with log_file.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = entry.get("ts", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        total += 1
        except OSError:
            continue

    return total
