"""RPC handler: fleet.cost

Aggregates per-project token spend from each project's cost_summary.json.
Returns fleet totals and projected end-of-day burn.

Response::

    {
      "total_24h": 45000,
      "total_7d": 180000,
      "projected_eod": 55000,
      "per_project": [
        {
          "name": "fulcrumaxe",
          "tokens_24h": 30000,
          "tokens_7d": 120000,
          "projected_eod_tokens": 36000
        },
        ...
      ],
      "etag": "<sha1>"
    }

ETag/304: pass {"if_none_match": "<etag>"} to get {"not_modified": true} on cache hit.

state_dir is read internally as the join key for each project's
cost_summary.json (see the discover_projects() loop below), but it is never
included in the per_project records this handler returns (D#2239) -- this is
a host-wide, unauthenticated endpoint and state_dir is a filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def handle(params: dict) -> dict:
    """Aggregate fleet-wide cost from per-project cost_summary.json files."""
    from backend.fleet.discovery import discover_projects
    from backend.fleet.cost_summary import read_cost_summary

    projects = discover_projects()

    per_project = []
    total_24h = 0
    total_7d = 0
    total_projected_eod = 0

    for project in projects:
        if not project.get("ok"):
            per_project.append({
                "name": project.get("name", "unknown"),
                "ok": False,
                "error": project.get("error", "project not readable"),
                "tokens_24h": 0,
                "tokens_7d": 0,
                "projected_eod_tokens": 0,
            })
            continue

        # state_dir is the internal join key for cost_summary.json -- read it
        # here but never put it in a per_project record (see module docstring).
        state_dir = Path(project["state_dir"])
        try:
            summary = read_cost_summary(state_dir)
        except Exception as exc:
            per_project.append({
                "name": project["name"],
                "ok": False,
                "error": f"Failed to read cost_summary: {exc}",
                "tokens_24h": 0,
                "tokens_7d": 0,
                "projected_eod_tokens": 0,
            })
            continue

        t24 = summary.get("tokens_24h", 0)
        t7d = summary.get("tokens_7d", 0)
        proj = summary.get("projected_eod_tokens", 0)

        total_24h += t24
        total_7d += t7d
        total_projected_eod += proj

        per_project.append({
            "name": project["name"],
            "ok": True,
            "tokens_24h": t24,
            "tokens_7d": t7d,
            "projected_eod_tokens": proj,
        })

    result = {
        "total_24h": total_24h,
        "total_7d": total_7d,
        "projected_eod": total_projected_eod,
        "per_project": per_project,
    }

    payload_bytes = json.dumps(result, sort_keys=True).encode()
    etag = hashlib.sha1(payload_bytes).hexdigest()

    if_none_match = params.get("if_none_match", "")
    if if_none_match and if_none_match == etag:
        return {"not_modified": True, "etag": etag}

    result["etag"] = etag
    return result
