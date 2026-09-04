"""
FastAPI router — plugin GET routes.

Migrates from api.py:
  GET /plugins          (line 2818) — list all plugins
  GET /plugins/{name}   (line 2832) — detail for a specific plugin

Both require bearer auth + RBAC("GET", path).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.deps.auth import require_auth
from backend.deps.rbac import make_require_rbac
from backend.plugin_loader import PluginLoader

router = APIRouter(
    tags=["info-plugins"],
    dependencies=[Depends(require_auth)],
)

# Module-level loader — same pattern as legacy _plugin_loader at api.py:1999.
_plugin_loader = PluginLoader()


@router.get(
    "/plugins",
    summary="List all plugins",
    description=(
        "Returns a summary list of all loaded plugins. "
        "Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/plugins"))],
)
def plugins_list() -> Any:
    """Plugin list — mirrors api.py:2818-2830."""
    plugins = _plugin_loader.list_plugins()
    result = []
    for name in plugins:
        p = _plugin_loader.get_plugin(name)
        if p is not None:
            result.append({
                "name": p.name,
                "description": p.description,
                "version": p.version,
                "review_pipeline": p.review_pipeline,
            })
    return {"plugins": result}


@router.get(
    "/plugins/{name}",
    summary="Plugin detail",
    description=(
        "Returns the full definition for the named plugin. "
        "404 if not found. Requires authentication."
    ),
    dependencies=[Depends(make_require_rbac("GET", "/plugins/{name}"))],
)
def plugins_detail(name: str) -> Any:
    """Plugin detail — mirrors api.py:2832-2850."""
    if not name:
        raise HTTPException(status_code=400, detail="plugin name required")
    p = _plugin_loader.get_plugin(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"plugin '{name}' not found")
    return {
        "name": p.name,
        "description": p.description,
        "version": p.version,
        "system_prompt": p.system_prompt,
        "tools": p.tools,
        "review_pipeline": p.review_pipeline,
        "triggers": p.triggers,
        "source_file": p.source_file,
    }
