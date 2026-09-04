"""
FastAPI router — health routes.

Migrates three legacy GET handlers from backend/api.py onto the FastAPI app.
All three are PUBLIC (no auth required) — exactly as in the legacy server where
they are served before the _check_auth() gate at api.py:2671.

Routes:
    GET /health         — {"ok": True, loop_last_run, loop_duration_s, loop_idle_rate, malformed_lines}
    GET /health/loop    — loop health for dashboard (lastRun, status, duration)
    GET /health/modules — module import health (cached 60 s)

Legacy handlers: api.py:2421, api.py:2430, api.py:2436.

Response shapes are dynamic / extend as the underlying modules add fields, so
we use plain dict returns (FastAPI will serialise them to JSON automatically).
Pydantic models are defined for the stable core fields to power the OpenAPI docs.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

import backend.module_health as _module_health
from backend.health_monitor import get_loop_health_dashboard, get_loop_metrics

router = APIRouter(tags=["health"])


# ---------------------------------------------------------------------------
# Pydantic response models (stable core fields only — extras pass through)
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    ok: bool
    loop_last_run: Optional[str] = None
    loop_duration_s: Optional[int] = None
    loop_idle_rate: Optional[float] = None
    malformed_lines: int = 0

    model_config = {"extra": "allow"}


class LoopHealthResponse(BaseModel):
    """Response body for GET /health/loop."""

    lastRun: str
    status: str
    duration: int

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description=(
        "Returns ok:true plus loop-metrics fields. "
        "No authentication required."
    ),
)
def health() -> Any:
    """Public health check — identical payload to legacy /health handler."""
    loop_metrics = get_loop_metrics()
    body: dict[str, Any] = {"ok": True}
    body.update(loop_metrics)
    return body


@router.get(
    "/health/loop",
    response_model=LoopHealthResponse,
    summary="Loop health",
    description=(
        "Returns loop health in the shape expected by the dashboard LoopHealth interface. "
        "No authentication required."
    ),
)
def health_loop() -> Any:
    """Loop health for the dashboard — identical payload to legacy /health/loop handler."""
    return get_loop_health_dashboard()


@router.get(
    "/health/modules",
    summary="Module health",
    description=(
        "Returns module import health (cached 60 s). "
        "No authentication required."
    ),
)
def health_modules() -> Any:
    """Module import health — identical payload to legacy /health/modules handler."""
    return _module_health.get_cached_module_health()
