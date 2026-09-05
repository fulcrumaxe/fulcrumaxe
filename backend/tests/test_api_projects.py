"""Tests for backend/api.py project liveness probe (D#2314).

The old version of this file was the bug, not just a gap in it: an autouse
fixture that changed the working directory to a temp dir for every test,
plus every test passing the same one hardcoded placeholder project name,
meant no test here could ever have caught a cross-project leak — the suite
structurally could not express "two different projects disagree." It has
been rewritten from scratch rather than extended; every test below names
two distinct projects.

`_probe_liveness` no longer reads any cron-adjacent signal
(`active-loops.json`, `/tmp/af-trigger.fifo`, `now.md` are gone — see
backend/api.py). It reads live rows from fleet.db, scoped by project name.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.api import _enrich_project, _probe_liveness, _resolve_fleet_project_name  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Fleet isolation — concurrency.py reads AUTONOMOUS_FLEET_STATE_DIR into
# module-level constants at import time (concurrency.py:64-67), so tests
# monkeypatch the constants directly rather than relying on the env var
# having been set before the module's first import. Same pattern as
# backend/tests/test_fleet_concurrency.py's isolated_fleet_db fixture.
# ---------------------------------------------------------------------------


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Point backend.fleet.concurrency at a throwaway fleet dir for this test."""
    import backend.fleet.concurrency as fc

    fleet_dir = tmp_path / "fleet-state"
    fleet_dir.mkdir()
    monkeypatch.setattr(fc, "FLEET_STATE_DIR", fleet_dir)
    monkeypatch.setattr(fc, "FLEET_DB_PATH", fleet_dir / "fleet.db")
    monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", fleet_dir / "config.json")
    return fc


# ---------------------------------------------------------------------------
# Real-process positive -> negative, one variable (Spec item 5)
# ---------------------------------------------------------------------------


def test_active_with_live_pid_idle_after_kill(fleet):
    """Same row, opposite answers, with a real PID as the only variable —
    and no reap_stale() call in between (the read must filter dead PIDs
    on its own)."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        fleet.register("proj-a", "agent-x", "executor", pid=proc.pid)
        assert _probe_liveness("proj-a") == "active"

        proc.kill()
        proc.wait(timeout=10)

        assert _probe_liveness("proj-a") == "idle"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Cross-project isolation (Spec item 6) — the check the old suite could not
# express, because every test in it used one shared placeholder project name.
# ---------------------------------------------------------------------------


def test_cross_project_isolation(fleet):
    fleet.register("proj-a", "agent-1", "executor", pid=os.getpid())
    assert _probe_liveness("proj-a") == "active"
    assert _probe_liveness("proj-b") == "idle"


def test_registering_one_project_never_changes_another(fleet):
    """Registering under proj-a must not move proj-b's reading at all,
    before or after — not just 'both eventually settle correctly'."""
    assert _probe_liveness("proj-a") == "idle"
    assert _probe_liveness("proj-b") == "idle"

    fleet.register("proj-a", "agent-1", "executor", pid=os.getpid())

    assert _probe_liveness("proj-a") == "active"
    assert _probe_liveness("proj-b") == "idle"


# ---------------------------------------------------------------------------
# No-signal path (Spec item 9) — unresolvable name or a failed read must
# answer 'unknown', never 'idle'.
# ---------------------------------------------------------------------------


def test_unknown_when_project_name_empty(fleet):
    assert _probe_liveness("") == "unknown"


def test_unknown_when_fleet_db_read_raises(fleet):
    """A corrupt fleet.db must surface as 'unknown', not a silent 'idle'."""
    fleet.FLEET_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fleet.FLEET_DB_PATH.write_bytes(b"not a sqlite database")
    assert _probe_liveness("proj-a") == "unknown"


def test_idle_not_unknown_when_db_simply_has_no_rows(fleet):
    """A missing/empty fleet.db (nothing has ever registered) is 'idle',
    not 'unknown' — the absence of agents is itself a real, testable answer,
    distinct from 'the read failed'."""
    assert _probe_liveness("proj-a") == "idle"


# ---------------------------------------------------------------------------
# HTTP surface, not just the function (Spec item 7) — a probe that is
# correct but wired to nothing would pass every test above and still ship
# the original bug.
# ---------------------------------------------------------------------------


def _make_client():
    from backend.asgi_app import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


class TestLivenessHttpSurface:
    def test_liveness_and_active_agents_flip_through_the_endpoint(self, fleet, monkeypatch):
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        projects = [{"id": "proj-http", "name": "proj-http", "repo": "x/y", "createdAt": ""}]
        with patch("backend.api._load_projects_raw", return_value=projects):
            client = _make_client()

            idle_resp = client.get("/api/projects")
            assert idle_resp.status_code == 200
            idle_body = idle_resp.json()[0]
            assert idle_body["liveness"] == "idle"
            assert idle_body.get("activeAgents") == 0

            fleet.register("proj-http", "agent-http-1", "executor", pid=os.getpid())

            active_resp = client.get("/api/projects")
            active_body = active_resp.json()[0]
            assert active_body["liveness"] == "active"
            assert active_body["activeAgents"] == 1

            fleet.unregister("proj-http", "agent-http-1")

            idle_again_resp = client.get("/api/projects")
            idle_again_body = idle_again_resp.json()[0]
            assert idle_again_body["liveness"] == "idle"

    def test_no_signal_project_carries_no_active_agents_number(self, fleet, monkeypatch):
        """Spec item 9, through the wire: a project whose fleet key can't be
        resolved gets 'unknown' and NO activeAgents field — not 0."""
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        # No "id"/"name" at all -> _resolve_fleet_project_name returns "".
        projects = [{"id": "", "name": "", "repo": "x/y", "createdAt": ""}]
        with patch("backend.api._load_projects_raw", return_value=projects):
            client = _make_client()
            resp = client.get("/api/projects")

        body = resp.json()[0]
        assert body["liveness"] == "unknown"
        assert "activeAgents" not in body or body["activeAgents"] is None


# ---------------------------------------------------------------------------
# Name-mismatch regression (Spec item 8) — the D1 defect: the read side and
# the write side must resolve the byte-identical project name from the one
# shared resolver. Registration goes through the bash-callable CLI
# (scripts/pre-spawn-check.sh); reads go through the Python import
# (backend/api.py). This asserts both paths into backend.fleet.project_name
# agree.
# ---------------------------------------------------------------------------


def test_resolver_cli_and_python_import_agree(tmp_path):
    (tmp_path / ".autonomous-team").mkdir()
    (tmp_path / ".autonomous-team" / "config.json").write_text(json.dumps({
        "repo": "someowner/somerepo",
        "project_name": "somerepo",
    }))

    from backend.fleet.project_name import resolve_project_name
    python_side = resolve_project_name(tmp_path)

    cli = subprocess.run(
        [sys.executable, "-m", "backend.fleet.project_name", str(tmp_path)],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, check=True,
    )
    cli_side = cli.stdout.strip()

    assert python_side == cli_side == "somerepo"


def test_resolver_falls_back_to_repo_key_when_project_name_absent(tmp_path):
    """D1: config.json missing 'project_name' used to silently fall back to
    a hardcoded 'autonomous-forever' on the write side. The resolver must
    derive from 'repo' instead — loudly, not silently to the wrong value."""
    (tmp_path / ".autonomous-team").mkdir()
    (tmp_path / ".autonomous-team" / "config.json").write_text(json.dumps({
        "repo": "autonomous-agent-7/fulcrumaxe",
    }))

    from backend.fleet.project_name import resolve_project_name
    assert resolve_project_name(tmp_path) == "fulcrumaxe"


def test_resolver_raises_loudly_when_unresolvable(tmp_path):
    from backend.fleet.project_name import ProjectNameUnresolvable, resolve_project_name

    (tmp_path / ".autonomous-team").mkdir()
    (tmp_path / ".autonomous-team" / "config.json").write_text(json.dumps({"unrelated": True}))

    with pytest.raises(ProjectNameUnresolvable):
        resolve_project_name(tmp_path)


# ---------------------------------------------------------------------------
# _resolve_fleet_project_name — the id-vs-name inconsistency (D#2314's other
# half of D1): the probe used to key off `id`, activeAgents off `name`.
# ---------------------------------------------------------------------------


def test_resolve_fleet_project_name_uses_display_name_for_non_primary():
    result = _resolve_fleet_project_name({"id": "some-adopter", "name": "some-adopter"})
    assert result == "some-adopter"


def test_resolve_fleet_project_name_falls_back_to_id_when_name_missing():
    result = _resolve_fleet_project_name({"id": "proj-a"})
    assert result == "proj-a"


def test_resolve_fleet_project_name_uses_resolver_for_primary_project():
    from backend._repo import REPO_NAME

    with patch("backend.fleet.project_name.resolve_project_name", return_value="resolved-name"):
        result = _resolve_fleet_project_name({"id": REPO_NAME, "name": "some-other-display-name"})
    assert result == "resolved-name"


# ---------------------------------------------------------------------------
# _get_dashboard_config — state-dir runtime.json takes priority
# (unrelated to the liveness probe; kept from the previous version of this
# file, unmodified in substance).
# ---------------------------------------------------------------------------


def test_dashboard_config_reads_state_dir_first(tmp_path, monkeypatch):
    """State-dir dashboard-runtime.json provides rpcBaseUrl."""
    import backend.api as api_mod
    from backend.api import _get_dashboard_config

    state_runtime = tmp_path / "dashboard-runtime.json"
    state_runtime.write_text(json.dumps({
        "rpcBaseUrl": "http://localhost:9999",
        "dashboardVersion": "1.2.3",
    }))

    with patch.object(api_mod, "_STATE_DIR", tmp_path), \
         patch.object(api_mod, "_REPO_ROOT", tmp_path):
        result = _get_dashboard_config()

    assert result["rpcBaseUrl"] == "http://localhost:9999"


def test_dashboard_config_fallback_to_repo_side(tmp_path, monkeypatch):
    """Falls back to repo-side runtime.json when state-dir file is absent."""
    import backend.api as api_mod
    from backend.api import _get_dashboard_config

    repo_runtime = tmp_path / ".autonomous-team" / "dashboard-runtime.json"
    repo_runtime.parent.mkdir(exist_ok=True)
    repo_runtime.write_text(json.dumps({"rpcBaseUrl": "http://repo-side:8765"}))

    with patch.object(api_mod, "_STATE_DIR", tmp_path / "empty-state"), \
         patch.object(api_mod, "_REPO_ROOT", tmp_path):
        result = _get_dashboard_config()

    assert result["rpcBaseUrl"] == "http://repo-side:8765"


def test_dashboard_config_defaults_when_no_files(tmp_path, monkeypatch):
    """Returns hard-coded defaults when neither runtime file exists."""
    import backend.api as api_mod
    from backend.api import _get_dashboard_config

    with patch.object(api_mod, "_STATE_DIR", tmp_path / "no-state"), \
         patch.object(api_mod, "_REPO_ROOT", tmp_path):
        result = _get_dashboard_config()

    assert result["rpcBaseUrl"] == "http://localhost:8765"


# ---------------------------------------------------------------------------
# _enrich_project — 'primary' flag (D#2234)
# ---------------------------------------------------------------------------


def test_enrich_project_marks_repo_owner_primary(fleet):
    """'primary' is True only for the project whose id is this repo's own name."""
    from backend._repo import REPO_NAME

    own = _enrich_project({"id": REPO_NAME, "name": REPO_NAME, "repo": "x/y", "createdAt": ""})
    assert own["primary"] is True

    other = _enrich_project({"id": "some-adopter", "name": "some-adopter", "repo": "a/b", "createdAt": ""})
    assert other["primary"] is False
