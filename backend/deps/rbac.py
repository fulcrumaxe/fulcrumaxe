"""
RBAC dependency for FastAPI routes.

Wraps backend/rbac.py's RBACManager and exposes a ``require_rbac`` FastAPI
dependency factory that mirrors the legacy ``_check_rbac`` method in api.py.

Legacy allow-all-on-missing-role behaviour is preserved verbatim:
  - RBAC not configured (no rbac section in config.json) → always pass
  - Token not in RBAC key table (single-key AF_API_AUTH_KEY model) → allow
  - Token has an explicit role that denies the route → 403

Usage::

    from backend.deps.rbac import make_require_rbac

    @router.get("/api/foo", dependencies=[Depends(make_require_rbac("GET", "/api/foo"))])
    def foo():
        ...

Or inject as a dependency that receives the request::

    from backend.deps.rbac import require_rbac_dep
    # then use Depends(require_rbac_dep("GET", "/api/foo"))
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import Depends, HTTPException, Request

from backend.rbac import RBACManager

# ---------------------------------------------------------------------------
# Shared RBAC manager — loaded once, same config file as legacy api.py.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_FILE = _REPO_ROOT / ".autonomous-team" / "config.json"

# Module-level singleton so the config is read once at import time.
# Tests can monkeypatch this reference or rebuild via _make_rbac_manager().
_rbac_manager: RBACManager = RBACManager(_CONFIG_FILE)


def _make_rbac_manager(config_path: Path | str | None = None) -> RBACManager:
    """Create a fresh RBACManager from *config_path* (or the default)."""
    return RBACManager(config_path or _CONFIG_FILE)


def _get_bearer(request: Request) -> str | None:
    """Extract the Bearer token from the Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    return None


def make_require_rbac(method: str, path: str) -> Callable:
    """Return a FastAPI dependency that enforces RBAC for *method* + *path*.

    Mirrors ``api.py:_check_rbac`` exactly:
    - No token (auth disabled or token not supplied) → pass
    - RBAC not configured → pass (allow-all)
    - Token not in RBAC key table → pass (legacy single-key model)
    - Token has an explicit role that does not allow method+path → 403 "forbidden"
    """
    async def _check(request: Request) -> None:
        token = _get_bearer(request)
        if token is None:
            # No token — auth layer handles this; RBAC is a post-auth gate.
            return
        if not _rbac_manager.enabled:
            return
        # Token not listed in RBAC table → legacy key, allow through.
        if _rbac_manager.get_role_for_token(token) is None:
            return
        if not _rbac_manager.check(token, method, path):
            raise HTTPException(status_code=403, detail="forbidden")

    return _check
