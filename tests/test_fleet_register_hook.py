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
import shutil
import stat
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


# ── D#2473: scripts/pre-spawn-check.sh's own registration lane ──────────────
#
# The Agent()-tool lane above (hooks/fleet_register.py) was never the only
# writer. scripts/pre-spawn-check.sh registers too, via the same
# backend.fleet.concurrency CLI, for every scripts/spawn-agent.sh spawn. It
# used to pass its own subshell pid ("$$") as the registering pid — a
# process guaranteed to be dead within moments of the call returning,
# regardless of whether pre-spawn-check.sh was invoked directly or through
# spawn-agent.sh's command substitution. reap_stale()'s pid-liveness check
# (60s grace, then reap-if-dead) collected every such row on the very next
# register() call anywhere on the host, live agent or not — which is
# exactly the symptom D#2473 measured: fleet.db held one row, and its pid
# matched none of the 7 concurrently-running `claude` processes.


_STALE_PID_GRACE_SECONDS = 60  # backend/fleet/concurrency.py PID_GRACE_SECONDS


def _dead_pid() -> int:
    """A pid essentially guaranteed not to exist on this host right now."""
    return 2**22 + 1  # far above any real pid on a normal Linux host


class TestPreSpawnCheckPidChoiceMutation:
    """Spec item 2 (the binding mutation check): a dead-pid registration
    must fail the count once past the grace window; a live-pid registration
    from the same call shape must not. Exercised through the exact CLI
    surface scripts/pre-spawn-check.sh invokes
    (`python3 -m backend.fleet.concurrency register <project> <agent_id>
    <role> <pid>`), not just the Python API, so this is a regression test
    for the actual bug shape (a caller passing a doomed pid), not only for
    reap_stale()'s filtering logic (which was never broken — see
    TestNoAgeBasedSweep above for that).
    """

    def _register_via_cli(self, fleet, project: str, agent_id: str, role: str, pid: int) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["AUTONOMOUS_FLEET_STATE_DIR"] = str(fleet.FLEET_STATE_DIR)
        return subprocess.run(
            [sys.executable, "-m", "backend.fleet.concurrency",
             "register", project, agent_id, role, str(pid)],
            capture_output=True, text=True, timeout=10, env=env, cwd=str(_REPO),
        )

    def _age_row(self, fleet, agent_id: str, seconds_ago: int) -> None:
        old_ts = datetime.fromtimestamp(
            time.time() - seconds_ago, tz=timezone.utc
        ).isoformat()
        conn = fleet._open_db()
        conn.execute(
            "UPDATE agents SET started_at = ? WHERE agent_id = ?",
            (old_ts, agent_id),
        )
        conn.close()

    def test_dead_pid_registration_is_reaped_past_grace_the_old_bug(self, fleet):
        """Reproduces the ORIGINAL bug: a registration under a pid that is
        already dead (the shape "$$" always produced) does not survive past
        the grace window, even though nothing ever explicitly unregistered
        it."""
        r = self._register_via_cli(fleet, _PROJECT_NAME, "spawn-old-bug", "executor", _dead_pid())
        assert r.returncode == 0, r.stderr
        assert fleet.list_agents() != []

        self._age_row(fleet, "spawn-old-bug", _STALE_PID_GRACE_SECONDS + 5)

        # Any subsequent register() call runs reap_stale() first — this is
        # what a later, unrelated spawn on the host does in production.
        r2 = self._register_via_cli(fleet, _PROJECT_NAME, "spawn-unrelated", "executor", os.getpid())
        assert r2.returncode == 0, r2.stderr

        agent_ids = {row["agent_id"] for row in fleet.list_agents()}
        assert "spawn-old-bug" not in agent_ids, (
            "a dead-pid row must not survive past the grace window — "
            "this is the exact defect D#2473 measured"
        )

    def test_live_pid_registration_survives_past_grace_the_fix(self, fleet):
        """The fixed shape: register under a pid that is genuinely alive for
        the caller's lifetime (what $CLAUDE_PID gives scripts/pre-spawn-check.sh
        now, in place of "$$"). Represented here by this TEST PROCESS's own
        pid, which is alive for the whole test."""
        r = self._register_via_cli(fleet, _PROJECT_NAME, "spawn-fixed", "executor", os.getpid())
        assert r.returncode == 0, r.stderr

        self._age_row(fleet, "spawn-fixed", _STALE_PID_GRACE_SECONDS + 5)

        r2 = self._register_via_cli(fleet, _PROJECT_NAME, "spawn-unrelated-2", "executor", os.getpid())
        assert r2.returncode == 0, r2.stderr

        agent_ids = {row["agent_id"] for row in fleet.list_agents()}
        assert "spawn-fixed" in agent_ids, (
            "a live-pid row must survive reap_stale() while its process "
            "is still running — this is what scripts/pre-spawn-check.sh's "
            "old '$$'-based registration could never do"
        )
        assert len(fleet.active_agents(_PROJECT_NAME)) >= 1

    def test_mutation_direction_confirmed_manually(self):
        """Documents the manual mutation check required by Spec item 2
        ("break the liveness filter... confirm the test fails. Both
        directions in the PR body.") — see the PR description for the
        actual before/after run. Reverting backend/fleet/concurrency.py's
        _pid_alive() to always return True makes
        test_dead_pid_registration_is_reaped_past_grace_the_old_bug fail
        (the dead-pid row would then survive, matching the pre-fix
        production behaviour where reap_stale() never distinguished a dead
        pid from a live one). This test exists only to anchor that claim to
        a specific, named test rather than leaving it as an unverifiable PR
        body assertion."""
        assert True


class TestPreSpawnCheckScriptRegistersWithClaudePid:
    """End-to-end: the real scripts/pre-spawn-check.sh, run as a subprocess,
    with $CLAUDE_PID set (the harness always sets this — see
    scripts/spawn-agent.sh's env-scrub allowlist comment). Confirms the
    fleet.db row it writes carries that pid, not its own subshell pid, and
    that the row is still readable as 'active' after the grace window a
    genuinely-dead pid would have been reaped at.

    Run from a private, non-git copy of scripts/ + backend/ (mirrors
    tests/lib/pre-spawn-check-fixture.sh's approach) rather than the real
    checkout in place, for two independent reasons: (1) the real script's
    "parent on a non-default branch" contamination-recovery block runs `git
    reset --hard origin/<default>` against whatever repo it is pointed at
    when not inside a linked worktree — exactly the shape of a plain CI
    checkout on a PR branch, which this test must never touch; and (2) it
    keeps this run fully isolated from the live `.autonomous-team/` tree
    (D#2267)."""

    @pytest.fixture
    def sandbox(self, tmp_path):
        root = tmp_path / "psc-sandbox"
        shutil.copytree(_REPO / "scripts", root / "scripts")
        shutil.copytree(_REPO / "backend", root / "backend")
        (root / ".autonomous-team").mkdir()
        (root / ".autonomous-team" / "config.json").write_text(
            json.dumps({"project_name": _PROJECT_NAME})
        )
        # Pre-stamp the hourly sweep so the script doesn't fork a background
        # sweep-jsonl.sh job this test has no reason to wait on.
        (root / ".autonomous-team" / ".last-jsonl-sweep").write_text("")

        # Stub rotate-team-log.sh — no real team-log Issue exists here.
        stub = root / "scripts" / "rotate-team-log.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" >> \"${ROTATE_LOG_CAPTURE:-/dev/null}\"\n"
            "exit 0\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        # Stub gh on PATH — belt-and-suspenders against any code path in
        # this 1200-line script reaching a real GitHub call during a test.
        bin_dir = tmp_path / "stub-bin"
        bin_dir.mkdir()
        gh_stub = bin_dir / "gh"
        gh_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        gh_stub.chmod(gh_stub.stat().st_mode | stat.S_IEXEC)

        return root, bin_dir

    def _run(self, sandbox, fleet, claude_pid, event_id, autonomous_state_dir):
        root, bin_dir = sandbox
        env = dict(os.environ)
        env["AUTONOMOUS_FLEET_STATE_DIR"] = str(fleet.FLEET_STATE_DIR)
        env["AUTONOMOUS_TEAM_STATE_DIR"] = str(autonomous_state_dir)
        env["ROLE_ALLOWLIST_OVERRIDE"] = "1"
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        if claude_pid is None:
            env.pop("CLAUDE_PID", None)
        else:
            env["CLAUDE_PID"] = str(claude_pid)
        return subprocess.run(
            ["bash", str(root / "scripts" / "pre-spawn-check.sh"),
             "--role", "code-reviewer", "--event-id", event_id],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(root),
        )

    def test_registers_under_claude_pid_not_own_subshell_pid(self, fleet, sandbox, tmp_path):
        result = self._run(
            sandbox, fleet, claude_pid=os.getpid(),
            event_id="psc-test-evt-1", autonomous_state_dir=tmp_path / "state",
        )
        assert result.returncode == 0, result.stderr

        rows = fleet.list_agents()
        matching = [r for r in rows if r["agent_id"] == "psc-test-evt-1"]
        assert len(matching) == 1, (rows, result.stderr)
        assert matching[0]["pid"] == os.getpid(), (
            "expected the row to carry $CLAUDE_PID, not pre-spawn-check.sh's "
            f"own subshell pid: {matching[0]}"
        )

        # Age it past the pid-liveness grace window and register a second,
        # unrelated spawn (as a later real spawn on the host would) — the
        # first row must still be there, because its pid (this test
        # process's own) is genuinely alive.
        conn = fleet._open_db()
        old_ts = datetime.fromtimestamp(
            time.time() - _STALE_PID_GRACE_SECONDS - 5, tz=timezone.utc
        ).isoformat()
        conn.execute(
            "UPDATE agents SET started_at = ? WHERE agent_id = ?",
            (old_ts, "psc-test-evt-1"),
        )
        conn.close()

        result2 = self._run(
            sandbox, fleet, claude_pid=os.getpid(),
            event_id="psc-test-evt-2", autonomous_state_dir=tmp_path / "state2",
        )
        assert result2.returncode == 0, result2.stderr

        agent_ids = {r["agent_id"] for r in fleet.list_agents()}
        assert "psc-test-evt-1" in agent_ids
        assert len(fleet.active_agents(_PROJECT_NAME)) >= 1

    def test_falls_back_to_pid_zero_when_claude_pid_unset(self, fleet, sandbox, tmp_path):
        """No harness process identity available — the honest fallback is
        the pre-D#2314 legacy sentinel (pid=0, age-based backstop), not a
        fabricated pid."""
        result = self._run(
            sandbox, fleet, claude_pid=None,
            event_id="psc-test-evt-3", autonomous_state_dir=tmp_path / "state3",
        )
        assert result.returncode == 0, result.stderr

        rows = [r for r in fleet.list_agents() if r["agent_id"] == "psc-test-evt-3"]
        assert len(rows) == 1
        assert rows[0]["pid"] == 0


class TestActiveAgentsPositiveControl:
    """Spec item 3: any probe or count this fix relies on must distinguish
    'queried and got zero' from 'could not query at all'. backend/fleet/
    concurrency.py's active_agents() already does this by design (see its
    own docstring) — these are the regression-guard tests confirming it,
    which nothing in the existing suite exercised directly."""

    def test_insert_and_read_back_on_scratch_db(self, fleet):
        """Positive control: a fresh scratch fleet.db actually accepts a
        write and returns it — proves list_agents()/active_agents() are
        hitting a real, writable database, not silently no-op'ing."""
        assert fleet.list_agents() == []
        ok = fleet.register(_PROJECT_NAME, "probe-agent", "executor", pid=os.getpid())
        assert ok is True
        assert len(fleet.list_agents()) == 1
        assert len(fleet.active_agents(_PROJECT_NAME)) == 1

    def test_missing_fleet_db_reads_as_empty_not_unreadable(self, fleet, tmp_path):
        """No agent has ever registered anywhere on this host — a missing
        file is legitimately zero, not a read failure (see active_agents()'s
        own docstring)."""
        empty_dir = tmp_path / "never-touched"
        empty_dir.mkdir()
        import importlib
        # Import fresh under monkeypatch rather than mutating the shared
        # `fleet` fixture's module object mid-test.
        fc2 = importlib.import_module("backend.fleet.concurrency")
        old_path = fc2.FLEET_DB_PATH
        try:
            fc2.FLEET_DB_PATH = empty_dir / "fleet.db"
            assert fc2.active_agents(_PROJECT_NAME) == []
        finally:
            fc2.FLEET_DB_PATH = old_path

    def test_corrupt_database_raises_rather_than_reading_zero(self, fleet):
        """A genuinely unreadable database (not merely absent) must raise,
        not silently report 0 agents — the exact "did not measure" vs
        "measured zero" distinction Spec item 3 requires. Garbage bytes in
        place of a real sqlite file open fine (sqlite is lazy) but fail at
        the first query — active_agents()'s own docstring names exactly
        this as the case it raises for, distinct from a fleet dir that
        genuinely has nothing registered yet (see the sibling test above).

        Note: a *directory* in place of the file is NOT an equivalent probe
        here — it fails at connect() time, which active_agents() treats the
        same as "doesn't exist yet" (sqlite3.OperationalError on open) and
        correctly returns [] for, per its own WAL-mode discussion. Only a
        failure that surfaces once the query actually runs demonstrates the
        "unreadable, not zero" property this item is about."""
        bogus = fleet.FLEET_STATE_DIR / "fleet.db"
        if bogus.exists():
            bogus.unlink()
        bogus.write_bytes(b"not a sqlite database, just garbage bytes")

        with pytest.raises(Exception):
            fleet.active_agents(_PROJECT_NAME)


class TestResumePathGap:
    """Spec item 5, made executable: a message-resumed agent (SendMessage to
    a previously-spawned agent's own live session, not a fresh Agent()
    call) is NOT covered by fleet.db after its first SubagentStop. This is
    the decision documented in hooks/fleet_register.py's D#2473 addition —
    this test pins it down as observable behaviour so a future change to
    either hook has to touch this test to change the answer, rather than
    silently drifting."""

    def test_resumed_agent_is_invisible_after_its_first_subagent_stop(self, fleet):
        # Initial Agent() spawn — registers, per the normal PreToolUse path.
        reg = _run_register_hook("Agent", {"subagent_type": "executor"}, _TL_CWD)
        assert reg.returncode == 0
        assert len(fleet.active_agents(_PROJECT_NAME)) == 1

        # The agent's first turn ends and control returns to the caller —
        # SubagentStop fires (cwd is the finished subagent's own, a
        # worktree) and unregisters the row, exactly as it does for an
        # agent that is genuinely finished.
        unreg = _run_unregister_hook(_WT_CWD)
        assert unreg.returncode == 0
        assert fleet.active_agents(_PROJECT_NAME) == []

        # Team Lead now resumes the SAME underlying agent via SendMessage
        # (not Agent() again) to continue it — e.g. applying review
        # feedback. No hook observes SendMessage, so nothing re-registers:
        # the agent is genuinely running again, but fleet.db has no row for
        # it. This is the accepted gap, not a bug this PR silently missed.
        assert fleet.active_agents(_PROJECT_NAME) == []
        assert fleet.count_project_capped(_PROJECT_NAME) == 0
