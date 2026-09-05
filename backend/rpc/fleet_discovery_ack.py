"""RPC handler: fleet.discovery_ack / fleet.discovery_known

fleet.discovery_ack is a write: it marks a project as "seen" in
~/.autonomous-fleet-state/known.json. Called by the dashboard's
new-project-detector when the operator acknowledges a toast.

Request::

    {"method": "fleet.discovery_ack", "params": {"project_name": "projectb"}}

Response::

    {"ok": true, "known": ["autonomous-forever", "projectb"]}

fleet.discovery_known is the read-only counterpart (D#2317 PR-a item 11):
it never mutates known.json, it just reports the persisted list. Added so
new-project-detector.ts can treat the backend as the source of truth and
localStorage as a fast-path cache, rather than the other way around --
before this, a fresh browser profile or cleared localStorage had no way to
ask "what does the backend already know?" and re-announced every project
as new on every visit.

Request::

    {"method": "fleet.discovery_known", "params": {}}

Response::

    {"known": ["autonomous-forever", "projectb"]}
"""

from __future__ import annotations

import json
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

_FLEET_STATE_DIR = Path.home() / ".autonomous-fleet-state"
_KNOWN_JSON = _FLEET_STATE_DIR / "known.json"


def _read_known() -> list[str]:
    """Return the list of known project names."""
    if not _KNOWN_JSON.exists():
        return []
    try:
        data = json.loads(_KNOWN_JSON.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
        return []
    except (OSError, json.JSONDecodeError):
        return []


def _write_known(known: list[str]) -> None:
    """Persist the known project list atomically."""
    _FLEET_STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _KNOWN_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(set(known)), indent=2), encoding="utf-8")
    tmp.replace(_KNOWN_JSON)


def handle(params: dict) -> dict:
    """Mark a project as acknowledged (seen by the operator)."""
    project_name = params.get("project_name", "").strip()
    if not project_name:
        return {"ok": False, "error": "project_name is required"}

    known = _read_known()
    if project_name not in known:
        known.append(project_name)
    _write_known(known)

    return {"ok": True, "known": sorted(set(known))}


def handle_query(params: dict) -> dict:
    """Return the persisted known-projects list. Never mutates known.json."""
    return {"known": _read_known()}
