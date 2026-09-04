"""
dashboard_origins.py — CORS allow-list discovery for the dashboard RPC server.

Moved out of backend/server.py (a hub file) per the module-per-feature
convention in CLAUDE.md. server.py keeps a one-line alias.

The allow-list is additive from three sources:
  1. Static defaults (ports 5173/4173) — backward compat with plain `vite dev`.
  2. Every ``~/.*-state/dashboard-runtime.json`` file (the original glob,
     anchored to the *server process's* home directory).
  3. ``<state_paths.STATE_DIR>/dashboard-runtime.json`` — the state dir the
     server is actually configured to serve, which may live outside $HOME
     (AUTONOMOUS_TEAM_STATE_DIR). This is the fix for D#2251: an adopter
     whose state dir sits beside their project, not under $HOME, never had
     their vite origin discovered by source #2 alone.

Source #3 is exception-guarded: resolving STATE_DIR can raise
(UnsandboxedStatePathError under pytest with the env var unset,
RelativeStateDirError on a relative value) and reading/parsing the file can
raise OSError/ValueError/json.JSONDecodeError. Any of those causes source #3
to be skipped — the static defaults and the home glob still apply. A
misconfigured state dir must never take the RPC server down.

Also owns rejected-origin logging: log_rejected_origin() emits one sanitised
WARN per origin per cache period, so a CORS rejection is visible in the
server log instead of only in the browser console.
"""

from __future__ import annotations

import glob as _glob
import json
import logging
import time
from pathlib import Path

from backend import state_paths as _state_paths

logger = logging.getLogger(__name__)

# CORS: allowed origins cache (set, computed_at_monotonic)
# Refreshed at most once per 60 seconds from dashboard-runtime.json files.
_origins_cache: tuple[set[str], float] | None = None
_ORIGINS_CACHE_TTL = 60.0  # seconds

# Historical defaults — kept for backward compat with the plain `vite dev` workflow.
_STATIC_ORIGINS: set[str] = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",  # vite preview default
    "http://127.0.0.1:4173",
}

# Origins already WARN-logged this cache period — cleared whenever the
# origins cache is recomputed, so a repeatedly-rejected origin logs at most
# once per TTL window instead of once per preflight.
_warned_origins: set[str] = set()

_MAX_LOGGED_ORIGIN_LEN = 200


def _add_ports_from_runtime_file(path_str: str, origins: set[str]) -> None:
    try:
        data = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    ports = data.get("ports", {})
    if not isinstance(ports, dict):
        return
    vite_port = ports.get("vite")
    if isinstance(vite_port, int):
        if not (1024 <= vite_port <= 65535):
            logger.debug(
                "dynamic CORS: skipping vite_port %d from %s — out of range [1024, 65535]",
                vite_port,
                path_str,
            )
            return
        origins.add(f"http://localhost:{vite_port}")
        origins.add(f"http://127.0.0.1:{vite_port}")


def _state_dir_runtime_file() -> Path | None:
    """Return <STATE_DIR>/dashboard-runtime.json, or None if STATE_DIR can't
    be resolved right now. Never raises.
    """
    try:
        return _state_paths.STATE_DIR / "dashboard-runtime.json"
    except Exception as exc:  # noqa: BLE001 — a misconfigured state dir must not 500 the server
        logger.debug("dynamic CORS: STATE_DIR unavailable, skipping state-dir source: %s", exc)
        return None


def compute_allowed_origins() -> set[str]:
    """Return the full set of allowed CORS origins.

    Starts from the static defaults (ports 5173/4173) for backward compat,
    then adds per-project origins discovered from:
      - every ~/.*-state/dashboard-runtime.json file (written by start-dashboard.sh)
      - <state_paths.STATE_DIR>/dashboard-runtime.json — the state dir this
        server process is actually configured to serve, which may be outside
        $HOME.

    Each runtime file contributes:
      http://localhost:<vite_port>   and   http://127.0.0.1:<vite_port>

    There is no per-project Vite preview port convention (4173 is the global
    default already included in the static set), so no preview-port variant
    is added here.

    Discovery is additive only — neither new source ever removes an origin
    the other found. The result is cached for 60 seconds so preflight
    requests don't hit the filesystem on every OPTIONS call.
    """
    global _origins_cache

    now = time.monotonic()
    if _origins_cache is not None and (now - _origins_cache[1]) < _ORIGINS_CACHE_TTL:
        return _origins_cache[0]

    origins = set(_STATIC_ORIGINS)

    pattern = str(Path.home() / ".*-state" / "dashboard-runtime.json")
    for path_str in _glob.glob(pattern):
        _add_ports_from_runtime_file(path_str, origins)

    state_runtime = _state_dir_runtime_file()
    if state_runtime is not None:
        _add_ports_from_runtime_file(str(state_runtime), origins)

    _origins_cache = (origins, now)
    _warned_origins.clear()
    return origins


def reset_cache() -> None:
    """Clear the cached allow-list and the per-period WARN dedup set.

    Exists for tests that need a fresh scan without re-importing the module.
    """
    global _origins_cache
    _origins_cache = None
    _warned_origins.clear()


def _sanitize_for_log(origin: str) -> str:
    """Strip CR/LF (log injection) and cap length before an attacker-controlled
    Origin header reaches a log line.
    """
    cleaned = origin.replace("\r", "").replace("\n", "")
    return cleaned[:_MAX_LOGGED_ORIGIN_LEN]


def log_rejected_origin(origin: str, allowed: set[str]) -> None:
    """Log exactly one WARN per rejected *origin* per cache period.

    No-op for an empty origin (curl / same-origin requests carry no Origin
    header and must not generate noise). Sanitises the origin (CR/LF
    stripped, length-capped) before it reaches the log line.
    """
    if not origin:
        return
    if origin in _warned_origins:
        return
    _warned_origins.add(origin)
    safe_origin = _sanitize_for_log(origin)
    logger.warning(
        "dashboard CORS: rejected origin %s — allowed origins: %s",
        safe_origin,
        sorted(allowed),
    )
