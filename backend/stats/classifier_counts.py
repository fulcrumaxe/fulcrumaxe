"""backend/stats/classifier_counts.py — top-N classifier counts from agent retros.

Reads the agent-retros JSONL (written by backend/agent_retros.py) and
returns classifier hit counts for the last 24 hours, sorted by count
descending.

NOTE: Classifier data lives in the retros JSONL, not in stats.duckdb.
The module follows the stats/ module-per-feature pattern and returns a
structure compatible with the TUI DataTable.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Default retros file — overridable via AF_RETROS_FILE for tests.
# Uses for_project("fulcrumaxe") to avoid a hardcoded AF path.
def _default_retros_path() -> Path:
    env = os.environ.get("AF_RETROS_FILE")
    if env:
        return Path(env)
    from backend.state_paths import for_project as _fp  # noqa: PLC0415
    return _fp("fulcrumaxe").state_dir / "agent-retros.jsonl"

_TOP_N_DEFAULT = 20


def _retros_path_for_project(project: "str | None") -> Path:
    """Return the agent-retros.jsonl path for *project*, or the AF default."""
    if not project:
        return _default_retros_path()
    from backend.state_paths import for_project as _fp  # noqa: PLC0415
    return _fp(project).state_dir / "agent-retros.jsonl"


def top_classifiers(
    n: int = _TOP_N_DEFAULT,
    since_hours: int = 24,
    retros_file: Path | None = None,
    project: "str | None" = None,
) -> list[dict]:
    """Return the top-N classifiers by hit count over the last since_hours hours.

    Returns a list of dicts with keys:
        classifier  — str
        count_24h   — int (count within the window)
        pct         — float (share of total hits in the window, 0-100)

    Returns [] if the retros file does not exist or has no entries in the window.
    """
    path = retros_file if retros_file is not None else _retros_path_for_project(project)
    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    counts: Counter[str] = Counter()

    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = entry.get("ts", "")
                classifier = entry.get("classifier", "")
                if not classifier or not ts_str:
                    continue

                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                if ts >= cutoff:
                    counts[classifier] += 1

    except OSError:
        return []

    if not counts:
        return []

    total = sum(counts.values())
    return [
        {
            "classifier": clf,
            "count_24h": cnt,
            "pct": round(cnt / total * 100, 1) if total else 0.0,
        }
        for clf, cnt in counts.most_common(n)
    ]
