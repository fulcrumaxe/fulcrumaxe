"""
Tests for backend/a2a_broker.py

Covers:
- Schema validation (missing fields, oversize body, unknown kind)
- Authorization matrix (per-kind restrictions, from-claim vs token mismatch)
- Loopback enforcement
- Persistence replay (unread messages survive restart)
- Rate limiting on status kind
- Empty inbox returns 204
- Ack workflow
- Broadcast delivery to all registered agents
- Per-kind authz: broadcast/interrupt-request only for team-lead-*
"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Ensure backend/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# D#1810 round 3: this used to unconditionally set
# os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = "" here at module import time (no
# saved original, no fixture, never restored). Under the old frozen-constant
# state_paths design that was harmless — nothing re-read the env after
# import. Now that state_paths resolves at access time, an empty string is
# falsy and strips AUTONOMOUS_TEAM_STATE_DIR for the rest of the pytest
# session, tripping state_paths.UnsandboxedStatePathError in every
# downstream test file that doesn't set its own override. Removed: almost
# every test/fixture in this file already sets AUTONOMOUS_TEAM_STATE_DIR
# itself via monkeypatch.setenv() (see the `broker` fixture and most of
# TestRPCHandlers) before touching anything backend-related, so this line
# mostly did nothing useful. The one exception,
# TestRPCHandlers::test_a2a_active_broker_down, never touched a
# STATE_DIR-derived path at all (it only patches A2A_PORT and asserts on an
# unreachable-broker response), so it was never relying on this line either.


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def broker(tmp_path, monkeypatch):
    """Spin up a broker on a free port; yield (base_url, admin_token, register_fn); stop after test."""
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))

    # Reload module to pick up env var
    if "backend.a2a_broker" in sys.modules:
        del sys.modules["backend.a2a_broker"]
    if "backend" in sys.modules:
        # Don't del backend — it breaks other imports
        pass

    import importlib
    import backend.a2a_broker as broker_mod
    importlib.reload(broker_mod)

    # Reset in-process state
    broker_mod._registrations.clear()
    broker_mod._inboxes.clear()
    broker_mod._messages_by_id.clear()
    broker_mod._status_rate.clear()
    broker_mod._sse_connection_count = 0

    # Find free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    admin_token = "test-admin-secret"
    broker_mod._ADMIN_TOKEN = admin_token

    server = broker_mod.ThreadedHTTPServer(("127.0.0.1", port), broker_mod.A2AHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    base = f"http://127.0.0.1:{port}"

    def register(agent_id: str, token: str) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        req = urllib.request.Request(
            f"{base}/a2a/register",
            data=json.dumps({"agent_id": agent_id, "token_sha256": token_hash}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {admin_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

    yield base, admin_token, register

    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post_message(base: str, token: str, payload: dict) -> tuple[int, dict]:
    try:
        req = urllib.request.Request(
            f"{base}/a2a/message",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get_inbox(base: str, token: str, agent_id: str, peek: bool = False) -> tuple[int, dict | None]:
    url = f"{base}/a2a/inbox/{agent_id}"
    if peek:
        url += "?peek=1"
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 204:
                return 204, None
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Tests: schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:

    def test_missing_field_rejected(self, broker):
        base, admin_token, register = broker
        register("agent-a", "token-a")
        code, data = post_message(base, "token-a", {"from": "agent-a", "to": "agent-b", "kind": "status"})
        assert code == 400
        assert "body" in data["error"]

    def test_unknown_kind_rejected(self, broker):
        base, admin_token, register = broker
        register("agent-a", "token-a")
        code, data = post_message(base, "token-a", {
            "from": "agent-a", "to": "agent-b", "kind": "telepathy", "body": "hi"
        })
        assert code == 400
        assert "kind" in data["error"].lower()

    def test_oversize_body_rejected(self, broker):
        base, admin_token, register = broker
        register("agent-a", "token-a")
        big_body = "x" * 3000  # > 2048 bytes
        code, data = post_message(base, "token-a", {
            "from": "agent-a", "to": "agent-b", "kind": "question", "body": big_body
        })
        assert code == 400
        assert "2048" in data["error"] or "exceed" in data["error"].lower()

    def test_valid_message_accepted(self, broker):
        base, admin_token, register = broker
        register("agent-a", "token-a")
        register("agent-b", "token-b")
        code, data = post_message(base, "token-a", {
            "from": "agent-a", "to": "agent-b", "kind": "question", "body": "What should I do?"
        })
        assert code == 200
        assert data["id"].startswith("msg-")


# ---------------------------------------------------------------------------
# Tests: authorization
# ---------------------------------------------------------------------------

class TestAuthorization:

    def test_invalid_token_rejected(self, broker):
        base, admin_token, register = broker
        code, data = post_message(base, "bad-token", {
            "from": "agent-a", "to": "agent-b", "kind": "status", "body": "hi"
        })
        assert code == 401

    def test_from_mismatch_rejected(self, broker):
        """Token belongs to agent-a but from claims to be agent-x."""
        base, admin_token, register = broker
        register("agent-a", "token-a")
        code, data = post_message(base, "token-a", {
            "from": "agent-x", "to": "agent-b", "kind": "status", "body": "impersonation"
        })
        assert code == 403

    def test_broadcast_from_non_team_lead_rejected(self, broker):
        base, admin_token, register = broker
        register("executor-abc", "token-exec")
        code, data = post_message(base, "token-exec", {
            "from": "executor-abc", "to": "*", "kind": "broadcast", "body": "hello"
        })
        assert code == 403

    def test_broadcast_from_team_lead_accepted(self, broker):
        base, admin_token, register = broker
        register("team-lead-session-123", "token-tl")
        code, data = post_message(base, "token-tl", {
            "from": "team-lead-session-123", "to": "*", "kind": "broadcast", "body": "all hands"
        })
        assert code == 200

    def test_interrupt_request_from_executor_rejected(self, broker):
        base, admin_token, register = broker
        register("executor-xyz", "token-exec")
        code, data = post_message(base, "token-exec", {
            "from": "executor-xyz", "to": "executor-abc", "kind": "interrupt-request", "body": "stop"
        })
        assert code == 403

    def test_interrupt_request_from_team_lead_accepted(self, broker):
        base, admin_token, register = broker
        register("team-lead-session-1", "token-tl1")
        code, data = post_message(base, "token-tl1", {
            "from": "team-lead-session-1", "to": "executor-abc", "kind": "interrupt-request", "body": "abort"
        })
        assert code == 200


# ---------------------------------------------------------------------------
# Tests: inbox / empty inbox 204
# ---------------------------------------------------------------------------

class TestInbox:

    def test_empty_inbox_returns_204(self, broker):
        base, admin_token, register = broker
        register("agent-b", "token-b")
        code, data = get_inbox(base, "token-b", "agent-b")
        assert code == 204
        assert data is None

    def test_message_delivered_to_inbox(self, broker):
        base, admin_token, register = broker
        register("sender", "tok-s")
        register("recipient", "tok-r")
        post_message(base, "tok-s", {
            "from": "sender", "to": "recipient", "kind": "status", "body": "working"
        })
        code, data = get_inbox(base, "tok-r", "recipient")
        assert code == 200
        assert data["count"] == 1
        assert data["messages"][0]["body"] == "working"

    def test_messages_marked_read_after_fetch(self, broker):
        base, admin_token, register = broker
        register("sender", "tok-s")
        register("recipient", "tok-r")
        post_message(base, "tok-s", {
            "from": "sender", "to": "recipient", "kind": "status", "body": "once"
        })
        # First fetch — returns message
        code, data = get_inbox(base, "tok-r", "recipient")
        assert code == 200
        # Second fetch — inbox empty
        code2, data2 = get_inbox(base, "tok-r", "recipient")
        assert code2 == 204

    def test_peek_does_not_mark_read(self, broker):
        base, admin_token, register = broker
        register("sender", "tok-s")
        register("recipient", "tok-r")
        post_message(base, "tok-s", {
            "from": "sender", "to": "recipient", "kind": "status", "body": "peek me"
        })
        code, data = get_inbox(base, "tok-r", "recipient", peek=True)
        assert code == 200
        # peek should not consume — still readable
        code2, data2 = get_inbox(base, "tok-r", "recipient")
        assert code2 == 200
        assert data2["count"] == 1

    def test_wrong_token_inbox_rejected(self, broker):
        base, admin_token, register = broker
        register("agent-b", "tok-b")
        code, data = get_inbox(base, "wrong-token", "agent-b")
        assert code == 401


# ---------------------------------------------------------------------------
# Tests: ack
# ---------------------------------------------------------------------------

class TestAck:

    def test_explicit_ack(self, broker):
        base, admin_token, register = broker
        register("sender", "tok-s")
        register("recipient", "tok-r")
        _, send_data = post_message(base, "tok-s", {
            "from": "sender", "to": "recipient", "kind": "question", "body": "ack me"
        })
        msg_id = send_data["id"]

        req = urllib.request.Request(
            f"{base}/a2a/ack/{msg_id}",
            data=b"",
            headers={"Authorization": "Bearer tok-r"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ack_data = json.loads(resp.read())
        assert ack_data["acked"] == msg_id

        # After ack, inbox should be empty
        code, _ = get_inbox(base, "tok-r", "recipient")
        assert code == 204


# ---------------------------------------------------------------------------
# Tests: broadcast
# ---------------------------------------------------------------------------

class TestBroadcast:

    def test_broadcast_delivered_to_all(self, broker):
        base, admin_token, register = broker
        register("team-lead-session-99", "tok-tl")
        register("exec-1", "tok-e1")
        register("exec-2", "tok-e2")
        register("exec-3", "tok-e3")

        post_message(base, "tok-tl", {
            "from": "team-lead-session-99", "to": "*", "kind": "broadcast", "body": "all eyes here"
        })

        for tok, eid in [("tok-e1", "exec-1"), ("tok-e2", "exec-2"), ("tok-e3", "exec-3")]:
            code, data = get_inbox(base, tok, eid)
            assert code == 200, f"{eid} didn't receive broadcast"
            assert data["count"] >= 1


# ---------------------------------------------------------------------------
# Tests: rate limiting
# ---------------------------------------------------------------------------

class TestRateLimit:

    def test_status_rate_limited(self, broker):
        base, admin_token, register = broker
        register("agent-r", "tok-r")
        register("other", "tok-o")

        # First status should succeed
        code1, _ = post_message(base, "tok-r", {
            "from": "agent-r", "to": "other", "kind": "status", "body": "first"
        })
        assert code1 == 200

        # Second immediate status should be rate-limited
        code2, data2 = post_message(base, "tok-r", {
            "from": "agent-r", "to": "other", "kind": "status", "body": "second"
        })
        assert code2 == 429


# ---------------------------------------------------------------------------
# Tests: persistence replay
# ---------------------------------------------------------------------------

class TestPersistenceReplay:

    def test_unread_messages_survive_reload(self, broker, tmp_path, monkeypatch):
        """Write a message, then reload broker state from JSONL — message still present."""
        base, admin_token, register = broker
        register("sender", "tok-s")
        register("recipient", "tok-r")
        post_message(base, "tok-s", {
            "from": "sender", "to": "recipient", "kind": "status", "body": "survive me"
        })

        # Simulate reload: clear in-memory inbox and reload from JSONL
        import backend.a2a_broker as bmod
        with bmod._inboxes_lock:
            bmod._inboxes.pop("recipient", None)
            bmod._messages_by_id.clear()

        code, data = get_inbox(base, "tok-r", "recipient")
        assert code == 200
        assert data["count"] >= 1
        assert data["messages"][0]["body"] == "survive me"


# ---------------------------------------------------------------------------
# Tests: loopback enforcement
# ---------------------------------------------------------------------------

class TestLoopbackEnforcement:

    def test_health_endpoint_responds(self, broker):
        base, _, _ = broker
        with urllib.request.urlopen(f"{base}/a2a/health", timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["status"] == "ok"

    def test_tasks_endpoint_responds(self, broker):
        base, _, _ = broker
        with urllib.request.urlopen(f"{base}/a2a/tasks", timeout=5) as resp:
            data = json.loads(resp.read())
        assert "tasks" in data


# ---------------------------------------------------------------------------
# Tests: tasks endpoint
# ---------------------------------------------------------------------------

class TestTasks:

    def test_task_card_appears_in_tasks(self, broker):
        base, admin_token, register = broker
        register("team-lead-session-5", "tok-tl5")
        register("exec-t", "tok-et")

        post_message(base, "tok-tl5", {
            "from": "team-lead-session-5",
            "to": "exec-t",
            "kind": "task-card",
            "body": json.dumps({"step": "implementing AC#1", "progress": 30}),
        })

        with urllib.request.urlopen(f"{base}/a2a/tasks", timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["count"] >= 1
        kinds = [t["kind"] for t in data["tasks"]]
        assert "task-card" in kinds
        # body must be stripped — tasks endpoint is unauthenticated
        for t in data["tasks"]:
            assert "body" not in t, "tasks endpoint must not return message body"
            for field in ("id", "from", "to", "kind", "ts"):
                assert field in t, f"expected field {field!r} missing from task"


# ---------------------------------------------------------------------------
# Tests: RPC handlers (unit level — no live broker needed)
# ---------------------------------------------------------------------------

class TestRPCHandlers:

    def test_a2a_tail_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        if "backend.rpc.a2a_tail" in sys.modules:
            del sys.modules["backend.rpc.a2a_tail"]
        if "backend.state_paths" in sys.modules:
            del sys.modules["backend.state_paths"]
        import importlib
        import backend.rpc.a2a_tail as tail_mod
        importlib.reload(tail_mod)
        result = tail_mod.handle({})
        assert result["count"] == 0
        assert result["entries"] == []

    def test_a2a_active_broker_down(self, monkeypatch):
        """a2a_active.handle returns empty result when broker is unreachable."""
        monkeypatch.setenv("A2A_PORT", "19999")  # nothing listening
        if "backend.rpc.a2a_active" in sys.modules:
            del sys.modules["backend.rpc.a2a_active"]
        import importlib
        import backend.rpc.a2a_active as active_mod
        importlib.reload(active_mod)
        result = active_mod.handle({})
        assert result["count"] == 0
        assert result.get("broker_unreachable") is True
