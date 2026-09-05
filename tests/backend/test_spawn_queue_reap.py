"""
Tests for SpawnQueue.reap() — TTL expiry, target-validity pruning, dry-run,
config TTL override, and concurrency safety.

All gh/subprocess calls are mocked so tests run hermetically (no network).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.spawn_queue import SpawnQueue, _DEFAULT_TTL_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue(tmp_path: Path, config_ttl: int | None = None) -> SpawnQueue:
    """Create a SpawnQueue backed by tmp files, optionally with a config TTL override."""
    queue_file = tmp_path / "spawn-queue.json"
    config_file = tmp_path / "config.json"

    config: dict = {}
    if config_ttl is not None:
        config = {"policies": {"queue": {"ttl_seconds": config_ttl}}}
    config_file.write_text(json.dumps(config))

    return SpawnQueue(queue_file=queue_file, config_file=config_file)


def _enqueue_aged(q: SpawnQueue, role: str, age_seconds: int, **kwargs) -> str:
    """Enqueue an item and back-date its enqueued_at by age_seconds."""
    req_id = q.enqueue(role, None, "test", **kwargs)
    # Patch the enqueued_at timestamp in the queue file.
    state = json.loads(q._queue_file.read_text())
    for item in state["pending"]:
        if item["id"] == req_id:
            old_time = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
            item["enqueued_at"] = old_time.isoformat(timespec="seconds")
    q._queue_file.write_text(json.dumps(state))
    return req_id


def _gh_pr_found() -> MagicMock:
    """Stub: gh pr view exits 0 (PR exists)."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = '{"number": 42, "state": "open"}'
    m.stderr = ""
    return m


def _gh_pr_not_found() -> MagicMock:
    """Stub: gh pr view exits 1 with 'no pull requests found'."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = "no pull requests found"
    return m


def _gh_pr_rate_limit() -> MagicMock:
    """Stub: gh pr view exits 1 with a rate limit message (transient)."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = "HTTP 429: rate limit exceeded"
    return m


def _gh_discussion_found(disc_num: int) -> MagicMock:
    """Stub: GraphQL returns a valid discussion."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = json.dumps({
        "data": {
            "repository": {
                "discussion": {"number": disc_num}
            }
        }
    })
    m.stderr = ""
    return m


def _gh_discussion_not_found() -> MagicMock:
    """Stub: GraphQL returns null discussion (not found)."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = json.dumps({
        "data": {
            "repository": {
                "discussion": None
            }
        }
    })
    m.stderr = ""
    return m


def _gh_discussion_timeout() -> MagicMock:
    """Stub: GraphQL exits non-zero with timeout message (transient)."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = "connection timeout"
    return m


# ---------------------------------------------------------------------------
# AC 1 — expired item is pruned with reason ttl_expired
# ---------------------------------------------------------------------------


def test_ttl_expired_item_is_pruned(tmp_path):
    q = _make_queue(tmp_path)
    # Enqueue an item older than the default TTL.
    req_id = _enqueue_aged(q, "executor", age_seconds=_DEFAULT_TTL_SECONDS + 60)

    def fake_run(cmd, **kw):
        # Only logging (gh issue list/comment) calls are expected — no gh pr view or graphql.
        assert "pr" not in cmd or "view" not in cmd, f"Unexpected gh pr view call for TTL-expired item: {cmd}"
        assert "graphql" not in cmd, f"Unexpected graphql call for TTL-expired item: {cmd}"
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap()

    assert req_id in result["pruned_ttl"]
    assert req_id not in result["pruned_missing"]
    assert result["kept"] == 0

    state = json.loads(q._queue_file.read_text())
    assert all(i["id"] != req_id for i in state["pending"])
    failed_ids = [i["id"] for i in state["failed"]]
    assert req_id in failed_ids
    failed_item = next(i for i in state["failed"] if i["id"] == req_id)
    assert failed_item["reason"] == "ttl_expired"
    assert "failed_at" in failed_item


# ---------------------------------------------------------------------------
# AC 2 — pending item with non-existent PR is pruned
# ---------------------------------------------------------------------------


def test_missing_pr_item_is_pruned(tmp_path):
    q = _make_queue(tmp_path)
    req_id = q.enqueue("code-reviewer", None, "review", pr=999999)

    def fake_run(cmd, **kw):
        if "pr" in cmd and "view" in cmd:
            return _gh_pr_not_found()
        # team-log calls
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap()

    assert req_id in result["pruned_missing"]
    state = json.loads(q._queue_file.read_text())
    assert all(i["id"] != req_id for i in state["pending"])
    failed_item = next(i for i in state["failed"] if i["id"] == req_id)
    assert failed_item["reason"] == "target_missing"


# ---------------------------------------------------------------------------
# AC 3 — pending item with non-existent Discussion is pruned
# ---------------------------------------------------------------------------


def test_missing_discussion_item_is_pruned(tmp_path):
    q = _make_queue(tmp_path)
    req_id = q.enqueue("executor", 999999, "implement")

    def fake_run(cmd, **kw):
        if "graphql" in cmd:
            return _gh_discussion_not_found()
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap()

    assert req_id in result["pruned_missing"]
    state = json.loads(q._queue_file.read_text())
    failed_item = next(i for i in state["failed"] if i["id"] == req_id)
    assert failed_item["reason"] == "target_missing"


# ---------------------------------------------------------------------------
# AC 4 — valid PR item is NOT pruned (state-agnostic: existence is enough)
# ---------------------------------------------------------------------------


def test_valid_pr_item_is_kept(tmp_path):
    q = _make_queue(tmp_path)
    req_id = q.enqueue("code-reviewer", None, "review", pr=42)

    def fake_run(cmd, **kw):
        if "pr" in cmd and "view" in cmd:
            return _gh_pr_found()
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap()

    assert req_id not in result["pruned_ttl"]
    assert req_id not in result["pruned_missing"]
    assert result["kept"] == 1
    state = json.loads(q._queue_file.read_text())
    assert any(i["id"] == req_id for i in state["pending"])


# ---------------------------------------------------------------------------
# AC 5 — young item with no PR/Discussion target is NOT pruned
# ---------------------------------------------------------------------------


def test_young_no_target_item_is_kept(tmp_path):
    q = _make_queue(tmp_path)
    req_id = q.enqueue("project-manager", None, "generate ideas")

    with patch("subprocess.run") as mock_run:
        result = q.reap()

    assert req_id not in result["pruned_ttl"]
    assert req_id not in result["pruned_missing"]
    assert result["kept"] == 1
    # No network call needed for an item with no pr and no discussion.
    # (project-manager with no discussion falls through to "ok")


# ---------------------------------------------------------------------------
# AC 5b — valid Discussion item is NOT pruned
# ---------------------------------------------------------------------------


def test_valid_discussion_item_is_kept(tmp_path):
    q = _make_queue(tmp_path)
    req_id = q.enqueue("executor", 336, "implement")

    def fake_run(cmd, **kw):
        if "graphql" in cmd:
            return _gh_discussion_found(336)
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap()

    assert req_id not in result["pruned_ttl"]
    assert req_id not in result["pruned_missing"]
    assert result["kept"] == 1


# ---------------------------------------------------------------------------
# AC 6 — transient error leaves item in pending
# ---------------------------------------------------------------------------


def test_transient_error_leaves_item_pending(tmp_path):
    q = _make_queue(tmp_path)
    req_id = q.enqueue("code-reviewer", None, "review", pr=55)

    def fake_run(cmd, **kw):
        if "pr" in cmd and "view" in cmd:
            return _gh_pr_rate_limit()
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap()

    assert req_id in result["skipped_transient"]
    assert req_id not in result["pruned_ttl"]
    assert req_id not in result["pruned_missing"]
    state = json.loads(q._queue_file.read_text())
    assert any(i["id"] == req_id for i in state["pending"])


# ---------------------------------------------------------------------------
# AC 6b — Discussion transient error leaves item pending
# ---------------------------------------------------------------------------


def test_discussion_transient_error_leaves_item_pending(tmp_path):
    q = _make_queue(tmp_path)
    req_id = q.enqueue("executor", 999, "implement")

    def fake_run(cmd, **kw):
        if "graphql" in cmd:
            return _gh_discussion_timeout()
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap()

    assert req_id in result["skipped_transient"]
    state = json.loads(q._queue_file.read_text())
    assert any(i["id"] == req_id for i in state["pending"])


# ---------------------------------------------------------------------------
# AC 7 — dry-run reports without mutating
# ---------------------------------------------------------------------------


def test_dry_run_does_not_mutate(tmp_path):
    q = _make_queue(tmp_path)
    # One expired item.
    expired_id = _enqueue_aged(q, "executor", age_seconds=_DEFAULT_TTL_SECONDS + 1)
    # One missing-PR item.
    missing_pr_id = q.enqueue("code-reviewer", None, "review", pr=999999)

    state_before = q._queue_file.read_text()

    def fake_run(cmd, **kw):
        if "pr" in cmd and "view" in cmd:
            return _gh_pr_not_found()
        m = MagicMock()
        m.returncode = 0
        m.stdout = "342"
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        result = q.reap(dry_run=True)

    state_after = q._queue_file.read_text()
    # File must not change in dry-run mode.
    assert state_before == state_after

    assert expired_id in result["pruned_ttl"]
    assert missing_pr_id in result["pruned_missing"]


# ---------------------------------------------------------------------------
# AC 8 — config TTL override is respected
# ---------------------------------------------------------------------------


def test_config_ttl_override(tmp_path):
    # Config TTL = 60s; item aged 90s should be pruned.
    q = _make_queue(tmp_path, config_ttl=60)
    req_id = _enqueue_aged(q, "executor", age_seconds=90)

    with patch("subprocess.run"):
        result = q.reap()

    assert req_id in result["pruned_ttl"]


def test_config_ttl_young_item_kept(tmp_path):
    # Config TTL = 60s; item aged 30s should survive.
    q = _make_queue(tmp_path, config_ttl=60)
    req_id = _enqueue_aged(q, "executor", age_seconds=30)

    with patch("subprocess.run") as mock_run:
        result = q.reap()

    assert req_id not in result["pruned_ttl"]
    assert req_id not in result["pruned_missing"]


# ---------------------------------------------------------------------------
# AC 9 — concurrent reap + dequeue do not corrupt the queue
# ---------------------------------------------------------------------------


def test_concurrent_reap_and_dequeue(tmp_path):
    """
    Run reap() and dequeue() in two threads simultaneously.
    The reaper should not corrupt items that the drainer has already dequeued.
    """
    q = _make_queue(tmp_path)

    # Add several young items (none should be TTL-pruned).
    ids = [q.enqueue("executor", None, f"work {i}") for i in range(10)]

    errors: list[str] = []

    def run_dequeue():
        for _ in range(5):
            try:
                q.dequeue()
            except Exception as e:  # noqa: BLE001
                errors.append(f"dequeue: {e}")
            time.sleep(0.001)

    def run_reap():
        for _ in range(5):
            try:
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="342", stderr="")
                    q.reap()
            except Exception as e:  # noqa: BLE001
                errors.append(f"reap: {e}")
            time.sleep(0.001)

    t1 = threading.Thread(target=run_dequeue)
    t2 = threading.Thread(target=run_reap)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Concurrent errors: {errors}"

    # Verify the queue file is valid JSON and not corrupted.
    state = json.loads(q._queue_file.read_text())
    assert isinstance(state["pending"], list)
    assert isinstance(state["active"], list)
    assert isinstance(state["failed"], list)


# ---------------------------------------------------------------------------
# AC 10 — history cap is respected
# ---------------------------------------------------------------------------


def test_failed_history_cap(tmp_path):
    from backend.spawn_queue import _MAX_HISTORY

    q = _make_queue(tmp_path)
    # Fill failed list to just under cap.
    initial_failed = [
        {"id": f"deadbeef{i:02d}", "role": "executor", "failed_at": "2020-01-01T00:00:00+00:00", "reason": "old"}
        for i in range(_MAX_HISTORY - 2)
    ]
    state = {"pending": [], "active": [], "completed": [], "failed": initial_failed}
    q._queue_file.write_text(json.dumps(state))

    # Expire 5 items — total failed would exceed cap.
    for _ in range(5):
        _enqueue_aged(q, "executor", age_seconds=_DEFAULT_TTL_SECONDS + 1)

    with patch("subprocess.run"):
        q.reap()

    final_state = json.loads(q._queue_file.read_text())
    assert len(final_state["failed"]) <= _MAX_HISTORY
