"""
Tests for backend/discussion_cache.py — the fresh-read path (D#1778).

The spawn gate was reading Discussion bodies through a 300s-TTL cache with no way
to force a live read, so a PM's just-written STATUS was invisible to the very next
spawn for up to five minutes. These tests cover the fix: an explicit `fresh=True`
opt-in that bypasses the TTL, while the default (cached) path and the
GraphQL-failure stale-fallback safety net both keep their existing behaviour.

Run with:
    python3 -m pytest backend/tests/test_discussion_cache.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend import discussion_cache as dc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(monkeypatch, tmp_path: Path):
    """Point the module's DB path at a scratch file so tests never touch real state.

    D#1810 round 3: _DB_PATH was converted from a frozen module constant to a
    call-time accessor function, _db_path() — patch the function itself, not
    a "_DB_PATH" name that no longer exists on this module (discussion_cache.py
    has no __getattr__ shim re-exposing it, unlike backend/db.py).
    """
    scratch_db = tmp_path / "discussion_cache.db"
    monkeypatch.setattr(dc, "_db_path", lambda: scratch_db)
    return tmp_path


def _seed_row(number: int, body: str, cached_at: str) -> None:
    """Write a cache row directly, bypassing _fetch_one, with an explicit cached_at."""
    with dc._conn() as con:
        con.execute(
            "INSERT INTO discussion_cache(number, body, title, labels, updated_at, cached_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(number) DO UPDATE SET "
            "  body=excluded.body, cached_at=excluded.cached_at",
            (number, body, "", "[]", "", cached_at),
        )


def _fetch_counter(monkeypatch, record_or_none):
    """Patch _fetch_one to return a fixed value and count how many times it's called."""
    calls = {"n": 0}

    def _fake(number: int):
        calls["n"] += 1
        return record_or_none

    monkeypatch.setattr(dc, "_fetch_one", _fake)
    return calls


# ---------------------------------------------------------------------------
# Part 1 — a fresh read exists
# ---------------------------------------------------------------------------


def test_fresh_bypasses_ttl_and_refetches(isolated_db, monkeypatch):
    """AC1: a row cached 1s ago is served from cache normally, but re-fetched when fresh=True."""
    _seed_row(1753, "OLD BODY", dc._now_iso())

    fresh_record = {
        "number": 1753,
        "title": "t",
        "body": "NEW BODY",
        "labels": [],
        "updated_at": "",
    }
    calls = _fetch_counter(monkeypatch, fresh_record)

    # Normal read: TTL is fresh (cached 1s ago), must NOT fetch.
    body = dc.get_body(1753)
    assert body == "OLD BODY"
    assert calls["n"] == 0

    # Fresh read: must bypass the TTL and fetch, returning the new value.
    body = dc.get_body(1753, fresh=True)
    assert body == "NEW BODY"
    assert calls["n"] == 1


def test_default_read_path_unchanged(isolated_db, monkeypatch):
    """AC2: get_body(number) with a fresh cached row never fetches — bulk paths keep their cache."""
    _seed_row(1753, "CACHED BODY", dc._now_iso())
    calls = _fetch_counter(monkeypatch, {"number": 1753, "title": "", "body": "SHOULD NOT SEE THIS", "labels": [], "updated_at": ""})

    for _ in range(3):
        body = dc.get_body(1753)
        assert body == "CACHED BODY"

    assert calls["n"] == 0


def test_stale_fallback_survives_fresh_read(isolated_db, monkeypatch, capsys):
    """AC3: fresh=True with a failing fetch must return the stale cached body and warn,
    never raise and never silently return ""."""
    _seed_row(1753, "STALE BODY", "2020-01-01T00:00:00Z")  # long past the 300s TTL
    _fetch_counter(monkeypatch, None)  # simulate GraphQL failure

    body = dc.get_body(1753, fresh=True)

    assert body == "STALE BODY"
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "#1753" in captured.err


def test_stale_fallback_status_is_distinguishable(isolated_db, monkeypatch):
    """get_body_status must let callers tell a stale fallback apart from a genuine fresh read."""
    _seed_row(1753, "STALE BODY", "2020-01-01T00:00:00Z")
    _fetch_counter(monkeypatch, None)

    body, status = dc.get_body_status(1753, fresh=True)
    assert body == "STALE BODY"
    assert status == "stale_fallback"

    _fetch_counter(monkeypatch, {"number": 1753, "title": "", "body": "LIVE BODY", "labels": [], "updated_at": ""})
    body, status = dc.get_body_status(1753, fresh=True)
    assert body == "LIVE BODY"
    assert status == "fetched"


def test_fresh_with_no_cache_and_failed_fetch_is_empty(isolated_db, monkeypatch):
    """No cached row and a failing fetch: get_body returns "" and status is "empty",
    never a crash — this is the case spawn-agent.sh already handles as
    "cannot read Discussion body"."""
    _fetch_counter(monkeypatch, None)

    body, status = dc.get_body_status(9999, fresh=True)
    assert body == ""
    assert status == "empty"
