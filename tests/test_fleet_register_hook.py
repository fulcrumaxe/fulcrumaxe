"""tests/test_fleet_register_hook.py

Unit tests for hooks/fleet_register.py (PreToolUse, matcher "Agent") and
hooks/fleet_unregister.py (SubagentStop, matcher "") — the D#2314 F1
registration-coverage fix for the Agent()-tool spawn path, which
scripts/pre-spawn-check.sh never covers.

Run with:
    python3 -m pytest tests/test_fleet_register_hook.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from testsupport.fixture_paths import FIXTURE_MAIN_REPO  # noqa: E402

_TL_CWD = FIXTURE_MAIN_REPO
_WT_CWD = f"{FIXTURE_MAIN_REPO}/.claude/worktrees/test-agent-123"
_UNTRUSTED_CWD = "/tmp/random"

# The real, byte-identical value backend/fleet/project_name.py resolves for
# THIS worktree's .autonomous-team/config.json — both hook scripts resolve
# their repo root from their own file location (hooks/fleet_register.py's
# parent.parent), not from the synthetic cwd fixture used for tiering, so
# this is the project name every registration in these tests lands under.
_PROJECT_NAME = "fulcrumaxe"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Isolate backend.fleet.concurrency to a scratch dir, in THIS process
    (direct import) and in any subprocess this test spawns (env var — a
    fresh process re-imports the module and reads it at import time)."""
    import backend.fleet.concurrency as fc

    fleet_dir = tmp_path / "fleet-state"
    fleet_dir.mkdir()
    monkeypatch.setattr(fc, "FLEET_STATE_DIR", fleet_dir)
    monkeypatch.setattr(fc, "FLEET_DB_PATH", fleet_dir / "fleet.db")
    monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", fleet_dir / "config.json")
    monkeypatch.setenv("AUTONOMOUS_FLEET_STATE_DIR", str(fleet_dir))
    return fc


def _run_register_hook(tool_name: str, tool_input: dict, cwd: str, env: dict | None = None):
    hook = str(_REPO / "hooks" / "fleet_register.py")
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd})
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, hook], input=payload, capture_output=True, text=True,
        timeout=10, env=full_env,
    )


def _run_unregister_hook(cwd: str, env: dict | None = None):
    hook = str(_REPO / "hooks" / "fleet_unregister.py")
    payload = json.dumps({"cwd": cwd})
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, hook], input=payload, capture_output=True, text=True,
        timeout=10, env=full_env,
    )


class TestFleetRegisterHook:
    def test_registers_from_team_lead_cwd(self, fleet):
        result = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        assert result.returncode == 0

        rows = fleet.list_agents()
        assert len(rows) == 1
        assert rows[0]["project_name"] == _PROJECT_NAME
        assert rows[0]["role"] == "executor"
        assert rows[0]["agent_id"].startswith("agent-tool-")

    def test_does_not_register_from_worktree_cwd(self, fleet):
        result = _run_register_hook("Agent", {"subagent_type": "executor"}, _WT_CWD)
        assert result.returncode == 0
        assert fleet.list_agents() == []

    def test_does_not_register_from_untrusted_cwd(self, fleet):
        result = _run_register_hook("Agent", {"subagent_type": "executor"}, _UNTRUSTED_CWD)
        assert result.returncode == 0
        assert fleet.list_agents() == []

    def test_ignores_non_agent_tool_calls(self, fleet):
        result = _run_register_hook("Bash", {"command": "echo hi"}, _TL_CWD)
        assert result.returncode == 0
        assert fleet.list_agents() == []

    def test_never_blocks_even_when_fleet_dir_unwritable(self, fleet):
        """Observe-only (Spec item 11): a registration failure must never
        turn into a blocked spawn."""
        os.chmod(fleet.FLEET_STATE_DIR, 0o555)
        try:
            result = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        finally:
            os.chmod(fleet.FLEET_STATE_DIR, 0o755)
        assert result.returncode == 0

    def test_never_blocks_on_malformed_stdin(self, fleet):
        hook = str(_REPO / "hooks" / "fleet_register.py")
        result = subprocess.run(
            [sys.executable, hook], input="not json {{{", capture_output=True,
            text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_does_not_block_when_fleet_cap_already_exceeded(self, fleet):
        """Spec item 11: registration must proceed to completion even when
        the fleet is already at capacity. Per D#2314 S2, agent-tool-
        registrations bypass the cap check entirely (they must never be
        denied), so this now also actually succeeds — a stronger form of
        observe-only than a hook whose exit code merely doesn't reflect an
        internal denial."""
        fleet.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 0}))
        result = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        assert result.returncode == 0
        rows = fleet.list_agents()
        assert len(rows) == 1
        assert rows[0]["agent_id"].startswith("agent-tool-")


class TestFleetUnregisterHook:
    def test_removes_matching_agent_tool_row(self, fleet):
        # The hook subprocess's os.getppid() equals THIS test process's pid,
        # since subprocess.run spawns it as a direct child.
        fleet.register(_PROJECT_NAME, "agent-tool-abc123", "executor", pid=os.getpid())

        result = _run_unregister_hook(_TL_CWD)
        assert result.returncode == 0
        assert fleet.list_agents() == []

    def test_leaves_non_agent_tool_rows_alone(self, fleet):
        """Rows registered by scripts/pre-spawn-check.sh (no agent-tool-
        prefix) must never be touched by this hook."""
        fleet.register(_PROJECT_NAME, "spawn-99999", "executor", pid=os.getpid())

        result = _run_unregister_hook(_TL_CWD)
        assert result.returncode == 0
        rows = fleet.list_agents()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "spawn-99999"

    def test_leaves_rows_from_a_different_pid_alone(self, fleet):
        """Cross-project/cross-session isolation: a row registered under a
        different parent PID must not be removed."""
        fleet.register(_PROJECT_NAME, "agent-tool-other-session", "executor", pid=os.getpid() + 1)

        result = _run_unregister_hook(_TL_CWD)
        assert result.returncode == 0
        rows = fleet.list_agents()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "agent-tool-other-session"

    def test_removes_matching_row_when_subagent_stop_cwd_is_a_worktree(self, fleet):
        """D#2314 S1 regression: SubagentStop's cwd is the finished subagent's
        OWN cwd (a worktree, for a worktree-isolated agent), not the caller's.
        Gating unregister on classify_cwd(cwd) == 'team_lead' — the check
        that's correct for fleet_register.py's PreToolUse context — meant
        this hook silently never fired for the common case, leaking one
        immortal row (pid = the long-lived session PID) per spawn. This must
        be removed regardless of what cwd SubagentStop reports."""
        fleet.register(_PROJECT_NAME, "agent-tool-abc123", "executor", pid=os.getpid())

        result = _run_unregister_hook(_WT_CWD)

        assert result.returncode == 0
        assert fleet.list_agents() == []

    def test_removes_oldest_when_multiple_candidates(self, fleet):
        import time
        fleet.register(_PROJECT_NAME, "agent-tool-first", "executor", pid=os.getpid())
        time.sleep(0.01)
        fleet.register(_PROJECT_NAME, "agent-tool-second", "executor", pid=os.getpid())

        result = _run_unregister_hook(_TL_CWD)
        assert result.returncode == 0
        rows = fleet.list_agents()
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "agent-tool-second"

    def test_never_blocks_on_malformed_stdin(self, fleet):
        hook = str(_REPO / "hooks" / "fleet_unregister.py")
        result = subprocess.run(
            [sys.executable, hook], input="not json {{{", capture_output=True,
            text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_noop_when_nothing_registered(self, fleet):
        result = _run_unregister_hook(_TL_CWD)
        assert result.returncode == 0
        assert fleet.list_agents() == []


class TestRegisterThenUnregisterEndToEnd:
    """The full observe-only lifecycle a real Agent() spawn goes through:
    PreToolUse registers, SubagentStop unregisters, and
    backend.fleet.concurrency.active_agents() reflects both transitions —
    the same read path backend/api.py's liveness probe uses."""

    def test_active_then_idle(self, fleet):
        reg = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        assert reg.returncode == 0

        assert len(fleet.active_agents(_PROJECT_NAME)) == 1

        # SubagentStop reports the finished subagent's own cwd — a worktree
        # for a worktree-isolated agent, the realistic case (D#2314 S1).
        unreg = _run_unregister_hook(_WT_CWD)
        assert unreg.returncode == 0

        assert fleet.active_agents(_PROJECT_NAME) == []

    def test_many_spawn_end_cycles_do_not_accumulate(self, fleet):
        """D#2314 S1/Gate-2 requirement: a single register/unregister pair
        passing is not enough evidence — the finding was about accumulation.
        Cycle well past the fleet cap and confirm nothing is left behind."""
        fleet.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 8}))

        cycles = 20  # > the cap
        for _ in range(cycles):
            reg = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
            assert reg.returncode == 0
            unreg = _run_unregister_hook(_WT_CWD)
            assert unreg.returncode == 0

        assert fleet.list_agents() == []


class TestAgentToolRowsExcludedFromFleetCap:
    """D#2314 S2: registration coverage must be observe-only in effect, not
    just in name. Before this fix, every agent-tool- row was counted by
    register()'s own fleet-wide cap check and by count_project(), which
    scripts/pre-spawn-check.sh's per-project cap check calls — so a busy
    consensus panel (5 specialists + researcher + PM = up to 7 concurrent
    rows against a default cap of 8) could deny real spawn-agent.sh spawns
    with blocked_reason=fleet_cap_exceeded."""

    def test_real_registrations_still_enforce_the_cap(self, fleet):
        """Cap enforcement on the spawn-agent.sh lane must be unchanged."""
        fleet.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 2}))
        assert fleet.register(_PROJECT_NAME, "spawn-1", "executor", pid=os.getpid()) is True
        assert fleet.register(_PROJECT_NAME, "spawn-2", "executor", pid=os.getpid()) is True
        assert fleet.register(_PROJECT_NAME, "spawn-3", "executor", pid=os.getpid()) is False

    def test_agent_tool_registrations_never_count_toward_the_cap(self, fleet):
        fleet.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 2}))
        assert fleet.register(_PROJECT_NAME, "spawn-1", "executor", pid=os.getpid()) is True
        assert fleet.register(_PROJECT_NAME, "spawn-2", "executor", pid=os.getpid()) is True
        # The cap is already full for real registrations, but a whole
        # consensus panel's worth of agent-tool- rows still succeeds.
        for i in range(7):
            assert fleet.register(
                _PROJECT_NAME, f"agent-tool-panel-{i}", "specialist", pid=os.getpid()
            ) is True

    def test_count_project_capped_excludes_agent_tool_rows(self, fleet):
        fleet.register(_PROJECT_NAME, "spawn-1", "executor", pid=os.getpid())
        fleet.register(_PROJECT_NAME, "agent-tool-a", "specialist", pid=os.getpid())
        fleet.register(_PROJECT_NAME, "agent-tool-b", "specialist", pid=os.getpid())

        assert fleet.count_project_capped(_PROJECT_NAME) == 1
        # count_project() itself is unchanged — other consumers (e.g. the
        # Fleet page RPC) still see the full, honest count.
        assert fleet.count_project(_PROJECT_NAME) == 3

    def test_accumulated_agent_tool_rows_never_exhaust_the_real_cap(self, fleet):
        """The Gate-2 accumulation scenario: many Agent()-tool spawns in a
        row (a busy day of consensus panels), none of them ever unregistered
        yet, must not deny a subsequent real spawn-agent.sh registration."""
        fleet.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 8}))

        for _ in range(20):  # > the cap
            result = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
            assert result.returncode == 0

        assert len(fleet.list_agents()) == 20
        assert fleet.count_project_capped(_PROJECT_NAME) == 0
        assert fleet.register(_PROJECT_NAME, "spawn-real", "executor", pid=os.getpid()) is True


class TestNoAgeBasedSweep:
    """D#2314 N1 (security re-review): hooks/fleet_register.py used to call
    a sweep_stale_agent_tool_rows() before every registration, deleting
    agent-tool- rows on age alone with no liveness condition. That is wrong
    on its own (it deletes a row for an agent that is merely old, not dead —
    CLAUDE.md defines project-manager and visual-verifier as persistent
    agents) and wrong in composition: it could delete agent A's live row,
    after which agent B's later SubagentStop call evicted the oldest
    *remaining* match — B's own row — leaving active_agents() report nothing
    while B was still running. That mechanism was removed entirely rather
    than given a liveness condition, because D#2314 S2's cap exclusion
    already makes a leaked row inert, and reap_stale()'s existing
    pid-liveness check is sufficient once the session pid actually dies.
    These are the regression tests for both shapes of the removed bug."""

    def test_a_stale_looking_but_live_row_survives_a_new_registration(self, fleet):
        """A pre-existing agent-tool- row with an old started_at but a live
        pid must be untouched by a brand-new Agent() spawn."""
        old_ts = datetime.fromtimestamp(time.time() - 10000, tz=timezone.utc).isoformat()
        conn = fleet._open_db()
        conn.execute(
            "INSERT INTO agents (project_name, agent_id, role, started_at, pid) VALUES (?, ?, ?, ?, ?)",
            (_PROJECT_NAME, "agent-tool-old-but-alive", "executor", old_ts, os.getpid()),
        )
        conn.close()

        result = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        assert result.returncode == 0

        rows = fleet.list_agents()
        agent_ids = {r["agent_id"] for r in rows}
        assert "agent-tool-old-but-alive" in agent_ids
        assert len(rows) == 2  # the old-but-alive row, plus the fresh registration

    def test_finishing_agent_a_does_not_evict_still_running_agent_b(self, fleet):
        """The exact scenario the security reviewer measured: spawn A, spawn
        B, finish A — B must still read 'active', not get evicted by a stale
        sweep or by A's SubagentStop matching B's row instead of A's."""
        reg_a = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        assert reg_a.returncode == 0
        time.sleep(0.01)
        reg_b = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        assert reg_b.returncode == 0

        assert len(fleet.list_agents()) == 2

        # A finishes — its SubagentStop call removes exactly one row (the
        # oldest match, which is A's own, since A registered first).
        unreg_a = _run_unregister_hook(_WT_CWD)
        assert unreg_a.returncode == 0

        rows = fleet.list_agents()
        assert len(rows) == 1  # B's row must still be present
        assert len(fleet.active_agents(_PROJECT_NAME)) == 1  # B still reads 'active'
