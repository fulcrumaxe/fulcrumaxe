"""RPC handler: fleet.cost

Aggregates per-project token spend from each project's cost_summary.json,
over the resolved fleet set (backend.fleet.fleet_set.resolve_fleet_set())
and the calendar window in backend.fleet.cost_window.

Why the resolved set (D#2317 PR-b item 1): this handler used to iterate
``discover_projects()`` alone, which globs ``~/.*-state/project.json``. The
one project whose ``cost_summary.json`` is actually being written --
``~/.autonomous-forever-state``, per
``scripts/hooks/post-agent.d/cost-summary.sh`` -- has no ``project.json``,
so the writer and this reader never met. The panel reported 1.2M "fleet"
tokens that belonged entirely to two dead fixtures, while the serving
project's own 1.9M day sat in a file this handler never opened.

Response::

    {
      "total_today_utc": 45000,
      "total_7d": 180000,
      "projected_eod": 55000,
      "per_project": [
        {
          "name": "autonomous-forever",
          "ok": true,
          "tokens_today_utc": 30000,
          "tokens_7d": 120000,
          "projected_eod_tokens": 36000
        },
        ...
      ],
      "etag": "<sha1>"
    }

Every token field is **omitted** rather than zeroed when there is no
observation behind it -- a project with no entry inside the window, or one
whose record could not be read at all. A `0` on this page means "measured
zero spend"; an absent key means "no signal", and the tile renders those
two differently. See backend/fleet/cost_window.py for why.

The 24h/today naming (D#2317 PR-b item 6): cost_summary.json has
calendar-date granularity only, so a true rolling 24-hour figure is not
computable from it. The field is named for what it is --
``tokens_today_utc`` -- and the tile labels it "Today (UTC)". Nothing here
claims "24h" any more.

ETag/304: pass {"if_none_match": "<etag>"} to get {"not_modified": true} on cache hit.

state_dir is read internally as the join key for each project's
cost_summary.json (see the loop below), but it is never included in the
per_project records this handler returns (D#2239) -- this is a host-wide,
unauthenticated endpoint and state_dir is a filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# The three token fields carried per project. Each is copied into the
# response only when read_cost_summary() gave a real number for it.
_TOKEN_FIELDS = ("tokens_today_utc", "tokens_7d", "projected_eod_tokens")

# per-project field -> fleet total field
_TOTAL_FIELDS = {
    "tokens_today_utc": "total_today_utc",
    "tokens_7d": "total_7d",
    "projected_eod_tokens": "projected_eod",
}


def handle(params: dict) -> dict:
    """Aggregate fleet-wide cost from per-project cost_summary.json files."""
    from backend.fleet.fleet_set import resolve_fleet_set
    from backend.fleet.cost_summary import read_cost_summary

    projects = resolve_fleet_set()

    per_project = []
    totals: dict[str, int | None] = {name: None for name in _TOTAL_FIELDS.values()}

    for project in projects:
        name = project.get("name", "unknown")

        if project.get("status") == "error" or not project.get("state_dir"):
            per_project.append({
                "name": name,
                "ok": False,
                "error": project.get("error", "project not readable"),
            })
            continue

        # state_dir is the internal join key for cost_summary.json -- read it
        # here but never put it in a per_project record (see module docstring).
        try:
            summary = read_cost_summary(Path(project["state_dir"]))
        except Exception as exc:
            per_project.append({
                "name": name,
                "ok": False,
                "error": f"Failed to read cost_summary: {exc}",
            })
            continue

        record: dict = {"name": name, "ok": True}
        for field in _TOKEN_FIELDS:
            value = summary.get(field)
            if value is None:
                continue
            record[field] = value
            total_key = _TOTAL_FIELDS[field]
            totals[total_key] = value + (totals[total_key] or 0)
        per_project.append(record)

    result = {k: v for k, v in totals.items() if v is not None}
    result["per_project"] = per_project

    payload_bytes = json.dumps(result, sort_keys=True).encode()
    etag = hashlib.sha1(payload_bytes).hexdigest()

    if_none_match = params.get("if_none_match", "")
    if if_none_match and if_none_match == etag:
        return {"not_modified": True, "etag": etag}

    result["etag"] = etag
    return result
