"""
Integration tests for sandbox_server.py and sandbox_session.py.

Tests run the SandboxServer handler directly (not over TCP) to avoid
port conflicts in CI.  The _TestClient helper fakes asyncio streams.
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import uuid
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sandbox_session import SessionManager
from sandbox_server import SandboxServer, _Response


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _FakeWriter:
    """Accumulates bytes written so tests can inspect the HTTP response."""

    def __init__(self) -> None:
        self.buffer = b""
        self._closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        pass

    # ---------- helpers ----------

    def _parse(self) -> tuple[int, dict[str, str], bytes]:
        """Return (status_code, headers_dict, body_bytes)."""
        header_part, _, body = self.buffer.partition(b"\r\n\r\n")
        lines = header_part.split(b"\r\n")
        status_code = int(lines[0].split(b" ")[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.strip().decode().lower()] = value.strip().decode()
        return status_code, headers, body

    @property
    def status(self) -> int:
        return self._parse()[0]

    @property
    def headers(self) -> dict[str, str]:
        return self._parse()[1]

    @property
    def json_body(self) -> dict:
        return json.loads(self._parse()[2])

    @property
    def body(self) -> bytes:
        return self._parse()[2]


async def _call(server: SandboxServer, method: str, path: str, body: bytes = b"") -> _FakeWriter:
    writer = _FakeWriter()
    await server.handle(method, path, body, writer)
    return writer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sm() -> SessionManager:
    return SessionManager()


@pytest.fixture
def server(sm: SessionManager) -> SandboxServer:
    return SandboxServer(sm)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(server: SandboxServer) -> None:
    w = await _call(server, "GET", "/health")
    assert w.status == 200
    body = w.json_body
    assert body["status"] == "ok"
    assert "session_count" in body


# ---------------------------------------------------------------------------
# POST /sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_session_returns_201(server: SandboxServer) -> None:
    payload = json.dumps({
        "role": "executor",
        "system_prompt": "You are an agent.",
        "working_dir": "/workspace",
    }).encode()
    w = await _call(server, "POST", "/sessions", payload)
    assert w.status == 201
    body = w.json_body
    assert "session_id" in body
    # session_id must be a valid UUID
    uuid.UUID(body["session_id"])
    assert body["stream_url"] == f"/sessions/{body['session_id']}/events"


@pytest.mark.asyncio
async def test_create_session_missing_role(server: SandboxServer) -> None:
    payload = json.dumps({"system_prompt": "x"}).encode()
    w = await _call(server, "POST", "/sessions", payload)
    assert w.status == 400


@pytest.mark.asyncio
async def test_create_session_invalid_json(server: SandboxServer) -> None:
    w = await _call(server, "POST", "/sessions", b"not-json")
    assert w.status == 400


# ---------------------------------------------------------------------------
# GET /sessions/<id>/events  — SSE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sse_emits_session_started(server: SandboxServer, sm: SessionManager) -> None:
    session = sm.create_session("executor", "", "/workspace")
    sid = session.session_id

    # The SSE handler blocks waiting for events; signal end so it terminates.
    async def _stream_and_end() -> bytes:
        writer = _FakeWriter()
        # Schedule session end after a tiny delay
        async def _end():
            await asyncio.sleep(0.01)
            sm.add_event(sid, {"type": "session.ended", "session_id": sid, "ts": "t", "exit_code": 0})
            for q in list(session._subscribers):
                q.put_nowait(None)
        asyncio.ensure_future(_end())
        await server.handle("GET", f"/sessions/{sid}/events", b"", writer)
        return writer.buffer

    buf = await asyncio.wait_for(_stream_and_end(), timeout=2.0)

    # Parse SSE lines
    text = buf.decode(errors="replace")
    data_lines = [l[len("data: "):] for l in text.splitlines() if l.startswith("data: ")]
    assert len(data_lines) >= 1

    first_event = json.loads(data_lines[0])
    assert first_event["type"] == "session.started"
    assert first_event["session_id"] == sid
    assert "ts" in first_event
    assert "role" in first_event


@pytest.mark.asyncio
async def test_sse_unknown_session_returns_404(server: SandboxServer) -> None:
    w = await _call(server, "GET", "/sessions/does-not-exist/events")
    assert w.status == 404


# ---------------------------------------------------------------------------
# Permission request / resolve lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permission_resolve_approve(server: SandboxServer, sm: SessionManager) -> None:
    session = sm.create_session("executor", "", "/workspace")
    sid = session.session_id
    perm_id = sm.request_permission(sid, "Bash", "rm -rf /tmp/x")

    payload = json.dumps({"decision": "approve"}).encode()
    w = await _call(server, "POST", f"/sessions/{sid}/permissions/{perm_id}", payload)
    assert w.status == 200
    assert w.json_body["ok"] is True

    perm = session.permissions[perm_id]
    assert perm.resolved
    assert perm.decision == "approve"
    assert perm.decided_by == "human"


@pytest.mark.asyncio
async def test_permission_resolve_deny_with_reason(server: SandboxServer, sm: SessionManager) -> None:
    session = sm.create_session("executor", "", "/workspace")
    sid = session.session_id
    perm_id = sm.request_permission(sid, "Bash", "rm -rf /")

    payload = json.dumps({"decision": "deny", "reason": "too dangerous"}).encode()
    w = await _call(server, "POST", f"/sessions/{sid}/permissions/{perm_id}", payload)
    assert w.status == 200

    perm = session.permissions[perm_id]
    assert perm.decision == "deny"
    assert perm.reason == "too dangerous"


@pytest.mark.asyncio
async def test_double_resolve_returns_409(server: SandboxServer, sm: SessionManager) -> None:
    session = sm.create_session("executor", "", "/workspace")
    sid = session.session_id
    perm_id = sm.request_permission(sid, "Bash", "rm -rf /tmp")

    payload = json.dumps({"decision": "approve"}).encode()
    w1 = await _call(server, "POST", f"/sessions/{sid}/permissions/{perm_id}", payload)
    assert w1.status == 200

    w2 = await _call(server, "POST", f"/sessions/{sid}/permissions/{perm_id}", payload)
    assert w2.status == 409


@pytest.mark.asyncio
async def test_permission_unknown_perm_id_returns_404(server: SandboxServer, sm: SessionManager) -> None:
    session = sm.create_session("executor", "", "/workspace")
    sid = session.session_id
    payload = json.dumps({"decision": "approve"}).encode()
    w = await _call(server, "POST", f"/sessions/{sid}/permissions/no-such-perm", payload)
    assert w.status == 404


@pytest.mark.asyncio
async def test_permission_unknown_session_returns_404(server: SandboxServer) -> None:
    payload = json.dumps({"decision": "approve"}).encode()
    w = await _call(server, "POST", "/sessions/ghost/permissions/p1", payload)
    assert w.status == 404


# ---------------------------------------------------------------------------
# session.started includes required fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_started_event_fields(sm: SessionManager) -> None:
    session = sm.create_session("executor", "sys", "/w")
    assert len(session.events) == 1
    ev = session.events[0]
    assert ev["type"] == "session.started"
    assert ev["session_id"] == session.session_id
    assert "ts" in ev
    assert ev["role"] == "executor"


# ---------------------------------------------------------------------------
# session.ended via /end internal endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_end_session(server: SandboxServer, sm: SessionManager) -> None:
    session = sm.create_session("executor", "", "/workspace")
    sid = session.session_id

    payload = json.dumps({"exit_code": 0}).encode()
    w = await _call(server, "POST", f"/sessions/{sid}/end", payload)
    assert w.status == 200

    # session.ended event should be in the event list
    ended_events = [e for e in session.events if e["type"] == "session.ended"]
    assert len(ended_events) == 1
    assert ended_events[0]["exit_code"] == 0
    assert "ts" in ended_events[0]
    assert "session_id" in ended_events[0]


# ---------------------------------------------------------------------------
# 404 for unknown routes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_route_returns_404(server: SandboxServer) -> None:
    w = await _call(server, "GET", "/nonexistent")
    assert w.status == 404
