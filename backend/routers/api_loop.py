"""
FastAPI router — loop run GET + POST endpoints.

Migrates from api.py:
  GET  /api/loop/runs              (line 2582) — list recent runs (pre-auth)
  GET  /api/loop/runs/{run_id}     (line 2592) — get one run (pre-auth)
  POST /api/loop/run               (line 3645) — start a run (auth + RBAC + spawn-guard)
  POST /api/loop/runs/{id}/cancel  (line 3773) — cancel a run (auth + RBAC)
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from backend.api import (
    _get_loop_run,
    _list_loop_runs,
    _cancel_loop_run,
    _validate_instruction,
    _validate_project_id,
    _audit_loop_run_request,
    _load_projects_raw,
)
from backend.deps.auth import require_auth
from backend.deps.origin_guard import require_not_test_origin
from backend.deps.rbac import make_require_rbac

router = APIRouter(tags=["api-loop"])

# Import _start_loop_run and _spawn_guard lazily via api module to avoid
# triggering subprocess-related side effects at import time.
import backend.api as _api_module  # noqa: E402


@router.get(
    "/api/loop/runs",
    summary="List recent loop runs",
    description=(
        "Returns recent loop runs in reverse-chronological order (summary fields only). "
        "Optional ?project_id=... to filter by project."
    ),
)
def api_loop_runs(
    project_id: Optional[str] = Query(
        default=None,
        description="Filter runs by project id.",
    ),
) -> Any:
    """List loop runs — mirrors api.py:2582-2589."""
    runs = _list_loop_runs(project_id=project_id)
    return {"runs": runs}


@router.get(
    "/api/loop/runs/{run_id}",
    summary="Get one loop run",
    description=(
        "Returns a single loop run by id. "
        "Optional ?since=N to return only new lines since index N."
    ),
)
def api_loop_run(
    run_id: str,
    since: int = Query(default=0, description="Return only lines since this index."),
) -> Any:
    """Get one loop run — mirrors api.py:2592-2606."""
    run = _get_loop_run(run_id, since_line=since)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return run


# ---------------------------------------------------------------------------
# POST mutation endpoints
# ---------------------------------------------------------------------------

def _check_auth_key_set() -> None:
    """Raise 503 if AF_API_AUTH_KEY is not set.

    Mirrors the kill-switch in api.py:3652-3658 / 3714-3720: the loop/run
    endpoint requires AF_API_AUTH_KEY to prevent accidentally exposing it on
    open networks.
    """
    if not os.environ.get("AF_API_AUTH_KEY"):
        raise HTTPException(
            status_code=503,
            detail=(
                "loop/run endpoint requires AF_API_AUTH_KEY to be set; "
                "set the env var and restart the server before enabling remote loop runs"
            ),
        )


def _handle_loop_run_permission_error(exc: PermissionError, source: str) -> JSONResponse:
    """Convert PermissionError from _start_loop_run into the right HTTP response.

    Mirrors the error handling in api.py:3677-3694 and api.py:3745-3759.
    """
    body_str = str(exc)
    if "rate-limited" in body_str:
        retry = 60
        return JSONResponse(
            status_code=429,
            content={"error": "rate-limited", "source": source, "retry_after_seconds": retry},
            headers={"Retry-After": str(retry)},
        )
    elif "spawn gate disabled" in body_str:
        raise HTTPException(
            status_code=503,
            detail={"error": "spawn gate disabled", "gate": "gates.allow_claude_spawn"},
        ) from exc
    else:
        raise HTTPException(
            status_code=503,
            detail={"error": "spawn-cap reached", "source": source},
        ) from exc


@router.post(
    "/api/loop/run",
    summary="Start a new loop run (spawn-guarded)",
    description=(
        "Starts a new loop run. "
        "Rejected with 403 for HeadlessChrome/Puppeteer/Playwright User-Agents. "
        "Requires AF_API_AUTH_KEY to be set and a valid bearer token."
    ),
    dependencies=[
        Depends(require_not_test_origin),
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/loop/run")),
    ],
)
async def api_loop_run_start(request: Request) -> Any:
    """Start a loop run — mirrors api.py:3645-3706. Side-effects MOCKED in tests."""
    _check_auth_key_set()

    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass

    instruction = body.get("instruction") or (
        "Run ONE /loop iteration per CLAUDE.md protocol. "
        "Report what you did in under 300 words."
    )
    ok, err = _validate_instruction(instruction)
    if not ok:
        raise HTTPException(status_code=400, detail=f"invalid instruction: {err}")

    req_project_id = (body.get("project_id") or "fulcrumaxe").strip()
    if not _validate_project_id(req_project_id):
        raise HTTPException(status_code=400, detail=f"invalid project_id: {req_project_id!r}")

    client_ip = request.client.host if request.client else "unknown"
    _audit_loop_run_request(instruction, client_ip)

    try:
        run = _api_module._start_loop_run(instruction, project_id=req_project_id, source="loop_run_global")
    except PermissionError as exc:
        return _handle_loop_run_permission_error(exc, "loop_run_global")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "run_id": run["run_id"],
        "started_at": run["started_at"],
        "log_path": run["log_path"],
        "instruction": instruction,
        "project_id": run["project_id"],
    }


@router.post(
    "/api/loop/runs/{run_id}/cancel",
    summary="Cancel an in-flight loop run",
    description="Cancels a running loop run by id. Requires auth.",
    dependencies=[
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/loop/runs")),
    ],
)
def api_loop_run_cancel(run_id: str) -> Any:
    """Cancel a loop run — mirrors api.py:3773-3780."""
    ok = _cancel_loop_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return {"ok": True, "run_id": run_id}
