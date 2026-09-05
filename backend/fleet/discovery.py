"""backend/fleet/discovery.py — discover all coldstarted projects.

Usage::

    from backend.fleet.discovery import discover_projects, invalidate_cache
    projects = discover_projects()
    # [
    #   {"name": "autonomous-forever", "state_dir": "...", "dashboard_port": 5173,
    #    "version": 1, "ok": True},
    #   {"name": "projectb", "state_dir": "...", "dashboard_port": 5100,
    #    "version": 1, "ok": True},
    #   {"name": "corrupt-project", "state_dir": "...", "ok": False,
    #    "error": "JSON parse error: ..."},
    # ]

Algorithm:
- Glob ~/.*-state/ for directories containing project.json.
- Deduplicate by realpath (coldstart creates symlinks; realpath collapses them).
- Parse each project.json; on any error return {ok: False, error: "..."}.
- Cache results for 5 seconds (in-process; not shared across workers).
- invalidate_cache() is called by coldstart-project.sh after writing a new
  project.json so the next discover_projects() call sees fresh data.

CLI entrypoint::

    python3 -m backend.fleet.discovery
"""

from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 5-second in-process LRU cache
# ---------------------------------------------------------------------------

_cache_ts: float = 0.0
_cache_result: list[dict[str, Any]] | None = None
_CACHE_TTL_S = 5.0


def invalidate_cache() -> None:
    """Force the next discover_projects() call to re-scan the filesystem."""
    global _cache_ts, _cache_result
    _cache_ts = 0.0
    _cache_result = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_projects() -> list[dict[str, Any]]:
    """Return one record per discovered project.

    Each record is a dict with at minimum:
      {"name": str, "state_dir": str, "ok": bool}

    On success, also includes:
      {"dashboard_port": int | None, "version": int | str | None, "repo": str | None, ...}

    On parse/IO error, instead includes:
      {"ok": False, "error": "<description>"}

    The list is sorted by name for stable ordering.
    """
    global _cache_ts, _cache_result

    now = time.monotonic()
    if _cache_result is not None and (now - _cache_ts) < _CACHE_TTL_S:
        return _cache_result

    result = _scan()
    _cache_result = result
    _cache_ts = now
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _scan() -> list[dict[str, Any]]:
    """Perform the actual filesystem scan. Called only when cache is cold."""
    pattern = str(Path.home() / ".*-state" / "project.json")
    seen_realpaths: set[str] = set()
    records: list[dict[str, Any]] = []

    for path_str in glob.glob(pattern):
        path = Path(path_str)
        state_dir = path.parent

        # Deduplicate via realpath (coldstart may create state-dir symlinks)
        try:
            real = str(state_dir.resolve())
        except OSError:
            real = str(state_dir)

        if real in seen_realpaths:
            continue
        seen_realpaths.add(real)

        record = _read_project(path, real)
        records.append(record)

    records.sort(key=lambda r: r.get("name", r.get("state_dir", "")))
    return records


def _read_project(project_json_path: Path, resolved_state_dir: str) -> dict[str, Any]:
    """Parse one project.json and return a record dict."""
    state_dir_str = resolved_state_dir

    try:
        text = project_json_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "name": _guess_name(project_json_path),
            "state_dir": state_dir_str,
            "ok": False,
            "error": f"IO error reading project.json: {exc}",
        }

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "name": _guess_name(project_json_path),
            "state_dir": state_dir_str,
            "ok": False,
            "error": f"JSON parse error: {exc}",
        }

    if not isinstance(data, dict):
        return {
            "name": _guess_name(project_json_path),
            "state_dir": state_dir_str,
            "ok": False,
            "error": "project.json root is not a JSON object",
        }

    # Sentinel: must have "version" field to be a valid fleet project
    if "version" not in data and "project_name" not in data:
        return {
            "name": _guess_name(project_json_path),
            "state_dir": state_dir_str,
            "ok": False,
            "error": "project.json missing both 'version' and 'project_name' fields",
        }

    return {
        "name": data.get("project_name") or _guess_name(project_json_path),
        "state_dir": state_dir_str,
        "dashboard_port": data.get("dashboard_port"),
        "version": data.get("version"),
        "repo": data.get("repo"),
        "language": data.get("language"),
        "ok": True,
    }


def _guess_name(project_json_path: Path) -> str:
    """Derive a project name from the state directory name when project.json is unreadable.

    ~/.projectb-state/project.json → "projectb"
    """
    dir_name = project_json_path.parent.name  # e.g. ".projectb-state"
    if dir_name.startswith(".") and dir_name.endswith("-state"):
        return dir_name[1:-6]  # strip leading . and trailing -state
    return dir_name


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    projects = discover_projects()
    print(json.dumps(projects, indent=2))


if __name__ == "__main__":
    main()
