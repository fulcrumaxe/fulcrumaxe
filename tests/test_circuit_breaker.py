"""
Tests for backend/circuit_breaker.py — record_failure, record_success, is_blocked,
transition history emit, and history() query.

Uses an isolated Blackboard (tmp_path) patched into the module-level _bb.
History file is redirected to tmp_path to avoid touching the real repo state.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard
import backend.circuit_breaker as cb


@pytest.fixture()
def isolated_bb(tmp_path):
    """Return a fresh Blackboard and patch it into the circuit_breaker module."""
    bb = Blackboard(root=tmp_path / "blackboard")
    with patch.object(cb, "_bb", bb):
        yield bb


@pytest.fixture()
def isolated_history(tmp_path, isolated_bb):
    """Redirect the history file to tmp_path so tests don't write to the repo."""
    history_path = tmp_path / "circuit-breaker-history.jsonl"
    with patch.object(cb, "_HISTORY_FILE", history_path):
        yield history_path


def test_record_failure_increments_count(isolated_bb):
    count = cb.record_failure(42, "executor", "timeout")
    assert count == 1
    count2 = cb.record_failure(42, "executor", "timeout")
    assert count2 == 2


def test_record_success_resets_counter(isolated_bb):
    cb.record_failure(10, "executor", "error")
    cb.record_failure(10, "executor", "error")
    cb.record_success(10)
    assert cb.is_blocked(10) is False
    # confirm key is deleted
    assert isolated_bb.read("failures/10") is None


def test_is_blocked_below_threshold(isolated_bb):
    cb.record_failure(7, "executor", "err")
    cb.record_failure(7, "executor", "err")
    # default threshold is 3 — 2 failures should NOT block
    assert cb.is_blocked(7) is False


def test_is_blocked_at_threshold(isolated_bb):
    for _ in range(3):
        cb.record_failure(99, "executor", "err")
    assert cb.is_blocked(99) is True


def test_is_blocked_with_custom_threshold(isolated_bb):
    cb.record_failure(5, "executor", "err")
    # threshold=1 means one failure is enough
    assert cb.is_blocked(5, threshold=1) is True
    # threshold=2 means one failure is not enough
    assert cb.is_blocked(5, threshold=2) is False


def test_record_failure_independent_discussions(isolated_bb):
    cb.record_failure(1, "executor", "err")
    cb.record_failure(1, "executor", "err")
    cb.record_failure(1, "executor", "err")
    # Discussion #2 must be unaffected
    assert cb.is_blocked(2) is False
    assert cb.is_blocked(1) is True


def test_record_success_on_never_failed_discussion(isolated_bb):
    # Should not raise even if no counter exists
    cb.record_success(999)
    assert cb.is_blocked(999) is False


def test_failure_count_persists_across_calls(isolated_bb):
    cb.record_failure(55, "code-reviewer", "bad output")
    val = isolated_bb.read("failures/55")
    assert val == 1


# ------------------------------------------------------------------
# History emit tests
# ------------------------------------------------------------------


def test_trip_emits_jsonl_line(isolated_history):
    """Crossing the threshold appends one line with correct schema fields."""
    threshold = cb.DEFAULT_THRESHOLD
    # Record enough failures to trip
    for i in range(threshold):
        cb.record_failure(200, "executor", "preflight failed", last_pr=412)

    lines = isolated_history.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["role"] == "executor"
    assert entry["from_state"] == "healthy"
    assert entry["to_state"] == "tripped"
    assert entry["last_pr"] == 412
    assert "timestamp" in entry
    assert "reason" in entry
    assert "context" in entry
    assert "recent_errors" in entry["context"]


def test_reset_emits_jsonl_line(isolated_history):
    """Resetting a tripped circuit appends a reset line."""
    threshold = cb.DEFAULT_THRESHOLD
    for _ in range(threshold):
        cb.record_failure(201, "executor", "lint failed")

    cb.record_success(201, agent="executor", last_pr=413)

    lines = isolated_history.read_text().strip().splitlines()
    assert len(lines) == 2  # trip + reset
    reset_entry = json.loads(lines[1])
    assert reset_entry["from_state"] == "tripped"
    assert reset_entry["to_state"] == "healthy"


def test_no_emit_below_threshold(isolated_history):
    """Failures below threshold must not write any history line."""
    for _ in range(cb.DEFAULT_THRESHOLD - 1):
        cb.record_failure(202, "executor", "err")
    assert not isolated_history.exists()


def test_no_reset_emit_when_not_tripped(isolated_history):
    """record_success on a healthy discussion must not write a history line."""
    cb.record_success(203, agent="executor")
    assert not isolated_history.exists()


def test_history_filters_by_role(isolated_history):
    """history() returns only lines for the requested role."""
    threshold = cb.DEFAULT_THRESHOLD
    for _ in range(threshold):
        cb.record_failure(300, "executor", "err A")
    for _ in range(threshold):
        cb.record_failure(301, "code-reviewer", "err B")

    exec_entries = cb.history("executor")
    assert all(e["role"] == "executor" for e in exec_entries)
    rev_entries = cb.history("code-reviewer")
    assert all(e["role"] == "code-reviewer" for e in rev_entries)


def test_history_limit_honored(isolated_history):
    """history(limit=N) returns at most N entries."""
    # Write 5 trips by manipulating the file directly
    threshold = cb.DEFAULT_THRESHOLD
    for disc in range(400, 405):
        for _ in range(threshold):
            cb.record_failure(disc, "executor", f"err {disc}")

    entries = cb.history("executor", limit=3)
    assert len(entries) == 3


def test_history_unknown_role_returns_empty(isolated_history):
    """history() with an unknown role returns an empty list and exits cleanly."""
    result = cb.history("nonexistent-role-xyz")
    assert result == []


def test_history_unknown_role_no_file():
    """history() returns empty list when the history file doesn't exist yet."""
    with patch.object(cb, "_HISTORY_FILE", Path("/tmp/does-not-exist-cb-history.jsonl")):
        result = cb.history("executor")
    assert result == []


def test_jsonl_atomic_append(isolated_history):
    """Multiple transitions write separate valid JSON lines (no corruption)."""
    import threading

    threshold = cb.DEFAULT_THRESHOLD
    errors: list[Exception] = []

    def trip(disc: int) -> None:
        try:
            for _ in range(threshold):
                cb.record_failure(disc, "executor", f"err {disc}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=trip, args=(500 + i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    lines = isolated_history.read_text().strip().splitlines()
    for line in lines:
        # Every line must be valid JSON
        parsed = json.loads(line)
        assert "role" in parsed
        assert "from_state" in parsed
