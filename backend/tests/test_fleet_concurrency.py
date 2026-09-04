"""Tests for backend/fleet/concurrency.py

Run with:
    pytest -x -q backend/tests/test_fleet_concurrency.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def isolated_fleet_db(tmp_path, monkeypatch):
    """Point fleet state at a temp dir so tests don't touch ~/.autonomous-fleet-state/."""
    monkeypatch.setenv("AUTONOMOUS_FLEET_STATE_DIR", str(tmp_path))
    # Reload module-level constants that were computed at import time
    import importlib
    import backend.fleet.concurrency as m
    monkeypatch.setattr(m, "FLEET_STATE_DIR", tmp_path)
    monkeypatch.setattr(m, "FLEET_DB_PATH", tmp_path / "fleet.db")
    monkeypatch.setattr(m, "FLEET_CONFIG_PATH", tmp_path / "config.json")
    yield


def _mod():
    import backend.fleet.concurrency as m
    return m


# ── register / unregister basics ─────────────────────────────────────────────

def test_register_and_count():
    m = _mod()
    assert m.count_fleet() == 0
    assert m.register("proj", "agent-1", "executor") is True
    assert m.count_fleet() == 1


def test_unregister_removes_entry():
    m = _mod()
    m.register("proj", "agent-1", "executor")
    m.unregister("proj", "agent-1")
    assert m.count_fleet() == 0


def test_fleet_cap_blocks_excess():
    m = _mod()
    # Write a config with cap=2
    import json
    m.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 2}))
    assert m.register("proj", "a1", "executor") is True
    assert m.register("proj", "a2", "executor") is True
    assert m.register("proj", "a3", "executor") is False  # cap exceeded


# ── agent-tool- cap exclusion (D#2314 S2) ────────────────────────────────────
#
# Rows registered by hooks/fleet_register.py for the Agent()-tool spawn path
# carry the AGENT_TOOL_ID_PREFIX prefix and must never consume, or be denied
# by, the fleet-wide/per-project cap that gates the spawn-agent.sh lane.

def test_agent_tool_registration_bypasses_a_full_cap():
    m = _mod()
    import json
    m.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 1}))
    assert m.register("proj", "spawn-1", "executor") is True
    # Cap (1) is already reached by a real registration, but an
    # agent-tool- one still succeeds -- it never even checks the cap.
    assert m.register("proj", f"{m.AGENT_TOOL_ID_PREFIX}a", "specialist") is True
    assert m.register("proj", f"{m.AGENT_TOOL_ID_PREFIX}b", "specialist") is True


def test_agent_tool_rows_do_not_count_toward_a_real_registrations_cap():
    m = _mod()
    import json
    m.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 1}))
    for i in range(5):
        assert m.register("proj", f"{m.AGENT_TOOL_ID_PREFIX}{i}", "specialist") is True
    # Five agent-tool- rows exist, but the real cap (1) is still fully
    # available to a genuine spawn-agent.sh registration.
    assert m.register("proj", "spawn-1", "executor") is True
    assert m.register("proj", "spawn-2", "executor") is False  # cap now genuinely full


def test_count_project_capped_excludes_agent_tool_rows():
    m = _mod()
    m.register("proj", "spawn-1", "executor")
    m.register("proj", f"{m.AGENT_TOOL_ID_PREFIX}a", "specialist")
    m.register("proj", f"{m.AGENT_TOOL_ID_PREFIX}b", "specialist")

    assert m.count_project_capped("proj") == 1
    assert m.count_project("proj") == 3  # unchanged for other, observational consumers


# ── agent-tool- rows have no age-based reaping (D#2314 N1) ──────────────────
#
# An earlier version of this module shipped sweep_stale_agent_tool_rows(), an
# age-based backstop with no liveness condition. It was removed: it deleted
# rows for agents that were merely old, not dead -- CLAUDE.md defines
# project-manager and visual-verifier as persistent agents, and it composed
# into a cascade where sweeping one in-flight agent-tool- row let a second
# agent's SubagentStop evict yet another still-running row. agent-tool- rows
# now rely solely on reap_stale()'s existing pid-liveness check, exactly like
# every other row in this table -- there is no separate mechanism to test
# here beyond confirming that stays true.

def test_agent_tool_row_survives_indefinitely_while_its_pid_is_alive():
    """The regression case for the removed sweep: an old-looking but
    genuinely live agent-tool- row must never be collected by anything."""
    m = _mod()
    from datetime import datetime, timezone
    old_ts = datetime.fromtimestamp(time.time() - 10000, tz=timezone.utc).isoformat()
    conn = m._open_db()
    conn.execute(
        "INSERT INTO agents (project_name, agent_id, role, started_at, pid) VALUES (?, ?, ?, ?, ?)",
        ("proj", f"{m.AGENT_TOOL_ID_PREFIX}long-running", "executor", old_ts, os.getpid()),
    )
    conn.close()

    reaped = m.reap_stale(max_age_seconds=7200)

    assert reaped == 0
    assert m.count_fleet() == 1
    assert len(m.active_agents("proj")) == 1


def test_agent_tool_row_is_reaped_once_its_pid_actually_dies():
    """reap_stale()'s existing pid-liveness check is the only backstop these
    rows get -- and it is sufficient once the session pid is genuinely gone."""
    m = _mod()
    from datetime import datetime, timezone
    old_ts = datetime.fromtimestamp(time.time() - 10000, tz=timezone.utc).isoformat()
    conn = m._open_db()
    conn.execute(
        "INSERT INTO agents (project_name, agent_id, role, started_at, pid) VALUES (?, ?, ?, ?, ?)",
        ("proj", f"{m.AGENT_TOOL_ID_PREFIX}crashed-session", "executor", old_ts, 999999999),
    )
    conn.close()

    reaped = m.reap_stale(max_age_seconds=7200)

    assert reaped == 1
    assert m.count_fleet() == 0


# ── reap_stale ────────────────────────────────────────────────────────────────

def test_reap_stale_removes_old_entries():
    """Entries older than max_age_seconds must be reaped."""
    m = _mod()
    import sqlite3
    from datetime import datetime, timezone

    # Insert one old entry manually (1 day ago)
    old_ts = datetime.fromtimestamp(time.time() - 86400, tz=timezone.utc).isoformat()
    conn = m._open_db()
    conn.execute(
        "INSERT INTO agents (project_name, agent_id, role, started_at) VALUES (?, ?, ?, ?)",
        ("proj", "old-agent", "executor", old_ts),
    )
    conn.close()

    assert m.count_fleet() == 1
    reaped = m.reap_stale(max_age_seconds=3600)  # 1h threshold: old entry (1d old) must go
    assert reaped == 1
    assert m.count_fleet() == 0


def test_reap_stale_preserves_fresh_entries():
    """Entries younger than max_age_seconds must survive the reaper."""
    m = _mod()
    m.register("proj", "fresh-agent", "executor")
    reaped = m.reap_stale(max_age_seconds=3600)
    assert reaped == 0
    assert m.count_fleet() == 1


def test_reap_stale_mixed_entries():
    """Only stale entries are removed; fresh ones survive."""
    m = _mod()
    from datetime import datetime, timezone

    # Register the fresh agent first (before inserting stale entry directly)
    m.register("proj", "fresh-agent", "executor")

    # Insert one stale entry directly (bypassing register to avoid it being reaped by register())
    old_ts = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc).isoformat()
    conn = m._open_db()
    conn.execute(
        "INSERT INTO agents (project_name, agent_id, role, started_at) VALUES (?, ?, ?, ?)",
        ("proj", "old-agent", "executor", old_ts),
    )
    conn.close()

    assert m.count_fleet() == 2

    reaped = m.reap_stale(max_age_seconds=3600)  # 1h threshold — old entry (2h old) must go
    assert reaped == 1
    assert m.count_fleet() == 1

    agents = m.list_agents()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "fresh-agent"


def test_reap_stale_called_by_register(monkeypatch):
    """register() must invoke reap_stale() before checking the cap."""
    m = _mod()
    calls = []

    original_reap = m.reap_stale
    def spy_reap(*args, **kwargs):
        calls.append(True)
        return original_reap(*args, **kwargs)

    monkeypatch.setattr(m, "reap_stale", spy_reap)
    m.register("proj", "agent-x", "executor")
    assert len(calls) == 1, "reap_stale must be called exactly once per register()"


def test_reap_stale_env_var_override(monkeypatch):
    """AUTONOMOUS_FLEET_MAX_AGE_SECONDS env var overrides the default MAX_AGE_SECONDS."""
    monkeypatch.setenv("AUTONOMOUS_FLEET_MAX_AGE_SECONDS", "60")
    import importlib
    import backend.fleet.concurrency as m
    importlib.reload(m)
    assert m.MAX_AGE_SECONDS == 60
    # Restore to avoid contaminating other tests
    importlib.reload(m)


def test_reap_stale_is_best_effort(monkeypatch):
    """reap_stale must not raise even if the db call fails."""
    m = _mod()

    def bad_open():
        raise sqlite3.Error("simulated lock")

    import sqlite3
    monkeypatch.setattr(m, "_open_db", bad_open)
    result = m.reap_stale(max_age_seconds=1)
    assert result == 0  # returns 0, does not raise


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def test_cli_count_fleet(capsys):
    m = _mod()
    m.register("proj", "a1", "executor")
    sys.argv = ["concurrency", "count_fleet"]
    m._main()
    captured = capsys.readouterr()
    assert captured.out.strip() == "1"


def test_cli_reap_stale(capsys):
    """CLI reap_stale command prints 'reaped N'."""
    m = _mod()
    from datetime import datetime, timezone
    old_ts = datetime.fromtimestamp(time.time() - 100, tz=timezone.utc).isoformat()
    conn = m._open_db()
    conn.execute(
        "INSERT INTO agents (project_name, agent_id, role, started_at) VALUES (?, ?, ?, ?)",
        ("proj", "old-agent", "executor", old_ts),
    )
    conn.close()

    sys.argv = ["concurrency", "reap_stale", "10"]  # 10s threshold
    m._main()
    captured = capsys.readouterr()
    assert "reaped 1" in captured.out


def test_cli_register_rejects_forged_agent_tool_prefix(capsys):
    """D#2314 cap-exclusion guard: agent_id on the CLI register path is
    caller-supplied (pre-spawn-check.sh passes --event-id straight through).
    Since agent-tool- rows are excluded from the cap check, a caller forging
    that prefix on the real spawn-agent.sh lane would bypass the fleet cap
    entirely -- refuse it here rather than let it through."""
    m = _mod()
    import json
    m.FLEET_CONFIG_PATH.write_text(json.dumps({"fleet_cap": 1}))

    sys.argv = ["concurrency", "register", "proj", f"{m.AGENT_TOOL_ID_PREFIX}forged", "executor"]
    with pytest.raises(SystemExit) as exc_info:
        m._main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "reserved prefix" in captured.err
    assert m.count_fleet() == 0


def test_direct_register_call_is_unaffected_by_the_cli_guard():
    """The guard is CLI-only. hooks/fleet_register.py's own internal
    register() call (not this CLI) is trusted code and must keep working."""
    m = _mod()
    assert m.register("proj", f"{m.AGENT_TOOL_ID_PREFIX}internal", "executor") is True


# ── active_agents() — read-only, D#2314 ──────────────────────────────────────
#
# The dashboard's liveness probe reads this on every poll, so it must never
# take a write lock, run DDL, or mutate the table — see backend/api.py's
# _probe_liveness and the D#2314 Spec item 3 assertions this covers.

def test_active_agents_returns_live_rows():
    m = _mod()
    m.register("proj-a", "agent-1", "executor", pid=os.getpid())
    rows = m.active_agents("proj-a")
    assert len(rows) == 1
    assert rows[0]["project_name"] == "proj-a"
    assert rows[0]["agent_id"] == "agent-1"


def test_active_agents_filters_dead_pid():
    m = _mod()
    from datetime import datetime, timezone
    conn = m._open_db()
    conn.execute(
        "INSERT INTO agents (project_name, agent_id, role, started_at, pid) VALUES (?, ?, ?, ?, ?)",
        ("proj-a", "dead-agent", "executor", datetime.now(timezone.utc).isoformat(), 999999999),
    )
    conn.close()
    assert m.active_agents("proj-a") == []


def test_active_agents_cross_project_isolation():
    m = _mod()
    m.register("proj-a", "agent-1", "executor", pid=os.getpid())
    assert len(m.active_agents("proj-a")) == 1
    assert m.active_agents("proj-b") == []


def test_active_agents_empty_when_db_missing():
    """No fleet.db at all — nothing has ever registered — is [], not a raise."""
    m = _mod()
    assert not m.FLEET_DB_PATH.exists()
    assert m.active_agents("proj-a") == []


def test_active_agents_does_not_create_db_file():
    """A read must not conjure the db into existence — CREATE TABLE IF NOT
    EXISTS is _open_db()'s job (the write path); active_agents() must skip it."""
    m = _mod()
    m.active_agents("proj-a")
    assert not m.FLEET_DB_PATH.exists()


def test_active_agents_performs_no_write():
    m = _mod()
    m.register("proj-a", "agent-1", "executor", pid=os.getpid())
    before = m.FLEET_DB_PATH.stat().st_mtime_ns
    m.active_agents("proj-a")
    after = m.FLEET_DB_PATH.stat().st_mtime_ns
    assert before == after


def test_active_agents_survives_read_only_fleet_dir():
    """Spec item 3 assertion: chmod a-w on the fleet dir — reads still work."""
    m = _mod()
    m.register("proj-a", "agent-1", "executor", pid=os.getpid())
    os.chmod(m.FLEET_STATE_DIR, 0o555)
    try:
        rows = m.active_agents("proj-a")
        assert len(rows) == 1
    finally:
        os.chmod(m.FLEET_STATE_DIR, 0o755)


def test_active_agents_raises_on_corrupt_db():
    """A genuinely broken db file must raise (so callers can tell 'no
    agents' apart from 'couldn't read') rather than being swallowed here."""
    m = _mod()
    m.FLEET_STATE_DIR.mkdir(parents=True, exist_ok=True)
    m.FLEET_DB_PATH.write_bytes(b"not a sqlite database")
    with pytest.raises(Exception):
        m.active_agents("proj-a")


def test_cli_active_agents(capsys):
    m = _mod()
    m.register("proj-a", "agent-1", "executor", pid=os.getpid())
    sys.argv = ["concurrency", "active_agents", "proj-a"]
    m._main()
    captured = capsys.readouterr()
    import json as _json
    parsed = _json.loads(captured.out)
    assert len(parsed) == 1
    assert parsed[0]["project_name"] == "proj-a"
