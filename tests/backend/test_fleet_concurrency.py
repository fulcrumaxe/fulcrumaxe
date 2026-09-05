"""tests/backend/test_fleet_concurrency.py — PID-liveness reaper tests.

Tests the three core behaviours introduced by the PID-based reap fix:
  1. Orphan row (dead PID) past grace window is reaped.
  2. Row whose PID is alive (this process) is NOT reaped.
  3. 60-second grace window: dead-PID row created < 60s ago is NOT reaped yet.
  4. Legacy pid=0 row falls back to 2h started_at backstop.

Companion to test_fleet_concurrency_sqlite.py (register/cap/WAL tests).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest


# ── Fixture: isolated fleet dir ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_fleet_dir(tmp_path, monkeypatch):
    """Redirect all fleet state to a per-test temp directory."""
    import backend.fleet.concurrency as fc

    monkeypatch.setattr(fc, "FLEET_STATE_DIR", tmp_path)
    monkeypatch.setattr(fc, "FLEET_DB_PATH", tmp_path / "fleet.db")
    monkeypatch.setattr(fc, "FLEET_CONFIG_PATH", tmp_path / "config.json")
    yield tmp_path


# ── Helper ────────────────────────────────────────────────────────────────────

def _insert_row(tmp_path, project: str, agent_id: str, role: str,
                started_at: str, pid: int) -> None:
    """Insert a raw fleet row, bypassing register() so we control all fields."""
    import sqlite3
    from backend.fleet.concurrency import _open_db
    conn = _open_db()
    try:
        conn.execute(
            "INSERT INTO agents (project_name, agent_id, role, started_at, pid) VALUES (?,?,?,?,?)",
            (project, agent_id, role, started_at, pid),
        )
    finally:
        conn.close()


def _ts(offset_seconds: float = 0.0) -> str:
    """Return an ISO8601 UTC timestamp offset from now by *offset_seconds*."""
    ts = time.time() + offset_seconds
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── AC 1: orphan row (dead PID, past grace) is reaped ────────────────────────

def test_orphan_pid_past_grace_is_reaped(tmp_path):
    """A row with a non-existent PID older than 60s must be reaped."""
    from backend.fleet.concurrency import reap_stale, count_fleet

    # PID 99999 is extremely unlikely to exist; started 5 minutes ago (past 60s grace).
    dead_pid = 99999
    assert not os.path.exists(f"/proc/{dead_pid}"), "test assumption: PID 99999 must not exist"

    _insert_row(tmp_path, "test-proj", "orphan-agent", "executor",
                _ts(-300), dead_pid)
    assert count_fleet() == 1

    reaped = reap_stale()
    assert reaped == 1, f"Expected 1 reaped, got {reaped}"
    assert count_fleet() == 0


# ── AC 2: alive process is NOT reaped ────────────────────────────────────────

def test_alive_process_not_reaped(tmp_path):
    """A row whose PID is this running process must never be reaped."""
    from backend.fleet.concurrency import reap_stale, count_fleet

    our_pid = os.getpid()
    # Row started 10 minutes ago — well past grace. But the PID is alive.
    _insert_row(tmp_path, "test-proj", "live-agent", "executor",
                _ts(-600), our_pid)
    assert count_fleet() == 1

    reaped = reap_stale()
    assert reaped == 0, f"Expected 0 reaped (process alive), got {reaped}"
    assert count_fleet() == 1


# ── AC 3: dead-PID row within 60s grace is NOT reaped yet ────────────────────

def test_dead_pid_within_grace_not_reaped(tmp_path):
    """A dead-PID row created only 10s ago must survive the 60s grace window."""
    from backend.fleet.concurrency import reap_stale, count_fleet

    dead_pid = 99999
    assert not os.path.exists(f"/proc/{dead_pid}"), "test assumption: PID 99999 must not exist"

    # started 10s ago — inside grace window
    _insert_row(tmp_path, "test-proj", "young-orphan", "executor",
                _ts(-10), dead_pid)
    assert count_fleet() == 1

    reaped = reap_stale()
    assert reaped == 0, f"Expected 0 reaped (within grace), got {reaped}"
    assert count_fleet() == 1


# ── AC 4: legacy pid=0 falls back to 2h backstop ─────────────────────────────

def test_legacy_pid_zero_uses_age_backstop(tmp_path):
    """A row with pid=0 (pre-migration) is reaped when started_at > max_age_seconds old."""
    from backend.fleet.concurrency import reap_stale, count_fleet

    # 3 hours old, pid=0
    _insert_row(tmp_path, "test-proj", "legacy-agent", "executor",
                _ts(-10800), 0)
    assert count_fleet() == 1

    reaped = reap_stale(max_age_seconds=7200)
    assert reaped == 1, f"Expected 1 reaped (legacy backstop), got {reaped}"
    assert count_fleet() == 0


def test_legacy_pid_zero_young_row_not_reaped(tmp_path):
    """A pid=0 row that is only 5 minutes old must survive the 2h backstop."""
    from backend.fleet.concurrency import reap_stale, count_fleet

    _insert_row(tmp_path, "test-proj", "young-legacy", "executor",
                _ts(-300), 0)
    assert count_fleet() == 1

    reaped = reap_stale(max_age_seconds=7200)
    assert reaped == 0, f"Expected 0 reaped (too young for backstop), got {reaped}"
    assert count_fleet() == 1


# ── AC 5: register() writes caller's PID ─────────────────────────────────────

def test_register_writes_pid():
    """register() must store the caller's PID so reap_stale() can check liveness."""
    import sqlite3
    from backend.fleet.concurrency import register, FLEET_DB_PATH

    assert register("test-proj", "pid-check-agent", "executor") is True

    conn = sqlite3.connect(str(FLEET_DB_PATH))
    try:
        row = conn.execute(
            "SELECT pid FROM agents WHERE agent_id = 'pid-check-agent'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "Row not found after register()"
    stored_pid = row[0]
    assert stored_pid == os.getpid(), f"Expected pid={os.getpid()}, got {stored_pid}"


# ── AC 6: explicit pid param in register() ───────────────────────────────────

def test_register_explicit_pid_stored():
    """register() must honour an explicit pid argument."""
    import sqlite3
    from backend.fleet.concurrency import register, FLEET_DB_PATH

    custom_pid = 42
    assert register("test-proj", "custom-pid-agent", "executor", pid=custom_pid) is True

    conn = sqlite3.connect(str(FLEET_DB_PATH))
    try:
        row = conn.execute(
            "SELECT pid FROM agents WHERE agent_id = 'custom-pid-agent'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == custom_pid


# ── AC 7: schema migration on pre-existing db is idempotent ──────────────────

def test_migration_idempotent(tmp_path):
    """Calling _open_db() twice on the same database must not raise."""
    from backend.fleet.concurrency import _open_db

    conn1 = _open_db()
    conn1.close()
    # Second open triggers the ALTER TABLE again — should be a no-op via except.
    conn2 = _open_db()
    conn2.close()


# ── AC 8: reap_stale() returns 0 when all processes are alive ─────────────────

def test_no_reap_when_all_alive(tmp_path):
    """When every row has a live PID, reap_stale() must return 0."""
    from backend.fleet.concurrency import reap_stale, count_fleet

    our_pid = os.getpid()
    for i in range(3):
        _insert_row(tmp_path, "test-proj", f"live-{i}", "executor",
                    _ts(-600), our_pid)

    assert count_fleet() == 3
    reaped = reap_stale()
    assert reaped == 0
    assert count_fleet() == 3
