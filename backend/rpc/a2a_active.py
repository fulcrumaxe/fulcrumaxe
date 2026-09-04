"""RPC handler: a2a.list_active

Return active (unread) messages from the A2A broker's in-memory state.
Useful for the dashboard to poll current agent coordination activity.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def handle(params: dict) -> dict:
    """Return active unread messages across all inboxes.

    Response: {"messages": [...], "count": N}
    Each message has: id, from, to, kind, body, in_reply_to, ts, read
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    try:
        import urllib.request
        port = int(os.environ.get("A2A_PORT", "8830"))
        limit = int(params.get("limit", 50))

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/a2a/tasks",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        tasks = data.get("tasks", [])[:limit]
        return {"messages": tasks, "count": len(tasks)}
    except Exception:
        # Broker down or unreachable — return empty result
        return {"messages": [], "count": 0, "broker_unreachable": True}
