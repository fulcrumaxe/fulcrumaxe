"""Tests for the ideas API endpoint — _load_ideas and /api/ideas response shape."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_api_mod():
    """Import backend.api with heavy deps mocked out."""
    mocks = [
        "backend.agent_cards",
        "backend.audit_trail",
        "backend.api_version",
        "backend.plugin_loader",
        "backend.budget",
        "backend.cost_tracker",
        "backend.config_watcher",
        "backend.control_plane",
        "backend.dashboard",
        "backend.event_bus",
        "backend.health_monitor",
        "backend.kpi_engine",
        "backend.module_health",
        "backend.dep_graph",
        "backend.metrics",
        "backend.rate_limiter",
        "backend.rbac",
        "backend.registry",
    ]
    for mod in mocks:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    import backend.api as api_mod  # noqa: PLC0415
    return api_mod


# ---------------------------------------------------------------------------
# _load_ideas — unit tests
# ---------------------------------------------------------------------------

class TestLoadIdeas:
    def test_empty_dir_returns_empty_list_and_source_empty_true(self, tmp_path, monkeypatch):
        """When the ideas directory has no JSON files, source_empty is True."""
        api_mod = _get_api_mod()
        ideas_dir = tmp_path / "ideas"
        ideas_dir.mkdir()
        monkeypatch.setattr(api_mod, "_REPO_ROOT", tmp_path)

        ideas_list, is_empty = api_mod._load_ideas()

        assert ideas_list == []
        assert is_empty is True

    def test_with_ideas_returns_ideas_and_source_empty_false(self, tmp_path, monkeypatch):
        """When the ideas directory has JSON files, source_empty is False."""
        api_mod = _get_api_mod()
        ideas_dir = tmp_path / ".autonomous-team" / "blackboard" / "ideas"
        ideas_dir.mkdir(parents=True)
        idea = {
            "id": "test-idea",
            "title": "Test idea",
            "summary": "A real idea from the project-manager.",
            "votes": 2,
            "status": "pending",
            "created_at": "2026-05-10T10:00:00Z",
        }
        (ideas_dir / "test-idea.json").write_text(json.dumps(idea))
        monkeypatch.setattr(api_mod, "_REPO_ROOT", tmp_path)

        ideas_list, is_empty = api_mod._load_ideas()

        assert len(ideas_list) == 1
        assert ideas_list[0]["id"] == "test-idea"
        assert is_empty is False

    def test_sorts_by_votes_desc(self, tmp_path, monkeypatch):
        """Ideas are sorted by votes descending."""
        api_mod = _get_api_mod()
        ideas_dir = tmp_path / ".autonomous-team" / "blackboard" / "ideas"
        ideas_dir.mkdir(parents=True)
        for i, votes in enumerate([1, 5, 3]):
            idea = {
                "id": f"idea-{i}",
                "title": f"Idea {i}",
                "summary": "",
                "votes": votes,
                "status": "pending",
                "created_at": "2026-05-10T10:00:00Z",
            }
            (ideas_dir / f"idea-{i}.json").write_text(json.dumps(idea))
        monkeypatch.setattr(api_mod, "_REPO_ROOT", tmp_path)

        ideas_list, _ = api_mod._load_ideas()

        assert ideas_list[0]["votes"] == 5
        assert ideas_list[1]["votes"] == 3
        assert ideas_list[2]["votes"] == 1

    def test_no_auto_seed_on_empty_dir(self, tmp_path, monkeypatch):
        """Empty ideas dir must NOT be populated with seed fixtures."""
        api_mod = _get_api_mod()
        ideas_dir = tmp_path / ".autonomous-team" / "blackboard" / "ideas"
        ideas_dir.mkdir(parents=True)
        monkeypatch.setattr(api_mod, "_REPO_ROOT", tmp_path)

        ideas_list, is_empty = api_mod._load_ideas()

        assert ideas_list == [], "seed fixtures must not appear in an empty dir"
        assert is_empty is True
        # Confirm no JSON files were written
        assert list(ideas_dir.glob("*.json")) == []
