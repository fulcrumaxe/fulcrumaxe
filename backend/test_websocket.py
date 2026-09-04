"""
Unit tests for backend/websocket.py.

Tests cover:
1. Handshake accept-key computation
2. Frame encoding (text, ping, close)
3. Frame decoding (masked client frames)
4. Subscribe command filtering
5. Unsubscribe command filtering
6. Ping -> pong response
7. Auth rejection (missing token)
8. Auth rejection (wrong token)
9. Unknown command error
10. Heartbeat framing (send_json)

Run with: python -m pytest backend/test_websocket.py -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helper: mask payload as a client would (RFC 6455)
# ---------------------------------------------------------------------------

def _mask_payload(payload: bytes, mask_key: bytes) -> bytes:
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


def _make_client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Encode a masked client WebSocket frame (FIN=1, masked=1)."""
    mask_key = b"\x37\xfa\x21\x3d"
    masked = _mask_payload(payload, mask_key)
    length = len(payload)
    if length <= 125:
        header = struct.pack("BB", 0x80 | opcode, 0x80 | length)
    elif length <= 65535:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    return header + mask_key + masked


# ---------------------------------------------------------------------------
# 1. Handshake accept-key computation
# ---------------------------------------------------------------------------

class TestComputeAcceptKey(unittest.TestCase):
    def test_rfc_example(self):
        """RFC 6455 §1.3 gives a known test vector."""
        from backend.websocket import compute_accept_key
        # Known vector from RFC 6455
        client_key = "dGhlIHNhbXBsZSBub25jZQ=="
        expected = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        self.assertEqual(compute_accept_key(client_key), expected)

    def test_strips_whitespace(self):
        """Key with surrounding whitespace should still produce correct result."""
        from backend.websocket import compute_accept_key
        client_key = "  dGhlIHNhbXBsZSBub25jZQ==  "
        expected = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        self.assertEqual(compute_accept_key(client_key), expected)


# ---------------------------------------------------------------------------
# 2. Frame encoding
# ---------------------------------------------------------------------------

class TestEncodeFrame(unittest.TestCase):
    def test_small_text_frame(self):
        """Payload <= 125 bytes uses 1-byte length."""
        from backend.websocket import encode_frame, _OP_TEXT
        payload = b"hello"
        frame = encode_frame(payload, opcode=_OP_TEXT)
        self.assertEqual(frame[0], 0x80 | 0x1)  # FIN + text
        self.assertEqual(frame[1], 5)            # length, no mask bit
        self.assertEqual(frame[2:], payload)

    def test_medium_text_frame(self):
        """Payload 126-65535 bytes uses 3-byte length prefix."""
        from backend.websocket import encode_frame, _OP_TEXT
        payload = b"x" * 200
        frame = encode_frame(payload, opcode=_OP_TEXT)
        self.assertEqual(frame[0], 0x80 | 0x1)
        self.assertEqual(frame[1], 126)
        (length,) = struct.unpack("!H", frame[2:4])
        self.assertEqual(length, 200)
        self.assertEqual(frame[4:], payload)

    def test_close_frame(self):
        """Close opcode is encoded correctly."""
        from backend.websocket import encode_frame, _OP_CLOSE
        frame = encode_frame(b"", opcode=_OP_CLOSE)
        self.assertEqual(frame[0], 0x80 | 0x8)
        self.assertEqual(frame[1], 0)


# ---------------------------------------------------------------------------
# 3. Frame decoding
# ---------------------------------------------------------------------------

class TestDecodeFrame(unittest.TestCase):
    def _make_sock(self, data: bytes) -> socket.socket:
        """Return a mock socket whose recv reads from *data*."""
        mock = MagicMock(spec=socket.socket)
        buf = bytearray(data)

        def fake_recv(n):
            chunk = bytes(buf[:n])
            del buf[:n]
            return chunk

        mock.recv.side_effect = fake_recv
        return mock

    def test_decode_masked_text_frame(self):
        from backend.websocket import decode_frame, _OP_TEXT
        payload = b"hello world"
        frame = _make_client_frame(payload, opcode=0x1)
        sock = self._make_sock(frame)
        result = decode_frame(sock)
        self.assertIsNotNone(result)
        opcode, decoded = result
        self.assertEqual(opcode, _OP_TEXT)
        self.assertEqual(decoded, payload)

    def test_decode_close_frame(self):
        from backend.websocket import decode_frame, _OP_CLOSE
        frame = _make_client_frame(b"", opcode=0x8)
        sock = self._make_sock(frame)
        result = decode_frame(sock)
        self.assertIsNotNone(result)
        opcode, _ = result
        self.assertEqual(opcode, _OP_CLOSE)

    def test_decode_returns_none_on_eof(self):
        from backend.websocket import decode_frame
        mock = MagicMock(spec=socket.socket)
        mock.recv.return_value = b""
        self.assertIsNone(decode_frame(mock))


# ---------------------------------------------------------------------------
# 4 & 5. Subscribe / unsubscribe filtering
# ---------------------------------------------------------------------------

class TestSubscribeUnsubscribe(unittest.TestCase):
    def _make_handler(self):
        from backend.websocket import WebSocketHandler
        mock_sock = MagicMock(spec=socket.socket)
        handler = WebSocketHandler(
            sock=mock_sock,
            headers={},
            auth_key=None,
            query_params={},
        )
        return handler

    def test_default_subscribed_all(self):
        """Handler starts subscribed to all known event types."""
        from backend.websocket import _ALL_EVENT_TYPES
        handler = self._make_handler()
        self.assertEqual(handler._subscribed, set(_ALL_EVENT_TYPES))

    def test_subscribe_command_adds_type(self):
        """subscribe command updates _subscribed set."""
        handler = self._make_handler()
        handler._subscribed = set()  # start empty
        sent = []
        handler.send_json = lambda obj: sent.append(obj)

        handler._dispatch_command(
            json.dumps({"type": "subscribe", "events": ["AgentOutputEvent"]}).encode()
        )
        self.assertIn("AgentOutputEvent", handler._subscribed)
        self.assertEqual(sent[0]["type"], "subscribed")

    def test_unsubscribe_command_removes_type(self):
        """unsubscribe removes the specified event type."""
        handler = self._make_handler()
        sent = []
        handler.send_json = lambda obj: sent.append(obj)

        handler._dispatch_command(
            json.dumps({"type": "unsubscribe", "events": ["BudgetSpendEvent"]}).encode()
        )
        self.assertNotIn("BudgetSpendEvent", handler._subscribed)
        self.assertEqual(sent[0]["type"], "unsubscribed")

    def test_unknown_event_type_ignored(self):
        """Subscribing to an unknown event type is silently dropped."""
        handler = self._make_handler()
        before = set(handler._subscribed)
        sent = []
        handler.send_json = lambda obj: sent.append(obj)

        handler._dispatch_command(
            json.dumps({"type": "subscribe", "events": ["NonExistentEvent"]}).encode()
        )
        self.assertEqual(handler._subscribed, before)

    def test_on_event_respects_filter(self):
        """_on_event callback skips enqueue when type is not subscribed."""
        handler = self._make_handler()
        handler._subscribed = {"AgentOutputEvent"}
        enqueued = []
        handler._outbound.put = lambda x: enqueued.append(x)

        from backend.event_bus import BudgetSpendEvent
        cb = handler._on_event("BudgetSpendEvent")
        event = BudgetSpendEvent(source="test", agent_id="a1", role="executor")
        cb(event)

        self.assertEqual(len(enqueued), 0)

    def test_on_event_enqueues_when_subscribed(self):
        """_on_event callback enqueues when type is subscribed."""
        handler = self._make_handler()
        handler._subscribed = {"AgentOutputEvent"}
        enqueued = []
        original_put = handler._outbound.put
        handler._outbound.put = lambda x: enqueued.append(x)

        from backend.event_bus import AgentOutputEvent
        cb = handler._on_event("AgentOutputEvent")
        event = AgentOutputEvent(source="test", agent_id="a1", content="hi")
        cb(event)

        self.assertEqual(len(enqueued), 1)


# ---------------------------------------------------------------------------
# 6. Ping -> pong
# ---------------------------------------------------------------------------

class TestPingPong(unittest.TestCase):
    def test_ping_returns_pong(self):
        from backend.websocket import WebSocketHandler
        mock_sock = MagicMock(spec=socket.socket)
        handler = WebSocketHandler(
            sock=mock_sock,
            headers={},
            auth_key=None,
            query_params={},
        )
        sent = []
        handler.send_json = lambda obj: sent.append(obj)

        handler._dispatch_command(json.dumps({"type": "ping"}).encode())

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], {"type": "pong"})


# ---------------------------------------------------------------------------
# 7 & 8. Auth rejection
# ---------------------------------------------------------------------------

class TestAuthRejection(unittest.TestCase):
    def _make_handler(self, auth_key, token=None):
        from backend.websocket import WebSocketHandler
        mock_sock = MagicMock(spec=socket.socket)
        mock_sock.sendall = MagicMock()
        query_params = {}
        if token is not None:
            query_params["token"] = token
        handler = WebSocketHandler(
            sock=mock_sock,
            headers={"Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="},
            auth_key=auth_key,
            query_params=query_params,
        )
        return handler, mock_sock

    def test_missing_token_sends_403(self):
        """No token when auth is required returns 403."""
        handler, sock = self._make_handler(auth_key="secret")
        handler.handle()
        calls = [call.args[0].decode() for call in sock.sendall.call_args_list]
        self.assertTrue(any("403" in c for c in calls))

    def test_wrong_token_sends_403(self):
        """Wrong token returns 403."""
        handler, sock = self._make_handler(auth_key="secret", token="wrong")
        handler.handle()
        calls = [call.args[0].decode() for call in sock.sendall.call_args_list]
        self.assertTrue(any("403" in c for c in calls))

    def test_correct_token_proceeds_past_auth(self):
        """Correct token does NOT produce a 403 response."""
        handler, sock = self._make_handler(auth_key="secret", token="secret")
        # We don't run the full event loop — just check auth passes (no 403 sent).
        # Patch the rest so handle() exits quickly.
        with patch.object(handler, "_send_handshake_response"), \
             patch.object(handler, "_subscribe_bus", return_value=[]), \
             patch.object(handler, "_recv_loop"):
            handler.handle()
        calls = [call.args[0].decode() for call in sock.sendall.call_args_list]
        self.assertFalse(any("403" in c for c in calls))


# ---------------------------------------------------------------------------
# 9. Unknown command error
# ---------------------------------------------------------------------------

class TestUnknownCommand(unittest.TestCase):
    def test_unknown_command_returns_error(self):
        from backend.websocket import WebSocketHandler
        mock_sock = MagicMock(spec=socket.socket)
        handler = WebSocketHandler(
            sock=mock_sock, headers={}, auth_key=None, query_params={}
        )
        sent = []
        handler.send_json = lambda obj: sent.append(obj)

        handler._dispatch_command(json.dumps({"type": "explode"}).encode())

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "error")
        self.assertIn("explode", sent[0]["message"])


# ---------------------------------------------------------------------------
# 10. Heartbeat / send_json framing
# ---------------------------------------------------------------------------

class TestSendJson(unittest.TestCase):
    def test_send_json_enqueues_text_frame(self):
        """send_json encodes object as a valid WebSocket text frame."""
        from backend.websocket import WebSocketHandler, _OP_TEXT
        mock_sock = MagicMock(spec=socket.socket)
        handler = WebSocketHandler(
            sock=mock_sock, headers={}, auth_key=None, query_params={}
        )
        handler.send_json({"type": "heartbeat"})
        frame = handler._outbound.get_nowait()

        # Verify FIN + text opcode
        self.assertEqual(frame[0], 0x80 | _OP_TEXT)
        # Decode and check payload
        length = frame[1]  # small payload, fits in 1 byte
        payload = frame[2:2 + length]
        obj = json.loads(payload.decode("utf-8"))
        self.assertEqual(obj, {"type": "heartbeat"})


if __name__ == "__main__":
    unittest.main()
