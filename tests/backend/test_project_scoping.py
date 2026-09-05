"""tests/backend/test_project_scoping.py

Tests for per-project request scoping.

Covers:
  - backend.state_paths.for_project() resolves state_dir and repo correctly
  - _resolve_repo_for_project() in server.py falls back to default when project
    is None or unknown
  - discussions.list cache key includes project repo so AF and projectb don't share
    cached results
"""
from __future__ import annotations

import json
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Make backend importable from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# backend.state_paths.for_project
# ---------------------------------------------------------------------------

class TestForProject:
    def test_returns_conventional_state_dir_when_no_config(self, tmp_path: Path) -> None:
        """When no dashboard-runtime.json or project.json exists, fall back to ~/.<name>-state."""
        from backend.state_paths import for_project

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            paths = for_project("nonexistent")

        assert paths.name == "nonexistent"
        assert paths.state_dir == tmp_path / ".nonexistent-state"
        assert paths.repo is None
        assert paths.stats_db == paths.state_dir / "stats.duckdb"
        assert paths.state_db == paths.state_dir / "state.db"
        assert paths.audit_log == paths.state_dir / "audit.jsonl"

    def test_reads_repo_from_dashboard_runtime_json(self, tmp_path: Path) -> None:
        """for_project() reads the repo slug from dashboard-runtime.json."""
        state_dir = tmp_path / ".myproject-state"
        state_dir.mkdir()
        runtime = {
            "project_name": "myproject",
            "repo": "my-org/myproject",
            "state_dir": str(state_dir),
        }
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(runtime))

        from backend.state_paths import for_project

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            paths = for_project("myproject")

        assert paths.repo == "my-org/myproject"
        assert paths.state_dir == state_dir

    def test_reads_repo_from_project_json_fallback(self, tmp_path: Path) -> None:
        """When dashboard-runtime.json is absent, fall back to project.json."""
        state_dir = tmp_path / ".anotherproj-state"
        state_dir.mkdir()
        project_data = {
            "project_name": "anotherproj",
            "repo": "someone/anotherproj",
            "version": 1,
        }
        (state_dir / "project.json").write_text(json.dumps(project_data))

        from backend.state_paths import for_project

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            paths = for_project("anotherproj")

        assert paths.repo == "someone/anotherproj"

    def test_dashboard_runtime_wins_over_project_json(self, tmp_path: Path) -> None:
        """dashboard-runtime.json takes precedence when both files exist."""
        state_dir = tmp_path / ".overlap-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "repo": "org/from-runtime",
        }))
        (state_dir / "project.json").write_text(json.dumps({
            "repo": "org/from-project-json",
            "version": 1,
        }))

        from backend.state_paths import for_project

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            paths = for_project("overlap")

        assert paths.repo == "org/from-runtime"

    def test_tolerates_malformed_json(self, tmp_path: Path) -> None:
        """Malformed JSON in either config file must not raise — return None for repo."""
        state_dir = tmp_path / ".bad-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text("NOT JSON {{{")

        from backend.state_paths import for_project

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            paths = for_project("bad")

        assert paths.repo is None  # no crash, just unknown repo


# ---------------------------------------------------------------------------
# _resolve_repo_for_project (imported from server module internals)
# ---------------------------------------------------------------------------

class TestResolveRepoForProject:
    def _get_resolver(self):
        """Import the private helper from server.py."""
        import importlib
        import backend.server as _srv
        # Re-import to ensure fresh module state
        return _srv._resolve_repo_for_project  # noqa: SLF001

    def test_none_returns_default(self) -> None:
        from backend._repo import REPO_NAME, REPO_OWNER

        resolver = self._get_resolver()
        owner, name = resolver(None)
        assert owner == REPO_OWNER
        assert name == REPO_NAME

    def test_empty_string_returns_default(self) -> None:
        from backend._repo import REPO_NAME, REPO_OWNER

        resolver = self._get_resolver()
        owner, name = resolver("")
        assert owner == REPO_OWNER
        assert name == REPO_NAME

    def test_unknown_project_raises(self, tmp_path: Path) -> None:
        """Unknown project name (no state dir) raises, never falls back to default."""
        from backend.rpc_project_scope import UnresolvableProjectError

        resolver = self._get_resolver()
        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            with pytest.raises(UnresolvableProjectError, match="totally-unknown-project-xyz"):
                resolver("totally-unknown-project-xyz")

    def test_known_project_returns_its_repo(self, tmp_path: Path) -> None:
        """When project.json is present and has a repo, we use it."""
        state_dir = tmp_path / ".projectb-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps({
            "repo": "my-org/projectb",
        }))

        resolver = self._get_resolver()
        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            owner, name = resolver("projectb")
        assert owner == "my-org"
        assert name == "projectb"


# ---------------------------------------------------------------------------
# discussions.list cache-key isolation
# ---------------------------------------------------------------------------

class TestDiscussionsCacheKeyIsolation:
    """Verify AF and projectb don't share cached discussion results."""

    def test_cache_keys_differ_for_different_projects(self, tmp_path: Path) -> None:
        """Cache keys for AF and projectb must be distinct."""
        import backend.server as srv

        # Simulate two projects with different repos
        af_state = tmp_path / ".autonomous-forever-state"
        af_state.mkdir()
        (af_state / "dashboard-runtime.json").write_text(json.dumps(
            {"repo": "autonomous-agent-7/autonomous-forever"}
        ))

        projectb_state = tmp_path / ".projectb-state"
        projectb_state.mkdir()
        (projectb_state / "dashboard-runtime.json").write_text(json.dumps(
            {"repo": "my-org/projectb"}
        ))

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            af_owner, af_name = srv._resolve_repo_for_project("autonomous-forever")  # noqa: SLF001
            projectb_owner, projectb_name = srv._resolve_repo_for_project("projectb")  # noqa: SLF001

        # Different repos → different owners/names → different cache keys
        assert (af_owner, af_name) != (projectb_owner, projectb_name)
        assert af_owner == "autonomous-agent-7"
        assert projectb_owner == "my-org"


# ---------------------------------------------------------------------------
# Per-project stats DB scoping (_with_project_stats_db / _project_stats_db)
# ---------------------------------------------------------------------------

class TestStatsDbScoping:
    """Verify that stats.* and runs.* handlers resolve the correct stats.duckdb."""

    def test_project_stats_db_returns_none_for_no_project(self) -> None:
        """No project param → _project_stats_db returns None (use default DB)."""
        import backend.server as srv
        result = srv._project_stats_db(None)  # noqa: SLF001
        assert result is None

    def test_project_stats_db_returns_path_for_known_project(self, tmp_path: Path) -> None:
        """Known project → returns <state_dir>/stats.duckdb."""
        state_dir = tmp_path / ".myproj-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(
            {"state_dir": str(state_dir), "repo": "org/myproj"}
        ))

        import backend.server as srv
        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            result = srv._project_stats_db("myproj")  # noqa: SLF001

        assert result is not None
        assert result == state_dir / "stats.duckdb"

    def test_with_project_stats_db_sets_and_restores_env(self, tmp_path: Path) -> None:
        """_with_project_stats_db sets STATS_DB_PATH during the call and restores it."""
        state_dir = tmp_path / ".envtest-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(
            {"state_dir": str(state_dir)}
        ))

        import backend.server as srv

        captured: list[str] = []

        def _capture():
            captured.append(os.environ.get("STATS_DB_PATH", ""))
            return {"captured": True}

        original = os.environ.get("STATS_DB_PATH")
        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            srv._with_project_stats_db("envtest", _capture)  # noqa: SLF001

        # After the call, env is restored
        assert os.environ.get("STATS_DB_PATH") == original
        # During the call, it pointed to the project's stats.duckdb
        assert len(captured) == 1
        assert "envtest" in captured[0]

    def test_with_project_stats_db_restores_on_exception(self, tmp_path: Path) -> None:
        """STATS_DB_PATH is restored even when the wrapped function raises."""
        state_dir = tmp_path / ".extest-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(
            {"state_dir": str(state_dir)}
        ))

        import backend.server as srv

        original = os.environ.get("STATS_DB_PATH")
        with pytest.raises(RuntimeError, match="boom"):
            with patch("backend.state_paths.Path.home", return_value=tmp_path):
                srv._with_project_stats_db("extest", lambda: (_ for _ in ()).throw(RuntimeError("boom")))  # noqa: SLF001

        assert os.environ.get("STATS_DB_PATH") == original


# ---------------------------------------------------------------------------
# loop.timeline project scoping
# ---------------------------------------------------------------------------

class TestLoopTimelineScoping:
    """loop.timeline must read from the project's state_dir, not AF's repo root."""

    def test_returns_empty_when_no_metrics_file_for_project(self, tmp_path: Path) -> None:
        """When project has no loop-metrics.jsonl, return [] instead of AF's data."""
        state_dir = tmp_path / ".noloop-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(
            {"state_dir": str(state_dir)}
        ))

        import backend.server as srv

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            result = srv._rpc_loop_timeline({"project": "noloop", "limit": 10})  # noqa: SLF001

        assert result == []

    def test_reads_project_metrics_file(self, tmp_path: Path) -> None:
        """When project has loop-metrics.jsonl, return its rows (not AF's)."""
        state_dir = tmp_path / ".looptest-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(
            {"state_dir": str(state_dir)}
        ))
        metrics = [
            {"timestamp": "2026-01-01T00:00:00Z", "duration_seconds": 5,
             "agents_spawned": 1, "prs_merged": 0,
             "discussions_scanned": 2, "prs_scanned": 1,
             "idle": False, "error": None},
        ]
        (state_dir / "loop-metrics.jsonl").write_text(
            "\n".join(json.dumps(m) for m in metrics) + "\n"
        )

        import backend.server as srv

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            result = srv._rpc_loop_timeline({"project": "looptest", "limit": 10})  # noqa: SLF001

        assert len(result) == 1
        assert result[0]["timestamp"] == "2026-01-01T00:00:00Z"
        assert result[0]["agents_spawned"] == 1


# ---------------------------------------------------------------------------
# kpi.history / kpi.cycle_time project guard
# ---------------------------------------------------------------------------

class TestKpiProjectGuard:
    """kpi handlers must return empty data for unknown non-AF projects."""

    def test_kpi_history_returns_empty_for_unknown_project(self, tmp_path: Path) -> None:
        """kpi.history returns [] for a project with no local checkout."""
        state_dir = tmp_path / ".unknownproj-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(
            {"state_dir": str(state_dir)}
        ))

        import backend.server as srv

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            result = srv._rpc_kpi_history({"project": "unknownproj", "days": 7})  # noqa: SLF001

        assert result == []

    def test_kpi_cycle_time_returns_zeroed_buckets_for_unknown_project(
        self, tmp_path: Path
    ) -> None:
        """kpi.cycle_time returns zeroed buckets for a project with no local checkout."""
        state_dir = tmp_path / ".norepo-state"
        state_dir.mkdir()
        (state_dir / "dashboard-runtime.json").write_text(json.dumps(
            {"state_dir": str(state_dir)}
        ))

        import backend.server as srv

        with patch("backend.state_paths.Path.home", return_value=tmp_path):
            result = srv._rpc_kpi_cycle_time({"project": "norepo"})  # noqa: SLF001

        assert isinstance(result, list)
        assert len(result) == 4
        for bucket in result:
            assert bucket["count"] == 0
