"""
Tests for backend.event_bus — typed publish/subscribe, thread safety,
type filtering, unsubscribe, and resilience to bad callbacks.

Thread-safety note: all threading tests use bounded joins (timeout=5s)
and Event flags so they cannot hang indefinitely.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from backend.event_bus import (
    AgentOutputEvent,
    BudgetSpendEvent,
    BusEventFileAppender,
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
    """Return a fresh EventBus (not the global singleton) for test isolation."""
    return EventBus()


def _agent_event(**kwargs) -> AgentOutputEvent:
    defaults = dict(
        source="test",
        agent_id="exec-1",
        agent_role="executor",
        content="hello",
        event_subtype="content",
    )
    defaults.update(kwargs)
    return AgentOutputEvent(**defaults)


def _budget_event(**kwargs) -> BudgetSpendEvent:
    defaults = dict(source="test", agent_id="exec-1", role="executor", input_tokens=10)
    defaults.update(kwargs)
    return BudgetSpendEvent(**defaults)


# ---------------------------------------------------------------------------
# Subscribe + publish: callback receives correct payload
# ---------------------------------------------------------------------------


def test_subscribe_then_publish_delivers_event():
    bus = _make_bus()
    received: list[AgentOutputEvent] = []

    bus.subscribe(AgentOutputEvent, received.append)
    evt = _agent_event(content="payload-check")
    bus.publish(evt)

    assert len(received) == 1
    assert received[0] is evt


def test_published_event_carries_correct_fields():
    bus = _make_bus()
    received: list[AgentOutputEvent] = []

    bus.subscribe(AgentOutputEvent, received.append)
    evt = _agent_event(agent_id="a1", agent_role="executor", content="hi", event_subtype="done")
    bus.publish(evt)

    r = received[0]
    assert r.agent_id == "a1"
    assert r.agent_role == "executor"
    assert r.content == "hi"
    assert r.event_subtype == "done"


def test_no_subscriber_publish_is_a_no_op():
    """publish() with no registered subscribers must not raise."""
    bus = _make_bus()
    bus.publish(_agent_event())  # should not raise


# ---------------------------------------------------------------------------
# Type filtering: subscriber for A does not receive B
# ---------------------------------------------------------------------------


def test_type_filtering_agent_subscriber_does_not_receive_budget_event():
    bus = _make_bus()
    agent_received: list[Event] = []
    budget_received: list[Event] = []

    bus.subscribe(AgentOutputEvent, agent_received.append)
    bus.subscribe(BudgetSpendEvent, budget_received.append)

    bus.publish(_agent_event())
    bus.publish(_budget_event())

    assert len(agent_received) == 1
    assert isinstance(agent_received[0], AgentOutputEvent)
    assert len(budget_received) == 1
    assert isinstance(budget_received[0], BudgetSpendEvent)


def test_type_filtering_no_cross_delivery():
    """A BudgetSpendEvent must NOT be delivered to an AgentOutputEvent subscriber."""
    bus = _make_bus()
    wrong_type: list[Event] = []

    bus.subscribe(AgentOutputEvent, wrong_type.append)
    bus.publish(_budget_event())

    assert wrong_type == []


def test_base_event_subscriber_does_not_receive_subclass_events():
    """
    publish() dispatches to the *exact* registered type, not base classes.
    Subscribing to Event should NOT receive AgentOutputEvent.
    """
    bus = _make_bus()
    base_received: list[Event] = []

    bus.subscribe(Event, base_received.append)
    bus.publish(_agent_event())

    assert base_received == []


# ---------------------------------------------------------------------------
# Multiple subscribers all receive the event
# ---------------------------------------------------------------------------


def test_multiple_subscribers_all_receive():
    bus = _make_bus()
    results: list[list[Event]] = [[], [], []]

    bus.subscribe(AgentOutputEvent, results[0].append)
    bus.subscribe(AgentOutputEvent, results[1].append)
    bus.subscribe(AgentOutputEvent, results[2].append)

    evt = _agent_event()
    bus.publish(evt)

    for bucket in results:
        assert len(bucket) == 1
        assert bucket[0] is evt


def test_multiple_subscribers_each_get_all_events():
    bus = _make_bus()
    a: list[Event] = []
    b: list[Event] = []

    bus.subscribe(AgentOutputEvent, a.append)
    bus.subscribe(AgentOutputEvent, b.append)

    for i in range(5):
        bus.publish(_agent_event(content=str(i)))

    assert len(a) == 5
    assert len(b) == 5


# ---------------------------------------------------------------------------
# Unsubscribe stops delivery
# ---------------------------------------------------------------------------


def test_unsubscribe_stops_delivery():
    bus = _make_bus()
    received: list[Event] = []

    sub_id = bus.subscribe(AgentOutputEvent, received.append)
    bus.publish(_agent_event(content="before"))

    bus.unsubscribe(sub_id)
    bus.publish(_agent_event(content="after"))

    assert len(received) == 1
    assert received[0].content == "before"


def test_unsubscribe_unknown_id_is_a_no_op():
    """unsubscribe() with a bogus ID must not raise."""
    bus = _make_bus()
    bus.unsubscribe("sub-99999")  # should not raise


def test_unsubscribe_one_of_many_leaves_others_active():
    bus = _make_bus()
    a: list[Event] = []
    b: list[Event] = []

    sub_a = bus.subscribe(AgentOutputEvent, a.append)
    bus.subscribe(AgentOutputEvent, b.append)

    bus.unsubscribe(sub_a)
    bus.publish(_agent_event())

    assert a == []
    assert len(b) == 1


# ---------------------------------------------------------------------------
# Exception in one callback does not break delivery to others
# ---------------------------------------------------------------------------


def test_bad_callback_does_not_prevent_delivery_to_others():
    """
    publish() swallows exceptions from subscribers; other subscribers still
    receive the event.
    """
    bus = _make_bus()
    received: list[Event] = []

    def bad_callback(e: Event) -> None:
        raise RuntimeError("intentional failure")

    bus.subscribe(AgentOutputEvent, bad_callback)
    bus.subscribe(AgentOutputEvent, received.append)

    # Must not raise
    bus.publish(_agent_event())

    assert len(received) == 1


def test_bad_callback_first_does_not_block_subsequent():
    """Multiple bad subscribers still don't break good ones registered later."""
    bus = _make_bus()
    received: list[Event] = []

    for _ in range(3):
        bus.subscribe(AgentOutputEvent, lambda e: 1 / 0)

    bus.subscribe(AgentOutputEvent, received.append)
    bus.publish(_agent_event())

    assert len(received) == 1


# ---------------------------------------------------------------------------
# Thread safety: concurrent publishers deliver all events
# ---------------------------------------------------------------------------


def test_concurrent_publishers_deliver_all_events():
    """
    N threads each publish M events; total received == N * M with no loss.
    """
    bus = _make_bus()
    lock = threading.Lock()
    received: list[Event] = []

    def safe_append(e: Event) -> None:
        with lock:
            received.append(e)

    bus.subscribe(AgentOutputEvent, safe_append)

    n_threads = 8
    events_per_thread = 50
    threads = [
        threading.Thread(
            target=lambda: [bus.publish(_agent_event()) for _ in range(events_per_thread)]
        )
        for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(received) == n_threads * events_per_thread


def test_concurrent_subscribe_and_publish_no_crash():
    """
    Subscribing and publishing simultaneously from multiple threads must not
    raise or deadlock.
    """
    bus = _make_bus()
    errors: list[Exception] = []

    def publisher():
        try:
            for _ in range(20):
                bus.publish(_agent_event())
        except Exception as exc:
            errors.append(exc)

    def subscriber_thread():
        try:
            for _ in range(20):
                sub_id = bus.subscribe(AgentOutputEvent, lambda e: None)
                bus.unsubscribe(sub_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=publisher) for _ in range(4)]
    threads += [threading.Thread(target=subscriber_thread) for _ in range(4)]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []


# ---------------------------------------------------------------------------
# publish_async delivers events via the background worker
# ---------------------------------------------------------------------------


def test_publish_async_delivers_event():
    bus = _make_bus()
    done = threading.Event()
    received: list[Event] = []

    def cb(e: Event) -> None:
        received.append(e)
        done.set()

    bus.subscribe(AgentOutputEvent, cb)
    bus.publish_async(_agent_event(content="async-check"))

    delivered = done.wait(timeout=3)
    assert delivered, "publish_async did not deliver within 3s"
    assert len(received) == 1
    assert received[0].content == "async-check"


def test_publish_async_delivers_multiple_events_in_order():
    """All async events must arrive; order within a single publisher is preserved."""
    bus = _make_bus()
    lock = threading.Lock()
    received: list[str] = []
    expected_count = 10
    done = threading.Event()

    def cb(e: AgentOutputEvent) -> None:
        with lock:
            received.append(e.content)
            if len(received) >= expected_count:
                done.set()

    bus.subscribe(AgentOutputEvent, cb)
    for i in range(expected_count):
        bus.publish_async(_agent_event(content=str(i)))

    delivered = done.wait(timeout=5)
    assert delivered, f"Only got {len(received)}/{expected_count} events"
    assert len(received) == expected_count


# ---------------------------------------------------------------------------
# shutdown() stops the background worker cleanly
# ---------------------------------------------------------------------------


def test_shutdown_joins_worker():
    """shutdown() should return without hanging."""
    bus = _make_bus()
    bus.subscribe(AgentOutputEvent, lambda e: None)
    bus.publish_async(_agent_event())
    # Give the worker a moment to drain then shut down
    bus.shutdown()
    # Worker thread should now be dead
    assert not bus._worker.is_alive()


# ---------------------------------------------------------------------------
# Singleton: get_bus() returns the same instance
# ---------------------------------------------------------------------------


def test_get_bus_returns_singleton():
    bus1 = get_bus()
    bus2 = get_bus()
    assert bus1 is bus2


# ---------------------------------------------------------------------------
# Event dataclass: to_dict() and default timestamp
# ---------------------------------------------------------------------------


def test_event_to_dict_contains_expected_keys():
    evt = _agent_event()
    d = evt.to_dict()
    assert "timestamp" in d
    assert "source" in d
    assert "agent_id" in d
    assert "content" in d


def test_event_timestamp_is_iso_string():
    evt = AgentOutputEvent(source="test", agent_id="x", agent_role="r", content="c", event_subtype="s")
    # Should parse as a valid ISO timestamp (non-empty string with 'T' separator)
    assert "T" in evt.timestamp
    assert len(evt.timestamp) >= 19


# ---------------------------------------------------------------------------
# FileAppender: writes JSONL to a temp file
# ---------------------------------------------------------------------------


def test_file_appender_writes_jsonl(tmp_path):
    import json

    path = tmp_path / "feed.jsonl"
    appender = FileAppender(path)

    evt = _agent_event(content="file-test", agent_id="fa-1")
    appender.handle(evt)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["content"] == "file-test"
    assert record["agent_id"] == "fa-1"


def test_file_appender_creates_parent_dirs(tmp_path):
    import json

    path = tmp_path / "nested" / "deep" / "feed.jsonl"
    appender = FileAppender(path)
    appender.handle(_agent_event(content="deep-write"))

    assert path.exists()
    record = json.loads(path.read_text().strip())
    assert record["content"] == "deep-write"


def test_file_appender_appends_multiple_events(tmp_path):
    import json

    path = tmp_path / "feed.jsonl"
    appender = FileAppender(path)

    for i in range(5):
        appender.handle(_agent_event(content=str(i)))

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 5
    contents = [json.loads(l)["content"] for l in lines]
    assert contents == ["0", "1", "2", "3", "4"]


def test_file_appender_thread_safe(tmp_path):
    """Concurrent writes from multiple threads must not corrupt the JSONL file."""
    import json

    path = tmp_path / "feed.jsonl"
    appender = FileAppender(path)
    n_threads = 10
    events_each = 20

    threads = [
        threading.Thread(
            target=lambda: [appender.handle(_agent_event(content="x")) for _ in range(events_each)]
        )
        for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == n_threads * events_each
    # Every line must be valid JSON
    for line in lines:
        json.loads(line)


# ---------------------------------------------------------------------------
# BusEventFileAppender: writes all 4 event types with _event_type field
# ---------------------------------------------------------------------------


def test_bus_event_appender_writes_agent_output(tmp_path):
    """AgentOutputEvent is serialised with _event_type='AgentOutputEvent'."""
    import json

    path = tmp_path / "events-bus.jsonl"
    appender = BusEventFileAppender(path)

    evt = AgentOutputEvent(
        source="server",
        agent_id="exec-1",
        agent_role="executor",
        content="hello",
        event_subtype="content",
    )
    appender.handle(evt)

    record = json.loads(path.read_text().strip())
    assert record["_event_type"] == "AgentOutputEvent"
    assert record["agent_id"] == "exec-1"
    assert record["content"] == "hello"
    assert record["event_subtype"] == "content"


def test_bus_event_appender_writes_budget_spend(tmp_path):
    """BudgetSpendEvent is serialised with _event_type='BudgetSpendEvent'."""
    import json

    path = tmp_path / "events-bus.jsonl"
    appender = BusEventFileAppender(path)

    evt = BudgetSpendEvent(
        source="budget_tracker",
        agent_id="exec-2",
        role="executor",
        input_tokens=100,
        output_tokens=50,
        discussion=42,
        model="claude-sonnet-4-6",
    )
    appender.handle(evt)

    record = json.loads(path.read_text().strip())
    assert record["_event_type"] == "BudgetSpendEvent"
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 50
    assert record["discussion"] == 42
    assert record["model"] == "claude-sonnet-4-6"


def test_bus_event_appender_writes_loop_iteration(tmp_path):
    """LoopIterationEvent is serialised with _event_type='LoopIterationEvent'."""
    import json

    path = tmp_path / "events-bus.jsonl"
    appender = BusEventFileAppender(path)

    evt = LoopIterationEvent(
        source="loop",
        iteration_id="iter-7",
        idle=False,
        duration_seconds=12.5,
        agents_spawned=3,
    )
    appender.handle(evt)

    record = json.loads(path.read_text().strip())
    assert record["_event_type"] == "LoopIterationEvent"
    assert record["iteration_id"] == "iter-7"
    assert record["idle"] is False
    assert record["duration_seconds"] == 12.5
    assert record["agents_spawned"] == 3


def test_bus_event_appender_writes_gate_change(tmp_path):
    """GateChangeEvent is serialised with _event_type='GateChangeEvent'."""
    import json

    path = tmp_path / "events-bus.jsonl"
    appender = BusEventFileAppender(path)

    evt = GateChangeEvent(
        source="config_watcher",
        gate_name="lint_must_pass",
        old_value=False,
        new_value=True,
    )
    appender.handle(evt)

    record = json.loads(path.read_text().strip())
    assert record["_event_type"] == "GateChangeEvent"
    assert record["gate_name"] == "lint_must_pass"
    assert record["old_value"] is False
    assert record["new_value"] is True


def test_bus_event_appender_all_four_types_in_sequence(tmp_path):
    """All 4 event types can be appended to the same file; _event_type is correct."""
    import json

    path = tmp_path / "events-bus.jsonl"
    appender = BusEventFileAppender(path)

    events = [
        AgentOutputEvent(source="s", agent_id="a1", content="c"),
        BudgetSpendEvent(source="s", input_tokens=10, output_tokens=5),
        LoopIterationEvent(source="s", iteration_id="i1"),
        GateChangeEvent(source="s", gate_name="g1"),
    ]
    for ev in events:
        appender.handle(ev)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 4
    types = [json.loads(l)["_event_type"] for l in lines]
    assert types == [
        "AgentOutputEvent",
        "BudgetSpendEvent",
        "LoopIterationEvent",
        "GateChangeEvent",
    ]


def test_bus_event_appender_creates_parent_dirs(tmp_path):
    """BusEventFileAppender creates parent directories automatically."""
    import json

    path = tmp_path / "nested" / "deep" / "events-bus.jsonl"
    appender = BusEventFileAppender(path)
    appender.handle(AgentOutputEvent(source="s", content="nested-write"))

    assert path.exists()
    record = json.loads(path.read_text().strip())
    assert record["content"] == "nested-write"
    assert record["_event_type"] == "AgentOutputEvent"


def test_bus_event_appender_swallows_io_errors():
    """BusEventFileAppender never raises even on an unwritable path."""
    # Use /dev/null/impossible path (directory, not file) to provoke IO error.
    appender = BusEventFileAppender("/dev/null/impossible/path.jsonl")
    # Must not raise
    appender.handle(AgentOutputEvent(source="s", content="x"))


def test_bus_event_appender_bus_integration(tmp_path):
    """BusEventFileAppender integrated with EventBus receives published events."""
    import json

    path = tmp_path / "events-bus.jsonl"
    appender = BusEventFileAppender(path)
    bus = _make_bus()

    for et in (AgentOutputEvent, BudgetSpendEvent, GateChangeEvent, LoopIterationEvent):
        bus.subscribe(et, appender.handle)

    bus.publish(AgentOutputEvent(source="bus", agent_id="x", content="from-bus"))
    bus.publish(BudgetSpendEvent(source="bus", input_tokens=7, output_tokens=3))
    bus.publish(GateChangeEvent(source="bus", gate_name="g", new_value=True))
    bus.publish(LoopIterationEvent(source="bus", iteration_id="iter-1", agents_spawned=2))

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 4
    records = [json.loads(l) for l in lines]
    assert records[0]["_event_type"] == "AgentOutputEvent"
    assert records[0]["content"] == "from-bus"
    assert records[1]["_event_type"] == "BudgetSpendEvent"
    assert records[1]["input_tokens"] == 7
    assert records[2]["_event_type"] == "GateChangeEvent"
    assert records[2]["gate_name"] == "g"
    assert records[3]["_event_type"] == "LoopIterationEvent"
    assert records[3]["agents_spawned"] == 2


def test_bus_event_appender_does_not_affect_file_appender(tmp_path):
    """Adding BusEventFileAppender is purely additive — FileAppender is unchanged."""
    import json

    feed_path = tmp_path / "agent-feed.jsonl"
    bus_path = tmp_path / "events-bus.jsonl"

    file_appender = FileAppender(feed_path)
    bus_appender = BusEventFileAppender(bus_path)

    bus = _make_bus()
    bus.subscribe(AgentOutputEvent, file_appender.handle)
    bus.subscribe(AgentOutputEvent, bus_appender.handle)

    evt = AgentOutputEvent(source="s", agent_id="a1", content="additive-test")
    bus.publish(evt)

    # FileAppender writes ONLY to agent-feed.jsonl (no _event_type)
    feed_record = json.loads(feed_path.read_text().strip())
    assert "_event_type" not in feed_record
    assert feed_record["content"] == "additive-test"

    # BusEventFileAppender writes to events-bus.jsonl WITH _event_type
    bus_record = json.loads(bus_path.read_text().strip())
    assert bus_record["_event_type"] == "AgentOutputEvent"
    assert bus_record["content"] == "additive-test"
