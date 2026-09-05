"""
WebSocket handler for the autonomous-forever API server.

Implements a minimal RFC 6455 WebSocket server using only the Python standard
library (hashlib, base64, struct). Clients receive all event bus events as JSON
text frames and can send JSON commands to subscribe/unsubscribe/ping.

Supported inbound commands:
    {"type": "subscribe",   "events": ["AgentOutputEvent"]}
    {"type": "unsubscribe", "events": ["AgentOutputEvent"]}
    {"type": "ping"}  -> server replies {"type": "pong"}

The handler is invoked from api.py's do_GET when it detects an HTTP Upgrade
to WebSocket on the /ws path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.event_bus import Event


# RFC 6455 magic GUID used in the handshake key derivation.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# WebSocket opcodes
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

# All known event type names — used for subscription filtering.
_ALL_EVENT_TYPES = frozenset(
    ["AgentOutputEvent", "BudgetSpendEvent", "GateChangeEvent", "LoopIterationEvent"]
)

# Heartbeat interval in seconds (requirement: 30s)
_HEARTBEAT_INTERVAL = 30.0


def compute_accept_key(client_key: str) -> str:
    """Derive the Sec-WebSocket-Accept value for the handshake response."""
    raw = (client_key.strip() + _WS_GUID).encode("utf-8")
    digest = hashlib.sha1(raw).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, opcode: int = _OP_TEXT, fin: bool = True) -> bytes:
    """
    Encode *payload* as a WebSocket frame.

    Server-to-client frames are never masked (RFC 6455 §5.1).
    """
    length = len(payload)
    # Byte 0: FIN bit + opcode
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    # Byte 1+: payload length (no mask bit for server frames)
    if length <= 125:
        header = struct.pack("BB", b0, length)
    elif length <= 65535:
        header = struct.pack("!BBH", b0, 126, length)
    else:
        header = struct.pack("!BBQ", b0, 127, length)
    return header + payload


def decode_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """
    Read one WebSocket frame from *sock*.

    Returns (opcode, payload_bytes) or None on connection close/error.
    Client-to-server frames are always masked; we unmask them.
    """
    try:
        # Read first 2 bytes
        header = _recv_exact(sock, 2)
        if header is None:
            return None
        b0, b1 = header[0], header[1]
        # b0: FIN + RSV + opcode
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F

        if length == 126:
            ext = _recv_exact(sock, 2)
            if ext is None:
                return None
            (length,) = struct.unpack("!H", ext)
        elif length == 127:
            ext = _recv_exact(sock, 8)
            if ext is None:
                return None
            (length,) = struct.unpack("!Q", ext)

        mask_key = b""
        if masked:
            mask_key = _recv_exact(sock, 4)
            if mask_key is None:
                return None

        payload = _recv_exact(sock, length)
        if payload is None:
            return None

        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        return opcode, payload

    except (OSError, struct.error):
        return None


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """Read exactly *n* bytes from *sock*, returning None on EOF or error."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


class WebSocketHandler:
    """
    Manages a single WebSocket connection lifecycle.

    Constructed by api.py after HTTP upgrade detection. Runs the receive loop
    in the calling thread; a background thread handles outbound event dispatch.
    """

    def __init__(
        self,
        sock: socket.socket,
        headers: dict[str, str],
        auth_key: str | None,
        query_params: dict[str, str],
        enable_streaming: bool = True,
    ) -> None:
        self._sock = sock
        self._headers = headers
        self._auth_key = auth_key
        self._query_params = query_params
        self._enable_streaming = enable_streaming

        # Active event subscriptions. Default: all event types.
        self._subscribed: set[str] = set(_ALL_EVENT_TYPES)
        self._sub_lock = threading.Lock()

        # Outbound queue — event thread enqueues, send loop dequeues.
        self._outbound: queue.Queue[bytes | None] = queue.Queue()

        # Track last send time for heartbeat.
        self._last_sent = time.monotonic()
        self._last_sent_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def handle(self) -> None:
        """
        Perform handshake then run the connection until close.

        Returns after the connection is fully torn down.
        """
        # Auth check before upgrade
        if self._auth_key:
            token = self._query_params.get("token", "")
            import hmac as _hmac  # noqa: PLC0415
            if not token or not _hmac.compare_digest(token, self._auth_key):
                self._send_http_error(403, "Forbidden")
                return

        ws_key = self._headers.get("Sec-WebSocket-Key", "").strip()
        if not ws_key:
            self._send_http_error(400, "Bad Request: missing Sec-WebSocket-Key")
            return

        accept_key = compute_accept_key(ws_key)
        self._send_handshake_response(accept_key)

        # Subscribe to the event bus and start sender thread
        sub_ids = self._subscribe_bus()
        sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        sender_thread.start()
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        try:
            self._recv_loop()
        finally:
            # Tear down
            for sub_id in sub_ids:
                try:
                    from backend.event_bus import get_bus  # noqa: PLC0415
                    get_bus().unsubscribe(sub_id)
                except Exception:  # noqa: BLE001
                    pass
            # Signal sender to stop
            self._outbound.put(None)
            try:
                self._sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def _send_http_error(self, code: int, message: str) -> None:
        """Send a plain HTTP error response (before WebSocket upgrade)."""
        reason = {403: "Forbidden", 400: "Bad Request", 429: "Too Many Requests"}.get(
            code, "Error"
        )
        response = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(message)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{message}"
        )
        try:
            self._sock.sendall(response.encode("utf-8"))
        except OSError:
            pass

    def _send_handshake_response(self, accept_key: str) -> None:
        """Send the 101 Switching Protocols response to complete the handshake."""
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n"
            "\r\n"
        )
        self._sock.sendall(response.encode("utf-8"))

    # ------------------------------------------------------------------
    # Event bus wiring
    # ------------------------------------------------------------------

    def _subscribe_bus(self) -> list[str]:
        """Subscribe to all event types on the global bus. Returns sub IDs."""
        from backend.event_bus import (  # noqa: PLC0415
            AgentOutputEvent,
            BudgetSpendEvent,
            GateChangeEvent,
            LoopIterationEvent,
            get_bus,
        )
        bus = get_bus()
        sub_ids = [
            bus.subscribe(AgentOutputEvent, self._on_event("AgentOutputEvent")),
            bus.subscribe(BudgetSpendEvent, self._on_event("BudgetSpendEvent")),
            bus.subscribe(GateChangeEvent, self._on_event("GateChangeEvent")),
            bus.subscribe(LoopIterationEvent, self._on_event("LoopIterationEvent")),
        ]
        return sub_ids

    def _on_event(self, event_type_name: str):
        """Return a callback that enqueues an event if the client has subscribed."""
        def _callback(event: "Event") -> None:
            with self._sub_lock:
                subscribed = event_type_name in self._subscribed
            if not subscribed:
                return
            data = event.to_dict()
            data["_event_type"] = event_type_name
            payload = json.dumps(data, default=str).encode("utf-8")
            frame = encode_frame(payload, opcode=_OP_TEXT)
            self._outbound.put(frame)
        return _callback

    # ------------------------------------------------------------------
    # Sender and heartbeat threads
    # ------------------------------------------------------------------

    def _sender_loop(self) -> None:
        """Background thread: drain outbound queue and write to socket."""
        while True:
            try:
                frame = self._outbound.get()
            except Exception:  # noqa: BLE001
                break
            if frame is None:
                break
            try:
                self._sock.sendall(frame)
                with self._last_sent_lock:
                    self._last_sent = time.monotonic()
            except OSError:
                break

    def _heartbeat_loop(self) -> None:
        """Background thread: send heartbeat every 30s if no events were sent."""
        while True:
            time.sleep(1.0)
            with self._last_sent_lock:
                idle_seconds = time.monotonic() - self._last_sent
            if idle_seconds >= _HEARTBEAT_INTERVAL:
                payload = json.dumps({"type": "heartbeat"}).encode("utf-8")
                frame = encode_frame(payload, opcode=_OP_TEXT)
                try:
                    self._outbound.put_nowait(frame)
                except queue.Full:
                    pass

    # ------------------------------------------------------------------
    # Receive loop — command dispatch
    # ------------------------------------------------------------------

    def _recv_loop(self) -> None:
        """Main loop: read frames from client and dispatch commands."""
        while True:
            result = decode_frame(self._sock)
            if result is None:
                break
            opcode, payload = result

            if opcode == _OP_CLOSE:
                # Echo close frame and stop
                self._send_frame(b"", opcode=_OP_CLOSE)
                break

            elif opcode == _OP_PING:
                # RFC 6455 §5.5.3 — respond with pong
                self._send_frame(payload, opcode=_OP_PONG)

            elif opcode == _OP_PONG:
                pass  # unsolicited pong — ignore

            elif opcode == _OP_TEXT:
                self._dispatch_command(payload)

            # Ignore binary, continuation frames

    def _send_frame(self, payload: bytes, opcode: int = _OP_TEXT) -> None:
        """Encode and enqueue a frame for the sender thread."""
        frame = encode_frame(payload, opcode=opcode)
        self._outbound.put(frame)

    def send_json(self, obj: object) -> None:
        """Serialize *obj* as JSON and enqueue as a text frame."""
        payload = json.dumps(obj, default=str).encode("utf-8")
        self._send_frame(payload, opcode=_OP_TEXT)

    def _dispatch_command(self, payload: bytes) -> None:
        """Parse an inbound JSON command and act on it."""
        try:
            cmd = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"type": "error", "message": "invalid JSON"})
            return

        if not isinstance(cmd, dict):
            self.send_json({"type": "error", "message": "expected JSON object"})
            return

        cmd_type = cmd.get("type", "")

        if cmd_type == "ping":
            self.send_json({"type": "pong"})

        elif cmd_type == "subscribe":
            events = cmd.get("events", [])
            if isinstance(events, list):
                with self._sub_lock:
                    for ev in events:
                        if ev in _ALL_EVENT_TYPES:
                            self._subscribed.add(ev)
            self.send_json({"type": "subscribed", "events": list(self._subscribed)})

        elif cmd_type == "unsubscribe":
            events = cmd.get("events", [])
            if isinstance(events, list):
                with self._sub_lock:
                    for ev in events:
                        self._subscribed.discard(ev)
            self.send_json({"type": "unsubscribed", "events": list(self._subscribed)})

        else:
            self.send_json({"type": "error", "message": f"unknown command: {cmd_type!r}"})
