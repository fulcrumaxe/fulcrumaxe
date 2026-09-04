"""
FastAPI router — KPI read-only GET routes.

Migrates from api.py:
  GET /kpi             (line 2852) — full KPI data (?project= optional)
  GET /kpi/velocity    (line 2865) — velocity sub-key only
  GET /kpi/cycle-time  (line 2878) — pr_cycle_time sub-key only

All three require auth + RBAC and support an optional ?project= parameter
(CWE-22 validated).

KPI computation is cached per-project with a 60-second TTL to avoid
recomputing on every scrape (mirrors the caching in api.py:1895-1992).
Module-level cache is process-local — same behaviour as the legacy server.
"""

from __future__ import annotations

import collections
import json
import re
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import backend.kpi_engine as kpi_engine
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac

# ---------------------------------------------------------------------------
# Project name validation — CWE-22 guard (same regex as api.py:157)
# ---------------------------------------------------------------------------

_VALID_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_project_name(name: str) -> bool:
    return bool(_VALID_PROJECT_NAME_RE.fullmatch(name))


# ---------------------------------------------------------------------------
# KPI cache — mirrors api.py:1895-1939
# ---------------------------------------------------------------------------

_kpi_cache: dict = {"data": None, "expires_at": 0.0}

_KPI_PROJECT_CACHE_MAX = 64
_kpi_project_cache: collections.OrderedDict[str, dict] = collections.OrderedDict()

_KPI_EMPTY: dict = {
    "version": 1,
    "computed_at": None,
    "velocity": {"last_24h": 0, "all_time_per_day": 0.0, "total_done": 0},
    "estimation_accuracy": {
        "tasks_with_estimates": 0,
        "mean_absolute_error_hours": None,
        "within_1_5x_pct": None,
    },
    "estimation": {"accuracy": None, "total_measured": 0, "min_samples": 5},
    "idle_rate": {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0},
    "pr_cycle_time": {"mean_hours": None, "median_hours": None, "total_measured": 0},
}


def _get_cached_kpi() -> dict:
    """Return default-project KPI from cache, recomputing when TTL elapsed."""
    now = time.monotonic()
    if _kpi_cache["data"] is None or now >= _kpi_cache["expires_at"]:
        try:
            data = kpi_engine.compute_all()
        except Exception:  # noqa: BLE001
            data = {
                "version": 1,
                "computed_at": None,
                "velocity": {"last_24h": 0, "all_time_per_day": 0.0, "total_done": 0},
                "estimation_accuracy": {
                    "tasks_with_estimates": 0,
                    "mean_absolute_error_hours": None,
                    "within_1_5x_pct": None,
                },
                "idle_rate": {
                    "last_24h_pct": None,
                    "all_time_pct": None,
                    "total_iterations": 0,
                },
                "pr_cycle_time": {
                    "mean_hours": None,
                    "median_hours": None,
                    "total_measured": 0,
                },
            }
        _kpi_cache["data"] = data
        _kpi_cache["expires_at"] = now + 60.0
    return _kpi_cache["data"]  # type: ignore[return-value]


def _get_project_kpi(project_name: str) -> dict:
    """Return KPI data for project_name — mirrors api.py:1942-1992."""
    from backend._repo import REPO as _GH_REPO  # noqa: PLC0415

    _af_name = _GH_REPO.split("/", 1)[-1] if "/" in _GH_REPO else _GH_REPO
    if not project_name or project_name == _af_name:
        return _get_cached_kpi()

    now = time.monotonic()
    if project_name not in _kpi_project_cache:
        _kpi_project_cache[project_name] = {"data": None, "expires_at": 0.0}
        while len(_kpi_project_cache) > _KPI_PROJECT_CACHE_MAX:
            _kpi_project_cache.popitem(last=False)
    else:
        _kpi_project_cache.move_to_end(project_name)

    bucket = _kpi_project_cache[project_name]
    if bucket["data"] is None or now >= bucket["expires_at"]:
        try:
            from backend.state_paths import for_project as _fp  # noqa: PLC0415

            paths = _fp(project_name)
            registry_path = paths.state_dir / ".autonomous-team" / "registry.json"
            if not registry_path.exists():
                registry_path = paths.state_dir / "registry.json"
            if registry_path.exists():
                raw = json.loads(registry_path.read_text())
                discussions = raw.get("discussions", []) if isinstance(raw, dict) else []
            else:
                discussions = []
            data: dict = {
                "version": 1,
                "computed_at": None,
                "velocity": kpi_engine.compute_velocity(discussions),
                "estimation_accuracy": kpi_engine.compute_estimation_accuracy(discussions),
                "estimation": kpi_engine.compute_estimation_metrics(discussions),
                "idle_rate": {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0},
                "pr_cycle_time": kpi_engine.compute_pr_cycle_time(discussions),
            }
        except Exception:  # noqa: BLE001
            data = dict(_KPI_EMPTY)
        bucket["data"] = data
        bucket["expires_at"] = now + 60.0
    return bucket["data"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_auth)],
)


@router.get(
    "/kpi",
    summary="Full KPI data",
    description=(
        "Returns all KPI metrics. Optional ?project= param scopes to a named project. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/kpi"))],
)
def kpi(
    project: Optional[str] = Query(
        default=None,
        description="Project slug to scope KPIs to (optional).",
    ),
) -> Any:
    """Full KPI — mirrors api.py:2852-2863."""
    if project and not _validate_project_name(project):
        raise HTTPException(status_code=400, detail=f"invalid project name: {project!r}")
    return _get_project_kpi(project or "")


@router.get(
    "/kpi/velocity",
    summary="KPI velocity sub-key",
    description=(
        "Returns the velocity sub-object from KPI data. "
        "Optional ?project= param. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/kpi/velocity"))],
)
def kpi_velocity(
    project: Optional[str] = Query(default=None, description="Project slug (optional)."),
) -> Any:
    """KPI velocity — mirrors api.py:2865-2876."""
    if project and not _validate_project_name(project):
        raise HTTPException(status_code=400, detail=f"invalid project name: {project!r}")
    return _get_project_kpi(project or "").get("velocity", {})


@router.get(
    "/kpi/cycle-time",
    summary="KPI PR cycle time sub-key",
    description=(
        "Returns the pr_cycle_time sub-object from KPI data. "
        "Optional ?project= param. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/kpi/cycle-time"))],
)
def kpi_cycle_time(
    project: Optional[str] = Query(default=None, description="Project slug (optional)."),
) -> Any:
    """KPI cycle-time — mirrors api.py:2878-2889."""
    if project and not _validate_project_name(project):
        raise HTTPException(status_code=400, detail=f"invalid project name: {project!r}")
    return _get_project_kpi(project or "").get("pr_cycle_time", {})
