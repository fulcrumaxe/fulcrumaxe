"""
Tests for backend/blackboard.py — Blackboard class.
"""

import threading
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard


def test_write_and_read(bb):
    bb.write("foo/bar", "hello", updated_by="test")
    assert bb.read("foo/bar") == "hello"


def test_read_missing_key(bb):
    assert bb.read("no/such/key") is None


def test_read_entry_returns_metadata(bb):
    bb.write("meta/key", 42, updated_by="tester")
    entry = bb.read_entry("meta/key")
    assert entry is not None
    assert "version" in entry
    assert "updated_at" in entry
    assert "updated_by" in entry
    assert entry["updated_by"] == "tester"
    assert entry["value"] == 42


def test_write_increments_version(bb):
    bb.write("v/key", "first")
    bb.write("v/key", "second")
    entry = bb.read_entry("v/key")
    assert entry["version"] == 2


def test_cas_success(bb):
    bb.write("cas/key", "initial")
    entry = bb.read_entry("cas/key")
    assert entry["version"] == 1

    ok = bb.cas("cas/key", "updated", expected_version=1)
    assert ok is True

    entry2 = bb.read_entry("cas/key")
    assert entry2["version"] == 2
    assert entry2["value"] == "updated"


def test_cas_version_conflict(bb):
    bb.write("cas/conflict", "initial")
    # Use a wrong expected_version
    ok = bb.cas("cas/conflict", "bad", expected_version=99)
    assert ok is False
    # Value unchanged
    assert bb.read("cas/conflict") == "initial"


def test_cas_missing_key(bb):
    ok = bb.cas("cas/missing", "value", expected_version=1)
    assert ok is False


def test_delete_existing_key(bb):
    bb.write("del/key", "to-delete")
    result = bb.delete("del/key")
    assert result is True
    assert bb.read("del/key") is None


def test_delete_missing_key(bb):
    result = bb.delete("del/nonexistent")
    assert result is False


def test_list_keys_empty(bb):
    keys = bb.list_keys()
    assert keys == []


def test_list_keys_with_prefix(bb):
    bb.write("a/one", 1)
    bb.write("a/two", 2)
    bb.write("b/three", 3)

    a_keys = bb.list_keys(prefix="a/")
    assert sorted(a_keys) == ["a/one", "a/two"]

    all_keys = bb.list_keys()
    assert sorted(all_keys) == ["a/one", "a/two", "b/three"]


def test_key_validation_rejects_dotdot(bb):
    with pytest.raises(ValueError, match="must not contain"):
        bb.write("a/../b", "bad")


def test_key_validation_rejects_absolute(bb):
    with pytest.raises(ValueError, match="must not be absolute"):
        bb.write("/absolute/key", "bad")


def test_concurrent_writes(bb):
    """5 threads each writing to a distinct key — none should stomp each other."""
    errors = []

    def write_key(idx):
        try:
            bb.write(f"thread/key{idx}", idx, updated_by=f"thread-{idx}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_key, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"

    for i in range(5):
        val = bb.read(f"thread/key{i}")
        assert val == i, f"Expected {i} for thread/key{i}, got {val}"
