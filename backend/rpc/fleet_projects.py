"""RPC handler: fleet.projects

Return the resolved fleet set -- the union of both fleet-discovery
mechanisms (see backend/fleet/fleet_set.py), one record per project, each
with a measured ``status`` (``ok`` / ``down`` / ``unknown`` / ``error``)
rather than the old boolean ``ok`` (which only ever meant "project.json
parsed", never "this project is actually up").

Called by the fleet UI's ProjectListTile to populate the project list.

Response is redacted at this boundary via fleet_set.redact_for_dashboard():
resolve_fleet_set() returns state_dir, repo, ports and pids for internal
use, but this is a host-wide, unauthenticated endpoint (D#2239) -- those
fields never leave this function. GET /api/fleet/projects
(backend/api.py) shares the exact same helper so the two surfaces can
never drift apart (D#2317 PR-a item 7).
"""
from __future__ import annotations

import hashlib
import json
import sys
import os

# Ensure repo root is on sys.path so `backend.fleet.fleet_set` is importable
# regardless of how the server was launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def handle(params: dict) -> dict:
    """Return the resolved fleet set.

    Response::

        {
          "projects": [
            {"name": "fulcrumaxe", "dashboard_port": 5174, "status": "ok", "agents_running": 2},
            {"name": "projectb", "dashboard_port": 5100, "status": "down"},
            {"name": "gatekeep", "status": "unknown"},
            {"name": "corrupt", "status": "error", "error": "JSON parse error: ..."},
          ],
          "etag": "<sha1-of-redacted-payload>"
        }

    The caller may pass ``{"if_none_match": "<etag>"}`` to get a 304-style
    response -- ``{"not_modified": true}`` with no ``projects`` key.
    """
    from backend.fleet.fleet_set import resolve_fleet_set, redact_for_dashboard  # noqa: PLC0415

    projects = [redact_for_dashboard(p) for p in resolve_fleet_set()]
    payload_bytes = json.dumps(projects, sort_keys=True).encode()
    etag = hashlib.sha1(payload_bytes).hexdigest()

    if_none_match = params.get("if_none_match", "")
    if if_none_match and if_none_match == etag:
        return {"not_modified": True, "etag": etag}

    return {"projects": projects, "etag": etag}
