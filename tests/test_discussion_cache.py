"""Unit tests for backend/discussion_cache.py

Covers:
- TTL freshness (hit returns cached, stale triggers fetch)
- invalidate forces a fresh fetch
- missing key fetches exactly once
- GraphQL failure falls back to stale cache
- stats module hit_ratio
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure backend is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def dc(tmp_path, monkeypatch):
    """Fresh discussion_cache module with isolated SQLite db per test.

    D#1810: state_paths resolves STATE_DIR at call time now, so setting
    AUTONOMOUS_TEAM_STATE_DIR is sufficient — no module reload or private
    attribute patching needed to get isolation.
    """
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

    import backend.discussion_cache as _dc
    yield _dc


def _fake_record(number: int = 42, body: str = "test body") -> dict:
    return {
        "number": number,
        "title": "Test Discussion",
        "body": body,
        "labels": ["SPEC_READY"],
        "updated_at": "2026-05-13T00:00:00Z",
    }


def _seed(dc, number: int, body: str, cached_at: str = "2000-01-01T00:00:00Z") -> None:
    """Insert a row directly into the cache db (bypassing TTL logic)."""
    with dc._conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO discussion_cache"
            "(number, body, title, labels, updated_at, cached_at) "
            "VALUES(?,?,?,?,?,?)",
            (number, body, "T", "[]", "", cached_at),
        )


# ---------------------------------------------------------------------------
# TTL freshness
# ---------------------------------------------------------------------------

def test_cache_hit_within_ttl(dc):
    fetch_calls = []

    def fake_fetch(n):
        fetch_calls.append(n)
        return _fake_record(n, "fetched body")

    with patch.object(dc, "_fetch_one", side_effect=fake_fetch):
        body1 = dc.get_body(42)
        assert body1 == "fetched body"
        assert len(fetch_calls) == 1

        # Second call within TTL — cache hit, no new fetch
        body2 = dc.get_body(42)
        assert body2 == "fetched body"
        assert len(fetch_calls) == 1


def test_cache_miss_when_stale(dc):
    fetch_calls = []

    def fake_fetch(n):
        fetch_calls.append(n)
        return _fake_record(n, f"body-v{len(fetch_calls)}")

    with patch.object(dc, "_fetch_one", side_effect=fake_fetch):
        dc.get_body(42)
        assert len(fetch_calls) == 1

        # Expire the cache entry
        with dc._conn() as con:
            con.execute(
                "UPDATE discussion_cache SET cached_at = '2000-01-01T00:00:00Z' WHERE number = 42"
            )

        dc.get_body(42)
        assert len(fetch_calls) == 2  # stale → re-fetch


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------

def test_invalidate_forces_fetch(dc):
    fetch_calls = []

    def fake_fetch(n):
        fetch_calls.append(n)
        return _fake_record(n, "original")

    with patch.object(dc, "_fetch_one", side_effect=fake_fetch):
        dc.get_body(42)
        assert len(fetch_calls) == 1

        dc.invalidate(42)
        dc.get_body(42)
        assert len(fetch_calls) == 2


# ---------------------------------------------------------------------------
# Missing key fetches exactly once
# ---------------------------------------------------------------------------

def test_missing_key_fetches_once(dc):
    fetch_calls = []

    def fake_fetch(n):
        fetch_calls.append(n)
        return _fake_record(n, "new body")

    with patch.object(dc, "_fetch_one", side_effect=fake_fetch):
        result = dc.get_body(99)

    assert result == "new body"
    assert len(fetch_calls) == 1


def test_missing_key_graphql_fail_returns_empty(dc):
    with patch.object(dc, "_fetch_one", return_value=None):
        result = dc.get_body(99)

    assert result == ""  # nothing in cache, nothing from API


# ---------------------------------------------------------------------------
# GraphQL failure — stale fallback
# ---------------------------------------------------------------------------

def test_graphql_fail_returns_stale(dc):
    _seed(dc, 42, "stale body", cached_at="2000-01-01T00:00:00Z")

    with patch.object(dc, "_fetch_one", return_value=None):
        body = dc.get_body(42)

    assert body == "stale body"


# ---------------------------------------------------------------------------
# list_open
# ---------------------------------------------------------------------------

def test_list_open_caches_all_records(dc):
    records = [_fake_record(1, "body1"), _fake_record(2, "body2")]

    with patch.object(dc, "_fetch_all_open", return_value=records):
        result = dc.list_open()

    assert len(result) == 2
    assert {r["number"] for r in result} == {1, 2}

    # Confirm DB was populated
    with dc._conn() as con:
        rows = con.execute("SELECT number FROM discussion_cache ORDER BY number").fetchall()
    assert [r[0] for r in rows] == [1, 2]


def test_list_open_fallback_on_fail(dc):
    _seed(dc, 5, "old body")

    with patch.object(dc, "_fetch_all_open", return_value=None):
        result = dc.list_open()

    assert len(result) == 1
    assert result[0]["body"] == "old body"


# ---------------------------------------------------------------------------
# Stats / hit_ratio
# ---------------------------------------------------------------------------

def test_hit_ratio_empty(dc):
    stats = dc.get_stats()
    assert stats["hit_ratio"] == 0.0
    assert stats["total"] == 0


def test_hit_ratio_after_hits_and_misses(dc):
    fetch_calls = []

    def fake_fetch(n):
        fetch_calls.append(n)
        return _fake_record(n)

    with patch.object(dc, "_fetch_one", side_effect=fake_fetch):
        # 1 miss (fetch), then 3 hits
        dc.get_body(10)
        dc.get_body(10)
        dc.get_body(10)
        dc.get_body(10)

    stats = dc.get_stats()
    assert stats["hits"] == 3
    assert stats["misses"] == 1
    assert stats["total"] == 4
    assert abs(stats["hit_ratio"] - 0.75) < 0.001


def test_stats_module_hit_ratio(dc):
    # Generate some hits via the cache module
    with patch.object(dc, "_fetch_one", return_value=_fake_record(1)):
        dc.get_body(1)
        dc.get_body(1)  # hit

    # stats module delegates to discussion_cache.get_stats — verify shape
    result = dc.get_stats()
    assert "hit_ratio" in result
    assert result["hits"] >= 1
    assert result["total"] >= 2


# ---------------------------------------------------------------------------
# Repo resolution — backend._repo._load_repo() resolution order
# ---------------------------------------------------------------------------

def test_load_repo_reads_project_json(tmp_path, monkeypatch):
    """State-dir project.json takes precedence over the hardcoded fallback."""
    # Write a project.json into a fake state dir
    project_json = tmp_path / "project.json"
    project_json.write_text(json.dumps({"repo": "test/proj", "project_name": "test"}))

    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMOUS_TEAM_REPO", raising=False)

    import backend._repo as repo_mod
    importlib.reload(repo_mod)

    repo = repo_mod._load_repo()
    assert repo == "test/proj", f"Expected 'test/proj', got {repo!r}"
    assert repo != "autonomous-agent-7/autonomous-forever"


def test_load_repo_falls_back_to_env(tmp_path, monkeypatch):
    """AUTONOMOUS_TEAM_REPO env var takes highest priority over project.json."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_REPO", "env/repo")
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))  # empty dir, no project.json

    import backend._repo as repo_mod
    importlib.reload(repo_mod)

    repo = repo_mod._load_repo()
    assert repo == "env/repo"


def test_load_repo_fallback_default(tmp_path, monkeypatch):
    """When no env var and no state-dir project.json, _load_repo falls through
    to the repo-root .autonomous-team/project.json this repo commits — not a
    hard-coded slug (see backend/_repo.py's module docstring, D#1870)."""
    monkeypatch.delenv("AUTONOMOUS_TEAM_REPO", raising=False)
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))  # empty dir, no project.json

    import backend._repo as repo_mod
    importlib.reload(repo_mod)

    repo = repo_mod._load_repo()
    assert repo == "autonomous-agent-7/fulcrumaxe"
