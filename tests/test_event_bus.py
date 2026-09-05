"""
Tests for backend/event_bus.py — in-process publish/subscribe event bus.

Coverage:
  - subscribe / unsubscribe lifecycle
  - publish dispatches to correct subscribers only
  - multiple subscribers for the same event type
  - event type filtering (different types do not cross-deliver)
  - thread safety: concurrent publishers and subscribers
  - FileAppender writes AgentOutputEvent to a JSONL file
  - publish_async delivers events asynchronously
  - singleton get_bus() returns the same instance
  - unsubscribe is idempotent
  - publish silently swallows subscriber exceptions
  - GateChangeEvent, BudgetSpendEvent, LoopIterationEvent dataclass fields
  - Event.to_dict() round-trip
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.event_bus import (
    AgentOutputEvent,
    BudgetSpendEvent,
    Event,
    EventBus,
    FileAppender,
    GateChangeEvent,
    LoopIterationEvent,
    get_bus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bus() -> EventBus:
    """Return a fresh EventBus for each test (not the singleton)."""
    return EventBus()


def _agent_event(**kwargs) -> AgentOutputEvent:
    return AgentOutputEvent(
        source="test",
        agent_id=kwargs.get("agent_id", "agent-1"),
        agent_role=kwargs.get("agent_role", "executor"),
        content=kwargs.get("content", "hello"),
        event_subtype=kwargs.get("event_subtype", "content"),
    )


# ---------------------------------------------------------------------------
# 1. Subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestSubscribeUnsubscribe:
    def test_subscribe_returns_unique_ids(self) -> None:
        bus = _make_bus()
        id1 = bus.subscribe(AgentOutputEvent, lambda e: None)
        id2 = bus.subscribe(AgentOutputEvent, lambda e: None)
        assert id1 != id2

    def test_unsubscribe_prevents_delivery(self) -> None:
        bus = _make_bus()
        received: list = []
        sub_id = bus.subscribe(AgentOutputEvent, received.append)
        bus.unsubscribe(sub_id)
        bus.publish(_agent_event())
        assert received == []

    def test_unsubscribe_is_idempotent(self) -> None:
        bus = _make_bus()
        sub_id = bus.subscribe(AgentOutputEvent, lambda e: None)
        bus.unsubscribe(sub_id)
        # Second call must not raise
        bus.unsubscribe(sub_id)

    def test_unsubscribe_unknown_id_is_safe(self) -> None:
        bus = _make_bus()
        bus.unsubscribe("sub-99999")  # must not raise


# ---------------------------------------------------------------------------
# 2. Publish dispatches to correct subscribers
# ---------------------------------------------------------------------------


class TestPublish:
    def test_publish_delivers_to_subscriber(self) -> None:
        bus = _make_bus()
        received: list[AgentOutputEvent] = []
        bus.subscribe(AgentOutputEvent, received.append)
        event = _agent_event(content="ping")
        bus.publish(event)
        assert len(received) == 1
        assert received[0].content == "ping"

    def test_publish_to_multiple_subscribers(self) -> None:
        bus = _make_bus()
        r1: list = []
        r2: list = []
        bus.subscribe(AgentOutputEvent, r1.append)
        bus.subscribe(AgentOutputEvent, r2.append)
        bus.publish(_agent_event())
        assert len(r1) == 1
        assert len(r2) == 1

    def test_publish_does_not_deliver_to_wrong_type(self) -> None:
        bus = _make_bus()
        received: list = []
        bus.subscribe(BudgetSpendEvent, received.append)
        bus.publish(_agent_event())  # AgentOutputEvent — should NOT reach BudgetSpend subs
        assert received == []

    def test_publish_swallows_subscriber_exception(self) -> None:
        bus = _make_bus()

        def bad_callback(e: Event) -> None:
            raise ValueError("subscriber bug")

        bus.subscribe(AgentOutputEvent, bad_callback)
        # Must not propagate the exception
        bus.publish(_agent_event())

    def test_publish_delivers_correct_event_object(self) -> None:
        bus = _make_bus()
        received: list[AgentOutputEvent] = []
        bus.subscribe(AgentOutputEvent, received.append)
        event = AgentOutputEvent(
            source="srv",
            agent_id="a1",
            agent_role="executor",
            content="body",
            event_subtype="done",
        )
        bus.publish(event)
        assert received[0] is event


# ---------------------------------------------------------------------------
# 3. Event type filtering
# ---------------------------------------------------------------------------


class TestEventTypeFiltering:
    def test_gate_change_event_only_reaches_gate_subscribers(self) -> None:
        bus = _make_bus()
        gate_received: list = []
        agent_received: list = []
        bus.subscribe(GateChangeEvent, gate_received.append)
        bus.subscribe(AgentOutputEvent, agent_received.append)

        bus.publish(GateChangeEvent(source="cfg", gate_name="spawn_enabled", old_value=False, new_value=True))
        assert len(gate_received) == 1
        assert agent_received == []

    def test_budget_event_only_reaches_budget_subscribers(self) -> None:
        bus = _make_bus()
        budget_received: list = []
        bus.subscribe(BudgetSpendEvent, budget_received.append)
        bus.publish(_agent_event())
        assert budget_received == []
        bus.publish(BudgetSpendEvent(source="budget", agent_id="x", role="executor",
                                     input_tokens=100, output_tokens=50))
        assert len(budget_received) == 1


# ---------------------------------------------------------------------------
# 4. Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_publish_10_threads(self) -> None:
        bus = _make_bus()
        received: list = []
        lock = threading.Lock()

        def safe_append(e: Event) -> None:
            with lock:
                received.append(e)

        bus.subscribe(AgentOutputEvent, safe_append)

        def publisher() -> None:
            for _ in range(10):
                bus.publish(_agent_event())

        threads = [threading.Thread(target=publisher) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(received) == 100  # 10 threads × 10 publishes

    def test_concurrent_subscribe_and_publish(self) -> None:
        bus = _make_bus()
        errors: list[Exception] = []

        def subscriber_worker() -> None:
            try:
                for _ in range(20):
                    sub_id = bus.subscribe(AgentOutputEvent, lambda e: None)
                    bus.unsubscribe(sub_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def publisher_worker() -> None:
            try:
                for _ in range(20):
                    bus.publish(_agent_event())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            *[threading.Thread(target=subscriber_worker) for _ in range(5)],
            *[threading.Thread(target=publisher_worker) for _ in range(5)],
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"


# ---------------------------------------------------------------------------
# 5. FileAppender
# ---------------------------------------------------------------------------


class TestFileAppender:
    def test_file_appender_writes_jsonl(self, tmp_path: Path) -> None:
        feed = tmp_path / "agent-feed.jsonl"
        appender = FileAppender(feed)
        event = _agent_event(content="test-content")
        appender.handle(event)
        lines = feed.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["content"] == "test-content"

    def test_file_appender_appends_multiple_events(self, tmp_path: Path) -> None:
        feed = tmp_path / "feed.jsonl"
        appender = FileAppender(feed)
        for i in range(5):
            appender.handle(_agent_event(content=f"msg-{i}"))
        lines = feed.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5

    def test_file_appender_creates_parent_dirs(self, tmp_path: Path) -> None:
        feed = tmp_path / "nested" / "deep" / "feed.jsonl"
        appender = FileAppender(feed)
        appender.handle(_agent_event())
        assert feed.exists()

    def test_file_appender_output_matches_event_to_dict(self, tmp_path: Path) -> None:
        feed = tmp_path / "feed.jsonl"
        appender = FileAppender(feed)
        event = AgentOutputEvent(
            source="srv", agent_id="a1", agent_role="executor",
            content="body", event_subtype="done",
        )
        appender.handle(event)
        written = json.loads(feed.read_text(encoding="utf-8").strip())
        expected = event.to_dict()
        assert written == expected


# ---------------------------------------------------------------------------
# 6. publish_async
# ---------------------------------------------------------------------------


class TestPublishAsync:
    def test_publish_async_delivers_event(self) -> None:
        bus = _make_bus()
        received: list = []
        lock = threading.Lock()
        ready = threading.Event()

        def cb(e: Event) -> None:
            with lock:
                received.append(e)
            ready.set()

        bus.subscribe(AgentOutputEvent, cb)
        bus.publish_async(_agent_event(content="async"))

        delivered = ready.wait(timeout=2.0)
        assert delivered, "publish_async did not deliver within 2 seconds"
        assert received[0].content == "async"


# ---------------------------------------------------------------------------
# 7. Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_bus_returns_same_instance(self) -> None:
        b1 = get_bus()
        b2 = get_bus()
        assert b1 is b2


# ---------------------------------------------------------------------------
# 8. Dataclass field verification
# ---------------------------------------------------------------------------


class TestEventDataclasses:
    def test_event_to_dict_round_trip(self) -> None:
        event = AgentOutputEvent(
            source="srv",
            agent_id="a-1",
            agent_role="executor",
            content="hi",
            event_subtype="content",
        )
        d = event.to_dict()
        assert d["agent_id"] == "a-1"
        assert d["source"] == "srv"
        assert "timestamp" in d

    def test_budget_spend_event_fields(self) -> None:
        e = BudgetSpendEvent(
            source="budget",
            agent_id="exec-1",
            role="executor",
            input_tokens=1000,
            output_tokens=500,
            discussion=42,
        )
        assert e.input_tokens == 1000
        assert e.discussion == 42

    def test_loop_iteration_event_fields(self) -> None:
        e = LoopIterationEvent(
            source="loop",
            iteration_id="iter-1",
            idle=True,
            duration_seconds=12.5,
            agents_spawned=3,
        )
        assert e.idle is True
        assert e.agents_spawned == 3

    def test_gate_change_event_fields(self) -> None:
        e = GateChangeEvent(
            source="cfg",
            gate_name="spawn_enabled",
            old_value=False,
            new_value=True,
        )
        assert e.gate_name == "spawn_enabled"
        assert e.new_value is True
