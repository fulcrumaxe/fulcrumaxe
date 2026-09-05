"""
Tests for backend/db.py — SQLite abstraction layer.

Covers: CRUD operations, WAL mode, query(), list_keys(), insert_notification(),
concurrent writes, and migration idempotency helpers.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from backend.db import Database, get_db, state_db_exists


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """Return a fresh in-memory-ish Database per test (uses tmp_path)."""
    return Database(tmp_path / "state.db")


# ------------------------------------------------------------------
# WAL mode
# ------------------------------------------------------------------


def test_wal_mode_is_active(db: Database) -> None:
    conn = db._conn()
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


# ------------------------------------------------------------------
# Blackboard CRUD
# ------------------------------------------------------------------


def test_put_and_get_blackboard(db: Database) -> None:
    entry = {"value": "idle", "version": 1, "updated_at": "2026-01-01T00:00:00+00:00"}
    db.put("blackboard", "loop/status", entry)
    result = db.get("blackboard", "loop/status")
    assert result is not None
    assert result["key"] == "loop/status"
    # The full entry dict is stored as the 'value' blob, so result["value"] is the entry.
    assert result["value"]["value"] == "idle"
    assert result["value"]["version"] == 1


def test_get_missing_key_returns_none(db: Database) -> None:
    assert db.get("blackboard", "nonexistent/key") is None


def test_put_overwrites_existing(db: Database) -> None:
    db.put("blackboard", "loop/status", {"value": "idle", "version": 1, "updated_at": "2026-01-01T00:00:00+00:00"})
    db.put("blackboard", "loop/status", {"value": "running", "version": 2, "updated_at": "2026-01-01T00:01:00+00:00"})
    result = db.get("blackboard", "loop/status")
    assert result["value"]["value"] == "running"


def test_delete_existing_key(db: Database) -> None:
    db.put("blackboard", "loop/status", {"value": "x", "version": 1, "updated_at": "2026-01-01T00:00:00+00:00"})
    deleted = db.delete("blackboard", "loop/status")
    assert deleted is True
    assert db.get("blackboard", "loop/status") is None


def test_delete_nonexistent_key(db: Database) -> None:
    assert db.delete("blackboard", "no/such/key") is False


def test_list_keys_all(db: Database) -> None:
    for k in ("a/x", "a/y", "b/z"):
        db.put("blackboard", k, {"value": k, "version": 1, "updated_at": "2026-01-01T00:00:00+00:00"})
    keys = db.list_keys("blackboard")
    assert sorted(keys) == ["a/x", "a/y", "b/z"]


def test_list_keys_with_prefix(db: Database) -> None:
    for k in ("loop/status", "loop/count", "budget/ceiling"):
        db.put("blackboard", k, {"value": k, "version": 1, "updated_at": "2026-01-01T00:00:00+00:00"})
    keys = db.list_keys("blackboard", "loop/")
    assert set(keys) == {"loop/status", "loop/count"}


# ------------------------------------------------------------------
# Sessions CRUD
# ------------------------------------------------------------------


def test_put_and_get_session(db: Database) -> None:
    data = {
        "session_id": "abc-123",
        "started_at": "2026-01-01T00:00:00",
        "ended_at": None,
        "iteration_count": 0,
        "prs_merged": [],
        "discussions_completed": [],
    }
    db.put("sessions", "abc-123", data)
    result = db.get("sessions", "abc-123")
    assert result is not None
    assert result["id"] == "abc-123"
    loaded_data = result["data"]
    assert loaded_data["session_id"] == "abc-123"


def test_query_sessions_by_status(db: Database) -> None:
    active = {
        "session_id": "active-1",
        "started_at": "2026-01-01T00:00:00",
        "ended_at": None,
        "iteration_count": 0,
        "prs_merged": [],
        "discussions_completed": [],
    }
    closed = {
        "session_id": "closed-1",
        "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T01:00:00",
        "iteration_count": 5,
        "prs_merged": [1],
        "discussions_completed": [2],
    }
    db.put("sessions", "active-1", active)
    db.put("sessions", "closed-1", closed)

    active_rows = db.query("sessions", "status = ?", ["active"])
    assert len(active_rows) == 1
    assert active_rows[0]["id"] == "active-1"

    closed_rows = db.query("sessions", "status = ?", ["closed"])
    assert len(closed_rows) == 1
    assert closed_rows[0]["id"] == "closed-1"


# ------------------------------------------------------------------
# Notifications
# ------------------------------------------------------------------


def test_insert_notification(db: Database) -> None:
    row_id = db.insert_notification(
        event_type="pr_merged",
        channel="slack",
        success=True,
        message="PR #42 merged",
    )
    assert isinstance(row_id, int)
    assert row_id >= 1

    rows = db.query("notifications", "event_type = ?", ["pr_merged"])
    assert len(rows) == 1
    assert rows[0]["channel"] == "slack"
    assert rows[0]["success"] == 1


def test_insert_notification_failure(db: Database) -> None:
    db.insert_notification(
        event_type="alert",
        channel="pagerduty",
        success=False,
        error="timeout",
    )
    rows = db.query("notifications", "success = ?", [0])
    assert len(rows) == 1
    assert rows[0]["error"] == "timeout"


# ------------------------------------------------------------------
# Unknown table
# ------------------------------------------------------------------


def test_unknown_table_raises(db: Database) -> None:
    with pytest.raises(ValueError, match="Unknown table"):
        db.get("nonexistent_table", "key")


# ------------------------------------------------------------------
# Migration idempotency (INSERT OR REPLACE)
# ------------------------------------------------------------------


def test_double_put_is_idempotent(db: Database) -> None:
    entry = {"value": "hello", "version": 1, "updated_at": "2026-01-01T00:00:00+00:00"}
    db.put("blackboard", "idempotent/key", entry)
    db.put("blackboard", "idempotent/key", entry)  # second run — must not duplicate

    keys = db.list_keys("blackboard")
    assert keys.count("idempotent/key") == 1


# ------------------------------------------------------------------
# Concurrent writes
# ------------------------------------------------------------------


def test_concurrent_writes_no_corruption(tmp_path: Path) -> None:
    """Two threads writing different keys should not corrupt or deadlock."""
    db = Database(tmp_path / "concurrent.db")
    errors: list[Exception] = []

    def writer(key: str, iters: int) -> None:
        for i in range(iters):
            try:
                db.put(
                    "blackboard",
                    key,
                    {"value": i, "version": i, "updated_at": "2026-01-01T00:00:00+00:00"},
                )
            except Exception as exc:
                errors.append(exc)

    t1 = threading.Thread(target=writer, args=("thread/alpha", 20))
    t2 = threading.Thread(target=writer, args=("thread/beta", 20))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Concurrent write errors: {errors}"
    # Both keys should be present
    assert db.get("blackboard", "thread/alpha") is not None
    assert db.get("blackboard", "thread/beta") is not None


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------


def test_get_db_singleton(tmp_path: Path) -> None:
    """get_db() should return the same instance on repeated calls."""
    import backend.db as db_module
    # Reset singleton for test isolation
    original = db_module._db_instance
    db_module._db_instance = None
    try:
        a = get_db(tmp_path / "singleton.db")
        b = get_db(tmp_path / "singleton.db")
        assert a is b
    finally:
        if a is not None:
            a.close()
        db_module._db_instance = original


def test_state_db_exists(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    assert not state_db_exists(path)
    Database(path)  # creates the file
    assert state_db_exists(path)
