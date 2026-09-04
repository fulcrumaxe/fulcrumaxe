"""
Tests for backend.rbac — RBACManager role-based access control.

Run with:
    python -m pytest backend/test_rbac.py -v
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from backend.rbac import RBACManager, _sha256


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(rbac: dict | None, tmp_path: Path) -> Path:
    """Write a minimal config.json with the given rbac section."""
    cfg: dict = {"version": "2.0.0"}
    if rbac is not None:
        cfg["rbac"] = rbac
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return path


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture()
def tmp_path_local(tmp_path: Path) -> Path:  # noqa: D401
    return tmp_path


# ---------------------------------------------------------------------------
# Helper: build a config with specific keys wired to roles
# ---------------------------------------------------------------------------


def _config_with_keys(tmp_path: Path, key_map: dict[str, str]) -> RBACManager:
    """Return an RBACManager whose key table is *key_map* {token → role}."""
    rbac = {
        "keys": {_hash(tok): role for tok, role in key_map.items()},
    }
    path = _make_config(rbac, tmp_path)
    return RBACManager(path)


# ---------------------------------------------------------------------------
# Tests: backward compatibility
# ---------------------------------------------------------------------------


class TestNoRbacSection:
    def test_allow_all_when_disabled(self, tmp_path: Path) -> None:
        """No rbac section → every call returns True (backward compatible)."""
        path = _make_config(None, tmp_path)
        mgr = RBACManager(path)
        assert not mgr.enabled
        assert mgr.check("any-token", "GET", "/budget/status")
        assert mgr.check("any-token", "POST", "/control/set")
        assert mgr.check("unknown", "DELETE", "/anything")

    def test_missing_config_file(self, tmp_path: Path) -> None:
        """Missing file → graceful allow-all (no crash)."""
        mgr = RBACManager(tmp_path / "nonexistent.json")
        assert not mgr.enabled
        assert mgr.check("tok", "GET", "/health")


# ---------------------------------------------------------------------------
# Tests: admin role
# ---------------------------------------------------------------------------


class TestAdminRole:
    def test_admin_allows_everything(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"admin-token": "admin"})
        assert mgr.check("admin-token", "GET", "/budget/status")
        assert mgr.check("admin-token", "POST", "/control/set")
        assert mgr.check("admin-token", "GET", "/rbac/whoami")
        assert mgr.check("admin-token", "GET", "/kpi/velocity")
        assert mgr.check("admin-token", "POST", "/budget/init")

    def test_admin_allows_arbitrary_path(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"admin-token": "admin"})
        assert mgr.check("admin-token", "DELETE", "/nonexistent/path")


# ---------------------------------------------------------------------------
# Tests: viewer role
# ---------------------------------------------------------------------------


class TestViewerRole:
    def test_viewer_allows_get(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"viewer-token": "viewer"})
        assert mgr.check("viewer-token", "GET", "/health")
        assert mgr.check("viewer-token", "GET", "/budget/status")
        assert mgr.check("viewer-token", "GET", "/registry")
        assert mgr.check("viewer-token", "GET", "/kpi")

    def test_viewer_rejects_post(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"viewer-token": "viewer"})
        assert not mgr.check("viewer-token", "POST", "/control/set")
        assert not mgr.check("viewer-token", "POST", "/budget/init")


# ---------------------------------------------------------------------------
# Tests: agent role
# ---------------------------------------------------------------------------


class TestAgentRole:
    def test_agent_allows_listed_endpoints(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"agent-token": "agent"})
        assert mgr.check("agent-token", "GET", "/health")
        assert mgr.check("agent-token", "GET", "/agents")
        assert mgr.check("agent-token", "GET", "/agents/executor")
        assert mgr.check("agent-token", "GET", "/kpi")
        assert mgr.check("agent-token", "GET", "/rbac/whoami")
        assert mgr.check("agent-token", "POST", "/budget/init")

    def test_agent_rejects_control_set(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"agent-token": "agent"})
        assert not mgr.check("agent-token", "POST", "/control/set")

    def test_agent_rejects_replays_post(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"agent-token": "agent"})
        assert not mgr.check("agent-token", "POST", "/replays")


# ---------------------------------------------------------------------------
# Tests: unknown token
# ---------------------------------------------------------------------------


class TestUnknownToken:
    def test_unknown_token_rejected(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"real-token": "admin"})
        assert not mgr.check("fake-token", "GET", "/health")
        assert not mgr.check("", "GET", "/health")
        assert not mgr.check("real-token-typo", "GET", "/budget/status")


# ---------------------------------------------------------------------------
# Tests: wildcard patterns
# ---------------------------------------------------------------------------


class TestWildcardPatterns:
    def test_star_matches_any_subpath(self, tmp_path: Path) -> None:
        rbac = {
            "roles": {
                "limited": {"label": "Limited", "allow": ["GET /agents/*"]},
            },
            "keys": {_hash("lim-tok"): "limited"},
        }
        path = _make_config(rbac, tmp_path)
        mgr = RBACManager(path)
        assert mgr.check("lim-tok", "GET", "/agents/executor")
        assert mgr.check("lim-tok", "GET", "/agents/code-reviewer")
        # exact /agents (no trailing slash) does NOT match /agents/*
        assert not mgr.check("lim-tok", "GET", "/agents")
        assert not mgr.check("lim-tok", "GET", "/budget/status")

    def test_bare_star_matches_everything(self, tmp_path: Path) -> None:
        """A rule of '*' alone should match any method + path."""
        rbac = {
            "roles": {
                "superadmin": {"label": "Super", "allow": ["*"]},
            },
            "keys": {_hash("super-tok"): "superadmin"},
        }
        path = _make_config(rbac, tmp_path)
        mgr = RBACManager(path)
        assert mgr.check("super-tok", "POST", "/anything/at/all")

    def test_method_star(self, tmp_path: Path) -> None:
        """``GET *`` matches any GET path."""
        rbac = {
            "roles": {
                "readonly": {"label": "RO", "allow": ["GET *"]},
            },
            "keys": {_hash("ro-tok"): "readonly"},
        }
        path = _make_config(rbac, tmp_path)
        mgr = RBACManager(path)
        assert mgr.check("ro-tok", "GET", "/anything")
        assert not mgr.check("ro-tok", "POST", "/anything")


# ---------------------------------------------------------------------------
# Tests: token hashing
# ---------------------------------------------------------------------------


class TestTokenHashing:
    def test_plain_token_not_stored_as_plain(self, tmp_path: Path) -> None:
        """Passing the raw hash value as a token should NOT authenticate."""
        real_token = "super-secret"
        token_hash = _hash(real_token)
        rbac = {
            "keys": {token_hash: "admin"},
        }
        path = _make_config(rbac, tmp_path)
        mgr = RBACManager(path)
        # Correct token → passes
        assert mgr.check(real_token, "GET", "/health")
        # Passing the hash itself as the token → fails (double-hash mismatch)
        assert not mgr.check(token_hash, "GET", "/health")

    def test_sha256_helper(self) -> None:
        digest = _sha256("hello")
        assert digest == hashlib.sha256(b"hello").hexdigest()
        assert len(digest) == 64


# ---------------------------------------------------------------------------
# Tests: get_role_for_token / get_role_info
# ---------------------------------------------------------------------------


class TestRoleInspection:
    def test_get_role_for_token(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"my-token": "viewer"})
        assert mgr.get_role_for_token("my-token") == "viewer"
        assert mgr.get_role_for_token("wrong") is None

    def test_get_role_info_builtin(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"t": "admin"})
        info = mgr.get_role_info("admin")
        assert info is not None
        assert "allow" in info
        assert info["allow"] == ["*"]

    def test_get_role_info_unknown(self, tmp_path: Path) -> None:
        mgr = _config_with_keys(tmp_path, {"t": "admin"})
        assert mgr.get_role_info("ghost") is None
