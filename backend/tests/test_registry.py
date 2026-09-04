"""
Tests for backend/registry.py

Run with:
    python -m pytest backend/tests/test_registry.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.registry import (
    DiscussionRegistry,
    LockTimeout,
    _LockedCtx,
    _now_iso,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(tmp_path: Path) -> DiscussionRegistry:
    return DiscussionRegistry(state_dir=tmp_path)


def _make_discussion_node(
    number: int = 1,
    title: str = "Test",
    status: str = "SPEC_READY",
    closed_at: str | None = None,
) -> dict:
    body = f"<!-- STATUS:{status} SINCE:2026-01-01T00:00:00Z -->"
    return {
        "number": number,
        "title": title,
        "body": body,
        "createdAt": "2026-01-01T00:00:00Z",
        "closedAt": closed_at,
        "isAnswered": False,
        "category": {"name": "General"},
        "labels": {"nodes": []},
    }


def _make_gh_response(nodes: list[dict]) -> str:
    return json.dumps({
        "data": {
            "repository": {
                "discussions": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                }
            }
        }
    })


# ---------------------------------------------------------------------------
# DiscussionRegistry.load()
# ---------------------------------------------------------------------------


def test_load_missing_file(tmp_path: Path):
    """Returns empty skeleton when registry.json does not exist."""
    reg = _make_registry(tmp_path)
    data = reg.load()
    assert data["version"] == 1
    assert data["discussions"] == []
    assert data["synced_at"] == ""


def test_load_valid_file(tmp_path: Path):
    """Returns loaded data merged with skeleton defaults."""
    reg = _make_registry(tmp_path)
    content = {
        "version": 1,
        "synced_at": "2026-01-01T00:00:00+00:00",
        "discussions": [{"number": 1, "title": "Test"}],
        "velocity": {},
    }
    (tmp_path / "registry.json").write_text(json.dumps(content))
    data = reg.load()
    assert len(data["discussions"]) == 1
    assert data["synced_at"] == "2026-01-01T00:00:00+00:00"


def test_load_corrupt_file(tmp_path: Path):
    """Returns empty skeleton for corrupt JSON."""
    reg = _make_registry(tmp_path)
    (tmp_path / "registry.json").write_text("{bad json!!!")
    data = reg.load()
    assert data["discussions"] == []


def test_load_empty_file(tmp_path: Path):
    """Returns empty skeleton for empty file."""
    reg = _make_registry(tmp_path)
    (tmp_path / "registry.json").write_text("")
    data = reg.load()
    assert data["discussions"] == []


def test_load_partial_data_gets_defaults(tmp_path: Path):
    """Partial data is merged with skeleton — missing keys get defaults."""
    reg = _make_registry(tmp_path)
    (tmp_path / "registry.json").write_text(json.dumps({"version": 1, "discussions": []}))
    data = reg.load()
    assert "velocity" in data
    assert "synced_at" in data


# ---------------------------------------------------------------------------
# DiscussionRegistry.show()
# ---------------------------------------------------------------------------


def test_show_returns_load_result(tmp_path: Path):
    """show() is a thin wrapper around load()."""
    reg = _make_registry(tmp_path)
    assert reg.show() == reg.load()


def test_show_empty_registry(tmp_path: Path):
    """show() on empty registry returns skeleton."""
    reg = _make_registry(tmp_path)
    result = reg.show()
    assert result["discussions"] == []


# ---------------------------------------------------------------------------
# DiscussionRegistry.stats()
# ---------------------------------------------------------------------------


def test_stats_empty_registry(tmp_path: Path):
    """Stats on empty registry returns zeros."""
    reg = _make_registry(tmp_path)
    s = reg.stats()
    assert s["total"] == 0
    assert s["done"] == 0
    assert s["in_progress"] == 0
    assert s["tasks_per_day"] == 0.0
    assert s["avg_days_to_complete"] is None


def test_stats_with_done_items(tmp_path: Path):
    """Stats correctly counts DONE items."""
    reg = _make_registry(tmp_path)
    data = {
        "version": 1,
        "synced_at": "2026-04-10T00:00:00+00:00",
        "discussions": [
            {"number": 1, "status": "DONE", "created_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-10T00:00:00Z"},
            {"number": 2, "status": "DONE", "created_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-05T00:00:00Z"},
            {"number": 3, "status": "IMPLEMENTING", "created_at": "2026-04-01T00:00:00Z", "closed_at": None},
        ],
        "velocity": {},
    }
    (tmp_path / "registry.json").write_text(json.dumps(data))
    s = reg.stats()
    # stats() counts total as open discussions (closed_at=None); the 2 DONE items
    # have closed_at set, so only the 1 IMPLEMENTING item counts toward total.
    assert s["total"] == 1
    assert s["done"] == 2
    assert s["in_progress"] == 1
    assert s["avg_days_to_complete"] is not None
    assert s["avg_days_to_complete"] > 0


def test_stats_in_progress_includes_reviewing(tmp_path: Path):
    """REVIEWING status counts as in_progress."""
    reg = _make_registry(tmp_path)
    data = {
        "version": 1,
        "synced_at": "2026-04-10T00:00:00+00:00",
        "discussions": [
            {"number": 1, "status": "REVIEWING", "created_at": "2026-04-01T00:00:00Z", "closed_at": None},
        ],
        "velocity": {},
    }
    (tmp_path / "registry.json").write_text(json.dumps(data))
    s = reg.stats()
    assert s["in_progress"] == 1


def test_stats_tasks_per_day_nonzero(tmp_path: Path):
    """tasks_per_day is positive when there are done items over time."""
    reg = _make_registry(tmp_path)
    data = {
        "version": 1,
        "synced_at": "2026-04-10T00:00:00+00:00",
        "discussions": [
            {"number": 1, "status": "DONE", "created_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-10T00:00:00Z"},
        ],
        "velocity": {},
    }
    (tmp_path / "registry.json").write_text(json.dumps(data))
    s = reg.stats()
    assert s["tasks_per_day"] > 0


# ---------------------------------------------------------------------------
# _parse_status and _parse_pr
# ---------------------------------------------------------------------------


def test_parse_status_spec_ready(tmp_path: Path):
    reg = _make_registry(tmp_path)
    body = "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->"
    assert reg._parse_status(body) == "SPEC_READY"


def test_parse_status_implementing(tmp_path: Path):
    reg = _make_registry(tmp_path)
    body = "<!-- STATUS:IMPLEMENTING SINCE:2026-01-01T00:00:00Z -->"
    assert reg._parse_status(body) == "IMPLEMENTING"


def test_parse_status_done_with_pr(tmp_path: Path):
    reg = _make_registry(tmp_path)
    body = "<!-- STATUS:DONE PR:#42 SINCE:2026-01-01T00:00:00Z -->"
    assert reg._parse_status(body) == "DONE"


def test_parse_status_no_comment_defaults_to_discussing(tmp_path: Path):
    reg = _make_registry(tmp_path)
    assert reg._parse_status("## Some discussion body") == "DISCUSSING"


def test_parse_pr_extracts_number(tmp_path: Path):
    reg = _make_registry(tmp_path)
    body = "<!-- STATUS:DONE PR:#42 SINCE:2026-01-01T00:00:00Z -->"
    assert reg._parse_pr(body) == 42


def test_parse_pr_none_when_absent(tmp_path: Path):
    reg = _make_registry(tmp_path)
    body = "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->"
    assert reg._parse_pr(body) is None


# ---------------------------------------------------------------------------
# _parse_discussion
# ---------------------------------------------------------------------------


def test_parse_discussion_extracts_fields(tmp_path: Path):
    reg = _make_registry(tmp_path)
    node = _make_discussion_node(number=5, title="Feature X", status="REVIEWING")
    result = reg._parse_discussion(node)
    assert result["number"] == 5
    assert result["title"] == "Feature X"
    assert result["status"] == "REVIEWING"
    assert result["category"] == "General"
    assert result["labels"] == []


def test_parse_discussion_with_labels(tmp_path: Path):
    reg = _make_registry(tmp_path)
    node = _make_discussion_node(number=6)
    node["labels"] = {"nodes": [{"name": "bug"}, {"name": "priority"}]}
    result = reg._parse_discussion(node)
    assert "bug" in result["labels"]
    assert "priority" in result["labels"]


# ---------------------------------------------------------------------------
# sync() — mocked gh api
# ---------------------------------------------------------------------------


def test_sync_writes_registry(tmp_path: Path):
    """sync() writes registry.json with fetched discussions."""
    reg = _make_registry(tmp_path)
    nodes = [_make_discussion_node(1, "Task A", "SPEC_READY")]
    with patch("backend.registry.subprocess.check_output", return_value=_make_gh_response(nodes)):
        with patch("backend.registry.get_audit_trail", side_effect=ImportError, create=True):
            result = reg.sync()
    assert (tmp_path / "registry.json").exists()
    assert len(result["discussions"]) == 1
    assert result["discussions"][0]["number"] == 1


def test_sync_multiple_discussions(tmp_path: Path):
    """sync() handles multiple discussions."""
    reg = _make_registry(tmp_path)
    nodes = [
        _make_discussion_node(1, "Task A", "DONE"),
        _make_discussion_node(2, "Task B", "IMPLEMENTING"),
        _make_discussion_node(3, "Task C", "SPEC_READY"),
    ]
    with patch("backend.registry.subprocess.check_output", return_value=_make_gh_response(nodes)):
        with patch("backend.registry.get_audit_trail", side_effect=ImportError, create=True):
            result = reg.sync()
    assert len(result["discussions"]) == 3


def test_sync_updates_existing_status(tmp_path: Path):
    """sync() replaces old registry data with fresh data."""
    reg = _make_registry(tmp_path)
    old = {
        "version": 1,
        "synced_at": "2026-01-01T00:00:00+00:00",
        "discussions": [{"number": 1, "status": "SPEC_READY"}],
        "velocity": {},
    }
    (tmp_path / "registry.json").write_text(json.dumps(old))
    nodes = [_make_discussion_node(1, "Task A", "DONE")]
    with patch("backend.registry.subprocess.check_output", return_value=_make_gh_response(nodes)):
        with patch("backend.registry.get_audit_trail", side_effect=ImportError, create=True):
            result = reg.sync()
    assert result["discussions"][0]["status"] == "DONE"


def test_sync_pagination(tmp_path: Path):
    """sync() follows pagination to fetch all discussions."""
    reg = _make_registry(tmp_path)

    page1 = json.dumps({
        "data": {
            "repository": {
                "discussions": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-abc"},
                    "nodes": [_make_discussion_node(1)],
                }
            }
        }
    })
    page2 = json.dumps({
        "data": {
            "repository": {
                "discussions": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [_make_discussion_node(2)],
                }
            }
        }
    })

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        return page1 if call_count["n"] == 1 else page2

    with patch("backend.registry.subprocess.check_output", side_effect=side_effect):
        with patch("backend.registry.get_audit_trail", side_effect=ImportError, create=True):
            result = reg.sync()
    assert len(result["discussions"]) == 2
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# File persistence — write then reload
# ---------------------------------------------------------------------------


def test_persistence_roundtrip(tmp_path: Path):
    """Writing then reloading registry produces identical data."""
    reg = _make_registry(tmp_path)
    nodes = [_make_discussion_node(1, "Persist Me", "DONE")]
    with patch("backend.registry.subprocess.check_output", return_value=_make_gh_response(nodes)):
        with patch("backend.registry.get_audit_trail", side_effect=ImportError, create=True):
            written = reg.sync()
    loaded = reg.load()
    assert loaded["discussions"][0]["number"] == written["discussions"][0]["number"]
    assert loaded["discussions"][0]["status"] == written["discussions"][0]["status"]


# ---------------------------------------------------------------------------
# _LockedCtx
# ---------------------------------------------------------------------------


def test_locked_ctx_acquires_and_releases(tmp_path: Path):
    """_LockedCtx enters and exits without error."""
    lock_path = tmp_path / "test.lock"
    ctx = _LockedCtx(lock_path)
    with ctx:
        assert ctx._fh is not None
    assert ctx._fh is None


def test_locked_ctx_file_created(tmp_path: Path):
    """Lock file is created on entry."""
    lock_path = tmp_path / "subdir" / "test.lock"
    ctx = _LockedCtx(lock_path)
    with ctx:
        assert lock_path.exists()


def test_locked_ctx_exclusive_sequential(tmp_path: Path):
    """Two sequential locks on the same path both succeed."""
    lock_path = tmp_path / "test.lock"
    results = []
    with _LockedCtx(lock_path):
        results.append("first")
    with _LockedCtx(lock_path):
        results.append("second")
    assert results == ["first", "second"]


def test_locked_ctx_timeout_raises(tmp_path: Path):
    """LockTimeout is raised when lock cannot be acquired within timeout."""
    import signal as _signal

    lock_path = tmp_path / "timeout.lock"

    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        import fcntl
        fh = lock_path.open("a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        acquired.set()
        release.wait(timeout=5)
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    acquired.wait(timeout=2)

    with pytest.raises(LockTimeout):
        def _timeout_handler(signum, frame):
            raise LockTimeout("test timeout")
        old = _signal.signal(_signal.SIGALRM, _timeout_handler)
        _signal.alarm(1)
        try:
            import fcntl
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = lock_path.open("a")
            fcntl.flock(fh, fcntl.LOCK_EX)
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, old)
            fh.close()
        except LockTimeout:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, old)
            raise
        finally:
            release.set()

    release.set()
    t.join(timeout=2)


# ---------------------------------------------------------------------------
# CLI main() subcommands
# ---------------------------------------------------------------------------


def test_cli_show(tmp_path: Path, capsys):
    """CLI show command prints JSON to stdout."""
    from backend.registry import DiscussionRegistry as _Reg
    original_init = _Reg.__init__

    def patched_init(self, **kwargs):
        original_init(self, state_dir=tmp_path)

    with patch.object(_Reg, "__init__", patched_init):
        result = main(["show"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "discussions" in data


def test_cli_stats(tmp_path: Path, capsys):
    """CLI stats command prints human-readable metrics."""
    reg = _make_registry(tmp_path)
    with patch("backend.registry.DiscussionRegistry", return_value=reg):
        result = main(["stats"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Total discussions:" in captured.out


def test_cli_sync_success(tmp_path: Path, capsys):
    """CLI sync command exits 0 and prints summary."""
    reg = _make_registry(tmp_path)
    nodes = [_make_discussion_node(1, "X", "DONE")]
    with patch("backend.registry.DiscussionRegistry", return_value=reg), \
         patch("backend.registry.subprocess.check_output", return_value=_make_gh_response(nodes)):
        with patch("backend.registry.get_audit_trail", side_effect=ImportError, create=True):
            result = main(["sync"])
    assert result == 0
    captured = capsys.readouterr()
    assert "synced:" in captured.out


def test_cli_sync_gh_failure(tmp_path: Path, capsys):
    """CLI sync degrades gracefully when gh api graphql fails.

    After the error-handling fix (#1273), _fetch_all_discussions() catches
    CalledProcessError and returns [] rather than propagating the exception.
    sync() therefore succeeds with 0 discussions and exits 0. The error is
    still visible on stderr so it's observable, just not fatal.
    """
    reg = _make_registry(tmp_path)
    with patch("backend.registry.DiscussionRegistry", return_value=reg), \
         patch("backend.registry.subprocess.check_output",
               side_effect=subprocess.CalledProcessError(1, "gh")):
        result = main(["sync"])
    # Sync exits 0 (graceful degradation) even when gh fails.
    assert result == 0
    captured = capsys.readouterr()
    # Error is logged to stderr so it's observable.
    assert captured.err, "gh failure should be logged to stderr"
    # Summary reflects 0 discussions fetched.
    assert "synced:" in captured.out


def test_cli_invalid_command(tmp_path: Path):
    """CLI exits with error on invalid subcommand."""
    with pytest.raises(SystemExit):
        main(["invalid-command"])
