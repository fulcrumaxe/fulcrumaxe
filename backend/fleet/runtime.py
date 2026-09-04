"""backend/fleet/runtime.py — discover running dashboard instances via dashboard-runtime.json.

Scans all known state directories for dashboard-runtime.json files and
TCP-probes each listed port to determine whether the instance is alive.

Usage::

    from backend.fleet.runtime import discover_running_projects
    projects = discover_running_projects()
    # [
    #   {
    #     "name": "fulcrumaxe",
    #     "repo": "fulcrumaxe/fulcrumaxe",
    #     "state_dir": "<home>/.fulcrumaxe-state",
    #     "ports": {"vite": 5173, "api": 18099, "rpc": 8765, "sse": 8420},
    #     "pids": {"api": 1234, "server": 1235, "sse": 1236, "vite": 1237},
    #     "started_at": "2026-05-18T16:00:00Z",
    #     "alive": true,
    #     "last_seen": "2026-05-18T16:00:00Z",
    #   },
    # ]
"""

from __future__ import annotations

import glob
import json
import socket
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 5-second in-process cache (separate from project-level cache)
# ---------------------------------------------------------------------------

_cache_ts: float = 0.0
_cache_result: list[dict[str, Any]] | None = None
_CACHE_TTL_S = 5.0


def invalidate_cache() -> None:
    """Force the next discover_running_projects() call to re-scan."""
    global _cache_ts, _cache_result
    _cache_ts = 0.0
    _cache_result = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_running_projects() -> list[dict[str, Any]]:
    """Return one record per project that has a dashboard-runtime.json.

    Each record includes an ``alive`` field (True if ALL ports respond to
    a TCP-connect within 1 second).

    Results are cached for 5 seconds to avoid hammering ports on every request.
    """
    global _cache_ts, _cache_result

    now = time.monotonic()
    if _cache_result is not None and (now - _cache_ts) < _CACHE_TTL_S:
        return _cache_result

    result = _scan()
    _cache_result = result
    _cache_ts = now
    return result


def redact_for_unauthenticated_response(project: dict[str, Any]) -> dict[str, Any]:
    """Project a discover_running_projects() record down to fields safe to
    hand back over an unauthenticated surface (D#2239).

    Shared by every response boundary that wraps discover_running_projects()
    -- backend/routers/api_fleet.py's FastAPI route and backend/api.py's
    legacy inline handler both call this, so the two projections cannot
    drift apart. discover_running_projects() itself is unmodified: internal
    callers still get state_dir/repo/ports/pids in full.

    Drops state_dir, repo, ports and pids -- an adopter's dashboard hitting
    either surface must never learn another project's filesystem path, repo
    slug, or port/process assignments.
    """
    keep = ("name", "ok", "alive", "error", "started_at", "last_seen")
    return {k: project[k] for k in keep if k in project}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _scan() -> list[dict[str, Any]]:
    """Scan ~/.*-state/dashboard-runtime.json files and probe ports."""
    pattern = str(Path.home() / ".*-state" / "dashboard-runtime.json")
    seen_realpaths: set[str] = set()
    records: list[dict[str, Any]] = []

    for path_str in glob.glob(pattern):
        path = Path(path_str)
        state_dir = path.parent

        # Deduplicate via realpath
        try:
            real = str(state_dir.resolve())
        except OSError:
            real = str(state_dir)

        if real in seen_realpaths:
            continue
        seen_realpaths.add(real)

        record = _read_runtime(path, real)
        records.append(record)

    records.sort(key=lambda r: r.get("name", r.get("state_dir", "")))
    return records


def _read_runtime(runtime_path: Path, resolved_state_dir: str) -> dict[str, Any]:
    """Parse one dashboard-runtime.json and probe ports."""
    state_dir_str = resolved_state_dir

    try:
        text = runtime_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "name": _guess_name(runtime_path),
            "state_dir": state_dir_str,
            "ok": False,
            "alive": False,
            "error": f"IO error reading dashboard-runtime.json: {exc}",
        }

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "name": _guess_name(runtime_path),
            "state_dir": state_dir_str,
            "ok": False,
            "alive": False,
            "error": f"JSON parse error: {exc}",
        }

    if not isinstance(data, dict):
        return {
            "name": _guess_name(runtime_path),
            "state_dir": state_dir_str,
            "ok": False,
            "alive": False,
            "error": "dashboard-runtime.json root is not a JSON object",
        }

    name = data.get("project_name") or _guess_name(runtime_path)
    ports = data.get("ports", {})
    pids = data.get("pids", {})
    started_at = data.get("started_at")

    # Probe each port — alive only if all ports respond
    alive = _probe_ports(ports)

    return {
        "name": name,
        "repo": data.get("project_repo") or data.get("repo", ""),
        "state_dir": state_dir_str,
        "ports": ports,
        "pids": pids,
        "started_at": started_at,
        "alive": alive,
        "last_seen": started_at,
        "ok": True,
    }


def _probe_ports(ports: dict[str, Any], timeout_s: float = 1.0) -> bool:
    """TCP-connect to each port in *ports*. Returns True if ALL integer-valued
    ports succeed AND at least one port was actually probed.

    Only probes ports that have integer values. Returns False when no integer
    ports are present (nothing was probed) or when any connection fails.
    """
    if not ports:
        return False

    probed = 0
    for _name, port in ports.items():
        if not isinstance(port, int):
            continue
        probed += 1
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
                pass
        except OSError:
            return False

    return probed > 0


def _guess_name(runtime_json_path: Path) -> str:
    """Derive project name from the state directory name.

    ~/.projectb-state/dashboard-runtime.json → "projectb"
    """
    dir_name = runtime_json_path.parent.name
    if dir_name.startswith(".") and dir_name.endswith("-state"):
        return dir_name[1:-6]
    return dir_name


# ---------------------------------------------------------------------------
# Helpers for deriving ports from dashboard_port
# ---------------------------------------------------------------------------


def derive_ports(dashboard_port: int) -> dict[str, int]:
    """Return the 4-tuple derived from a single dashboard_port.

    vite = dashboard_port
    api  = vite + 100
    rpc  = vite + 200
    sse  = vite + 300
    """
    return {
        "vite": dashboard_port,
        "api": dashboard_port + 100,
        "rpc": dashboard_port + 200,
        "sse": dashboard_port + 300,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    projects = discover_running_projects()
    print(json.dumps(projects, indent=2))


if __name__ == "__main__":
    main()
