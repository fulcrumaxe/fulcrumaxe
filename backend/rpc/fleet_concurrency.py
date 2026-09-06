"""backend/rpc/fleet_concurrency.py — RPC handler for fleet.concurrency().

Two populations, never one number
---------------------------------
``fleet_cap`` governs exactly one spawn lane: the one ``spawn-agent.sh`` uses,
which reaches ``backend.fleet.concurrency.register()``.  Rows registered for
the ``Agent()``-tool lane carry the ``agent-tool-`` prefix and are excluded
from every cap check in that module by design (D#2314 S2) -- so however many
of them pile up, they can neither consume nor be denied a cap slot.

This handler therefore returns the two counts separately and never adds them
together:

    {
        "available": true,
        "fleet_cap": 8,
        "capped_agents": 1,           # governed by fleet_cap
        "uncapped_agents": 20,        # Agent()-tool lane, governed by nothing
        "per_project": [
            {"name": "autonomous-forever", "capped_agents": 1,
             "uncapped_agents": 20, "cap": 8, "ok": true},
        ],
    }

D#2323: before this, the handler returned ``fleet_total`` from the unfiltered
``count_fleet()`` and the tile rendered it against ``fleet_cap`` -- on the
operator host that read "21 of 8", a ratio between two numbers describing
different populations.  ``fleet_total`` and ``per_project[].agents_running``
were dropped rather than redefined: a field whose meaning changes under a
name that stayed the same is exactly how the misread happened.

Unreadable state is a failure, not a zero
-----------------------------------------
The previous exception path returned ``fleet_total: 0`` with a valid-looking
shape, so a missing or unreadable fleet.db rendered identically to an idle
fleet.  It now returns::

    {"available": false, "unavailable_reason": "<why>", ...}

and the tile renders the reason.  A real ``0`` is reachable only from an
``agents`` table that exists and is empty.  Note the consequence: this handler
no longer creates fleet.db as a side effect of a read -- an absent database is
reported, not silently instantiated.
"""

from __future__ import annotations


def _unavailable(reason: str, cap: int | None = None) -> dict:
    """Response shape for "the fleet state could not be read"."""
    return {
        "available": False,
        "unavailable_reason": reason,
        "fleet_cap": cap,
        "capped_agents": None,
        "uncapped_agents": None,
        "per_project": [],
    }


def handle(params: dict | None = None) -> dict:
    """RPC handler for fleet.concurrency — capped and uncapped counts, kept apart."""
    import sys

    cap: int | None = None
    try:
        from backend.fleet.concurrency import (
            FLEET_DB_PATH,
            count_fleet_capped,
            count_fleet_observational,
            count_project_capped,
            count_project_observational,
            fleet_cap,
            reap_stale,
        )
        from backend.fleet.discovery import discover_projects

        cap = fleet_cap()

        # Read-before-reap: reap_stale() opens (and would create) fleet.db.
        # An absent database is an unreadable fleet state, not an idle one.
        if not FLEET_DB_PATH.exists():
            # Reason names the file, not its absolute path: this string is
            # rendered in the dashboard and the path sits under $HOME.
            return _unavailable(
                f"fleet state not readable: {FLEET_DB_PATH.name} not found in the fleet "
                "state directory (nothing has registered yet, or the directory is gone)",
                cap,
            )

        reap_stale()  # Prune crashed/leaked agents before counting — stale rows inflate the display

        capped_agents = count_fleet_capped()
        uncapped_agents = count_fleet_observational()
        projects = discover_projects()

        per_project = []
        for p in projects:
            name = p.get("project_name") or p.get("name", "")
            ok = bool(p.get("ok", True))
            entry: dict = {
                "name": name,
                "capped_agents": count_project_capped(name) if ok else 0,
                "uncapped_agents": count_project_observational(name) if ok else 0,
                "cap": cap,
                "ok": ok,
            }
            if not ok and "error" in p:
                entry["error"] = p["error"]
            per_project.append(entry)

        return {
            "available": True,
            "fleet_cap": cap,
            "capped_agents": capped_agents,
            "uncapped_agents": uncapped_agents,
            "per_project": per_project,
        }
    except Exception as exc:
        print(f"[fleet_concurrency rpc] WARN: {exc}", file=sys.stderr)
        return _unavailable(f"fleet state not readable: {type(exc).__name__}: {exc}", cap)
