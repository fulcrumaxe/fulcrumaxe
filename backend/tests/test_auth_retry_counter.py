"""Tests for backend/rpc/auth_retry_counter.py

Run with:
    python -m pytest backend/tests/test_auth_retry_counter.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helper: in-memory blackboard backed by a simple dict
# ---------------------------------------------------------------------------

class _InMemoryBlackboard:
    """Minimal in-memory blackboard for tests (no filesystem / SQLite)."""

    def __init__(self) -> None:
        self._store: dict = {}

    def read(self, key: str) -> object:
        return self._store.get(key)

    def write(self, key: str, value: object, updated_by: str = "test") -> bool:
        self._store[key] = value
        return True


@pytest.fixture(autouse=True)
def fake_bb():
    """Patch get_blackboard to return a fresh in-memory instance for each test."""
    bb = _InMemoryBlackboard()
    with patch("backend.rpc.auth_retry_counter.get_blackboard", return_value=bb):
        yield bb


# ---------------------------------------------------------------------------
# Tests for handle_record
# ---------------------------------------------------------------------------

def test_record_increments_total_count(fake_bb):
    from backend.rpc.auth_retry_counter import handle_record

    result1 = handle_record({})
    assert result1["recorded"] is True
    assert result1["count_total"] == 1

    result2 = handle_record({})
    assert result2["recorded"] is True
    assert result2["count_total"] == 2


def test_record_appends_timestamp_to_list(fake_bb):
    from backend.rpc.auth_retry_counter import handle_record, _TS_KEY

    handle_record({})
    handle_record({})

    timestamps = fake_bb.read(_TS_KEY)
    assert isinstance(timestamps, list)
    assert len(timestamps) == 2
    # Each timestamp should be an ISO8601 string
    assert "T" in timestamps[0]


def test_record_returns_false_on_blackboard_error():
    """handle_record failing does not raise — returns recorded: False."""
    from backend.rpc.auth_retry_counter import handle_record

    def _exploding_bb():
        raise RuntimeError("DB unavailable")

    with patch("backend.rpc.auth_retry_counter.get_blackboard", side_effect=_exploding_bb):
        result = handle_record({})

    assert result["recorded"] is False


# ---------------------------------------------------------------------------
# Tests for handle_summary
# ---------------------------------------------------------------------------

def test_summary_fresh_db_returns_zeros(fake_bb):
    """auth_retry.summary on a fresh DB returns count_total=0, count_24h=0."""
    from backend.rpc.auth_retry_counter import handle_summary

    result = handle_summary({})

    assert result["count_24h"] == 0
    assert result["count_total"] == 0
    assert result["last_seen"] is None


def test_summary_reflects_recorded_events(fake_bb):
    from backend.rpc.auth_retry_counter import handle_record, handle_summary

    handle_record({})
    handle_record({})

    result = handle_summary({})

    assert result["count_total"] == 2
    assert result["count_24h"] == 2
    assert result["last_seen"] is not None


def test_summary_excludes_old_timestamps(fake_bb):
    """Timestamps older than 24h are not counted in count_24h."""
    import datetime
    from backend.rpc.auth_retry_counter import handle_record, handle_summary, _TS_KEY, _TOTAL_KEY

    # Pre-seed the blackboard with one old timestamp and one recent
    old_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
    ).isoformat()
    recent_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    fake_bb.write(_TS_KEY, [old_ts, recent_ts])
    fake_bb.write(_TOTAL_KEY, 2)

    result = handle_summary({})

    # Only the recent timestamp falls within the 24h window
    assert result["count_24h"] == 1
    assert result["count_total"] == 2
    assert result["last_seen"] == recent_ts
