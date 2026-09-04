"""
FastAPI router — stats routes.

Migrates five legacy GET handlers from backend/api.py onto the FastAPI app.
All five require authentication (they sit after the _check_auth() gate at
api.py:2671 in the legacy server).

Routes:
    GET /registry/stats   — velocity stats only (?project= optional)
    GET /audit/stats      — counts by source and action
    GET /quality/stats    — aggregate quality scorer stats
    GET /memory/stats     — agent memory lesson counts
    GET /traces/stats     — trace span stats (inline computation)

Legacy handlers: api.py:2737, api.py:2799, api.py:3247, api.py:3287, api.py:3373.

The business logic is IDENTICAL to the legacy handlers — same functions, same
inline computation for /traces/stats.  We never reimport from api.py itself
(that would pull in the whole legacy ThreadingHTTPServer at module import time).
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.audit_trail import get_audit_trail
from backend.deps.auth import require_auth
from backend.registry import DiscussionRegistry
from backend.state_paths import for_project as _state_for_project
from backend.tracing import get_collector

# ---------------------------------------------------------------------------
# Project name validation — CWE-22 path traversal guard (same regex as api.py)
# ---------------------------------------------------------------------------

_VALID_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_project_name(name: str) -> bool:
    return bool(_VALID_PROJECT_NAME_RE.fullmatch(name))


router = APIRouter(
    tags=["stats"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Pydantic response models (stable core fields)
# ---------------------------------------------------------------------------


class RegistryStatsResponse(BaseModel):
    total: int = 0
    done: int = 0
    in_progress: int = 0
    spec_ready: int = 0

    model_config = {"extra": "allow"}


class AuditStatsResponse(BaseModel):
    by_source: dict = {}
    by_action: dict = {}
    total: int = 0

    model_config = {"extra": "allow"}


class TracesStatsResponse(BaseModel):
    traces_per_minute: float
    avg_spans: float
    p50_duration_ms: float
    p95_duration_ms: float
    error_rate: float


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/registry/stats",
    response_model=RegistryStatsResponse,
    summary="Registry velocity stats",
    description=(
        "Returns discussion registry velocity stats. "
        "Optional ?project= param scopes stats to a named project. "
        "Requires authentication."
    ),
)
def registry_stats(
    project: Optional[str] = Query(
        default=None,
        description="Project slug to scope stats to (optional).",
    ),
) -> Any:
    """Registry stats — identical payload to legacy /registry/stats handler."""
    # CWE-22 guard on project name (same logic as api.py:2744-2758)
    if project and not _validate_project_name(project):
        raise HTTPException(status_code=400, detail=f"invalid project name: {project!r}")

    if project:
        try:
            state_paths = _state_for_project(project)
            state_dir = state_paths.state_dir / ".autonomous-team"
            if not state_dir.exists():
                state_dir = state_paths.state_dir
            reg = DiscussionRegistry(state_dir=state_dir)
        except Exception:
            # CWE-209: on error, return empty data — not AF state (api.py:2756)
            return {"done": 0, "total": 0, "in_progress": 0, "spec_ready": 0}
    else:
        reg = DiscussionRegistry()

    return reg.stats()


@router.get(
    "/audit/stats",
    response_model=AuditStatsResponse,
    summary="Audit trail stats",
    description=(
        "Returns audit trail event counts grouped by source and action. "
        "Requires authentication."
    ),
)
def audit_stats() -> Any:
    """Audit stats — identical payload to legacy /audit/stats handler."""
    at = get_audit_trail()
    return at.stats()


@router.get(
    "/quality/stats",
    summary="Quality scorer stats",
    description=(
        "Returns aggregate quality-scorer stats across all scored PRs. "
        "Requires authentication."
    ),
)
def quality_stats() -> Any:
    """Quality stats — identical payload to legacy /quality/stats handler."""
    from backend.quality_scorer import QualityScorer  # noqa: PLC0415 — deferred import same as legacy
    qs = QualityScorer()
    return qs.stats()


@router.get(
    "/memory/stats",
    summary="Agent memory stats",
    description=(
        "Returns agent lesson counts grouped by role and lesson type. "
        "Requires authentication."
    ),
)
def memory_stats() -> Any:
    """Memory stats — identical payload to legacy /memory/stats handler."""
    from backend.agent_memory import AgentMemory  # noqa: PLC0415 — deferred import same as legacy
    mem = AgentMemory()
    return mem.stats()


@router.get(
    "/traces/stats",
    response_model=TracesStatsResponse,
    summary="Trace span stats",
    description=(
        "Returns computed stats across in-memory trace spans: "
        "traces_per_minute, avg_spans, p50/p95 duration, error_rate. "
        "Requires authentication."
    ),
)
def traces_stats() -> Any:
    """Traces stats — identical inline computation to legacy /traces/stats handler (api.py:3373)."""
    spans = get_collector().peek(10000)
    if not spans:
        return {
            "traces_per_minute": 0.0,
            "avg_spans": 0.0,
            "p50_duration_ms": 0.0,
            "p95_duration_ms": 0.0,
            "error_rate": 0.0,
        }

    now_ns = time.time_ns()
    one_min_ns = 60 * 1_000_000_000
    recent = [sp for sp in spans if now_ns - sp.start_time_unix_nano <= one_min_ns]
    recent_traces = {sp.trace_id for sp in recent}
    traces_per_min = float(len(recent_traces))

    by_trace: dict = {}
    for sp in spans:
        by_trace.setdefault(sp.trace_id, []).append(sp)
    avg_spans_val = sum(len(v) for v in by_trace.values()) / len(by_trace)

    durations_ms = []
    error_count = 0
    for sp in spans:
        if sp.end_time_unix_nano > 0:
            dur = (sp.end_time_unix_nano - sp.start_time_unix_nano) / 1_000_000
            durations_ms.append(dur)
        if sp.status == "ERROR":
            error_count += 1

    durations_ms.sort()
    total = len(durations_ms)
    p50 = durations_ms[int(total * 0.50)] if total else 0.0
    p95 = durations_ms[int(total * 0.95)] if total else 0.0
    error_rate = error_count / len(spans) if spans else 0.0

    return {
        "traces_per_minute": traces_per_min,
        "avg_spans": round(avg_spans_val, 2),
        "p50_duration_ms": round(p50, 3),
        "p95_duration_ms": round(p95, 3),
        "error_rate": round(error_rate, 4),
    }
