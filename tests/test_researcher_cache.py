"""tests/test_researcher_cache.py — unit tests for backend/researcher.py"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Ensure backend/ is importable when running from repo root
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point STATE_DIR at a temp dir so tests never touch real state."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    # Re-import to pick up the patched env var
    import importlib
    import backend.state_paths as sp
    importlib.reload(sp)
    import backend.researcher as r
    importlib.reload(r)
    yield r


def test_purge_no_args_exits_zero():
    """python3 backend/researcher.py purge — should work without a URL arg."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "researcher.py"), "purge"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"purge failed: {result.stderr}"
    assert "Purged" in result.stdout


def test_purge_removes_expired(isolated_cache, tmp_path):
    """purge_expired() removes entries older than TTL and returns correct count."""
    r = isolated_cache
    # Write 2 entries: one fresh, one expired
    cache_file = Path(tmp_path) / "researcher-cache.json"
    old_ts = time.time() - 1000  # well past 15-min TTL
    fresh_ts = time.time()
    cache = {
        "aaa": {"url": "http://old.example", "body": "old", "stored_at": old_ts},
        "bbb": {"url": "http://fresh.example", "body": "fresh", "stored_at": fresh_ts},
    }
    cache_file.write_text(json.dumps(cache))

    # D#1810: STATE_DIR resolves at call time now, so the fixture's
    # AUTONOMOUS_TEAM_STATE_DIR is already enough — no reload needed, and no
    # need to assign backend.state_paths.STATE_DIR directly (that would set
    # a real module attribute that shadows __getattr__ for the rest of the
    # pytest session, since nothing resets it afterward).
    removed = r.purge_expired()
    assert removed == 1

    remaining = json.loads(cache_file.read_text())
    assert "aaa" not in remaining
    assert "bbb" in remaining


def test_set_and_get_round_trip(isolated_cache):
    """set_cache / get returns the body within TTL."""
    r = isolated_cache
    r.set_cache("http://example.com/page", "hello world")
    body = r.get("http://example.com/page")
    assert body == "hello world"


def test_get_cache_miss_returns_none(isolated_cache):
    """get() returns None for unknown URL."""
    r = isolated_cache
    assert r.get("http://not-cached.example") is None


def test_get_returns_none_after_ttl(isolated_cache, monkeypatch):
    """get() returns None after entry exceeds TTL."""
    r = isolated_cache
    r.set_cache("http://ttl-test.example", "data")

    # Fast-forward time past TTL
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 1000)

    assert r.get("http://ttl-test.example") is None


def test_no_args_shows_usage():
    """python3 backend/researcher.py with no subcommand exits nonzero."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "researcher.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Usage" in result.stdout


def test_get_cache_miss_exits_nonzero():
    """CLI: cache miss exits 1 so callers know to WebFetch."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "researcher.py"),
         "get", "http://not-in-cache.example"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
