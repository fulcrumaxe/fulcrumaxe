"""tests/backend/test_fleet_concurrency_rpc.py — Tests for fleet.concurrency RPC handler.

Two things this file pins down.

BUG 7 (original): fleet.concurrency was not registered in server.py and
returned a shape the tile could not consume, causing a runtime TypeError in
FleetConcurrencyTile. The registration and delegation checks below are that.

D#2323: the handler returned ``fleet_total`` from the *unfiltered*
``count_fleet()`` and the tile rendered it against ``fleet_cap`` — a ratio
between two populations, which on the operator host read "21 of 8". The
handler now returns the cap-governed count and the observational
(``agent-tool-``) count as separate fields and never sums them; and an
unreadable fleet state returns an explicit unavailable result instead of a
confident zero.

Run with:
    AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)" \
        python3 -m pytest -x -q tests/backend/test_fleet_concurrency_rpc.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


class TestFleetConcurrencyRpcRegistration:
    """BUG 7: fleet.concurrency must be registered in server.py _METHODS table."""

    def test_fleet_concurrency_registered_in_server(self):
        """server.py must have a @_rpc_method('fleet.concurrency') registration."""
        server_py = Path(__file__).resolve().parent.parent.parent / "backend" / "server.py"
        content = server_py.read_text()
        assert '"fleet.concurrency"' in content, (
            "fleet.concurrency is not registered in backend/server.py — "
            "FleetConcurrencyTile will show 'method not found'"
        )

    def test_fleet_concurrency_handler_importable(self):
        """backend/rpc/fleet_concurrency.py must exist and be importable."""
        from backend.rpc import fleet_concurrency  # noqa: F401
        assert hasattr(fleet_concurrency, "handle"), (
            "fleet_concurrency module must have a handle() function"
        )


@pytest.fixture
def fleet_dir(tmp_path, monkeypatch):
    """Redirect fleet state to a throwaway directory for one test.

    Nothing here may reach ~/.autonomous-fleet-state/fleet.db. The handler
    reads FLEET_DB_PATH off the module at call time, so patching the module
    attribute is what the handler actually sees.
    """
    import backend.fleet.concurrency as fc
    monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
    monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
    monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")
    return tmp_path


@pytest.fixture
def one_project(monkeypatch):
    """Make discovery deterministic — one project, no $HOME scanning."""
    import backend.fleet.discovery as disc
    monkeypatch.setattr(disc, "discover_projects", lambda: [{"name": "af", "ok": True}])


def _create_empty_db():
    """Create fleet.db with an agents table and no rows."""
    import backend.fleet.concurrency as fc
    fc._open_db().close()


def _register_lane_rows(project: str, spawn_lane: int, agent_tool: int) -> None:
    """Register *spawn_lane* ordinary rows and *agent_tool* observational ones."""
    import backend.fleet.concurrency as fc
    for i in range(spawn_lane):
        assert fc.register(project, f"spawn-{i}", "executor") is True
    for i in range(agent_tool):
        assert fc.register(project, f"{fc.AGENT_TOOL_ID_PREFIX}{i:016x}", "specialist") is True


class TestLanesAreReportedSeparately:
    """D#2323 items 1-2: the cap-governed count and the rest, never summed."""

    def test_response_keys(self, fleet_dir, one_project):
        from backend.rpc.fleet_concurrency import handle
        _create_empty_db()
        result = handle(params=None)
        assert result["available"] is True
        for key in ("fleet_cap", "capped_agents", "uncapped_agents", "per_project"):
            assert key in result, f"missing {key!r}"
        assert isinstance(result["fleet_cap"], int) and result["fleet_cap"] > 0

    def test_twenty_one_rows_one_capped(self, fleet_dir, one_project):
        """The measured shape from D#2314's review: 21 rows, 1 of them capped.

        The pre-fix handler answered 21 against a cap of 8 for both the
        headline and the project row. Nothing in this response may say 21.
        """
        import json
        from backend.fleet.concurrency import count_fleet
        from backend.rpc.fleet_concurrency import handle

        (fleet_dir / "config.json").write_text(json.dumps({"fleet_cap": 8}))
        _register_lane_rows("af", spawn_lane=1, agent_tool=20)
        assert count_fleet() == 21, "precondition: 21 total rows"

        result = handle(params=None)

        assert result["fleet_cap"] == 8
        assert result["capped_agents"] == 1
        assert result["uncapped_agents"] == 20
        entry = result["per_project"][0]
        assert entry["name"] == "af"
        assert entry["capped_agents"] == 1
        assert entry["uncapped_agents"] == 20
        assert entry["cap"] == 8
        assert 21 not in (result["capped_agents"], entry["capped_agents"])

    def test_redefined_fields_are_gone(self, fleet_dir, one_project):
        """``fleet_total``/``agents_running`` were dropped, not redefined.

        Reusing either name under the corrected meaning would leave a reader
        who knew the old one silently misinformed — which is the defect.
        """
        from backend.rpc.fleet_concurrency import handle
        _create_empty_db()
        result = handle(params=None)
        assert "fleet_total" not in result
        assert "agents" not in result, "'agents' is a legacy field — use per_project"
        assert "count" not in result, "'count' is a legacy field"
        assert "agents_running" not in result["per_project"][0]

    def test_capped_count_matches_the_cap_check_population(self, fleet_dir, one_project):
        """capped_agents must equal what register()'s own cap check counts."""
        from backend.fleet.concurrency import count_fleet_capped
        from backend.rpc.fleet_concurrency import handle

        _register_lane_rows("af", spawn_lane=2, agent_tool=3)
        result = handle(params=None)
        assert result["capped_agents"] == count_fleet_capped() == 2

    def test_unhealthy_project_reports_zero_in_both_lanes(self, fleet_dir, monkeypatch):
        import backend.fleet.discovery as disc
        from backend.rpc.fleet_concurrency import handle

        monkeypatch.setattr(
            disc, "discover_projects",
            lambda: [{"name": "broken", "ok": False, "error": "unreadable project.json"}],
        )
        _create_empty_db()
        entry = handle(params=None)["per_project"][0]
        assert entry["ok"] is False
        assert entry["capped_agents"] == 0
        assert entry["uncapped_agents"] == 0
        assert entry["error"] == "unreadable project.json"


class TestUnreadableStateIsNotZero:
    """D#2323 items 5-7: absent database vs present-and-empty are distinguishable."""

    def test_absent_db_is_unavailable_with_a_reason(self, fleet_dir, one_project):
        from backend.rpc.fleet_concurrency import handle

        assert not (fleet_dir / "fleet.db").exists(), "precondition: no database"
        result = handle(params=None)

        assert result["available"] is False
        assert result["capped_agents"] is None
        assert result["uncapped_agents"] is None
        assert result["per_project"] == []
        assert isinstance(result["unavailable_reason"], str)
        assert result["unavailable_reason"].strip(), "reason must not be empty"

    def test_read_does_not_create_the_database(self, fleet_dir, one_project):
        """A read path that instantiates the state it failed to find is how
        'unavailable' turned into '0' in the first place."""
        from backend.rpc.fleet_concurrency import handle
        handle(params=None)
        assert not (fleet_dir / "fleet.db").exists()

    def test_present_but_empty_table_is_a_real_zero(self, fleet_dir, one_project):
        from backend.rpc.fleet_concurrency import handle

        _create_empty_db()
        assert (fleet_dir / "fleet.db").exists(), "precondition: database exists"
        result = handle(params=None)

        assert result["available"] is True
        assert result["capped_agents"] == 0
        assert result["uncapped_agents"] == 0
        assert "unavailable_reason" not in result

    def test_handler_failure_is_unavailable_not_zero(self, fleet_dir, one_project, monkeypatch):
        """Any raise inside the handler must surface as unavailable."""
        import sqlite3
        import backend.fleet.concurrency as fc
        from backend.rpc.fleet_concurrency import handle

        _create_empty_db()

        def boom():
            raise sqlite3.OperationalError("database disk image is malformed")

        monkeypatch.setattr(fc, "count_fleet_capped", boom)
        result = handle(params=None)

        assert result["available"] is False
        assert result["capped_agents"] is None
        assert "malformed" in result["unavailable_reason"]


class TestFleetConcurrencyStaleReap:
    """RPC read path must prune stale entries before counting (D#987 regression)."""

    def _insert_stale_entry(self, project: str, agent_id: str, role: str, age_seconds: int) -> None:
        """Insert a fleet row with a backdated started_at to simulate a crashed agent."""
        import backend.fleet.concurrency as fc
        from datetime import datetime, timezone
        import time

        stale_ts = datetime.fromtimestamp(
            time.time() - age_seconds, tz=timezone.utc
        ).isoformat()
        conn = fc._open_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO agents (project_name, agent_id, role, started_at) VALUES (?, ?, ?, ?)",
                (project, agent_id, role, stale_ts),
            )
        finally:
            conn.close()

    def test_stale_entry_reaped_on_rpc_call(self, fleet_dir, one_project):
        from backend.fleet.concurrency import count_fleet
        from backend.rpc.fleet_concurrency import handle

        self._insert_stale_entry("af", "zombie-agent", "executor", age_seconds=3 * 3600)
        assert count_fleet() == 1, "stale entry should be visible before reap"

        result = handle(params=None)

        assert result["capped_agents"] == 0, (
            f"capped_agents should be 0 after stale reap, got {result['capped_agents']}"
        )
        assert count_fleet() == 0, "stale row should be deleted from DB after handle() call"

    def test_fresh_entry_not_reaped(self, fleet_dir, one_project):
        from backend.fleet.concurrency import register
        from backend.rpc.fleet_concurrency import handle

        register("af", "fresh-agent", "executor")
        result = handle(params=None)

        assert result["capped_agents"] >= 1, "fresh agent should not be reaped"

    def test_mixed_stale_and_fresh(self, fleet_dir, one_project):
        from backend.fleet.concurrency import register, count_fleet
        from backend.rpc.fleet_concurrency import handle

        register("af", "live-agent", "executor")
        self._insert_stale_entry("af", "zombie-agent", "executor", age_seconds=3 * 3600)
        assert count_fleet() == 2, "both entries visible before reap"

        result = handle(params=None)

        assert result["capped_agents"] == 1, (
            f"capped_agents should be 1 (fresh only), got {result['capped_agents']}"
        )
        assert count_fleet() == 1, "only stale row should be deleted"


class TestFleetConcurrencyServerDispatch:
    """Integration: verify server.py dispatches fleet.concurrency correctly."""

    def test_server_dispatch_table_contains_fleet_concurrency(self):
        """After importing server, the _RPC_METHODS dict must have fleet.concurrency."""
        import backend.server as server
        methods = getattr(server, "_RPC_METHODS", {})
        assert "fleet.concurrency" in methods, (
            "server._RPC_METHODS missing 'fleet.concurrency' — registration decorator did not fire"
        )
