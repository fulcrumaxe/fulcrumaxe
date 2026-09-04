"""RPC handlers: dial.list + dial.set

dial.list — read-only; returns each op-class dial with current level, ceiling,
            active directive count, and TTL info.

dial.set  — mutating; wraps dial_registry.set_dial(name, level, ttl).
            Enforces ceiling and writes a hash-chained audit row.
            Uses source={"kind": "system", "reason": "dashboard_rpc"} which
            must appear in <STATE_DIR>/dial-directive-allowlist.json.

Auth: all POST /rpc calls already require Bearer token (enforced by the HTTP
      layer in server.py::do_POST before any handler is called). dial.set
      additionally requires the dashboard source entry in the allowlist so
      that programmatic changes are distinguishable from GUI changes in the
      audit trail.
"""

from __future__ import annotations

_DASHBOARD_SOURCE = {"kind": "system", "reason": "dashboard_rpc"}

# TTL options accepted by dial.set — passed directly to set_dial()
_VALID_TTLS = frozenset({"for-today", None})


def handle_list(params: dict) -> dict:
    """Return current dial state for all registered classes.

    Params: {} (no params required)

    Response:
        {
            "dials": [
                {
                    "name": str,
                    "level": int,
                    "ceiling": int,
                    "active_directives": int,
                    "ttl_revert_at": str | null,
                }
            ]
        }
    """
    from backend.dial_registry import list_directives  # noqa: PLC0415

    directives = list_directives()
    dials = []
    for entry in directives:
        # Find the earliest TTL from active directives (if any)
        active = [d for d in entry.get("directives", []) if d.get("ttl_until")]
        ttl_revert_at: str | None = None
        if active:
            ttl_revert_at = min(d["ttl_until"] for d in active)

        dials.append({
            "name": entry["class"],
            "level": entry["level"],
            "ceiling": entry["ceiling"],
            "active_directives": len(entry.get("directives", [])),
            "ttl_revert_at": ttl_revert_at,
        })

    return {"dials": dials}


def handle_set(params: dict) -> dict:
    """Set a dial level.

    Params:
        name  (str, required) — dial class name
        level (int, required) — target level 1–ceiling
        ttl   (str | null, optional) — "for-today" or ISO-8601 string

    Returns:
        {
            "name": str,
            "level": int,
            "ceiling": int,
        }

    Raises ValueError on:
        - missing / invalid params
        - level above ceiling (DialCeilingExceeded)
        - source not in allowlist (unauthenticated)
        - unknown dial class
    """
    from backend.dial_registry import set_dial, DialCeilingExceeded  # noqa: PLC0415

    name = params.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("params.name is required and must be a string")

    level = params.get("level")
    if level is None or not isinstance(level, int):
        raise ValueError("params.level is required and must be an integer")

    ttl = params.get("ttl", None)
    if ttl is not None and not isinstance(ttl, str):
        raise ValueError("params.ttl must be a string or null")

    try:
        result = set_dial(name, level, ttl=ttl, source=_DASHBOARD_SOURCE)
    except DialCeilingExceeded as exc:
        raise ValueError(f"ceiling_exceeded: {exc}") from exc

    return {
        "name": name,
        "level": result["level"],
        "ceiling": result["ceiling"],
    }
