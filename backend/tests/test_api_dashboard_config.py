"""Tests for backend/api.py _get_dashboard_config() token priority logic.

Verifies that the project-scoped rpcToken from STATE_DIR/dashboard-runtime.json
takes priority over the shared repo-level dashboard-token file, and that the
repo-level file is used only as a fallback when the runtime token is absent/empty.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.api import _get_dashboard_config  # noqa: E402


@pytest.fixture()
def isolated_dirs(tmp_path):
    """Set up a temporary STATE_DIR and REPO_ROOT with the expected subdirectory layout."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    repo_root = tmp_path / "repo"
    (repo_root / ".autonomous-team").mkdir(parents=True)

    with (
        patch("backend.api._STATE_DIR", state_dir),
        patch("backend.api._REPO_ROOT", repo_root),
    ):
        yield state_dir, repo_root


class TestRpcTokenPriority:
    """_get_dashboard_config must prefer STATE_DIR rpcToken over the shared token file."""

    def test_runtime_token_preferred_over_repo_level_file(self, isolated_dirs):
        """When STATE_DIR/dashboard-runtime.json has a non-empty rpcToken,
        _get_dashboard_config must return that token even when the repo-level
        dashboard-token file contains a different value."""
        state_dir, repo_root = isolated_dirs

        runtime_token = "project-scoped-token-abc123"
        stale_shared_token = "stale-shared-token-xyz999"

        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:8765",
            "rpcToken": runtime_token,
            "dashboardVersion": "0.1.0",
        }))
        (repo_root / ".autonomous-team" / "dashboard-token").write_text(stale_shared_token)

        cfg = _get_dashboard_config()

        assert cfg["rpcToken"] == runtime_token, (
            f"Expected project-scoped token {runtime_token!r} but got {cfg['rpcToken']!r}. "
            "The shared repo-level dashboard-token must NOT override a valid runtime token."
        )

    def test_fallback_to_repo_level_when_runtime_token_empty(self, isolated_dirs):
        """When the STATE_DIR runtime file has an empty rpcToken, fall back to
        the repo-level dashboard-token file."""
        state_dir, repo_root = isolated_dirs

        fallback_token = "fallback-token-fallback111"

        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:8765",
            "rpcToken": "",          # empty — should trigger fallback
            "dashboardVersion": "0.1.0",
        }))
        (repo_root / ".autonomous-team" / "dashboard-token").write_text(fallback_token)

        cfg = _get_dashboard_config()

        assert cfg["rpcToken"] == fallback_token, (
            f"Expected fallback token {fallback_token!r} but got {cfg['rpcToken']!r}. "
            "When runtime rpcToken is empty the repo-level file should be used."
        )

    def test_fallback_to_repo_level_when_runtime_file_absent(self, isolated_dirs):
        """When STATE_DIR/dashboard-runtime.json does not exist, fall back to
        the repo-level dashboard-token file."""
        state_dir, repo_root = isolated_dirs

        fallback_token = "fallback-token-no-runtime"

        # No state_dir runtime file written — it simply doesn't exist.
        (repo_root / ".autonomous-team" / "dashboard-token").write_text(fallback_token)

        cfg = _get_dashboard_config()

        assert cfg["rpcToken"] == fallback_token

    def test_empty_token_when_no_files_exist(self, isolated_dirs):
        """When neither the runtime file nor the repo-level token file exists,
        the returned rpcToken must be an empty string (not an exception)."""
        cfg = _get_dashboard_config()
        assert cfg["rpcToken"] == ""

    def test_rpc_base_url_is_project_scoped(self, isolated_dirs):
        """rpcBaseUrl is already project-scoped via the runtime file — verify it is
        not accidentally overwritten by the token-fallback logic."""
        state_dir, repo_root = isolated_dirs

        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:9999",
            "rpcToken": "some-token",
            "dashboardVersion": "0.1.0",
        }))

        cfg = _get_dashboard_config()

        assert cfg["rpcBaseUrl"] == "http://localhost:9999"
        assert cfg["rpcToken"] == "some-token"

    def test_token_value_not_logged(self, isolated_dirs, caplog):
        """No log message should contain the rpcToken value (token hygiene)."""
        import logging
        state_dir, repo_root = isolated_dirs

        secret_token = "super-secret-token-do-not-log"
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "rpcBaseUrl": "http://localhost:8765",
            "rpcToken": secret_token,
        }))

        with caplog.at_level(logging.DEBUG):
            _get_dashboard_config()

        for record in caplog.records:
            assert secret_token not in record.getMessage(), (
                "Token value found in log output — token hygiene violation."
            )
