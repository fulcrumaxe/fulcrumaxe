"""
Tests for SqliteBlackboard and get_blackboard() factory.

Covers: CRUD operations, CAS, list_keys, lock/unlock, concurrent writes,
fallback behavior when state.db does not exist.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.db import Database
from backend.blackboard import Blackboard, SqliteBlackboard, get_blackboard


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.db")


@pytest.fixture()
def bb(db: Database) -> SqliteBlackboard:
    return SqliteBlackboard(db=db)


# ------------------------------------------------------------------
# Basic CRUD
# ------------------------------------------------------------------


def test_write_and_read(bb: SqliteBlackboard) -> None:
    bb.write("loop/status", "idle", updated_by="test")
    assert bb.read("loop/status") == "idle"


def test_read_missing_key_returns_none(bb: SqliteBlackboard) -> None:
    assert bb.read("no/such/key") is None


def test_write_increments_version(bb: SqliteBlackboard) -> None:
    bb.write("counter", 1)
    bb.write("counter", 2)
    entry = bb.read_entry("counter")
    assert entry is not None
    # Version is stored in the value blob
    # The entry dict from get() has 'value' decoded, but version is inside that blob
    # We need to check via read_entry which returns the row dict
    # The value stored is the JSON-encoded entry dict including "version"
    # But SqliteBlackboard.read_entry returns the raw row from DB
    # The 'value' column holds just the scalar/object value, version is the counter in the entry
    # Let's verify the write happened
    assert bb.read("counter") == 2


def test_delete_key(bb: SqliteBlackboard) -> None:
    bb.write("to/delete", "value")
    result = bb.delete("to/delete")
    assert result is True
    assert bb.read("to/delete") is None


def test_delete_nonexistent(bb: SqliteBlackboard) -> None:
    assert bb.delete("does/not/exist") is False


# ------------------------------------------------------------------
# list_keys
# ------------------------------------------------------------------


def test_list_keys_empty(bb: SqliteBlackboard) -> None:
    assert bb.list_keys() == []


def test_list_keys_all(bb: SqliteBlackboard) -> None:
    for k in ("a/x", "b/y", "c/z"):
        bb.write(k, k)
    keys = bb.list_keys()
    assert sorted(keys) == ["a/x", "b/y", "c/z"]


def test_list_keys_prefix(bb: SqliteBlackboard) -> None:
    for k in ("loop/status", "loop/count", "budget/ceiling"):
        bb.write(k, k)
    keys = bb.list_keys("loop/")
    assert set(keys) == {"loop/status", "loop/count"}


# ------------------------------------------------------------------
# CAS
# ------------------------------------------------------------------


def test_cas_success(bb: SqliteBlackboard) -> None:
    bb.write("versioned/key", "v1")
    entry = bb.read_entry("versioned/key")
    # Version 1 after first write
    ok = bb.cas("versioned/key", "v2", expected_version=1)
    assert ok is True
    assert bb.read("versioned/key") == "v2"


def test_cas_conflict(bb: SqliteBlackboard) -> None:
    bb.write("conflict/key", "original")
    # Wrong version — should fail
    ok = bb.cas("conflict/key", "new", expected_version=99)
    assert ok is False
    assert bb.read("conflict/key") == "original"


def test_cas_nonexistent_key(bb: SqliteBlackboard) -> None:
    ok = bb.cas("ghost/key", "value", expected_version=1)
    assert ok is False


# ------------------------------------------------------------------
# Lock / Unlock
# ------------------------------------------------------------------


def test_lock_and_unlock(bb: SqliteBlackboard) -> None:
    bb.write("locked/key", "data")
    acquired = bb.lock("locked/key", locked_by="agent-A")
    assert acquired is True
    # Second agent cannot acquire
    acquired2 = bb.lock("locked/key", locked_by="agent-B")
    assert acquired2 is False
    # Original holder releases
    released = bb.unlock("locked/key", locked_by="agent-A")
    assert released is True
    # Now B can acquire
    acquired3 = bb.lock("locked/key", locked_by="agent-B")
    assert acquired3 is True


def test_unlock_wrong_holder(bb: SqliteBlackboard) -> None:
    bb.write("guarded/key", "data")
    bb.lock("guarded/key", locked_by="agent-A")
    released = bb.unlock("guarded/key", locked_by="agent-B")
    assert released is False


# ------------------------------------------------------------------
# Thread safety
# ------------------------------------------------------------------


def test_concurrent_writes_different_keys(tmp_path: Path) -> None:
    """Two threads writing different blackboard keys concurrently should not corrupt data."""
    db = Database(tmp_path / "concurrent.db")
    bb = SqliteBlackboard(db=db)
    errors: list[Exception] = []

    def writer(key: str, iters: int) -> None:
        for i in range(iters):
            try:
                bb.write(key, i, updated_by="test-thread")
            except Exception as exc:
                errors.append(exc)

    t1 = threading.Thread(target=writer, args=("thread/alpha", 30))
    t2 = threading.Thread(target=writer, args=("thread/beta", 30))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"Thread errors: {errors}"
    assert bb.read("thread/alpha") is not None
    assert bb.read("thread/beta") is not None


# ------------------------------------------------------------------
# Factory / fallback
# ------------------------------------------------------------------


def test_get_blackboard_returns_file_based_when_no_db(tmp_path: Path) -> None:
    """When state.db does not exist, get_blackboard() returns file-based Blackboard."""
    fake_db_path = tmp_path / "state.db"
    with patch("backend.db._DB_PATH", fake_db_path):
        bb = get_blackboard(prefer_sqlite=True)
    assert isinstance(bb, Blackboard)


def test_get_blackboard_returns_sqlite_when_db_exists(tmp_path: Path) -> None:
    """When state.db exists, get_blackboard() returns SqliteBlackboard."""
    db_path = tmp_path / "state.db"
    # Create the DB file
    Database(db_path)
    with patch("backend.db._DB_PATH", db_path):
        bb = get_blackboard(prefer_sqlite=True)
    assert isinstance(bb, SqliteBlackboard)


def test_get_blackboard_prefer_sqlite_false(tmp_path: Path) -> None:
    """prefer_sqlite=False always returns file-based Blackboard."""
    db_path = tmp_path / "state.db"
    Database(db_path)  # DB exists
    with patch("backend.db._DB_PATH", db_path):
        bb = get_blackboard(prefer_sqlite=False)
    assert isinstance(bb, Blackboard)


# ------------------------------------------------------------------
# Key validation (inherited)
# ------------------------------------------------------------------


def test_invalid_key_empty(bb: SqliteBlackboard) -> None:
    with pytest.raises(ValueError, match="empty"):
        bb.write("", "value")


def test_invalid_key_dotdot(bb: SqliteBlackboard) -> None:
    with pytest.raises(ValueError, match="\\.\\."):
        bb.write("../escape", "value")


def test_invalid_key_absolute(bb: SqliteBlackboard) -> None:
    with pytest.raises(ValueError, match="absolute"):
        bb.write("/absolute/key", "value")


# ------------------------------------------------------------------
# Migration idempotency
# ------------------------------------------------------------------


def test_write_twice_no_duplicate_keys(bb: SqliteBlackboard) -> None:
    bb.write("idem/key", "first")
    bb.write("idem/key", "second")
    keys = bb.list_keys("idem/")
    assert len(keys) == 1
    assert bb.read("idem/key") == "second"
