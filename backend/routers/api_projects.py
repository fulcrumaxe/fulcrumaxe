"""
FastAPI router — project GET + POST endpoints.

Migrates from api.py:
  GET  /api/projects                          (line 2476) — list all projects
  GET  /api/projects/{pid}                    (line 2511) — single project detail
  GET  /api/projects/{pid}/loop/runs          (line 2612) — list loop runs for project
  GET  /api/projects/{pid}/loop/runs/{run_id} (line 2612) — get one run for project
  POST /api/projects                          (line 3594) — create project (auth + RBAC)
  POST /api/projects/{pid}/budget/reset       (line 3577) — reset budget (auth + RBAC)
  POST /api/projects/{pid}/loop/run           (line 3710) — start scoped run (auth + RBAC + spawn-guard)

GET routes sit before _check_auth in the legacy flow — no bearer token needed.
POST routes are mutations and require require_auth + RBAC("POST", path).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api import (
    _get_loop_run,
    _list_loop_runs,
    _list_projects,
    _load_projects_raw,
    _project_sub_endpoint,
    _validate_project_id,
    _validate_project_name,
    _validate_instruction,
    _audit_loop_run_request,
    _create_project,
    _bust_budget_cache,
)
from backend.budget import BudgetTracker
from backend.deps.auth import require_auth
from backend.deps.origin_guard import require_not_test_origin
from backend.deps.rbac import make_require_rbac

import backend.api as _api_module  # noqa: E402

router = APIRouter(tags=["api-projects"])


@router.get(
    "/api/projects",
    summary="List all projects",
    description="Returns the list of known projects with enriched fields.",
)
def api_list_projects() -> Any:
    """List projects — same payload as legacy handler at api.py:2477."""
    return _list_projects()


@router.get(
    "/api/projects/{pid}",
    summary="Single project or project sub-endpoint",
    description=(
        "Returns detail for a single project when no sub-path follows. "
        "Sub-paths like /api/projects/{pid}/loop/runs are handled by dedicated routes."
    ),
)
def api_project_detail(pid: str) -> Any:
    """Project detail or sub-endpoint dispatch.

    Mirrors the legacy handler at api.py:2511-2522.
    The project id is validated before use (CWE-22 guard).
    """
    if not _validate_project_name(pid):
        raise HTTPException(status_code=400, detail=f"invalid project id: {pid!r}")

    # No sub-path — return project detail.
    projects = _load_projects_raw()
    for p in projects:
        if p.get("id") == pid or p.get("name") == pid:
            return p
    raise HTTPException(status_code=404, detail=f"project {pid!r} not found")


@router.get(
    "/api/projects/{pid}/loop/runs",
    summary="List loop runs for a project",
    description="Returns recent loop runs scoped to the given project id.",
)
def api_project_loop_runs(pid: str) -> Any:
    """List loop runs for a project — mirrors api.py:2612-2626."""
    if not _validate_project_id(pid):
        raise HTTPException(status_code=400, detail=f"invalid project_id: {pid!r}")

    known_ids = {p.get("id") for p in _load_projects_raw()}
    if pid not in known_ids:
        raise HTTPException(status_code=404, detail=f"project {pid!r} not found")

    runs = _list_loop_runs(project_id=pid)
    return {"runs": runs}


@router.get(
    "/api/projects/{pid}/loop/runs/{run_id}",
    summary="Get one loop run for a project",
    description="Returns a single loop run scoped to the given project id.",
)
def api_project_loop_run(
    pid: str,
    run_id: str,
    since: int = Query(default=0, description="Return only lines since this index."),
) -> Any:
    """Get one loop run for a project — mirrors api.py:2628-2641."""
    if not _validate_project_id(pid):
        raise HTTPException(status_code=400, detail=f"invalid project_id: {pid!r}")

    known_ids = {p.get("id") for p in _load_projects_raw()}
    if pid not in known_ids:
        raise HTTPException(status_code=404, detail=f"project {pid!r} not found")

    run = _get_loop_run(run_id, since_line=since, project_id=pid)
    if run is None:
        raise HTTPException(
            status_code=404, detail=f"run {run_id!r} not found in project {pid!r}"
        )
    return run


# ---------------------------------------------------------------------------
# POST mutation endpoints (require auth + RBAC)
# ---------------------------------------------------------------------------

@router.post(
    "/api/projects",
    summary="Create a project",
    description="Creates a new project. Body: {\"name\": str, \"repo\": str}. Requires auth.",
    status_code=200,
    dependencies=[
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/projects")),
    ],
)
async def api_project_create(request: Request) -> Any:
    """Create project — mirrors api.py:3594-3603."""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    name = (body.get("name") or "").strip()
    repo = (body.get("repo") or "").strip()
    if not name or not repo:
        raise HTTPException(status_code=400, detail="name and repo are required")
    return _create_project(name, repo)


@router.post(
    "/api/projects/{pid}/budget/reset",
    summary="Reset project budget",
    description="Clears all budget blackboard keys and busts the 60-second in-process cache. Requires auth.",
    dependencies=[
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/projects")),
    ],
)
def api_project_budget_reset(pid: str) -> Any:
    """Budget reset — mirrors api.py:3577-3591."""
    if not pid:
        raise HTTPException(status_code=400, detail="project id required")
    bt = BudgetTracker()
    bt.reset()
    _bust_budget_cache()
    return {"ok": True, "project": pid, "status": bt.get_status()}


def _check_loop_auth_key_set() -> None:
    """Raise 503 if AF_API_AUTH_KEY is not set (kill-switch for loop/run)."""
    if not os.environ.get("AF_API_AUTH_KEY"):
        raise HTTPException(
            status_code=503,
            detail=(
                "loop/run endpoint requires AF_API_AUTH_KEY to be set; "
                "set the env var and restart the server before enabling remote loop runs"
            ),
        )


def _handle_loop_run_permission_error_proj(exc: PermissionError, source: str):  # type: ignore[return]
    """Convert PermissionError from _start_loop_run into the right HTTP response.

    Mirrors error handling in api.py:3745-3759.
    """
    from fastapi.responses import JSONResponse as _JR  # noqa: PLC0415
    body_str = str(exc)
    if "rate-limited" in body_str:
        retry = 60
        return _JR(
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
    "/api/projects/{pid}/loop/run",
    summary="Start a project-scoped loop run (spawn-guarded)",
    description=(
        "Starts a loop run scoped to the given project. "
        "Rejected with 403 for HeadlessChrome/Puppeteer/Playwright User-Agents. "
        "Requires AF_API_AUTH_KEY to be set and a valid bearer token."
    ),
    dependencies=[
        Depends(require_not_test_origin),
        Depends(require_auth),
        Depends(make_require_rbac("POST", "/api/projects")),
    ],
)
async def api_project_loop_run_start(pid: str, request: Request) -> Any:
    """Start a project-scoped loop run — mirrors api.py:3710-3771. Side-effects MOCKED in tests."""
    _check_loop_auth_key_set()

    if not _validate_project_id(pid):
        raise HTTPException(status_code=400, detail=f"invalid project_id: {pid!r}")

    known_ids = {p.get("id") for p in _load_projects_raw()}
    if pid not in known_ids:
        raise HTTPException(status_code=404, detail=f"project {pid!r} not found")

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

    client_ip = request.client.host if request.client else "unknown"
    _audit_loop_run_request(instruction, client_ip)

    try:
        run = _api_module._start_loop_run(instruction, project_id=pid, source="loop_run_project")
    except PermissionError as exc:
        return _handle_loop_run_permission_error_proj(exc, "loop_run_project")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "run_id": run["run_id"],
        "started_at": run["started_at"],
        "log_path": run["log_path"],
        "instruction": instruction,
        "project_id": run["project_id"],
    }
