"""backend/rpc/fleet_concurrency.py — RPC handler for fleet.concurrency().

Returns fleet-wide agent concurrency shaped to match FleetConcurrencyTile's
interface contract:

    {
        "fleet_total": 3,
        "fleet_cap": 8,
        "per_project": [
            {"name": "autonomous-forever", "agents_running": 2, "cap": 8, "ok": true},
            {"name": "projectb", "agents_running": 1, "cap": 8, "ok": true},
        ]
    }

On first boot (no fleet.db yet), returns fleet_total=0, per_project=[] which is
a valid state — FleetConcurrencyTile renders "No projects discovered" in that case.

Previous shape (ok/agents/count/cap) was replaced because the tile consumes
fleet_total/fleet_cap/per_project — the old shape caused a runtime TypeError
when the tile called data.per_project.map().
"""

from __future__ import annotations


def handle(params: dict | None = None) -> dict:
    """RPC handler for fleet.concurrency — returns fleet concurrency in tile-compatible shape."""
    try:
        from backend.fleet.concurrency import count_fleet, fleet_cap, count_project, reap_stale
        from backend.fleet.discovery import discover_projects

        reap_stale()  # Prune crashed/leaked agents before counting — prevents stale entries inflating the dashboard
        cap = fleet_cap()
        fleet_total = count_fleet()
        projects = discover_projects()

        per_project = []
        for p in projects:
            name = p.get("project_name") or p.get("name", "")
            ok = bool(p.get("ok", True))
            entry: dict = {
                "name": name,
                "agents_running": count_project(name) if ok else 0,
                "cap": cap,
                "ok": ok,
            }
            if not ok and "error" in p:
                entry["error"] = p["error"]
            per_project.append(entry)

        return {
            "fleet_total": fleet_total,
            "fleet_cap": cap,
            "per_project": per_project,
        }
    except Exception as exc:  # pragma: no cover
        # fleet.db may not exist yet (first boot, no agents registered)
        import sys
        print(f"[fleet_concurrency rpc] WARN: {exc}", file=sys.stderr)
        try:
            from backend.fleet.concurrency import fleet_cap
            cap = fleet_cap()
        except Exception:
            cap = 8
        return {
            "fleet_total": 0,
            "fleet_cap": cap,
            "per_project": [],
        }
