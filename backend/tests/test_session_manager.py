"""Tests for backend.session_manager."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.session_manager import SessionManager, SqliteSessionManager


# ---------------------------------------------------------------------------
# SessionManager (file-based) — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sm(tmp_path: Path) -> SessionManager:
    return SessionManager(sessions_dir=tmp_path / "sessions")


# ---------------------------------------------------------------------------
# SessionManager — lifecycle
# ---------------------------------------------------------------------------


def test_start_session_returns_dict(sm):
    s = sm.start_session()
    assert "session_id" in s
    assert s["ended_at"] is None
    assert s["iteration_count"] == 0
    assert s["prs_merged"] == []
    assert s["discussions_completed"] == []


def test_start_session_closes_previous(sm):
    s1 = sm.start_session()
    _s2 = sm.start_session()
    # original session must now be closed
    refreshed = sm.get_session(s1["session_id"])
    assert refreshed is not None
    assert refreshed["ended_at"] is not None


def test_current_session_returns_open_session(sm):
    s = sm.start_session()
    current = sm.current_session()
    assert current is not None
    assert current["session_id"] == s["session_id"]


def test_current_session_none_when_no_session(sm):
    assert sm.current_session() is None


def test_record_iteration(sm):
    sm.start_session()
    sm.record_iteration()
    sm.record_iteration()
    current = sm.current_session()
    assert current["iteration_count"] == 2


def test_record_pr_merged(sm):
    sm.start_session()
    sm.record_pr_merged(42)
    sm.record_pr_merged(43)
    current = sm.current_session()
    assert 42 in current["prs_merged"]
    assert 43 in current["prs_merged"]


def test_record_pr_merged_no_duplicates(sm):
    sm.start_session()
    sm.record_pr_merged(99)
    sm.record_pr_merged(99)
    current = sm.current_session()
    assert current["prs_merged"].count(99) == 1


def test_record_discussion_completed(sm):
    sm.start_session()
    sm.record_discussion_completed(7)
    current = sm.current_session()
    assert 7 in current["discussions_completed"]


def test_record_discussion_completed_no_duplicates(sm):
    sm.start_session()
    sm.record_discussion_completed(7)
    sm.record_discussion_completed(7)
    current = sm.current_session()
    assert current["discussions_completed"].count(7) == 1


def test_close_session(sm):
    s = sm.start_session()
    sm.close_session()
    refreshed = sm.get_session(s["session_id"])
    assert refreshed["ended_at"] is not None


def test_close_session_no_active_is_noop(sm):
    result = sm.close_session()
    assert result is None


def test_get_session_valid(sm):
    s = sm.start_session()
    retrieved = sm.get_session(s["session_id"])
    assert retrieved is not None
    assert retrieved["session_id"] == s["session_id"]


def test_get_session_invalid(sm):
    assert sm.get_session("nonexistent-uuid") is None


def test_list_sessions_empty(sm):
    assert sm.list_sessions() == []


def test_list_sessions_single(sm):
    sm.start_session()
    sessions = sm.list_sessions()
    assert len(sessions) == 1


def test_list_sessions_multiple_sorted_newest_first(sm):
    s1 = sm.start_session()
    sm.close_session()
    time.sleep(0.05)  # ensure distinct started_at timestamps
    s2 = sm.start_session()
    sm.close_session()
    sessions = sm.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == s2["session_id"]
    assert sessions[1]["session_id"] == s1["session_id"]


def test_compare_sessions(sm):
    s1 = sm.start_session()
    sm.record_iteration()
    sm.record_pr_merged(1)
    sm.close_session()

    s2 = sm.start_session()
    sm.close_session()

    result = sm.compare_sessions(s1["session_id"], s2["session_id"])
    assert "a" in result
    assert "b" in result
    assert "delta" in result
    assert result["delta"]["iterations"] == 1
    assert result["delta"]["prs"] == 1


def test_compare_sessions_invalid_id_raises(sm):
    s = sm.start_session()
    with pytest.raises(ValueError):
        sm.compare_sessions(s["session_id"], "nonexistent")


# ---------------------------------------------------------------------------
# SqliteSessionManager — fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path):
    """Create an in-process Database backed by a tmp file."""
    from backend.db import Database
    return Database(db_path=tmp_path / "test-state.db")


@pytest.fixture()
def sqlite_sm(tmp_path: Path) -> SqliteSessionManager:
    db = _make_db(tmp_path)
    return SqliteSessionManager(db=db)


# ---------------------------------------------------------------------------
# SqliteSessionManager — lifecycle mirrors SessionManager
# ---------------------------------------------------------------------------


def test_sqlite_start_session_returns_dict(sqlite_sm):
    s = sqlite_sm.start_session()
    assert "session_id" in s
    assert s["ended_at"] is None
    assert s["iteration_count"] == 0


def test_sqlite_current_session_none_initially(sqlite_sm):
    assert sqlite_sm.current_session() is None


def test_sqlite_current_session_after_start(sqlite_sm):
    s = sqlite_sm.start_session()
    current = sqlite_sm.current_session()
    assert current is not None
    assert current["session_id"] == s["session_id"]


def test_sqlite_record_iteration(sqlite_sm):
    sqlite_sm.start_session()
    sqlite_sm.record_iteration()
    sqlite_sm.record_iteration()
    assert sqlite_sm.current_session()["iteration_count"] == 2


def test_sqlite_record_pr_merged(sqlite_sm):
    sqlite_sm.start_session()
    sqlite_sm.record_pr_merged(10)
    assert 10 in sqlite_sm.current_session()["prs_merged"]


def test_sqlite_record_discussion_completed(sqlite_sm):
    sqlite_sm.start_session()
    sqlite_sm.record_discussion_completed(5)
    assert 5 in sqlite_sm.current_session()["discussions_completed"]


def test_sqlite_close_session(sqlite_sm):
    s = sqlite_sm.start_session()
    sqlite_sm.close_session()
    retrieved = sqlite_sm.get_session(s["session_id"])
    assert retrieved["ended_at"] is not None


def test_sqlite_get_session_invalid(sqlite_sm):
    assert sqlite_sm.get_session("no-such-id") is None


def test_sqlite_list_sessions(sqlite_sm):
    assert sqlite_sm.list_sessions() == []
    sqlite_sm.start_session()
    assert len(sqlite_sm.list_sessions()) == 1


def test_sqlite_compare_sessions(sqlite_sm):
    s1 = sqlite_sm.start_session()
    sqlite_sm.record_pr_merged(1)
    sqlite_sm.close_session()

    s2 = sqlite_sm.start_session()
    sqlite_sm.close_session()

    result = sqlite_sm.compare_sessions(s1["session_id"], s2["session_id"])
    assert result["delta"]["prs"] == 1


def test_sqlite_persistence_across_instances(tmp_path):
    """Session written by one instance is readable by a fresh instance."""
    from backend.db import Database
    db1 = Database(db_path=tmp_path / "shared.db")
    sm1 = SqliteSessionManager(db=db1)
    s = sm1.start_session()
    session_id = s["session_id"]
    db1.close()

    db2 = Database(db_path=tmp_path / "shared.db")
    sm2 = SqliteSessionManager(db=db2)
    retrieved = sm2.get_session(session_id)
    assert retrieved is not None
    assert retrieved["session_id"] == session_id


# ---------------------------------------------------------------------------
# get_session_manager factory
# ---------------------------------------------------------------------------


def test_get_session_manager_returns_sqlite_when_db_exists(tmp_path):
    # get_session_manager does `from backend.db import state_db_exists` locally,
    # so we patch it in the backend.db module namespace.
    import backend.db as db_mod
    with patch.object(db_mod, "state_db_exists", return_value=True):
        import backend.session_manager as sm_mod
        result = sm_mod.get_session_manager()
        assert isinstance(result, SqliteSessionManager)


def test_get_session_manager_returns_file_based_when_no_db(tmp_path):
    import backend.db as db_mod
    with patch.object(db_mod, "state_db_exists", return_value=False):
        import backend.session_manager as sm_mod
        result = sm_mod.get_session_manager()
        assert isinstance(result, SessionManager)
