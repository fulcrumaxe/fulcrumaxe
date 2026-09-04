"""
FastAPI router — GET /api/fleet/projects

Migrates the fleet projects handler from api.py (lines 2552-2554). Note that
the legacy handler in api.py is what actually runs by default -- see
backend/api.py's own "/api/fleet/projects" branch -- since
scripts/start-dashboard.sh launches backend/api.py directly. This router is
only live when a deployment has opted into the asgi_app migration via
scripts/cutover-dashboard.sh. Both call the same shared redaction helper so
they can't drift apart (D#2239).

Lists all running dashboard instances with their TCP-probed alive status.
No auth required (pre-auth route in legacy flow) -- this is one of the
(now two, plus a legacy third) unauthenticated surfaces D#2239 was filed
against, so its response is redacted at this boundary:
discover_running_projects() keeps returning state_dir/repo/ports/pids for
any internal caller, but none of those ever leave this handler.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from backend.fleet.runtime import redact_for_unauthenticated_response

router = APIRouter(tags=["api-fleet"])


@router.get(
    "/api/fleet/projects",
    summary="Running dashboard instances",
    description="Lists all running dashboard instances with alive status.",
)
def api_fleet_projects() -> Any:
    """Fleet projects — mirrors api.py:2552-2554, redacted at the response boundary."""
    from backend.fleet.runtime import discover_running_projects  # deferred, same as legacy
    return {"projects": [redact_for_unauthenticated_response(p) for p in discover_running_projects()]}
