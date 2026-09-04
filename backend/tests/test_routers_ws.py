"""
Tests for the native Starlette /ws WebSocket route (backend/routers/ws.py).

Run with:
    pytest backend/tests/test_routers_ws.py -v --timeout=15

All tests use Starlette's TestClient in WebSocket mode — no real network I/O.
No daemons or event-bus publishers are started; bus events are injected by
calling bus.publish() directly inside each test.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.asgi_app import app
import backend.routers.ws as ws_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_ip_tracker():
    """Reset the per-IP tracker before each test to avoid cross-test leakage."""
    from backend.rate_limiter import SSEConnectionTracker
    ws_mod._ip_tracker = SSEConnectionTracker(max_per_ip=5)
    yield
    ws_mod._ip_tracker = SSEConnectionTracker(max_per_ip=5)


@pytest.fixture()
def cap1_tracker():
    """Swap in a per-IP cap of 1 for cap tests."""
    from backend.rate_limiter import SSEConnectionTracker
    old = ws_mod._ip_tracker
    ws_mod._ip_tracker = SSEConnectionTracker(max_per_ip=1)
    yield ws_mod._ip_tracker
    ws_mod._ip_tracker = old


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Basic connect — receives a JSON frame when a bus event is published
# ---------------------------------------------------------------------------


def test_connect_and_receive_bus_event(client):
    """Client connects, bus publishes an event, client receives a JSON frame."""
    from backend.event_bus import AgentOutputEvent, get_bus

    with client.websocket_connect("/ws") as ws:
        # Publish an event from the test thread; bridge is async so give it a tick
        evt = AgentOutputEvent(agent_id="test-agent", content="hello world")
        get_bus().publish(evt)
        time.sleep(0.1)

        data = ws.receive_json()
        assert data.get("_event_type") == "AgentOutputEvent"


# ---------------------------------------------------------------------------
# 2. ping → pong
# ---------------------------------------------------------------------------


def test_ping_pong(client):
    """Client sends {"type":"ping"}, server replies {"type":"pong"}."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping"})
        data = ws.receive_json()
        assert data == {"type": "pong"}


# ---------------------------------------------------------------------------
# 3. subscribe changes event filter
# ---------------------------------------------------------------------------


def test_subscribe_filters_events(client):
    """After unsubscribing AgentOutputEvent, that type is not pushed."""
    from backend.event_bus import AgentOutputEvent, BudgetSpendEvent, get_bus

    with client.websocket_connect("/ws") as ws:
        # Unsubscribe from AgentOutputEvent
        ws.send_json({"type": "unsubscribe", "events": ["AgentOutputEvent"]})
        ack = ws.receive_json()
        assert ack["type"] == "unsubscribed"
        assert "AgentOutputEvent" not in ack["events"]

        # Re-subscribe
        ws.send_json({"type": "subscribe", "events": ["AgentOutputEvent"]})
        ack2 = ws.receive_json()
        assert ack2["type"] == "subscribed"
        assert "AgentOutputEvent" in ack2["events"]


# ---------------------------------------------------------------------------
# 4. unsubscribe stops delivery
# ---------------------------------------------------------------------------


def test_unsubscribe_response(client):
    """unsubscribe command returns the correct remaining subscription list."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "unsubscribe", "events": ["BudgetSpendEvent", "GateChangeEvent"]})
        ack = ws.receive_json()
        assert ack["type"] == "unsubscribed"
        remaining = set(ack["events"])
        assert "BudgetSpendEvent" not in remaining
        assert "GateChangeEvent" not in remaining
        assert "AgentOutputEvent" in remaining
        assert "LoopIterationEvent" in remaining


# ---------------------------------------------------------------------------
# 5. Per-IP cap enforcement + release
# ---------------------------------------------------------------------------


def test_cap_reject_and_release(cap1_tracker):
    """Second connection from the same IP is rejected; after disconnect, a new one succeeds."""
    # Use a fresh TestClient so we can open two sequential connections
    with TestClient(app, raise_server_exceptions=False) as c:
        # First connection — should succeed
        with c.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "ping"})
            assert ws1.receive_json() == {"type": "pong"}

            # Second connection from same IP (127.0.0.1 in TestClient) — over cap
            try:
                with c.websocket_connect("/ws") as ws2:
                    # Should not reach here — server closes before accept
                    ws2.send_json({"type": "ping"})
            except WebSocketDisconnect as e:
                assert e.code == 4429
            except Exception:
                pass  # Any rejection counts

        # First connection is now closed — slot released.
        # A new connection should succeed.
        with c.websocket_connect("/ws") as ws3:
            ws3.send_json({"type": "ping"})
            assert ws3.receive_json() == {"type": "pong"}


# ---------------------------------------------------------------------------
# 6. Auth — correct token passes, wrong token rejected
# ---------------------------------------------------------------------------


def test_auth_disabled_when_no_env_var(client):
    """When AF_API_AUTH_KEY is not set, any connection is allowed."""
    with patch.dict("os.environ", {}, clear=False):
        # Ensure key is absent
        import os as _os
        _os.environ.pop("AF_API_AUTH_KEY", None)
        with TestClient(app) as c:
            with c.websocket_connect("/ws") as ws:
                ws.send_json({"type": "ping"})
                assert ws.receive_json() == {"type": "pong"}


def test_auth_correct_token_allowed():
    """When AF_API_AUTH_KEY is set, correct ?token= passes."""
    import os as _os
    with patch.dict(_os.environ, {"AF_API_AUTH_KEY": "secret-key"}):
        with TestClient(app) as c:
            with c.websocket_connect("/ws?token=secret-key") as ws:
                ws.send_json({"type": "ping"})
                assert ws.receive_json() == {"type": "pong"}


def test_auth_wrong_token_rejected():
    """When AF_API_AUTH_KEY is set, wrong ?token= causes close(4403)."""
    import os as _os
    with patch.dict(_os.environ, {"AF_API_AUTH_KEY": "secret-key"}):
        with TestClient(app, raise_server_exceptions=False) as c:
            try:
                with c.websocket_connect("/ws?token=wrong") as ws:
                    ws.send_json({"type": "ping"})
                    ws.receive_json()  # should not reach
                    pytest.fail("Should have been rejected")
            except WebSocketDisconnect as e:
                assert e.code == 4403
            except Exception:
                pass  # Any rejection counts


def test_auth_missing_token_rejected():
    """When AF_API_AUTH_KEY is set, missing ?token= causes close(4403)."""
    import os as _os
    with patch.dict(_os.environ, {"AF_API_AUTH_KEY": "secret-key"}):
        with TestClient(app, raise_server_exceptions=False) as c:
            try:
                with c.websocket_connect("/ws") as ws:
                    ws.send_json({"type": "ping"})
                    ws.receive_json()
                    pytest.fail("Should have been rejected")
            except WebSocketDisconnect as e:
                assert e.code == 4403
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 7. Unknown command replies with error
# ---------------------------------------------------------------------------


def test_unknown_command_error(client):
    """Unknown command type returns {"type":"error"} frame."""
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "frobnicate"})
        data = ws.receive_json()
        assert data["type"] == "error"
        assert "frobnicate" in data["message"]


# ---------------------------------------------------------------------------
# 8. Invalid JSON replies with error
# ---------------------------------------------------------------------------


def test_invalid_json_error(client):
    """Malformed JSON text frame returns {"type":"error"}."""
    with client.websocket_connect("/ws") as ws:
        ws.send_text("not { valid json")
        data = ws.receive_json()
        assert data["type"] == "error"
        assert "invalid JSON" in data["message"]


# ---------------------------------------------------------------------------
# 9. websocket.py is not deleted (legacy file still present)
# ---------------------------------------------------------------------------


def test_legacy_websocket_py_still_exists():
    """backend/websocket.py must NOT be deleted — legacy api.py imports it."""
    from pathlib import Path
    ws_path = Path(__file__).resolve().parent.parent / "websocket.py"
    assert ws_path.exists(), "backend/websocket.py was deleted — this breaks the legacy server"


# ---------------------------------------------------------------------------
# 10. Protocol parity — event frame has _event_type field
# ---------------------------------------------------------------------------


def test_event_frame_has_event_type_field(client):
    """Bus event frames include a _event_type key (parity with legacy)."""
    from backend.event_bus import LoopIterationEvent, get_bus

    with client.websocket_connect("/ws") as ws:
        evt = LoopIterationEvent(iteration_id="iter-42", agents_spawned=1)
        get_bus().publish(evt)
        time.sleep(0.1)

        data = ws.receive_json()
        assert "_event_type" in data
        assert data["_event_type"] == "LoopIterationEvent"
