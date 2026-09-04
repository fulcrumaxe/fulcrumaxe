"""
FastAPI router — spawn-queue and spawn-blocks GET endpoints.

Migrates from api.py:
  GET /spawn-queue           (api.py:2930) — queue status (pending count, active list, utilization %)
  GET /spawn-queue/active    (api.py:2938) — list active agents
  GET /spawn-queue/pending   (api.py:2934) — list pending spawn requests
  GET /spawn-blocks          (api.py:2942) — recent blocked-spawn events (bare, prefix)

All four routes sit inside the _check_auth + _check_rbac("GET", path) block in
api.py:do_GET, so they require bearer auth + RBAC.

Implementation notes:
- /spawn-queue delegates to get_spawn_queue().status() — same as legacy.
- /spawn-queue/active delegates to get_spawn_queue().list_active().
- /spawn-queue/pending delegates to get_spawn_queue().list_pending().
- /spawn-blocks replicates the legacy inline handler (api.py:2942-2975): reads
  agent-feed.jsonl directly, filters for spawn_blocked events.  This is
  intentionally simpler than _spawn_blocks_list (used by /api/spawn-blocks),
  matching the legacy behaviour byte-for-byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from backend.spawn_queue import get_spawn_queue
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

# Repo root: backend/routers/ → backend/ → repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

router = APIRouter(
    tags=["spawn-queue"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# /spawn-queue — status (mirrors api.py:2930-2932)
# ---------------------------------------------------------------------------

@router.get(
    "/spawn-queue",
    summary="Spawn-queue status",
    description=(
        "Returns current queue depth, active agent count, utilization %, "
        "and per-role breakdown. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/spawn-queue"))],
)
def spawn_queue_status() -> Any:
    """Spawn-queue status — mirrors api.py:2930-2932."""
    sq = get_spawn_queue()
    return sq.status()


# ---------------------------------------------------------------------------
# /spawn-queue/pending — pending list (mirrors api.py:2934-2936)
# ---------------------------------------------------------------------------

@router.get(
    "/spawn-queue/pending",
    summary="Pending spawn requests",
    description=(
        "Returns all pending spawn requests sorted by priority. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/spawn-queue/pending"))],
)
def spawn_queue_pending() -> Any:
    """Spawn-queue pending — mirrors api.py:2934-2936."""
    sq = get_spawn_queue()
    return {"pending": sq.list_pending()}


# ---------------------------------------------------------------------------
# /spawn-queue/active — active agents (mirrors api.py:2938-2940)
# ---------------------------------------------------------------------------

@router.get(
    "/spawn-queue/active",
    summary="Active agents",
    description=(
        "Returns all currently active (running) agents. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/spawn-queue/active"))],
)
def spawn_queue_active() -> Any:
    """Spawn-queue active — mirrors api.py:2938-2940."""
    sq = get_spawn_queue()
    return {"active": sq.list_active()}


# ---------------------------------------------------------------------------
# /spawn-blocks — bare spawn-blocks (mirrors api.py:2942-2975)
#
# NOTE: /api/spawn-blocks is already migrated in backend/routers/api_ideas.py
# and uses _spawn_blocks_list() (richer multi-source logic).
# This bare /spawn-blocks route mirrors the legacy inline handler which reads
# only agent-feed.jsonl and only matches event_type == "spawn_blocked".
# ---------------------------------------------------------------------------

def _spawn_blocks_response(limit_val: int) -> Any:
    """Shared implementation for /spawn-blocks and /spawn-blocks/{sub}.

    Mirrors api.py:2942-2975 inline handler: reads agent-feed.jsonl, filters
    for event_type == "spawn_blocked".  Intentionally simpler than
    _spawn_blocks_list (used by /api/spawn-blocks).
    """
    blocks: list[dict] = []
    feed_path = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"
    if feed_path.exists():
        try:
            lines = feed_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("event_type") == "spawn_blocked":
                        blocks.append({
                            "role": ev.get("role", ""),
                            "reason": ev.get("reason", "unknown"),
                            "ts": ev.get("ts", ""),
                            "discussion": ev.get("discussion"),
                        })
                        if len(blocks) >= limit_val:
                            break
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
    return blocks


@router.get(
    "/spawn-blocks",
    summary="Recent blocked-spawn events (bare path)",
    description=(
        "Returns recent spawn_blocked events from agent-feed.jsonl. "
        "?limit=N controls how many to return (default 10). "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/spawn-blocks"))],
)
def spawn_blocks(
    limit: Optional[int] = Query(default=10, description="Max events to return."),
) -> Any:
    """Spawn-blocks — mirrors api.py:2942-2975 inline handler."""
    return _spawn_blocks_response(int(limit) if limit is not None else 10)


@router.get(
    "/spawn-blocks/{sub_path:path}",
    summary="Recent blocked-spawn events (sub-path)",
    description=(
        "Handles any sub-path under /spawn-blocks (legacy uses path.startswith). "
        "Returns the same blocked-spawn events feed as /spawn-blocks. "
        "Requires authentication."
    ),
    include_in_schema=False,
    dependencies=[Depends(make_require_rbac("GET", "/spawn-blocks"))],
)
def spawn_blocks_sub(
    sub_path: str,
    limit: Optional[int] = Query(default=10, description="Max events to return."),
) -> Any:
    """Spawn-blocks sub-path — covers legacy path.startswith('/spawn-blocks') prefix."""
    return _spawn_blocks_response(int(limit) if limit is not None else 10)
