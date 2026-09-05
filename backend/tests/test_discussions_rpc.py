"""
test_discussions_rpc.py — Unit tests for discussions.list and discussions.get
JSON-RPC methods in backend/server.py, plus backend/discussion_status.py helpers.
"""
from __future__ import annotations

import importlib
import json
import time
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# discussion_status helpers
# ---------------------------------------------------------------------------

from backend.discussion_status import (
    extract_linked_pr,
    extract_since,
    extract_status,
)


def test_extract_status_spec_ready():
    body = "<!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->"
    assert extract_status(body) == "SPEC_READY"


def test_extract_status_implementing_with_pr():
    body = "<!-- STATUS:IMPLEMENTING PR:#321 SINCE:2026-05-09T01:00:00Z -->"
    assert extract_status(body) == "IMPLEMENTING"


def test_extract_status_unknown():
    assert extract_status("no status here") == "UNKNOWN"
    assert extract_status("") == "UNKNOWN"


def test_extract_linked_pr():
    body = "<!-- STATUS:REVIEWING PR:#42 SINCE:2026-05-09T02:00:00Z -->"
    assert extract_linked_pr(body) == 42


def test_extract_linked_pr_none():
    body = "<!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->"
    assert extract_linked_pr(body) is None


def test_extract_since():
    body = "<!-- STATUS:DONE PR:#99 SINCE:2026-05-09T03:00:00Z -->"
    assert extract_since(body) == "2026-05-09T03:00:00Z"


# ---------------------------------------------------------------------------
# Cache helpers (imported directly from server module)
# ---------------------------------------------------------------------------

def _get_rpc_internals():
    """Import server and return the cache dict + helpers."""
    import backend.server as srv
    return (
        srv._DISCUSSIONS_CACHE,
        srv._discussions_cache_get,
        srv._discussions_cache_set,
        srv._DISCUSSIONS_CACHE_TTL,
    )


def test_cache_set_and_get():
    cache, get_fn, set_fn, ttl = _get_rpc_internals()
    key = ("test", "value")
    set_fn(key, {"hello": "world"})
    result = get_fn(key)
    assert result == {"hello": "world"}


def test_cache_expires():
    cache, get_fn, set_fn, ttl = _get_rpc_internals()
    key = ("expiry_test", "x")
    set_fn(key, "data")
    # Manually backdate the entry
    cache[key] = (time.time() - ttl - 1, "data")
    assert get_fn(key) is None


def test_cache_miss():
    _, get_fn, _, _ = _get_rpc_internals()
    result = get_fn(("nonexistent_key_xyz", "abc"))
    assert result is None


# ---------------------------------------------------------------------------
# discussions.list RPC (mocked gh subprocess)
# ---------------------------------------------------------------------------

FIXTURE_LIST_RESPONSE = {
    "data": {
        "repository": {
            "discussions": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "number": 363,
                        "title": "Discussion Explorer",
                        "body": "<!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->",
                        "url": "https://github.com/autonomous-agent-7/autonomous-forever/discussions/363",
                        "createdAt": "2026-05-01T00:00:00Z",
                        "updatedAt": "2026-05-09T00:00:00Z",
                        "category": {"name": "Ideas"},
                        "author": {"login": "example-owner"},
                    },
                    {
                        "number": 200,
                        "title": "Some other discussion",
                        "body": "<!-- STATUS:DONE PR:#100 SINCE:2026-04-01T00:00:00Z -->",
                        "url": "https://github.com/autonomous-agent-7/autonomous-forever/discussions/200",
                        "createdAt": "2026-04-01T00:00:00Z",
                        "updatedAt": "2026-04-01T00:00:00Z",
                        "category": {"name": "Ideas"},
                        "author": {"login": "agent"},
                    },
                ],
            }
        }
    }
}


def _mock_gh_graphql(response: dict):
    """Return a context manager that mocks _gh_graphql in server.py."""
    import backend.server as srv
    return patch.object(srv, "_gh_graphql", return_value=response)


def test_discussions_list_returns_items():
    import backend.server as srv
    # Clear cache to avoid state leakage
    srv._DISCUSSIONS_CACHE.clear()

    with _mock_gh_graphql(FIXTURE_LIST_RESPONSE):
        result = srv._rpc_discussions_list.__wrapped__({"status": "*", "limit": 50}) if hasattr(
            srv._rpc_discussions_list, "__wrapped__"
        ) else srv._RPC_METHODS["discussions.list"]({"status": "*", "limit": 50})

    assert "items" in result
    assert len(result["items"]) == 2
    numbers = [i["number"] for i in result["items"]]
    assert 363 in numbers
    assert 200 in numbers


def test_discussions_list_status_filter():
    import backend.server as srv
    srv._DISCUSSIONS_CACHE.clear()

    with _mock_gh_graphql(FIXTURE_LIST_RESPONSE):
        result = srv._RPC_METHODS["discussions.list"]({"status": "SPEC_READY", "limit": 50})

    assert len(result["items"]) == 1
    assert result["items"][0]["number"] == 363


def test_discussions_list_cached():
    import backend.server as srv
    srv._DISCUSSIONS_CACHE.clear()

    call_count = 0

    def mock_graphql(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return FIXTURE_LIST_RESPONSE

    with patch.object(srv, "_gh_graphql", side_effect=mock_graphql):
        srv._RPC_METHODS["discussions.list"]({"status": "*"})
        srv._RPC_METHODS["discussions.list"]({"status": "*"})

    # Second call should be served from cache — only 1 upstream call
    assert call_count == 1


# ---------------------------------------------------------------------------
# discussions.get RPC
# ---------------------------------------------------------------------------

FIXTURE_GET_RESPONSE = {
    "data": {
        "repository": {
            "discussion": {
                "number": 363,
                "title": "Discussion Explorer",
                "body": "<!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->\n\n## Summary\n\nAdd a new `/discussions` page.",
                "url": "https://github.com/autonomous-agent-7/autonomous-forever/discussions/363",
                "createdAt": "2026-05-01T00:00:00Z",
                "updatedAt": "2026-05-09T00:00:00Z",
                "author": {"login": "example-owner"},
                "category": {"name": "Ideas"},
                "comments": {
                    "nodes": [
                        {
                            "body": "LGTM!",
                            "createdAt": "2026-05-09T01:00:00Z",
                            "author": {"login": "reviewer"},
                        }
                    ]
                },
            }
        }
    }
}


def test_discussions_get_returns_detail():
    import backend.server as srv
    srv._DISCUSSIONS_CACHE.clear()

    with _mock_gh_graphql(FIXTURE_GET_RESPONSE):
        result = srv._RPC_METHODS["discussions.get"]({"number": 363})

    assert result["discussion"]["number"] == 363
    assert result["discussion"]["status"] == "SPEC_READY"
    assert len(result["comments"]) == 1
    assert result["comments"][0]["body"] == "LGTM!"
    assert result["linked_pr"] is None  # no PR in STATUS line
    assert isinstance(result["agent_runs"], list)


def test_discussions_get_invalid_number():
    import backend.server as srv

    with pytest.raises((ValueError, Exception)):
        srv._RPC_METHODS["discussions.get"]({"number": 0})


def test_discussions_get_cached():
    import backend.server as srv
    srv._DISCUSSIONS_CACHE.clear()

    call_count = 0

    def mock_graphql(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return FIXTURE_GET_RESPONSE

    with patch.object(srv, "_gh_graphql", side_effect=mock_graphql):
        srv._RPC_METHODS["discussions.get"]({"number": 363})
        srv._RPC_METHODS["discussions.get"]({"number": 363})

    assert call_count == 1
