"""
FastAPI router — agent-related GET routes.

Migrates from api.py:
  GET /agents                         (line 2803) — list agent card names
  GET /agents/{role}                  (line 2807) — card for a specific role
  GET /agents/profiles                (line 3205) — agent profiler snapshot
  GET /agents/profiles/summary        (line 3218) — aggregate profile summary
  GET /agents/profiles/{role_name}    (line 3226) — profile for a specific role

All require bearer auth + RBAC("GET", path).

IMPORTANT: FastAPI matches routes in registration order.
Fixed paths (/agents/profiles, /agents/profiles/summary) MUST be registered
before the parameterised catch-alls (/agents/{role},
/agents/profiles/{role_name}).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.agent_cards import AgentCards, AgentNotFoundError
from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac
from backend.plugin_loader import PluginLoader

router = APIRouter(
    tags=["info-agents"],
    dependencies=[Depends(require_auth)],
)

# Module-level loader — same pattern as legacy _plugin_loader at api.py:1999.
_plugin_loader = PluginLoader()


# ---------------------------------------------------------------------------
# /agents/profiles/summary — must come before /agents/profiles/{role_name}
# ---------------------------------------------------------------------------

@router.get(
    "/agents/profiles/summary",
    summary="Agent profile aggregate summary",
    description=(
        "Returns the aggregate section of the latest agent profiler snapshot. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/agents/profiles/summary"))],
)
def agents_profiles_summary() -> Any:
    """Profiles summary — mirrors api.py:3218-3224."""
    from backend.agent_profiler import AgentProfiler  # noqa: PLC0415
    profiler = AgentProfiler()
    snapshot = profiler.load_snapshot()
    if snapshot is None:
        snapshot = profiler.compute()
    return snapshot.get("aggregate", {})


# ---------------------------------------------------------------------------
# /agents/profiles — must come before /agents/{role}
# ---------------------------------------------------------------------------

@router.get(
    "/agents/profiles",
    summary="Agent profiler snapshot",
    description=(
        "Returns the latest agent profiler snapshot. Pass ?recompute=true to "
        "force a fresh compute. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/agents/profiles"))],
)
def agents_profiles(recompute: bool = Query(False, alias="recompute")) -> Any:
    """Profiles snapshot — mirrors api.py:3205-3216."""
    from backend.agent_profiler import AgentProfiler  # noqa: PLC0415
    profiler = AgentProfiler()
    if recompute:
        snapshot = profiler.compute()
    else:
        snapshot = profiler.load_snapshot()
        if snapshot is None:
            snapshot = profiler.compute()
    return snapshot


# ---------------------------------------------------------------------------
# /agents/profiles/{role_name}
# ---------------------------------------------------------------------------

@router.get(
    "/agents/profiles/{role_name}",
    summary="Profile for a specific agent role",
    description=(
        "Returns the profiler data for the given role. "
        "404 if no profile data exists. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/agents/profiles/{role_name}"))],
)
def agents_profiles_role(role_name: str) -> Any:
    """Profile by role — mirrors api.py:3226-3240."""
    if not role_name:
        raise HTTPException(status_code=400, detail="role name required")
    from backend.agent_profiler import AgentProfiler  # noqa: PLC0415
    profiler = AgentProfiler()
    snapshot = profiler.load_snapshot()
    if snapshot is None:
        snapshot = profiler.compute()
    role_profile = snapshot.get("roles", {}).get(role_name)
    if role_profile is None:
        raise HTTPException(status_code=404, detail=f"no profile data for role '{role_name}'")
    return role_profile


# ---------------------------------------------------------------------------
# /agents — must come before /agents/{role}
# ---------------------------------------------------------------------------

@router.get(
    "/agents",
    summary="List agent card names",
    description=(
        "Returns a list of all available agent role names. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/agents"))],
)
def agents_list() -> Any:
    """Agent list — mirrors api.py:2803-2805."""
    ac = AgentCards(plugin_loader=_plugin_loader)
    return {"agents": ac.list_agents()}


# ---------------------------------------------------------------------------
# /agents/{role}
# ---------------------------------------------------------------------------

@router.get(
    "/agents/{role}",
    summary="Agent card for a specific role",
    description=(
        "Returns the full agent card for the given role. "
        "404 if not found. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/agents/{role}"))],
)
def agents_role(role: str) -> Any:
    """Agent card by role — mirrors api.py:2807-2816."""
    if not role:
        raise HTTPException(status_code=400, detail="role name required")
    ac = AgentCards(plugin_loader=_plugin_loader)
    try:
        return ac.get_card(role)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
