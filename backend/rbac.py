"""
Role-based access control (RBAC) for the fulcrumaxe REST API.

Tokens are stored as SHA-256 hashes in .autonomous-team/config.json under
the ``rbac`` key. Each token maps to a role name; each role defines an
allow-list of ``METHOD /path-pattern`` rules.  Pattern matching uses fnmatch
so ``*`` is a single-segment wildcard and ``**`` is not needed — just use
``GET /agents/*`` to allow any GET under /agents/.

Backward-compatible: if no ``rbac`` section exists in config, every
authenticated request is allowed.

Usage::

    manager = RBACManager(config_path)
    ok = manager.check(token, "GET", "/budget/status")
    if not ok:
        # return 403
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUILT_IN_ROLES: dict[str, dict[str, Any]] = {
    "admin": {
        "label": "Administrator",
        "allow": ["*"],           # matches everything
    },
    "agent": {
        "label": "Agent (internal)",
        "allow": [
            "GET /health",
            "GET /health/*",
            "GET /metrics",
            "GET /budget/*",
            "GET /registry",
            "GET /registry/*",
            "GET /agents",
            "GET /agents/*",
            "GET /kpi",
            "GET /kpi/*",
            "GET /stream/*",
            "GET /replays",
            "GET /replays/*",
            "GET /rbac/whoami",
            "POST /budget/init",
        ],
    },
    "viewer": {
        "label": "Read-only viewer",
        "allow": [
            "GET *",              # any GET is fine; POSTs are not listed
        ],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(token: str) -> str:
    """Return the hex-encoded SHA-256 digest of *token*."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _match_rule(rule: str, method: str, path: str) -> bool:
    """Return True if *method* + *path* matches the allow-list *rule*.

    Rule forms:
        ``*``              — matches everything (any method, any path)
        ``METHOD /glob``   — matches the given method and path glob
        ``GET *``          — matches any GET regardless of path

    Path matching uses :func:`fnmatch.fnmatch`.
    """
    rule = rule.strip()
    if rule == "*":
        return True
    parts = rule.split(" ", 1)
    if len(parts) != 2:
        return False
    rule_method, rule_path = parts
    if rule_method.upper() != method.upper():
        return False
    return fnmatch.fnmatch(path, rule_path)


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class RBACManager:
    """Load RBAC config and answer access-control queries.

    Parameters
    ----------
    config_path:
        Path to ``.autonomous-team/config.json``.
    """

    def __init__(self, config_path: Path | str) -> None:
        self._config_path = Path(config_path)
        self._roles: dict[str, dict[str, Any]] = {}
        self._token_hashes: dict[str, str] = {}   # hash → role name
        self._enabled: bool = False
        self._load()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Parse config.json and populate internal tables."""
        try:
            raw = json.loads(self._config_path.read_text())
        except (OSError, json.JSONDecodeError):
            return

        rbac = raw.get("rbac")
        if not rbac:
            return   # no rbac section → backward-compatible allow-all mode

        self._enabled = True

        # Merge built-in roles with any overrides from config.
        merged_roles: dict[str, dict[str, Any]] = dict(_BUILT_IN_ROLES)
        for role_name, role_def in rbac.get("roles", {}).items():
            merged_roles[role_name] = role_def
        self._roles = merged_roles

        # Index token hashes.
        for token_hash, role_name in rbac.get("keys", {}).items():
            self._token_hashes[token_hash] = role_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when an rbac section is present in config."""
        return self._enabled

    def get_role_for_token(self, token: str) -> str | None:
        """Return the role name for *token*, or None if unknown."""
        if not self._enabled:
            return None
        h = _sha256(token)
        return self._token_hashes.get(h)

    def get_role_info(self, role_name: str) -> dict[str, Any] | None:
        """Return the role definition dict, or None if the role is unknown."""
        return self._roles.get(role_name)

    def check(self, token: str, method: str, path: str) -> bool:
        """Return True if *token* is allowed to call *method* on *path*.

        When RBAC is disabled (no ``rbac`` section in config), always returns
        True so callers with a valid bearer token continue to work unchanged.

        Returns False when:
        - RBAC is enabled and the token hash is not in the key table.
        - The resolved role has no matching allow rule for method + path.
        """
        if not self._enabled:
            return True

        role_name = self.get_role_for_token(token)
        if role_name is None:
            return False

        role = self._roles.get(role_name)
        if role is None:
            return False

        for rule in role.get("allow", []):
            if _match_rule(rule, method, path):
                return True
        return False
