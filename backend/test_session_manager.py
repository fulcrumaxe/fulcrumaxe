"""
Tests for backend/session_manager.py.

Run with: python -m pytest backend/test_session_manager.py -v
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from backend.session_manager import SessionManager


@pytest.fixture()
def mgr(tmp_path: Path) -> SessionManager:
    """Return a SessionManager backed by a temporary directory."""
    return SessionManager(sessions_dir=tmp_path / "sessions")


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


def test_start_session_creates_file(mgr: SessionManager, tmp_path: Path) -> None:
    s = mgr.start_session()
    assert s["session_id"]
    assert s["ended_at"] is None
    assert s["iteration_count"] == 0
    assert s["prs_merged"] == []
    assert s["discussions_completed"] == []
    # File must exist on disk.
    p = mgr._dir / f"{s['session_id']}.json"
    assert p.exists()


def test_start_session_closes_open_session(mgr: SessionManager) -> None:
    s1 = mgr.start_session()
    s2 = mgr.start_session()
    # First session should now be closed.
    closed = mgr.get_session(s1["session_id"])
    assert closed is not None
    assert closed["ended_at"] is not None
    # Second session is still open.
    assert s2["ended_at"] is None
    # Only one open session.
    assert mgr.current_session()["session_id"] == s2["session_id"]


# ---------------------------------------------------------------------------
# record_iteration
# ---------------------------------------------------------------------------


def test_record_iteration_increments(mgr: SessionManager) -> None:
    mgr.start_session()
    mgr.record_iteration()
    mgr.record_iteration()
    s = mgr.current_session()
    assert s["iteration_count"] == 2


def test_record_iteration_no_session_is_noop(mgr: SessionManager) -> None:
    # No crash when no session exists.
    mgr.record_iteration()


# ---------------------------------------------------------------------------
# record_pr_merged
# ---------------------------------------------------------------------------


def test_record_pr_merged_appends(mgr: SessionManager) -> None:
    mgr.start_session()
    mgr.record_pr_merged(42)
    mgr.record_pr_merged(99)
    s = mgr.current_session()
    assert 42 in s["prs_merged"]
    assert 99 in s["prs_merged"]


def test_record_pr_merged_no_duplicates(mgr: SessionManager) -> None:
    mgr.start_session()
    mgr.record_pr_merged(7)
    mgr.record_pr_merged(7)
    s = mgr.current_session()
    assert s["prs_merged"].count(7) == 1


# ---------------------------------------------------------------------------
# close_session
# ---------------------------------------------------------------------------


def test_close_session_sets_ended_at(mgr: SessionManager) -> None:
    mgr.start_session()
    closed = mgr.close_session()
    assert closed is not None
    assert closed["ended_at"] is not None
    # current_session() should now return None.
    assert mgr.current_session() is None


def test_close_session_no_open_is_none(mgr: SessionManager) -> None:
    result = mgr.close_session()
    assert result is None


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def test_list_sessions_returns_newest_first(mgr: SessionManager) -> None:
    s1 = mgr.start_session()
    mgr.close_session()
    time.sleep(0.01)  # ensure distinct timestamps
    s2 = mgr.start_session()

    sessions = mgr.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == s2["session_id"]
    assert sessions[1]["session_id"] == s1["session_id"]


def test_list_sessions_respects_limit(mgr: SessionManager) -> None:
    for _ in range(5):
        mgr.start_session()
        mgr.close_session()
    sessions = mgr.list_sessions(limit=3)
    assert len(sessions) == 3


# ---------------------------------------------------------------------------
# compare_sessions
# ---------------------------------------------------------------------------


def test_compare_sessions_deltas(mgr: SessionManager) -> None:
    sa = mgr.start_session()
    mgr.record_iteration()
    mgr.record_iteration()
    mgr.record_pr_merged(10)
    mgr.record_discussion_completed(5)
    mgr.close_session()

    sb = mgr.start_session()
    mgr.record_iteration()
    mgr.close_session()

    result = mgr.compare_sessions(sa["session_id"], sb["session_id"])
    assert result["delta"]["iterations"] == 1   # 2 - 1
    assert result["delta"]["prs"] == 1          # 1 - 0
    assert result["delta"]["discussions"] == 1  # 1 - 0
    assert result["a"]["session_id"] == sa["session_id"]
    assert result["b"]["session_id"] == sb["session_id"]


def test_compare_sessions_missing_id_raises(mgr: SessionManager) -> None:
    mgr.start_session()
    mgr.close_session()
    s = mgr.list_sessions()[0]
    with pytest.raises(ValueError, match="not found"):
        mgr.compare_sessions(s["session_id"], "nonexistent-id")


# ---------------------------------------------------------------------------
# Atomic writes — concurrent access does not corrupt
# ---------------------------------------------------------------------------


def test_atomic_writes_no_corruption(mgr: SessionManager) -> None:
    """Multiple threads incrementing iteration_count should not corrupt the file."""
    mgr.start_session()

    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(10):
                mgr.record_iteration()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # File must still be valid JSON.
    s = mgr.current_session()
    assert isinstance(s["iteration_count"], int)
