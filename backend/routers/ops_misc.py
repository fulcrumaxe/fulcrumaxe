"""
FastAPI router — miscellaneous operational POST endpoints.

Migrates from api.py:
  POST /notifications/test   (line 3880) — send a test notification
  POST /spawn-queue/enqueue  (line 3885) — enqueue a spawn request

Both require bearer auth + RBAC("POST", path).

CRITICAL: Tests MUST mock get_notifier().send_test() and
get_spawn_queue().enqueue() — these have real side-effects
(send emails/Slack, write to spawn queue, potentially trigger
subprocess spawns).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.notifier import get_notifier
from backend.spawn_queue import get_spawn_queue
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

router = APIRouter(
    tags=["ops-misc"],
    dependencies=[Depends(require_auth)],
)


@router.post(
    "/notifications/test",
    summary="Send a test notification",
    description=(
        "Sends a test notification via all configured channels. "
        "Returns per-channel results. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/notifications/test"))],
)
def ops_notifications_test() -> Any:
    """Notifications test — mirrors api.py:3880-3883."""
    results = get_notifier().send_test()
    return {"results": results}


@router.post(
    "/spawn-queue/enqueue",
    summary="Enqueue a spawn request",
    description=(
        "Enqueues a new spawn request. "
        "Body: {\"role\": str, \"prompt_context\"?: str, "
        "\"discussion\"?: int, \"priority\"?: str, \"requested_by\"?: str}. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("POST", "/spawn-queue/enqueue"))],
)
async def ops_spawn_queue_enqueue(request: Request) -> Any:
    """Spawn-queue enqueue — mirrors api.py:3885-3903."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    role = body.get("role")
    if not role:
        raise HTTPException(status_code=400, detail="'role' is required")

    prompt_context = body.get("prompt_context", "")
    discussion = body.get("discussion")
    priority = body.get("priority")
    requested_by = body.get("requested_by", "api")

    sq = get_spawn_queue()
    req_id = sq.enqueue(
        role=role,
        discussion=discussion,
        prompt_context=prompt_context,
        priority=priority,
        requested_by=requested_by,
    )
    return {"ok": True, "id": req_id}
