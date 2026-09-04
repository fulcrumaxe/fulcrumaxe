"""backend/stats/dial_usage.py — Dial-usage telemetry reader.

Reads the live registry state via backend.dial_registry.list_directives()
and scans the last 24h of audit.jsonl to produce counters and violation info.

Pure function — no side effects, no caching.  RPC handler calls this.

Audit event names (from dial_registry.py):
  kind="dial_change"             — accepted set_dial() call
  kind="dial_directive_rejected" — rejected call; .reason ∈ {ceiling_violation,
                                   unauthenticated_source, invalid_level}
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Verb labels per class name — human-readable action verbs for the tile.
_VERB_LABELS: dict[str, str] = {
    "docs.write":          "Write docs",
    "tests.add":           "Add tests",
    "deps.bump":           "Bump deps",
    "agent.spawn":         "Spawn agents",
    "merge.standard":      "Merge (standard)",
    "merge.fast-path":     "Merge (fast-path)",
    "intent.generate":     "Generate intent",
    "methodology.change":  "Change methodology",
    "external.system":     "External system",
    "sandbox.modify":      "Modify sandbox",
    "cost.spend":          "Spend budget",
    "memory.write":        "Write memory",
    "archive.move":        "Archive files",
}


def read_dial_usage(state_dir: "Path | str | None" = None) -> dict:
    """Return dial state and 24h activity counters.

    Parameters
    ----------
    state_dir:
        Path to the state directory.  When None, falls back to the module-level
        STATE_DIR from backend.state_paths (the AF default).

    Returns
    -------
    {
        "current_dials": [
            {
                "name":              str,
                "level":             int,
                "verb_label":        str,
                "ceiling":           int,
                "active_directives": int,
                "ttl_revert_at":     str | None,   # ISO-8601 of earliest TTL expiry
            },
            ...  # 13 entries
        ],
        "last_24h": {
            "accepted":   int,
            "rejected_by_reason": {
                "ceiling_violation":     int,
                "unauthenticated_source": int,
                "invalid_level":         int,
            },
            "ceiling_violations":    int,
            "last_ceiling_exceeded": {"class": str, "timestamp": ISO8601} | None,
        },
    }
    """
    # ------------------------------------------------------------------
    # Resolve paths
    # ------------------------------------------------------------------
    if state_dir is None:
        from backend.state_paths import STATE_DIR  # noqa: PLC0415
        _state_dir = STATE_DIR
    else:
        _state_dir = Path(state_dir)

    audit_log = _state_dir / "audit.jsonl"

    # ------------------------------------------------------------------
    # Live registry read
    # ------------------------------------------------------------------
    from backend.dial_registry import list_directives  # noqa: PLC0415

    raw_classes = list_directives()
    current_dials = []
    for entry in raw_classes:
        class_name = entry["class"]
        directives = entry.get("directives", [])

        # Earliest TTL among timed directives
        ttl_revert_at: str | None = None
        ttl_times = []
        for d in directives:
            t = d.get("ttl_until")
            if t:
                try:
                    ttl_times.append(datetime.fromisoformat(t.replace("Z", "+00:00")))
                except ValueError:
                    pass
        if ttl_times:
            ttl_revert_at = min(ttl_times).isoformat(timespec="seconds")

        current_dials.append({
            "name":              class_name,
            "level":             entry["level"],
            "verb_label":        _VERB_LABELS.get(class_name, class_name),
            "ceiling":           entry["ceiling"],
            "active_directives": len(directives),
            "ttl_revert_at":     ttl_revert_at,
        })

    # ------------------------------------------------------------------
    # 24h audit scan
    # ------------------------------------------------------------------
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    accepted = 0
    rejected_by_reason: dict[str, int] = {
        "ceiling_violation":      0,
        "unauthenticated_source": 0,
        "invalid_level":          0,
    }
    ceiling_violations = 0
    last_ceiling_exceeded: dict | None = None

    if audit_log.exists():
        try:
            with audit_log.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    kind = row.get("kind", "")
                    if kind not in ("dial_change", "dial_directive_rejected"):
                        continue

                    ts_str = row.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    if ts < cutoff:
                        continue

                    if kind == "dial_change":
                        accepted += 1
                    elif kind == "dial_directive_rejected":
                        reason = row.get("reason", "")
                        if reason in rejected_by_reason:
                            rejected_by_reason[reason] += 1
                        if reason == "ceiling_violation":
                            ceiling_violations += 1
                            # Track most recent ceiling violation
                            if last_ceiling_exceeded is None or ts_str > last_ceiling_exceeded["timestamp"]:
                                last_ceiling_exceeded = {
                                    "class":     row.get("class", ""),
                                    "timestamp": ts_str,
                                }
        except OSError:
            pass

    return {
        "current_dials": current_dials,
        "last_24h": {
            "accepted":           accepted,
            "rejected_by_reason": rejected_by_reason,
            "ceiling_violations": ceiling_violations,
            "last_ceiling_exceeded": last_ceiling_exceeded,
        },
    }
