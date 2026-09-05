"""tests/backend/test_fleet_concurrency_rpc.py — Tests for fleet.concurrency RPC handler.

BUG 7 fix: fleet.concurrency was not registered in server.py and returned the
wrong shape, causing a runtime TypeError in FleetConcurrencyTile.

Handler now returns the shape the tile expects:
  {fleet_total, fleet_cap, per_project: [{name, agents_running, cap, ok}]}

Verifies:
  - fleet.concurrency is registered in server.py dispatch table
  - fleet_concurrency.handle() returns the tile-compatible shape
  - Empty fleet returns fleet_total=0, per_project=[] (first-boot valid state)
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


class TestFleetConcurrencyHandlerShape:
    """fleet_concurrency.handle() must return the shape FleetConcurrencyTile expects."""

    @pytest.fixture(autouse=True)
    def isolated_fleet_dir(self, tmp_path, monkeypatch):
        """Redirect fleet state to a temp directory for each test."""
        import backend.fleet.concurrency as fc
        monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
        monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
        monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")
        yield tmp_path

    def test_empty_fleet_returns_tile_shape(self):
        """Empty fleet must return fleet_total=0, fleet_cap>0, per_project=[]."""
        from backend.rpc.fleet_concurrency import handle
        result = handle(params=None)
        assert isinstance(result.get("fleet_total"), int), "missing 'fleet_total'"
        assert result["fleet_total"] == 0
        assert isinstance(result.get("fleet_cap"), int), "missing 'fleet_cap'"
        assert result["fleet_cap"] > 0
        assert isinstance(result.get("per_project"), list), "missing 'per_project'"

    def test_response_has_required_keys(self):
        """Response must contain fleet_total, fleet_cap, per_project."""
        from backend.rpc.fleet_concurrency import handle
        result = handle(params=None)
        assert "fleet_total" in result, "missing 'fleet_total' field"
        assert "fleet_cap" in result, "missing 'fleet_cap' field"
        assert "per_project" in result, "missing 'per_project' field"

    def test_old_fields_not_present(self):
        """Old response fields (agents/count) must NOT be top-level keys.

        The tile never reads these — their presence caused confusion and masked
        the real missing fields until the tile crashed at runtime.
        """
        from backend.rpc.fleet_concurrency import handle
        result = handle(params=None)
        # These old top-level keys are no longer part of the contract
        assert "agents" not in result, "'agents' is a legacy field — use per_project instead"
        assert "count" not in result, "'count' is a legacy field — use fleet_total instead"

    def test_per_project_entry_shape_via_fake_discover(self, isolated_fleet_dir):
        """Each per_project entry must have name, agents_running, cap, ok."""
        import backend.fleet.discovery as disc_mod
        from backend.rpc.fleet_concurrency import handle

        original = disc_mod.discover_projects

        def fake_discover():
            return [{"name": "testproj", "ok": True}]

        disc_mod.discover_projects = fake_discover
        try:
            result = handle(params=None)
        finally:
            disc_mod.discover_projects = original

        assert len(result["per_project"]) == 1
        entry = result["per_project"][0]
        assert entry["name"] == "testproj"
        assert "agents_running" in entry, "per_project entry missing 'agents_running'"
        assert "cap" in entry, "per_project entry missing 'cap'"
        assert "ok" in entry, "per_project entry missing 'ok'"

    def test_fleet_total_increments_with_agent(self, isolated_fleet_dir):
        """When an agent is registered, fleet_total reflects it."""
        from backend.fleet.concurrency import register
        from backend.rpc.fleet_concurrency import handle

        register("af", "agent-1", "executor")
        result = handle(params=None)
        assert result["fleet_total"] >= 1

    def test_fleet_total_matches_registered_count(self, isolated_fleet_dir):
        """fleet_total must match the actual number of registered agents."""
        from backend.fleet.concurrency import count_fleet, register
        from backend.rpc.fleet_concurrency import handle

        register("af", "agent-1", "executor")
        register("af", "agent-2", "code-reviewer")
        result = handle(params=None)
        assert result["fleet_total"] == count_fleet()


class TestFleetConcurrencyStaleReap:
    """RPC read path must prune stale entries before returning counts (D#987 regression)."""

    @pytest.fixture(autouse=True)
    def isolated_fleet_dir(self, tmp_path, monkeypatch):
        """Redirect fleet state to a temp directory for each test."""
        import backend.fleet.concurrency as fc
        monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
        monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
        monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")
        yield tmp_path

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

    def test_stale_entry_reaped_on_rpc_call(self, isolated_fleet_dir):
        """A stale entry (>2h) must be removed when handle() is called, and fleet_total drops."""
        from backend.fleet.concurrency import count_fleet
        from backend.rpc.fleet_concurrency import handle

        # Insert one stale entry (3h old — well past the 2h TTL)
        self._insert_stale_entry("af", "zombie-agent", "executor", age_seconds=3 * 3600)
        assert count_fleet() == 1, "stale entry should be visible before reap"

        result = handle(params=None)

        assert result["fleet_total"] == 0, (
            f"fleet_total should be 0 after stale reap, got {result['fleet_total']}"
        )
        assert count_fleet() == 0, "stale row should be deleted from DB after handle() call"

    def test_fresh_entry_not_reaped(self, isolated_fleet_dir):
        """A recently registered agent (1 minute old) must survive the reap."""
        from backend.fleet.concurrency import register
        from backend.rpc.fleet_concurrency import handle

        register("af", "fresh-agent", "executor")
        result = handle(params=None)

        assert result["fleet_total"] >= 1, "fresh agent should not be reaped"

    def test_mixed_stale_and_fresh(self, isolated_fleet_dir):
        """Only stale entries are pruned; fresh entries remain and count correctly."""
        from backend.fleet.concurrency import register, count_fleet
        from backend.rpc.fleet_concurrency import handle

        # One fresh agent
        register("af", "live-agent", "executor")
        # One zombie (3h old)
        self._insert_stale_entry("af", "zombie-agent", "executor", age_seconds=3 * 3600)
        assert count_fleet() == 2, "both entries visible before reap"

        result = handle(params=None)

        # After the RPC call, only the fresh agent should remain
        assert result["fleet_total"] == 1, (
            f"fleet_total should be 1 (fresh only), got {result['fleet_total']}"
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
