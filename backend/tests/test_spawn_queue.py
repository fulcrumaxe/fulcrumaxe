"""
Tests for backend/spawn_queue.py

Run with:
    python -m pytest backend/tests/test_spawn_queue.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.spawn_queue import SpawnQueue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_queue(tmp_path: Path) -> SpawnQueue:
    """Return a SpawnQueue backed by a temporary directory."""
    queue_file = tmp_path / "spawn-queue.json"
    # No config file — uses defaults.
    return SpawnQueue(queue_file=queue_file, config_file=tmp_path / "nonexistent-config.json")


@pytest.fixture()
def tmp_queue_with_config(tmp_path: Path):
    """Return a SpawnQueue with a config that overrides limits."""
    config = {
        "settings": {
            "spawn_limits": {
                "executor": 3,
                "_total": 8,
            }
        },
        "policies": {
            "executor": {"timeout_minutes": 1},
            "code-reviewer": {"timeout_minutes": 1},
        },
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    queue_file = tmp_path / "spawn-queue.json"
    return SpawnQueue(queue_file=queue_file, config_file=config_file)


# ---------------------------------------------------------------------------
# AC 1: enqueue returns an ID and the request appears in list_pending
# ---------------------------------------------------------------------------


def test_enqueue_returns_id_and_appears_in_pending(tmp_queue: SpawnQueue) -> None:
    req_id = tmp_queue.enqueue("executor", 42, "Implement feature X")
    assert isinstance(req_id, str)
    assert len(req_id) == 8

    pending = tmp_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["id"] == req_id
    assert pending[0]["role"] == "executor"
    assert pending[0]["discussion"] == 42


# ---------------------------------------------------------------------------
# AC 2: dequeue returns the highest-priority pending request
# ---------------------------------------------------------------------------


def test_dequeue_returns_highest_priority(tmp_queue: SpawnQueue) -> None:
    tmp_queue.enqueue("project-manager", None, "Write spec", priority=40)
    tmp_queue.enqueue("executor", 42, "Implement", priority=20)
    tmp_queue.enqueue("code-reviewer", 42, "Review PR", priority=10)

    item = tmp_queue.dequeue()
    assert item is not None
    assert item["role"] == "code-reviewer"
    assert item["priority"] == 10


# ---------------------------------------------------------------------------
# AC 3: when role is at concurrency limit, dequeue skips it and picks next
# ---------------------------------------------------------------------------


def test_dequeue_skips_full_role(tmp_queue: SpawnQueue) -> None:
    # Fill executor slots (limit=2)
    id1 = tmp_queue.enqueue("executor", 1, "ctx1")
    id2 = tmp_queue.enqueue("executor", 2, "ctx2")

    item1 = tmp_queue.dequeue()
    assert item1 is not None
    tmp_queue.mark_active(item1["id"])

    item2 = tmp_queue.dequeue()
    assert item2 is not None
    tmp_queue.mark_active(item2["id"])

    # Now executor slots are full; add a third executor and a reviewer.
    tmp_queue.enqueue("executor", 3, "ctx3", priority=20)
    tmp_queue.enqueue("code-reviewer", 3, "review ctx", priority=10)

    # dequeue should skip executor (full) and return code-reviewer.
    item3 = tmp_queue.dequeue()
    assert item3 is not None
    assert item3["role"] == "code-reviewer"


# ---------------------------------------------------------------------------
# AC 4: when _total limit reached, dequeue returns None
# ---------------------------------------------------------------------------


def test_dequeue_returns_none_when_total_full(tmp_queue: SpawnQueue) -> None:
    # Default _total = 6. Fill with project-manager requests (limit=1 each type,
    # so use diverse roles to hit total without per-role limits being the blocker).
    # Actually simplest: fill with 6 code-reviewer slots... but limit is 2.
    # So: 2 code-reviewer + 2 executor + 1 security-reviewer + 1 project-manager = 6.
    roles = [
        ("code-reviewer", 2),
        ("executor", 2),
        ("security-reviewer", 1),
        ("project-manager", 1),
    ]
    active_ids = []
    for role, count in roles:
        for i in range(count):
            rid = tmp_queue.enqueue(role, i, f"ctx {role} {i}")
            item = tmp_queue.dequeue()
            assert item is not None
            tmp_queue.mark_active(item["id"])
            active_ids.append(item["id"])

    # Queue is now at total capacity (6 active). Add a new request.
    tmp_queue.enqueue("mission-analyst", None, "analyse")

    result = tmp_queue.dequeue()
    assert result is None


# ---------------------------------------------------------------------------
# AC 5: mark_done frees slot and role becomes available
# ---------------------------------------------------------------------------


def test_mark_done_frees_slot(tmp_queue: SpawnQueue) -> None:
    id1 = tmp_queue.enqueue("executor", 1, "first")
    id2 = tmp_queue.enqueue("executor", 2, "second")

    item1 = tmp_queue.dequeue()
    assert item1 is not None
    tmp_queue.mark_active(item1["id"])

    item2 = tmp_queue.dequeue()
    assert item2 is not None
    tmp_queue.mark_active(item2["id"])

    # Executor full — third enqueue should not dequeue.
    tmp_queue.enqueue("executor", 3, "third", priority=20)
    assert tmp_queue.dequeue() is None

    # Free one slot.
    tmp_queue.mark_done(item1["id"])

    # Now third should dequeue.
    item3 = tmp_queue.dequeue()
    assert item3 is not None
    assert item3["role"] == "executor"


# ---------------------------------------------------------------------------
# AC 6: stale agents are cleaned up automatically on dequeue
# ---------------------------------------------------------------------------


def test_stale_agents_cleaned_up_on_dequeue(tmp_queue_with_config: SpawnQueue) -> None:
    q = tmp_queue_with_config
    # Enqueue and manually activate an executor.
    req_id = q.enqueue("executor", 10, "stale")
    item = q.dequeue()
    assert item is not None
    q.mark_active(item["id"])

    # Manually backdate started_at to exceed timeout (config: 1 min).
    state_file = q._queue_file
    state = json.loads(state_file.read_text())
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    for entry in state["active"]:
        entry["started_at"] = past
    state_file.write_text(json.dumps(state))

    # Enqueue a fresh request; dequeue should cleanup stale and return it.
    q.enqueue("executor", 11, "fresh")
    result = q.dequeue()
    assert result is not None
    assert result["discussion"] == 11

    # Stale agent should appear in failed.
    st = q.status()
    assert st["failed"] >= 1


# ---------------------------------------------------------------------------
# AC 7: CLI status prints utilization summary
# ---------------------------------------------------------------------------


def test_cli_status(tmp_queue: SpawnQueue, capsys) -> None:
    from backend.spawn_queue import _fmt_status

    st = tmp_queue.status()
    output = _fmt_status(st)
    assert "Active:" in output
    assert "Pending:" in output


# ---------------------------------------------------------------------------
# AC 9: queue state survives process restart (persistence)
# ---------------------------------------------------------------------------


def test_persistence_across_reload(tmp_path: Path) -> None:
    queue_file = tmp_path / "spawn-queue.json"
    config_file = tmp_path / "config.json"

    q1 = SpawnQueue(queue_file=queue_file, config_file=config_file)
    req_id = q1.enqueue("executor", 99, "persist me")

    # Reload from same file.
    q2 = SpawnQueue(queue_file=queue_file, config_file=config_file)
    pending = q2.list_pending()
    assert len(pending) == 1
    assert pending[0]["id"] == req_id
    assert pending[0]["discussion"] == 99


# ---------------------------------------------------------------------------
# AC 11: concurrent enqueue/dequeue from multiple threads does not corrupt state
# ---------------------------------------------------------------------------


def test_concurrent_enqueue_dequeue(tmp_queue: SpawnQueue) -> None:
    errors: list[Exception] = []
    results: list[dict] = []
    lock = threading.Lock()

    def enqueue_worker(n: int) -> None:
        try:
            tmp_queue.enqueue("project-manager", n, f"context {n}")
        except Exception as exc:
            with lock:
                errors.append(exc)

    def dequeue_worker() -> None:
        try:
            item = tmp_queue.dequeue()
            if item:
                with lock:
                    results.append(item)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=enqueue_worker, args=(i,)))
    for _ in range(5):
        threads.append(threading.Thread(target=dequeue_worker))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Thread errors: {errors}"

    # No duplicate IDs among dequeued items.
    ids = [r["id"] for r in results]
    assert len(ids) == len(set(ids)), "Duplicate IDs found in dequeued results"

    # State is not corrupted.
    pending = tmp_queue.list_pending()
    active = tmp_queue.list_active()
    # Total accounted for: pending + dequeued = 10
    assert len(pending) + len(results) == 10


# ---------------------------------------------------------------------------
# AC 12: priority ordering (reviewer before executor)
# ---------------------------------------------------------------------------


def test_priority_ordering_reviewer_before_executor(tmp_queue: SpawnQueue) -> None:
    tmp_queue.enqueue("executor", 1, "exec ctx")        # priority 20
    tmp_queue.enqueue("code-reviewer", 1, "review ctx")  # priority 10

    first = tmp_queue.dequeue()
    assert first is not None
    assert first["role"] == "code-reviewer"

    second = tmp_queue.dequeue()
    assert second is not None
    assert second["role"] == "executor"


# ---------------------------------------------------------------------------
# Extra: cancel removes from pending
# ---------------------------------------------------------------------------


def test_cancel_pending_request(tmp_queue: SpawnQueue) -> None:
    req_id = tmp_queue.enqueue("executor", 5, "cancel me")
    assert len(tmp_queue.list_pending()) == 1

    result = tmp_queue.cancel(req_id)
    assert result is True
    assert len(tmp_queue.list_pending()) == 0

    # Cancelling again returns False.
    assert tmp_queue.cancel(req_id) is False


# ---------------------------------------------------------------------------
# Extra: drain clears all active agents
# ---------------------------------------------------------------------------


def test_drain(tmp_queue: SpawnQueue) -> None:
    for i in range(3):
        tmp_queue.enqueue("project-manager", i, f"pm {i}")

    # Activate one.
    item = tmp_queue.dequeue()
    assert item is not None
    tmp_queue.mark_active(item["id"])
    assert len(tmp_queue.list_active()) == 1

    count = tmp_queue.drain()
    assert count == 1
    assert len(tmp_queue.list_active()) == 0


# ---------------------------------------------------------------------------
# Extra: mark_failed moves entry to failed array
# ---------------------------------------------------------------------------


def test_mark_failed_active(tmp_queue: SpawnQueue) -> None:
    tmp_queue.enqueue("executor", 1, "will fail")
    item = tmp_queue.dequeue()
    assert item is not None
    tmp_queue.mark_active(item["id"])

    tmp_queue.mark_failed(item["id"], reason="test failure")
    assert len(tmp_queue.list_active()) == 0
    st = tmp_queue.status()
    assert st["failed"] >= 1


# ---------------------------------------------------------------------------
# Extra: config-driven limits override defaults
# ---------------------------------------------------------------------------


def test_config_driven_limits(tmp_queue_with_config: SpawnQueue) -> None:
    q = tmp_queue_with_config
    limits = q._effective_limits()
    assert limits["executor"] == 3   # overridden from 2
    assert limits["_total"] == 8     # overridden from 6
    assert limits["code-reviewer"] == 2  # still default


# ---------------------------------------------------------------------------
# CLI subcommands via main()
# ---------------------------------------------------------------------------


def test_cli_status_command(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main, get_spawn_queue
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")
    q.enqueue("executor", 1, "some work")

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["status"])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 0
    captured = capsys.readouterr()
    assert "Active:" in captured.out
    assert "Pending:" in captured.out


def test_cli_enqueue_command(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["enqueue", "executor", "42", "do the thing"])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 0
    captured = capsys.readouterr()
    assert "Enqueued:" in captured.out
    assert len(q.list_pending()) == 1


def test_cli_enqueue_with_priority(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["enqueue", "executor", "5", "high priority", "--priority", "1", "--requested-by", "test"])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 0
    pending = q.list_pending()
    assert pending[0]["priority"] == 1
    assert pending[0]["requested_by"] == "test"


def test_cli_active_empty(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["active"])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 0
    captured = capsys.readouterr()
    assert "No active agents." in captured.out


def test_cli_pending_empty(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["pending"])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 0
    captured = capsys.readouterr()
    assert "No pending requests." in captured.out


def test_cli_cancel_existing(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")
    req_id = q.enqueue("executor", 1, "cancel me")

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["cancel", req_id])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 0
    captured = capsys.readouterr()
    assert "Cancelled:" in captured.out


def test_cli_cancel_nonexistent(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["cancel", "deadbeef"])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 1  # not found -> exit code 1


def test_cli_drain_command(tmp_path: Path, capsys) -> None:
    from backend.spawn_queue import SpawnQueue, main
    import backend.spawn_queue as sq_mod

    queue_file = tmp_path / "spawn-queue.json"
    q = SpawnQueue(queue_file=queue_file, config_file=tmp_path / "no-config.json")
    q.enqueue("executor", 1, "work")
    item = q.dequeue()
    assert item is not None
    q.mark_active(item["id"])

    original = sq_mod._queue_singleton
    sq_mod._queue_singleton = q
    try:
        rc = main(["drain"])
    finally:
        sq_mod._queue_singleton = original

    assert rc == 0
    captured = capsys.readouterr()
    assert "Drained 1" in captured.out


# ---------------------------------------------------------------------------
# list_active returns correct agents
# ---------------------------------------------------------------------------


def test_list_active_returns_active_agents(tmp_queue: SpawnQueue) -> None:
    tmp_queue.enqueue("executor", 1, "first")
    tmp_queue.enqueue("executor", 2, "second")

    item1 = tmp_queue.dequeue()
    assert item1 is not None
    tmp_queue.mark_active(item1["id"])

    active = tmp_queue.list_active()
    assert len(active) == 1
    assert active[0]["role"] == "executor"


# ---------------------------------------------------------------------------
# mark_failed preserves reason string
# ---------------------------------------------------------------------------


def test_mark_failed_reason_preserved(tmp_queue: SpawnQueue) -> None:
    tmp_queue.enqueue("executor", 1, "will fail")
    item = tmp_queue.dequeue()
    assert item is not None
    tmp_queue.mark_active(item["id"])

    tmp_queue.mark_failed(item["id"], reason="something went wrong")

    st = tmp_queue.status()
    assert st["failed"] >= 1

    # Read the file directly to check reason is stored
    state = json.loads(tmp_queue._queue_file.read_text())
    reason = state["failed"][-1]["reason"]
    assert reason == "something went wrong"


# ---------------------------------------------------------------------------
# status() output includes expected counts
# ---------------------------------------------------------------------------


def test_status_counts(tmp_queue: SpawnQueue) -> None:
    tmp_queue.enqueue("executor", 1, "pending")
    tmp_queue.enqueue("executor", 2, "pending2")

    item = tmp_queue.dequeue()
    assert item is not None
    tmp_queue.mark_active(item["id"])

    st = tmp_queue.status()
    assert st["pending"] == 1
    assert st["active_total"] == 1
    assert "by_role" in st
    assert "utilization_pct" in st


# ---------------------------------------------------------------------------
# enqueue with all optional fields
# ---------------------------------------------------------------------------


def test_enqueue_all_optional_fields(tmp_queue: SpawnQueue) -> None:
    req_id = tmp_queue.enqueue(
        role="code-reviewer",
        discussion=99,
        prompt_context="review it",
        priority=5,
        requested_by="project-manager",
    )
    pending = tmp_queue.list_pending()
    assert len(pending) == 1
    entry = pending[0]
    assert entry["id"] == req_id
    assert entry["priority"] == 5
    assert entry["requested_by"] == "project-manager"
    assert entry["discussion"] == 99


# ---------------------------------------------------------------------------
# drain empty queue returns 0
# ---------------------------------------------------------------------------


def test_drain_empty_queue(tmp_queue: SpawnQueue) -> None:
    count = tmp_queue.drain()
    assert count == 0
