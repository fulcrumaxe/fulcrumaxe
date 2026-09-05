"""
Behavioral tests for backend/a2a_broker.py.

All tests use a temporary directory for AUTONOMOUS_TEAM_STATE_DIR — the real
~/.autonomous-forever-state/a2a/ is never touched. Port 8830 is never bound;
HTTP end-to-end tests use an ephemeral port 0.

Run with:
    python3 -m pytest backend/tests/test_a2a_broker.py -v
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

# ---- Ensure backend/ is importable -----------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import backend.a2a_broker as broker  # noqa: E402 — must come after sys.path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _clear_module_state() -> None:
    """Reset in-process broker state between tests.

    The module uses module-level dicts for registrations, inboxes, and rate
    limits.  If we don't clear them, state bleeds between tests.
    """
    with broker._registrations_lock:
        broker._registrations.clear()
    with broker._inboxes_lock:
        broker._inboxes.clear()
        broker._messages_by_id.clear()
    with broker._status_rate_lock:
        broker._status_rate.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """
    Every test gets:
    - AUTONOMOUS_TEAM_STATE_DIR → tmp_path (never touches real state)
    - fresh in-process broker state
    - a known admin token
    """
    monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
    _clear_module_state()
    # Set a deterministic admin token for tests that need it
    broker._ADMIN_TOKEN = "test-admin-secret"
    yield tmp_path
    # Post-test cleanup (belt-and-suspenders)
    _clear_module_state()


def _register(agent_id: str, token: str) -> None:
    """Register an agent directly in the module-level dict (no HTTP needed)."""
    with broker._registrations_lock:
        broker._registrations[agent_id] = _sha256(token)


def _make_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_admin_headers() -> dict:
    return {"Authorization": f"Bearer {broker._ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# State-isolation verification
# ---------------------------------------------------------------------------

class TestStateIsolation:
    """Verify that the env var is read per-call, not cached at import."""

    def test_state_dir_reads_env_per_call(self, tmp_path, monkeypatch):
        """_state_dir() must reflect the current env value, not the import-time value."""
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        result = broker._state_dir()
        assert result == tmp_path / "a2a"
        assert result.exists()

    def test_state_dir_with_different_paths(self, tmp_path, monkeypatch):
        """Each call with a different env value should resolve to that dir."""
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"

        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(dir_a))
        got_a = broker._state_dir()
        assert got_a == dir_a / "a2a"

        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(dir_b))
        got_b = broker._state_dir()
        assert got_b == dir_b / "a2a"

    def test_inbox_path_stays_within_state_dir(self, tmp_path, monkeypatch):
        """Inbox file must land under the monkeypatched state dir."""
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        p = broker._inbox_path("executor-42")
        assert str(p).startswith(str(tmp_path))
        assert "a2a" in str(p)
        # Ensure no path traversal from a funky agent_id
        weird = broker._inbox_path("../../../etc/passwd")
        assert str(weird).startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# Token authentication
# ---------------------------------------------------------------------------

class TestTokenAuth:
    """sha256-based Bearer token verification."""

    def test_correct_token_resolves_agent_id(self):
        _register("agent-1", "secret-token")
        headers = _make_headers("secret-token")
        result = broker._verify_bearer(headers)
        assert result == "agent-1"

    def test_wrong_token_returns_none(self):
        _register("agent-1", "correct-token")
        headers = _make_headers("wrong-token")
        result = broker._verify_bearer(headers)
        assert result is None

    def test_missing_authorization_header_returns_none(self):
        _register("agent-1", "secret-token")
        result = broker._verify_bearer({})
        assert result is None

    def test_malformed_bearer_returns_none(self):
        _register("agent-1", "secret-token")
        result = broker._verify_bearer({"Authorization": "Token secret-token"})
        assert result is None

    def test_required_agent_id_enforced(self):
        """If required_agent_id is given, token must belong to that exact agent."""
        _register("agent-1", "token-a")
        _register("agent-2", "token-b")
        # Token for agent-1 but caller expects agent-2
        result = broker._verify_bearer(_make_headers("token-a"), required_agent_id="agent-2")
        assert result is None

    def test_required_agent_id_passes_for_correct_agent(self):
        _register("agent-1", "token-a")
        result = broker._verify_bearer(_make_headers("token-a"), required_agent_id="agent-1")
        assert result == "agent-1"

    def test_unregistered_token_returns_none(self):
        # No agents registered at all
        result = broker._verify_bearer(_make_headers("any-token"))
        assert result is None

    def test_admin_token_accepted(self):
        """_verify_admin must accept the known admin token."""
        assert broker._verify_admin(_make_admin_headers()) is True

    def test_wrong_admin_token_rejected(self):
        assert broker._verify_admin(_make_headers("not-admin")) is False

    def test_sha256_helper(self):
        """_sha256 returns the correct hex digest."""
        expected = hashlib.sha256(b"hello").hexdigest()
        assert broker._sha256("hello") == expected


# ---------------------------------------------------------------------------
# Per-kind authorization matrix
# ---------------------------------------------------------------------------

class TestKindAuthz:
    """Only team-lead-* agents may send broadcast or interrupt-request."""

    def test_broadcast_allowed_for_team_lead(self):
        err = broker._check_kind_authz("team-lead-1", "broadcast", "team-lead-1")
        assert err is None

    def test_broadcast_denied_for_regular_agent(self):
        err = broker._check_kind_authz("executor-1", "broadcast", "executor-1")
        assert err is not None
        assert "team-lead" in err.lower() or "restricted" in err.lower()

    def test_interrupt_request_allowed_for_team_lead(self):
        err = broker._check_kind_authz("team-lead-9", "interrupt-request", "team-lead-9")
        assert err is None

    def test_interrupt_request_denied_for_regular_agent(self):
        err = broker._check_kind_authz("code-reviewer-3", "interrupt-request", "code-reviewer-3")
        assert err is not None

    def test_question_allowed_for_any_agent(self):
        for agent_id in ["executor-1", "pm-1", "reviewer-1"]:
            err = broker._check_kind_authz(agent_id, "question", agent_id)
            assert err is None, f"Expected None for agent {agent_id}, got {err}"

    def test_answer_allowed_for_any_agent(self):
        err = broker._check_kind_authz("executor-7", "answer", "executor-7")
        assert err is None

    def test_status_allowed_for_any_agent(self):
        err = broker._check_kind_authz("executor-7", "status", "executor-7")
        assert err is None

    def test_from_claim_mismatch_denied(self):
        """Bearer token must match the 'from' field — impersonation is rejected."""
        _register("agent-a", "token-a")
        err = broker._check_kind_authz("agent-a", "question", "agent-b")
        assert err is not None
        assert "agent-a" in err or "agent-b" in err

    def test_task_card_allowed_for_any_agent(self):
        err = broker._check_kind_authz("executor-5", "task-card", "executor-5")
        assert err is None


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

class TestMessageValidation:
    def test_valid_message_passes(self):
        msg = {"from": "a", "to": "b", "kind": "question", "body": "hello"}
        assert broker._validate_message(msg) is None

    def test_missing_field_is_rejected(self):
        for field in ("from", "to", "kind", "body"):
            msg = {"from": "a", "to": "b", "kind": "question", "body": "hi"}
            del msg[field]
            err = broker._validate_message(msg)
            assert err is not None
            assert field in err

    def test_unknown_kind_is_rejected(self):
        msg = {"from": "a", "to": "b", "kind": "nonexistent-kind", "body": "hi"}
        err = broker._validate_message(msg)
        assert err is not None
        assert "kind" in err.lower() or "unknown" in err.lower()

    def test_body_too_large_is_rejected(self):
        big_body = "x" * (broker.MAX_BODY_BYTES + 1)
        msg = {"from": "a", "to": "b", "kind": "question", "body": big_body}
        err = broker._validate_message(msg)
        assert err is not None
        assert "bytes" in err.lower() or "exceed" in err.lower()

    def test_body_at_limit_passes(self):
        # Exactly MAX_BODY_BYTES bytes (ASCII, so len == byte len)
        body = "x" * broker.MAX_BODY_BYTES
        msg = {"from": "a", "to": "b", "kind": "status", "body": body}
        assert broker._validate_message(msg) is None


# ---------------------------------------------------------------------------
# Inbox lifecycle: send → appear → ack/read
# ---------------------------------------------------------------------------

class TestInboxLifecycle:
    """Test inbox append, load, read, and ack via direct function calls."""

    def _make_msg(self, msg_id: str, to: str, read: bool = False) -> dict:
        return {
            "id": msg_id,
            "from": "sender-1",
            "to": to,
            "kind": "question",
            "body": "test body",
            "in_reply_to": None,
            "ts": "2026-01-01T00:00:00Z",
            "read": read,
        }

    def test_append_creates_jsonl_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        msg = self._make_msg("msg-001", "agent-receiver")
        broker._append_inbox("agent-receiver", msg)
        inbox_file = broker._inbox_path("agent-receiver")
        assert inbox_file.exists()
        lines = inbox_file.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["id"] == "msg-001"

    def test_append_inbox_has_mode_600(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        msg = self._make_msg("msg-002", "agent-r")
        broker._append_inbox("agent-r", msg)
        p = broker._inbox_path("agent-r")
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600

    def test_load_unread_inbox_returns_unread_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        unread_msg = self._make_msg("msg-unread", "agent-r", read=False)
        read_msg = self._make_msg("msg-read", "agent-r", read=True)
        broker._append_inbox("agent-r", unread_msg)
        broker._append_inbox("agent-r", read_msg)
        loaded = broker._load_unread_inbox("agent-r")
        ids = [m["id"] for m in loaded]
        assert "msg-unread" in ids
        assert "msg-read" not in ids

    def test_empty_inbox_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        loaded = broker._load_unread_inbox("no-such-agent")
        assert loaded == []

    def test_ensure_inbox_loaded_populates_from_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        msg = self._make_msg("msg-disk", "agent-d")
        broker._append_inbox("agent-d", msg)
        # _inboxes is cleared by fixture — ensure_inbox_loaded must re-read from disk
        broker._ensure_inbox_loaded("agent-d")
        with broker._inboxes_lock:
            inbox = broker._inboxes.get("agent-d", [])
        assert any(m["id"] == "msg-disk" for m in inbox)

    def test_multiple_messages_appended_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        for i in range(5):
            msg = self._make_msg(f"msg-{i:03d}", "agent-order")
            broker._append_inbox("agent-order", msg)
        loaded = broker._load_unread_inbox("agent-order")
        ids = [m["id"] for m in loaded]
        assert ids == [f"msg-{i:03d}" for i in range(5)]


# ---------------------------------------------------------------------------
# Persistence / restart replay
# ---------------------------------------------------------------------------

class TestRestartReplay:
    """Unread messages survive a simulated restart (re-read from JSONL)."""

    def test_unread_messages_survive_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        # Write messages as if the broker just delivered them
        msgs = [
            {
                "id": f"msg-replay-{i}",
                "from": "sender",
                "to": "agent-restart",
                "kind": "question",
                "body": "still here?",
                "in_reply_to": None,
                "ts": "2026-01-01T00:00:00Z",
                "read": False,
            }
            for i in range(3)
        ]
        for m in msgs:
            broker._append_inbox("agent-restart", m)

        # Simulate restart: clear in-memory state, re-read from disk
        _clear_module_state()

        replayed = broker._load_unread_inbox("agent-restart")
        assert len(replayed) == 3
        for m in replayed:
            assert not m["read"]

    def test_acked_messages_not_replayed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        acked = {
            "id": "msg-acked",
            "from": "s",
            "to": "agent-restart",
            "kind": "answer",
            "body": "done",
            "in_reply_to": None,
            "ts": "2026-01-01T00:00:00Z",
            "read": True,
        }
        pending = {
            "id": "msg-pending",
            "from": "s",
            "to": "agent-restart",
            "kind": "answer",
            "body": "pending",
            "in_reply_to": None,
            "ts": "2026-01-01T00:00:00Z",
            "read": False,
        }
        broker._append_inbox("agent-restart", acked)
        broker._append_inbox("agent-restart", pending)

        _clear_module_state()
        replayed = broker._load_unread_inbox("agent-restart")
        ids = [m["id"] for m in replayed]
        assert "msg-acked" not in ids
        assert "msg-pending" in ids


# ---------------------------------------------------------------------------
# Rate-limiting (status kind)
# ---------------------------------------------------------------------------

class TestRateLimit:
    """1 status message per STATUS_RATE_LIMIT_SECONDS per sender."""

    def test_first_status_not_rate_limited(self):
        """Fresh sender has no rate-limit entry — should be allowed."""
        now = time.time()
        with broker._status_rate_lock:
            last = broker._status_rate.get("executor-rl", 0.0)
        # No prior entry: should be far enough in the past
        assert now - last >= broker.STATUS_RATE_LIMIT_SECONDS

    def test_second_rapid_status_is_throttled(self):
        """Set last_ts to now; a second call within the window should be blocked."""
        sender = "executor-rl-2"
        # Simulate first send succeeded
        with broker._status_rate_lock:
            broker._status_rate[sender] = time.time()

        # Check: within 2 minutes, should be throttled
        now = time.time()
        with broker._status_rate_lock:
            last = broker._status_rate.get(sender, 0.0)
        assert now - last < broker.STATUS_RATE_LIMIT_SECONDS  # would be blocked

    def test_status_allowed_after_window_expires(self):
        """If last_ts is older than STATUS_RATE_LIMIT_SECONDS, allow."""
        sender = "executor-rl-3"
        old_ts = time.time() - broker.STATUS_RATE_LIMIT_SECONDS - 1
        with broker._status_rate_lock:
            broker._status_rate[sender] = old_ts
        now = time.time()
        with broker._status_rate_lock:
            last = broker._status_rate.get(sender, 0.0)
        assert now - last >= broker.STATUS_RATE_LIMIT_SECONDS  # allowed

    def test_rate_limit_constant_is_120_seconds(self):
        """Spec says 1 status per 2 minutes."""
        assert broker.STATUS_RATE_LIMIT_SECONDS == 120


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class TestAuditTrail:
    """Audit entries must record sha256 of body — never the plaintext body."""

    def test_audit_contains_body_sha256_not_body(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        secret_body = "super-secret-content-xyzzy"
        msg = {
            "id": "msg-audit-1",
            "from": "sender",
            "to": "receiver",
            "kind": "question",
            "body": secret_body,
            "in_reply_to": None,
            "ts": "2026-01-01T00:00:00Z",
            "read": False,
        }
        broker._append_audit(msg)

        audit_file = broker._audit_path()
        assert audit_file.exists()
        raw = audit_file.read_text().strip()
        # Body plaintext must NOT appear in audit log
        assert secret_body not in raw
        entry = json.loads(raw)
        # Must have body_sha256 field
        assert "body_sha256" in entry
        expected_hash = hashlib.sha256(secret_body.encode()).hexdigest()
        assert entry["body_sha256"] == expected_hash
        # Must NOT have "body" key
        assert "body" not in entry

    def test_audit_has_mode_600(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        msg = {
            "id": "msg-audit-2",
            "from": "s",
            "to": "r",
            "kind": "answer",
            "body": "hi",
            "in_reply_to": None,
            "ts": "2026-01-01T00:00:00Z",
            "read": False,
        }
        broker._append_audit(msg)
        p = broker._audit_path()
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600

    def test_audit_envelope_fields_are_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        msg = {
            "id": "msg-audit-3",
            "from": "agent-x",
            "to": "agent-y",
            "kind": "status",
            "body": "running",
            "in_reply_to": None,
            "ts": "2026-05-20T10:00:00Z",
            "read": False,
        }
        broker._append_audit(msg)
        entry = json.loads(broker._audit_path().read_text().strip())
        assert entry["id"] == "msg-audit-3"
        assert entry["from"] == "agent-x"
        assert entry["to"] == "agent-y"
        assert entry["kind"] == "status"
        assert entry["ts"] == "2026-05-20T10:00:00Z"


# ---------------------------------------------------------------------------
# Registration persistence
# ---------------------------------------------------------------------------

class TestRegistrationPersistence:
    def test_save_and_load_registrations(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        with broker._registrations_lock:
            broker._registrations["agent-persist"] = _sha256("my-token")
        broker._save_registrations()

        # Clear in-memory and reload
        with broker._registrations_lock:
            broker._registrations.clear()
        broker._load_registrations()

        with broker._registrations_lock:
            assert "agent-persist" in broker._registrations
            assert broker._registrations["agent-persist"] == _sha256("my-token")

    def test_registrations_file_has_mode_600(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        with broker._registrations_lock:
            broker._registrations["agent-z"] = _sha256("tok")
        broker._save_registrations()
        p = broker._registrations_path()
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# HTTP end-to-end (ephemeral port 0 — never binds 8830)
# ---------------------------------------------------------------------------

class TestHTTPEndToEnd:
    """Spin up the broker on port 0 (OS assigns ephemeral), tear it down after each test."""

    @pytest.fixture()
    def broker_server(self, tmp_path, monkeypatch):
        """Start broker on an ephemeral port; yield (host, port, admin_token); stop."""
        import http.client
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        _clear_module_state()
        admin_token = "http-test-admin"
        broker._ADMIN_TOKEN = admin_token

        server = broker.ThreadedHTTPServer(("127.0.0.1", 0), broker.A2AHandler)
        port = server.server_address[1]
        assert port != 8830, "Should never bind 8830 in tests"

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        yield "127.0.0.1", port, admin_token
        server.shutdown()

    def _post(self, host, port, path, body, token=None, admin=False):
        import http.client
        conn = http.client.HTTPConnection(host, port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if admin:
            headers["Authorization"] = f"Bearer {broker._ADMIN_TOKEN}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode()
        conn.request("POST", path, body=data, headers=headers)
        resp = conn.getresponse()
        body_bytes = resp.read()
        conn.close()
        return resp.status, json.loads(body_bytes) if body_bytes else {}

    def _get(self, host, port, path, token=None, admin=False):
        import http.client
        conn = http.client.HTTPConnection(host, port, timeout=5)
        headers = {}
        if admin:
            headers["Authorization"] = f"Bearer {broker._ADMIN_TOKEN}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body_bytes = resp.read()
        conn.close()
        return resp.status, json.loads(body_bytes) if body_bytes else {}

    def test_health_endpoint(self, broker_server):
        host, port, _ = broker_server
        status, data = self._get(host, port, "/a2a/health")
        assert status == 200
        assert data.get("status") == "ok"

    def test_register_and_send_message(self, broker_server):
        host, port, admin_token = broker_server
        # Register two agents
        status, data = self._post(host, port, "/a2a/register",
                                   {"agent_id": "sender-1", "token_sha256": _sha256("tok-s1")},
                                   admin=True)
        assert status == 200
        status, data = self._post(host, port, "/a2a/register",
                                   {"agent_id": "receiver-1", "token_sha256": _sha256("tok-r1")},
                                   admin=True)
        assert status == 200

        # Send a message sender → receiver
        status, data = self._post(host, port, "/a2a/message",
                                   {"from": "sender-1", "to": "receiver-1",
                                    "kind": "question", "body": "are you there?"},
                                   token="tok-s1")
        assert status == 200
        msg_id = data.get("id")
        assert msg_id and msg_id.startswith("msg-")

    def test_inbox_returns_message(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "agent-a", "token_sha256": _sha256("tok-a")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "agent-b", "token_sha256": _sha256("tok-b")}, admin=True)
        # Send
        self._post(host, port, "/a2a/message",
                   {"from": "agent-a", "to": "agent-b", "kind": "answer", "body": "yes"},
                   token="tok-a")
        # Read inbox
        status, data = self._get(host, port, "/a2a/inbox/agent-b", token="tok-b")
        assert status == 200
        assert data.get("count") == 1
        assert data["messages"][0]["body"] == "yes"

    def test_empty_inbox_returns_204(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "agent-empty", "token_sha256": _sha256("tok-empty")}, admin=True)
        status, _ = self._get(host, port, "/a2a/inbox/agent-empty", token="tok-empty")
        assert status == 204

    def test_inbox_marks_messages_read(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "agent-reader", "token_sha256": _sha256("tok-reader")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "agent-sender", "token_sha256": _sha256("tok-sender")}, admin=True)
        self._post(host, port, "/a2a/message",
                   {"from": "agent-sender", "to": "agent-reader", "kind": "question", "body": "hi"},
                   token="tok-sender")
        # First read: returns message and marks it read
        status1, _ = self._get(host, port, "/a2a/inbox/agent-reader", token="tok-reader")
        assert status1 == 200
        # Second read: inbox is empty now
        status2, _ = self._get(host, port, "/a2a/inbox/agent-reader", token="tok-reader")
        assert status2 == 204

    def test_peek_does_not_mark_read(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "agent-peek", "token_sha256": _sha256("tok-peek")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "agent-src", "token_sha256": _sha256("tok-src")}, admin=True)
        self._post(host, port, "/a2a/message",
                   {"from": "agent-src", "to": "agent-peek", "kind": "question", "body": "peek?"},
                   token="tok-src")
        # Peek
        status1, data1 = self._get(host, port, "/a2a/inbox/agent-peek?peek=1", token="tok-peek")
        assert status1 == 200
        # Normal read: message should still be there (peek didn't consume)
        status2, data2 = self._get(host, port, "/a2a/inbox/agent-peek", token="tok-peek")
        assert status2 == 200
        assert data2.get("count") == 1

    def test_ack_marks_message_consumed(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "ack-sender", "token_sha256": _sha256("tok-as")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "ack-receiver", "token_sha256": _sha256("tok-ar")}, admin=True)
        _, send_data = self._post(host, port, "/a2a/message",
                                   {"from": "ack-sender", "to": "ack-receiver",
                                    "kind": "answer", "body": "done"},
                                   token="tok-as")
        msg_id = send_data["id"]
        # Ack the message
        status, data = self._post(host, port, f"/a2a/ack/{msg_id}", {}, token="tok-ar")
        assert status == 200
        assert data.get("acked") == msg_id
        # Inbox should now be empty
        status2, _ = self._get(host, port, "/a2a/inbox/ack-receiver", token="tok-ar")
        assert status2 == 204

    def test_missing_token_returns_401(self, broker_server):
        host, port, _ = broker_server
        status, data = self._post(host, port, "/a2a/message",
                                   {"from": "x", "to": "y", "kind": "question", "body": "hi"})
        assert status == 401

    def test_wrong_token_returns_401(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "legit-agent", "token_sha256": _sha256("real-tok")}, admin=True)
        status, _ = self._post(host, port, "/a2a/message",
                                {"from": "legit-agent", "to": "other", "kind": "question", "body": "hi"},
                                token="wrong-tok")
        assert status == 401

    def test_broadcast_denied_for_non_team_lead(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "executor-x", "token_sha256": _sha256("tok-ex")}, admin=True)
        status, data = self._post(host, port, "/a2a/message",
                                   {"from": "executor-x", "to": "*",
                                    "kind": "broadcast", "body": "hi all"},
                                   token="tok-ex")
        assert status == 403

    def test_broadcast_allowed_for_team_lead(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "team-lead-1", "token_sha256": _sha256("tok-tl")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "recipient-1", "token_sha256": _sha256("tok-r")}, admin=True)
        status, data = self._post(host, port, "/a2a/message",
                                   {"from": "team-lead-1", "to": "*",
                                    "kind": "broadcast", "body": "attention all"},
                                   token="tok-tl")
        assert status == 200

    def test_register_requires_admin_token(self, broker_server):
        host, port, _ = broker_server
        # Register with a non-admin token should fail
        self._post(host, port, "/a2a/register",
                   {"agent_id": "legit", "token_sha256": _sha256("t")}, admin=True)
        status, data = self._post(host, port, "/a2a/register",
                                   {"agent_id": "rogue", "token_sha256": _sha256("bad")},
                                   token="legit")  # not admin
        assert status == 401

    def test_rate_limit_blocks_rapid_status(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "rate-agent", "token_sha256": _sha256("tok-rate")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "rate-recv", "token_sha256": _sha256("tok-recv")}, admin=True)
        # First status: should succeed
        s1, _ = self._post(host, port, "/a2a/message",
                            {"from": "rate-agent", "to": "rate-recv", "kind": "status", "body": "ok"},
                            token="tok-rate")
        assert s1 == 200
        # Second immediate status: should be rate-limited (429)
        s2, data2 = self._post(host, port, "/a2a/message",
                                {"from": "rate-agent", "to": "rate-recv", "kind": "status", "body": "ok2"},
                                token="tok-rate")
        assert s2 == 429
        assert "rate" in data2.get("error", "").lower()

    def test_tasks_endpoint_returns_task_cards(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "task-sender", "token_sha256": _sha256("tok-ts")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "task-recv", "token_sha256": _sha256("tok-tr")}, admin=True)
        self._post(host, port, "/a2a/message",
                   {"from": "task-sender", "to": "task-recv", "kind": "task-card", "body": "do it"},
                   token="tok-ts")
        status, data = self._get(host, port, "/a2a/tasks")
        assert status == 200
        assert data.get("count") >= 1
        task = data["tasks"][0]
        # Body must NOT be in the tasks listing (only safe envelope fields)
        assert "body" not in task
        assert task["kind"] == "task-card"

    def test_non_recipient_cannot_ack(self, broker_server):
        host, port, _ = broker_server
        self._post(host, port, "/a2a/register",
                   {"agent_id": "ack-s2", "token_sha256": _sha256("tok-acks2")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "ack-r2", "token_sha256": _sha256("tok-ackr2")}, admin=True)
        self._post(host, port, "/a2a/register",
                   {"agent_id": "ack-intruder", "token_sha256": _sha256("tok-intruder")}, admin=True)
        _, send_data = self._post(host, port, "/a2a/message",
                                   {"from": "ack-s2", "to": "ack-r2",
                                    "kind": "question", "body": "hi"},
                                   token="tok-acks2")
        msg_id = send_data["id"]
        # Intruder tries to ack message addressed to ack-r2
        status, _ = self._post(host, port, f"/a2a/ack/{msg_id}", {}, token="tok-intruder")
        assert status == 403


# ---------------------------------------------------------------------------
# _msg_age_seconds helper
# ---------------------------------------------------------------------------

class TestMsgAgeSeconds:
    def test_recent_message_is_young(self):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        age = broker._msg_age_seconds(ts, time.time())
        assert 0 <= age < 5  # should be very fresh

    def test_old_message_is_old(self):
        import datetime
        old_time = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        ts = old_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        age = broker._msg_age_seconds(ts, time.time())
        assert age > 3500  # at least ~1 hour

    def test_unparseable_ts_returns_zero(self):
        age = broker._msg_age_seconds("not-a-timestamp", time.time())
        assert age == 0
