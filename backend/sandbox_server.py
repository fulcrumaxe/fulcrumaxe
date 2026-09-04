"""
sandbox_server.py — lightweight HTTP/SSE server that runs inside the agent
Docker container.

Exposes the structured sandbox protocol:
  GET  /health
  POST /sessions
  GET  /sessions/<id>/events         (SSE stream)
  POST /sessions/<id>/permissions/<perm_id>
  POST /sessions/<id>/ingest         (internal — used by sandbox_entrypoint.sh)
  POST /sessions/<id>/end            (internal — used by sandbox_entrypoint.sh)

Usage:
  python backend/sandbox_server.py [--port 8080]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from backend.sandbox_session import SessionManager
from backend.permission_policy import evaluate as policy_evaluate


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Minimal asyncio HTTP server (stdlib only — no aiohttp/fastapi deps)
# ---------------------------------------------------------------------------

class _Response:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers: dict[str, str] = headers or {}

    @classmethod
    def json(cls, data: Any, status: int = 200) -> "_Response":
        return cls(
            status=status,
            body=json.dumps(data).encode(),
            content_type="application/json",
        )

    @classmethod
    def error(cls, status: int, message: str) -> "_Response":
        return cls.json({"error": message}, status=status)


# ---------------------------------------------------------------------------
# Request router
# ---------------------------------------------------------------------------

class SandboxServer:
    def __init__(self, session_manager: SessionManager) -> None:
        self._sm = session_manager

    async def handle(
        self,
        method: str,
        path: str,
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Dispatch a request and write the HTTP response."""
        parts = [p for p in path.split("/") if p]

        # GET /health
        if method == "GET" and parts == ["health"]:
            resp = _Response.json({
                "status": "ok",
                "session_count": self._sm.session_count,
            })
            await self._send_response(writer, resp)
            return

        # POST /sessions
        if method == "POST" and parts == ["sessions"]:
            await self._create_session(body, writer)
            return

        # /sessions/<id>/...
        if len(parts) >= 2 and parts[0] == "sessions":
            session_id = parts[1]

            # GET /sessions/<id>/events  — SSE stream
            if method == "GET" and parts[2:] == ["events"]:
                await self._stream_events(session_id, writer)
                return

            # POST /sessions/<id>/permissions/<perm_id>
            if method == "POST" and len(parts) == 4 and parts[2] == "permissions":
                perm_id = parts[3]
                await self._resolve_permission(session_id, perm_id, body, writer)
                return

            # POST /sessions/<id>/ingest  (internal)
            if method == "POST" and parts[2:] == ["ingest"]:
                await self._ingest_event(session_id, body, writer)
                return

            # POST /sessions/<id>/end  (internal)
            if method == "POST" and parts[2:] == ["end"]:
                await self._end_session(session_id, body, writer)
                return

        resp = _Response.error(404, "not found")
        await self._send_response(writer, resp)

    # ------------------------------------------------------------------
    # Endpoint handlers
    # ------------------------------------------------------------------

    async def _create_session(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            await self._send_response(writer, _Response.error(400, "invalid JSON"))
            return

        role = data.get("role", "")
        system_prompt = data.get("system_prompt", "")
        working_dir = data.get("working_dir", "/workspace")

        if not role:
            await self._send_response(writer, _Response.error(400, "role is required"))
            return

        session = self._sm.create_session(role, system_prompt, working_dir)
        resp = _Response.json(
            {
                "session_id": session.session_id,
                "stream_url": f"/sessions/{session.session_id}/events",
            },
            status=201,
        )
        await self._send_response(writer, resp)

    async def _stream_events(self, session_id: str, writer: asyncio.StreamWriter) -> None:
        session = self._sm.get_session(session_id)
        if session is None:
            await self._send_response(writer, _Response.error(404, "session not found"))
            return

        # Write SSE response headers
        status_line = b"HTTP/1.1 200 OK\r\n"
        headers = (
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        writer.write(status_line + headers)
        await writer.drain()

        q = session.add_subscriber()
        try:
            while True:
                data = await q.get()
                if data is None:
                    # Session ended — sentinel received
                    break
                writer.write(f"data: {data}\n\n".encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            session.remove_subscriber(q)

    async def _resolve_permission(
        self,
        session_id: str,
        perm_id: str,
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        session = self._sm.get_session(session_id)
        if session is None:
            await self._send_response(writer, _Response.error(404, "session not found"))
            return

        perm = session.permissions.get(perm_id)
        if perm is None:
            await self._send_response(writer, _Response.error(404, "permission not found"))
            return

        if perm.resolved:
            await self._send_response(writer, _Response.error(409, "permission already resolved"))
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            await self._send_response(writer, _Response.error(400, "invalid JSON"))
            return

        decision = data.get("decision")
        if decision not in ("approve", "deny"):
            await self._send_response(writer, _Response.error(400, "decision must be 'approve' or 'deny'"))
            return

        reason = data.get("reason")
        self._sm.resolve_permission(session_id, perm_id, decision, "human", reason)
        await self._send_response(writer, _Response.json({"ok": True}))

    async def _ingest_event(
        self,
        session_id: str,
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Accept a raw NDJSON line from the agent, normalize, check permissions."""
        session = self._sm.get_session(session_id)
        if session is None:
            await self._send_response(writer, _Response.error(404, "session not found"))
            return

        try:
            raw = json.loads(body)
        except json.JSONDecodeError:
            await self._send_response(writer, _Response.error(400, "invalid JSON"))
            return

        event_type = raw.get("type", "")
        item_id = raw.get("item_id", "")
        ts = _now_iso()

        if event_type == "item.started":
            self._sm.add_event(session_id, {
                "type": "item.started",
                "session_id": session_id,
                "item_id": item_id,
                "ts": ts,
                "kind": raw.get("kind", "text"),
            })

        elif event_type == "item.delta":
            self._sm.add_event(session_id, {
                "type": "item.delta",
                "session_id": session_id,
                "item_id": item_id,
                "ts": ts,
                "text": raw.get("text", ""),
            })

        elif event_type == "item.completed":
            tool_name = raw.get("tool", "")
            tool_input = raw.get("input", "")
            kind = raw.get("kind", "text")

            # Gate tool_call events through the permission policy
            if kind == "tool_call" and tool_name:
                decision = policy_evaluate(session.role, tool_name, tool_input)
                if decision == "human-approval":
                    perm_id = self._sm.request_permission(session_id, tool_name, tool_input)
                    # Block until resolved
                    perm = session.permissions[perm_id]
                    resolved_decision = await perm.wait_for_decision()
                    # Emit resolved event (already emitted by resolve_permission)
                    if resolved_decision == "deny":
                        # Skip the tool call — emit completed with denied output
                        self._sm.add_event(session_id, {
                            "type": "item.completed",
                            "session_id": session_id,
                            "item_id": item_id,
                            "ts": _now_iso(),
                            "kind": "tool_result",
                            "tool": tool_name,
                            "input": tool_input,
                            "output": "denied by human",
                        })
                        await self._send_response(writer, _Response.json({"ok": True, "gated": True, "decision": "deny"}))
                        return
                elif decision == "deny":
                    self._sm.add_event(session_id, {
                        "type": "item.completed",
                        "session_id": session_id,
                        "item_id": item_id,
                        "ts": _now_iso(),
                        "kind": "tool_result",
                        "tool": tool_name,
                        "input": tool_input,
                        "output": "denied by policy",
                    })
                    await self._send_response(writer, _Response.json({"ok": True, "gated": True, "decision": "deny"}))
                    return

            self._sm.add_event(session_id, {
                "type": "item.completed",
                "session_id": session_id,
                "item_id": item_id,
                "ts": ts,
                "kind": kind,
                "tool": raw.get("tool", ""),
                "input": raw.get("input", ""),
                "output": raw.get("output", ""),
            })

        else:
            # Pass unknown event types through as-is with session_id and ts
            normalized = {**raw, "session_id": session_id, "ts": ts}
            self._sm.add_event(session_id, normalized)

        await self._send_response(writer, _Response.json({"ok": True}))

    async def _end_session(
        self,
        session_id: str,
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        session = self._sm.get_session(session_id)
        if session is None:
            await self._send_response(writer, _Response.error(404, "session not found"))
            return

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        exit_code = data.get("exit_code", 0)
        self._sm.add_event(session_id, {
            "type": "session.ended",
            "session_id": session_id,
            "ts": _now_iso(),
            "exit_code": exit_code,
        })

        # Send sentinel to all SSE subscribers
        for q in list(session._subscribers):
            q.put_nowait(None)

        await self._send_response(writer, _Response.json({"ok": True}))

    # ------------------------------------------------------------------
    # Low-level HTTP response writer
    # ------------------------------------------------------------------

    async def _send_response(self, writer: asyncio.StreamWriter, resp: _Response) -> None:
        status_texts = {
            200: "OK",
            201: "Created",
            400: "Bad Request",
            404: "Not Found",
            409: "Conflict",
            500: "Internal Server Error",
        }
        status_text = status_texts.get(resp.status, "Unknown")
        headers = {
            "Content-Type": resp.content_type,
            "Content-Length": str(len(resp.body)),
            **resp.headers,
        }
        header_lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        response = (
            f"HTTP/1.1 {resp.status} {status_text}\r\n"
            f"{header_lines}\r\n"
            f"\r\n"
        ).encode() + resp.body
        writer.write(response)
        await writer.drain()


# ---------------------------------------------------------------------------
# Minimal HTTP/1.1 request parser
# ---------------------------------------------------------------------------

async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, bytes] | None:
    """Return (method, path, body) or None on connection close."""
    try:
        request_line = await reader.readline()
    except Exception:
        return None

    if not request_line:
        return None

    try:
        method, raw_path, _ = request_line.decode().strip().split(" ", 2)
    except ValueError:
        return None

    path = urlparse(raw_path).path

    # Read headers
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()

    # Read body if Content-Length specified
    body = b""
    content_length = int(headers.get("content-length", 0))
    if content_length > 0:
        body = await reader.read(content_length)

    return method, path, body


# ---------------------------------------------------------------------------
# Main server loop
# ---------------------------------------------------------------------------

async def _serve(host: str, port: int) -> None:
    sm = SessionManager()
    handler = SandboxServer(sm)

    async def client_connected(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            result = await _read_request(reader)
            if result is None:
                return
            method, path, body = result
            await handler.handle(method, path, body, writer)
        except Exception as exc:
            print(f"[sandbox-server] error: {exc}", file=sys.stderr)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(client_connected, host, port)
    addr = server.sockets[0].getsockname() if server.sockets else (host, port)
    print(f"[sandbox-server] listening on {addr[0]}:{addr[1]}", file=sys.stderr, flush=True)

    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent sandbox HTTP/SSE server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    asyncio.run(_serve(args.host, args.port))


if __name__ == "__main__":
    main()
