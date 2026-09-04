"""
Behavioral tests for backend/blackboard.py

Tests cover both Blackboard (file-based) and SqliteBlackboard (SQLite-backed)
via parametrize fixtures. All tests operate on temporary directories and
in-memory/temp sqlite databases — the real state dir is never touched.

Run with:
    python3 -m pytest backend/tests/test_blackboard.py -v
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.blackboard import Blackboard, LockTimeout, SqliteBlackboard
from backend.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def file_bb(tmp_path: Path) -> Blackboard:
    """File-based Blackboard rooted at a temp directory."""
    return Blackboard(root=tmp_path / "bb")


@pytest.fixture()
def sqlite_bb(tmp_path: Path) -> SqliteBlackboard:
    """SQLite-backed Blackboard using a temp db file."""
    db = Database(db_path=tmp_path / "state.db")
    return SqliteBlackboard(db=db)


@pytest.fixture(params=["file", "sqlite"])
def bb(request, tmp_path: Path):
    """Parametrized fixture that yields both Blackboard implementations."""
    if request.param == "file":
        return Blackboard(root=tmp_path / "bb")
    db = Database(db_path=tmp_path / "state.db")
    return SqliteBlackboard(db=db)


# ---------------------------------------------------------------------------
# write / read
# ---------------------------------------------------------------------------


def test_write_then_read_returns_value(bb):
    """write() followed by read() returns exactly the written value."""
    bb.write("test/key", "hello", updated_by="tester")
    assert bb.read("test/key") == "hello"


def test_read_missing_key_returns_none(bb):
    """read() on a key that was never written returns None."""
    assert bb.read("does/not/exist") is None


def test_write_various_types(bb):
    """write() handles strings, numbers, booleans, dicts, and lists."""
    bb.write("t/str", "a string")
    bb.write("t/int", 42)
    bb.write("t/bool", True)
    bb.write("t/dict", {"x": 1, "y": [2, 3]})
    bb.write("t/list", [1, "two", 3.0])

    assert bb.read("t/str") == "a string"
    assert bb.read("t/int") == 42
    assert bb.read("t/bool") is True
    assert bb.read("t/dict") == {"x": 1, "y": [2, 3]}
    assert bb.read("t/list") == [1, "two", 3.0]


def test_write_overwrites_existing_value(bb):
    """Subsequent writes overwrite the previous value."""
    bb.write("cfg/mode", "idle")
    bb.write("cfg/mode", "running")
    assert bb.read("cfg/mode") == "running"


def test_write_increments_version(bb):
    """Each write increments the version number by 1."""
    bb.write("v/key", "first")
    entry_v1 = bb.read_entry("v/key")
    assert entry_v1 is not None
    assert entry_v1["version"] == 1

    bb.write("v/key", "second")
    entry_v2 = bb.read_entry("v/key")
    assert entry_v2 is not None
    assert entry_v2["version"] == 2


# ---------------------------------------------------------------------------
# read_entry — metadata shape
# ---------------------------------------------------------------------------


def test_read_entry_returns_none_for_missing_key(bb):
    """read_entry() returns None when the key does not exist."""
    assert bb.read_entry("no/such/key") is None


def test_read_entry_shape(bb):
    """read_entry() returns a dict with the expected metadata fields."""
    bb.write("meta/key", "some value", updated_by="agent-42")
    entry = bb.read_entry("meta/key")

    assert entry is not None
    assert "value" in entry
    assert "version" in entry
    assert "updated_at" in entry
    assert "updated_by" in entry

    assert entry["value"] == "some value"
    assert entry["version"] >= 1
    assert entry["updated_by"] == "agent-42"
    # updated_at should be an ISO-8601 string
    assert isinstance(entry["updated_at"], str)
    assert "T" in entry["updated_at"] or entry["updated_at"]  # non-empty


# ---------------------------------------------------------------------------
# compare-and-swap (cas)
# ---------------------------------------------------------------------------


def test_cas_succeeds_when_version_matches(bb):
    """cas() returns True and updates value when expected_version matches."""
    bb.write("state/x", "v1")
    entry = bb.read_entry("state/x")
    assert entry is not None
    v = entry["version"]

    ok = bb.cas("state/x", "v2", expected_version=v)
    assert ok is True
    assert bb.read("state/x") == "v2"


def test_cas_fails_when_version_stale(bb):
    """cas() returns False without modifying the value when version is wrong."""
    bb.write("state/y", "original")
    # Use a clearly wrong version
    ok = bb.cas("state/y", "modified", expected_version=9999)
    assert ok is False
    assert bb.read("state/y") == "original"


def test_cas_fails_on_missing_key(bb):
    """cas() returns False when the key does not exist at all."""
    ok = bb.cas("state/nonexistent", "value", expected_version=1)
    assert ok is False


def test_cas_increments_version(bb):
    """Successful cas() bumps the version by 1."""
    bb.write("cas/ver", "start")
    entry = bb.read_entry("cas/ver")
    assert entry is not None
    v = entry["version"]

    bb.cas("cas/ver", "next", expected_version=v)
    updated = bb.read_entry("cas/ver")
    assert updated is not None
    assert updated["version"] == v + 1


def test_cas_prevents_concurrent_double_write(bb):
    """Two cas() calls with the same expected_version: only one succeeds."""
    bb.write("cas/race", "initial")
    entry = bb.read_entry("cas/race")
    assert entry is not None
    v = entry["version"]

    # First CAS wins
    first = bb.cas("cas/race", "winner", expected_version=v)
    # Second CAS with same old version must fail
    second = bb.cas("cas/race", "loser", expected_version=v)

    assert first is True
    assert second is False
    assert bb.read("cas/race") == "winner"


# ---------------------------------------------------------------------------
# list_keys
# ---------------------------------------------------------------------------


def test_list_keys_empty_blackboard(bb):
    """list_keys() on an empty blackboard returns an empty list."""
    assert bb.list_keys() == []


def test_list_keys_returns_all_keys(bb):
    """list_keys() with no prefix returns all stored keys."""
    bb.write("a/one", 1)
    bb.write("b/two", 2)
    bb.write("c/three", 3)

    keys = bb.list_keys()
    assert "a/one" in keys
    assert "b/two" in keys
    assert "c/three" in keys
    assert len(keys) == 3


def test_list_keys_prefix_filter(bb):
    """list_keys(prefix) returns only keys with that prefix."""
    bb.write("loop/status", "idle")
    bb.write("loop/count", 5)
    bb.write("agent/foo", "bar")

    loop_keys = bb.list_keys("loop/")
    assert "loop/status" in loop_keys
    assert "loop/count" in loop_keys
    assert "agent/foo" not in loop_keys
    assert len(loop_keys) == 2


def test_list_keys_nonmatching_prefix_returns_empty(bb):
    """list_keys(prefix) returns [] when no keys match."""
    bb.write("a/x", 1)
    assert bb.list_keys("zzz/") == []


def test_list_keys_sorted(bb):
    """list_keys() returns keys in sorted order."""
    bb.write("c/three", 3)
    bb.write("a/one", 1)
    bb.write("b/two", 2)

    keys = bb.list_keys()
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_existing_key_returns_true(bb):
    """delete() returns True when the key existed."""
    bb.write("del/me", "value")
    result = bb.delete("del/me")
    assert result is True


def test_delete_removes_key(bb):
    """After delete(), read() returns None for that key."""
    bb.write("del/gone", "value")
    bb.delete("del/gone")
    assert bb.read("del/gone") is None


def test_delete_nonexistent_key_returns_false(bb):
    """delete() returns False when the key was not present."""
    assert bb.delete("del/missing") is False


def test_delete_does_not_affect_other_keys(bb):
    """Deleting one key leaves sibling keys intact."""
    bb.write("ns/alpha", "a")
    bb.write("ns/beta", "b")
    bb.delete("ns/alpha")

    assert bb.read("ns/alpha") is None
    assert bb.read("ns/beta") == "b"


def test_delete_key_not_in_list_keys(bb):
    """After delete(), the key is absent from list_keys()."""
    bb.write("rem/key", 1)
    bb.delete("rem/key")
    assert "rem/key" not in bb.list_keys()


# ---------------------------------------------------------------------------
# Key validation (both implementations share _validate_key)
# ---------------------------------------------------------------------------


def test_empty_key_raises(bb):
    """An empty key raises ValueError."""
    with pytest.raises(ValueError, match="empty"):
        bb.write("", "x")


def test_dotdot_key_raises(bb):
    """A key containing '..' raises ValueError."""
    with pytest.raises(ValueError, match="'\\.\\.'"):
        bb.write("a/../b", "x")


def test_absolute_key_raises(bb):
    """A key starting with '/' raises ValueError."""
    with pytest.raises(ValueError, match="absolute"):
        bb.write("/absolute/key", "x")


# ---------------------------------------------------------------------------
# File-based Blackboard — flock locking
# ---------------------------------------------------------------------------


def test_file_write_returns_true(file_bb):
    """Blackboard.write() returns True on success."""
    result = file_bb.write("lock/key", "val")
    assert result is True


def test_file_bb_nested_key_paths(file_bb):
    """File backend creates nested directory structure for slash-separated keys."""
    file_bb.write("a/b/c/d", "deep")
    assert file_bb.read("a/b/c/d") == "deep"


# ---------------------------------------------------------------------------
# SqliteBlackboard — lock / unlock
# ---------------------------------------------------------------------------


def test_sqlite_lock_acquire_succeeds(sqlite_bb):
    """lock() returns True when the key is unlocked."""
    sqlite_bb.write("lk/x", "value")
    assert sqlite_bb.lock("lk/x", locked_by="agent-1") is True


def test_sqlite_lock_second_acquire_fails(sqlite_bb):
    """lock() returns False when already held by a different holder."""
    sqlite_bb.write("lk/y", "value")
    sqlite_bb.lock("lk/y", locked_by="agent-1")
    # A different holder cannot grab it
    assert sqlite_bb.lock("lk/y", locked_by="agent-2") is False


def test_sqlite_unlock_by_holder_succeeds(sqlite_bb):
    """unlock() returns True and releases the lock for the holder."""
    sqlite_bb.write("lk/z", "value")
    sqlite_bb.lock("lk/z", locked_by="agent-1")
    assert sqlite_bb.unlock("lk/z", locked_by="agent-1") is True


def test_sqlite_unlock_by_non_holder_fails(sqlite_bb):
    """unlock() returns False when called by a different agent than the holder."""
    sqlite_bb.write("lk/w", "value")
    sqlite_bb.lock("lk/w", locked_by="agent-1")
    assert sqlite_bb.unlock("lk/w", locked_by="agent-2") is False
    # Lock is still held — agent-2 still can't acquire it
    assert sqlite_bb.lock("lk/w", locked_by="agent-2") is False


def test_sqlite_after_unlock_can_reacquire(sqlite_bb):
    """After unlock(), another holder can acquire the lock."""
    sqlite_bb.write("lk/v", "value")
    sqlite_bb.lock("lk/v", locked_by="agent-1")
    sqlite_bb.unlock("lk/v", locked_by="agent-1")
    # Now agent-2 can get it
    assert sqlite_bb.lock("lk/v", locked_by="agent-2") is True


# ---------------------------------------------------------------------------
# Concurrency — file backend (thread safety)
# ---------------------------------------------------------------------------


def test_file_bb_concurrent_writes_all_succeed(file_bb):
    """Multiple threads writing different keys all succeed without corruption."""
    errors: list[Exception] = []

    def writer(i: int) -> None:
        try:
            file_bb.write(f"thread/key{i}", i * 10)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    for i in range(10):
        assert file_bb.read(f"thread/key{i}") == i * 10


def test_file_bb_cas_under_contention(file_bb):
    """Under concurrent CAS attempts, exactly one thread wins each round."""
    file_bb.write("race/slot", 0)

    wins: list[int] = []
    lock = threading.Lock()

    def try_increment() -> None:
        for _ in range(5):
            entry = file_bb.read_entry("race/slot")
            if entry is None:
                continue
            v = entry["version"]
            old_val = entry["value"]
            if file_bb.cas("race/slot", old_val + 1, expected_version=v):
                with lock:
                    wins.append(1)

    threads = [threading.Thread(target=try_increment) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The final value must equal the number of successful CAS operations
    final = file_bb.read("race/slot")
    assert final == len(wins)
    assert final > 0  # At least some increments succeeded
