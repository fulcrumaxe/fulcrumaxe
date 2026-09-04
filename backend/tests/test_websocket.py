"""
Tests for backend/websocket.py

RFC 6455 frame encode/decode, handshake, and command dispatch.
All tests use MagicMock sockets — no real network I/O.

Run with:
    pytest backend/tests/test_websocket.py -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.websocket import (
    WebSocketHandler,
    _OP_BINARY,
    _OP_CLOSE,
    _OP_PING,
    _OP_PONG,
    _OP_TEXT,
    _WS_GUID,
    compute_accept_key,
    decode_frame,
    encode_frame,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_payload(payload: bytes, mask_key: bytes) -> bytes:
    """Apply WebSocket masking to payload bytes."""
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


def _build_client_frame(
    payload: bytes,
    opcode: int = _OP_TEXT,
    fin: bool = True,
    masked: bool = True,
) -> bytes:
    """Build a client-to-server WebSocket frame (masked)."""
    mask_key = b"\x01\x02\x03\x04"
    length = len(payload)
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    mask_bit = 0x80 if masked else 0x00

    if length <= 125:
        header = struct.pack("BB", b0, mask_bit | length)
    elif length <= 65535:
        header = struct.pack("!BBH", b0, mask_bit | 126, length)
    else:
        header = struct.pack("!BBQ", b0, mask_bit | 127, length)

    if masked:
        masked_payload = _mask_payload(payload, mask_key)
        return header + mask_key + masked_payload
    return header + payload


def _make_recv_socket(data: bytes) -> MagicMock:
    """
    Return a mock socket whose recv() parcels out `data` one chunk at a time.

    Each call to recv(n) returns min(n, remaining) bytes.
    """
    pos = [0]

    def _recv(n: int) -> bytes:
        start = pos[0]
        chunk = data[start : start + n]
        pos[0] += len(chunk)
        return chunk

    sock = MagicMock()
    sock.recv.side_effect = _recv
    return sock


# ---------------------------------------------------------------------------
# compute_accept_key
# ---------------------------------------------------------------------------


class TestComputeAcceptKey:
    def test_rfc6455_example(self):
        # From RFC 6455 section 1.3
        client_key = "dGhlIHNhbXBsZSBub25jZQ=="
        expected = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        assert compute_accept_key(client_key) == expected

    def test_strips_whitespace(self):
        # Leading/trailing whitespace should be ignored
        client_key = "  dGhlIHNhbXBsZSBub25jZQ==  "
        expected = compute_accept_key("dGhlIHNhbXBsZSBub25jZQ==")
        assert compute_accept_key(client_key) == expected

    def test_deterministic(self):
        key = "abc123=="
        assert compute_accept_key(key) == compute_accept_key(key)

    def test_uses_sha1_and_guid(self):
        key = "test-key"
        raw = (key + _WS_GUID).encode("utf-8")
        expected = base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")
        assert compute_accept_key(key) == expected


# ---------------------------------------------------------------------------
# encode_frame
# ---------------------------------------------------------------------------


class TestEncodeFrame:
    def test_text_frame_short(self):
        payload = b"hello"
        frame = encode_frame(payload, opcode=_OP_TEXT)
        # First byte: FIN=1, opcode=1 -> 0x81
        assert frame[0] == 0x81
        # Second byte: no mask, length=5
        assert frame[1] == 5
        assert frame[2:] == payload

    def test_binary_frame(self):
        payload = b"\x00\x01\x02"
        frame = encode_frame(payload, opcode=_OP_BINARY)
        assert frame[0] == 0x82  # FIN + BINARY
        assert frame[1] == 3
        assert frame[2:] == payload

    def test_close_frame_empty(self):
        frame = encode_frame(b"", opcode=_OP_CLOSE)
        assert frame[0] == 0x88  # FIN + CLOSE
        assert frame[1] == 0

    def test_frame_exactly_125_bytes(self):
        payload = b"x" * 125
        frame = encode_frame(payload)
        assert frame[1] == 125  # short length encoding
        assert len(frame) == 2 + 125

    def test_frame_126_bytes_uses_extended_16bit(self):
        payload = b"y" * 126
        frame = encode_frame(payload)
        assert frame[1] == 126  # trigger 16-bit length
        (length,) = struct.unpack("!H", frame[2:4])
        assert length == 126
        assert frame[4:] == payload

    def test_frame_65536_bytes_uses_extended_64bit(self):
        payload = b"z" * 65536
        frame = encode_frame(payload)
        assert frame[1] == 127  # trigger 64-bit length
        (length,) = struct.unpack("!Q", frame[2:10])
        assert length == 65536
        assert frame[10:] == payload

    def test_fin_false(self):
        payload = b"frag"
        frame = encode_frame(payload, fin=False)
        # FIN bit should be 0
        assert frame[0] & 0x80 == 0

    def test_server_frames_not_masked(self):
        frame = encode_frame(b"data")
        # Mask bit in second byte must be 0
        assert frame[1] & 0x80 == 0


# ---------------------------------------------------------------------------
# decode_frame
# ---------------------------------------------------------------------------


class TestDecodeFrame:
    def test_masked_text_frame(self):
        payload = b"Hello"
        frame_data = _build_client_frame(payload, opcode=_OP_TEXT)
        sock = _make_recv_socket(frame_data)
        result = decode_frame(sock)
        assert result is not None
        opcode, decoded_payload = result
        assert opcode == _OP_TEXT
        assert decoded_payload == payload

    def test_unmasked_frame(self):
        # Unmasked client frame (mask bit clear)
        payload = b"raw"
        frame_data = _build_client_frame(payload, opcode=_OP_TEXT, masked=False)
        sock = _make_recv_socket(frame_data)
        result = decode_frame(sock)
        assert result is not None
        opcode, decoded_payload = result
        assert opcode == _OP_TEXT
        assert decoded_payload == payload

    def test_close_frame(self):
        frame_data = _build_client_frame(b"", opcode=_OP_CLOSE)
        sock = _make_recv_socket(frame_data)
        result = decode_frame(sock)
        assert result is not None
        opcode, _ = result
        assert opcode == _OP_CLOSE

    def test_ping_frame(self):
        frame_data = _build_client_frame(b"ping-body", opcode=_OP_PING)
        sock = _make_recv_socket(frame_data)
        result = decode_frame(sock)
        assert result is not None
        opcode, payload = result
        assert opcode == _OP_PING
        assert payload == b"ping-body"

    def test_returns_none_on_empty_data(self):
        sock = MagicMock()
        sock.recv.return_value = b""
        result = decode_frame(sock)
        assert result is None

    def test_returns_none_on_truncated_header(self):
        # Only 1 byte available — truncated
        sock = _make_recv_socket(b"\x81")
        result = decode_frame(sock)
        assert result is None

    def test_extended_16bit_payload(self):
        payload = b"a" * 200
        frame_data = _build_client_frame(payload, opcode=_OP_TEXT)
        sock = _make_recv_socket(frame_data)
        result = decode_frame(sock)
        assert result is not None
        opcode, decoded_payload = result
        assert opcode == _OP_TEXT
        assert decoded_payload == payload


# ---------------------------------------------------------------------------
# WebSocketHandler.handle — handshake and error paths
# ---------------------------------------------------------------------------


class TestWebSocketHandlerHandshake:
    def _make_handler(
        self,
        ws_key: str = "dGhlIHNhbXBsZSBub25jZQ==",
        auth_key: str | None = None,
        token: str = "",
    ) -> tuple[WebSocketHandler, MagicMock]:
        sock = MagicMock()
        # recv will return empty bytes to immediately terminate recv_loop
        sock.recv.return_value = b""
        headers = {}
        if ws_key:
            headers["Sec-WebSocket-Key"] = ws_key
        query_params = {}
        if token:
            query_params["token"] = token
        handler = WebSocketHandler(
            sock=sock,
            headers=headers,
            auth_key=auth_key,
            query_params=query_params,
            enable_streaming=False,
        )
        return handler, sock

    def test_missing_ws_key_sends_400(self):
        handler, sock = self._make_handler(ws_key="")
        # get_bus is imported lazily inside the method, patch at the source module
        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            mock_bus.subscribe.return_value = "sub-id"
            handler.handle()

        sent = b"".join(c.args[0] for c in sock.sendall.call_args_list)
        assert b"400" in sent

    def test_valid_key_sends_101(self):
        handler, sock = self._make_handler()
        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            mock_bus.subscribe.return_value = "sub-id"
            handler.handle()

        sent = b"".join(c.args[0] for c in sock.sendall.call_args_list)
        assert b"101 Switching Protocols" in sent

    def test_auth_wrong_token_sends_403(self):
        handler, sock = self._make_handler(auth_key="secret", token="wrong")
        handler.handle()

        sent = b"".join(c.args[0] for c in sock.sendall.call_args_list)
        assert b"403" in sent

    def test_auth_correct_token_proceeds(self):
        handler, sock = self._make_handler(auth_key="secret", token="secret")
        with patch("backend.event_bus.get_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus
            mock_bus.subscribe.return_value = "sub-id"
            handler.handle()

        sent = b"".join(c.args[0] for c in sock.sendall.call_args_list)
        assert b"101 Switching Protocols" in sent


# ---------------------------------------------------------------------------
# WebSocketHandler.send_json
# ---------------------------------------------------------------------------


class TestSendJson:
    def test_send_json_enqueues_text_frame(self):
        sock = MagicMock()
        handler = WebSocketHandler(
            sock=sock,
            headers={},
            auth_key=None,
            query_params={},
        )
        obj = {"type": "pong"}
        handler.send_json(obj)

        # One frame should be in the outbound queue
        frame = handler._outbound.get_nowait()
        # First byte: FIN + TEXT opcode
        assert frame[0] == 0x81
        # Decode payload
        length = frame[1] & 0x7F
        payload = frame[2 : 2 + length]
        assert json.loads(payload) == obj

    def test_send_json_serializes_nested(self):
        sock = MagicMock()
        handler = WebSocketHandler(
            sock=sock,
            headers={},
            auth_key=None,
            query_params={},
        )
        obj = {"a": [1, 2, 3], "b": {"nested": True}}
        handler.send_json(obj)

        frame = handler._outbound.get_nowait()
        length = frame[1] & 0x7F
        payload = frame[2 : 2 + length]
        assert json.loads(payload) == obj


# ---------------------------------------------------------------------------
# WebSocketHandler._dispatch_command
# ---------------------------------------------------------------------------


class TestDispatchCommand:
    def _make_handler(self) -> WebSocketHandler:
        sock = MagicMock()
        return WebSocketHandler(
            sock=sock,
            headers={},
            auth_key=None,
            query_params={},
        )

    def test_ping_replies_pong(self):
        handler = self._make_handler()
        handler._dispatch_command(json.dumps({"type": "ping"}).encode())
        frame = handler._outbound.get_nowait()
        length = frame[1]
        payload = json.loads(frame[2 : 2 + length])
        assert payload["type"] == "pong"

    def test_subscribe_adds_event(self):
        handler = self._make_handler()
        handler._subscribed = set()  # clear subscriptions
        handler._dispatch_command(
            json.dumps({"type": "subscribe", "events": ["AgentOutputEvent"]}).encode()
        )
        assert "AgentOutputEvent" in handler._subscribed

    def test_unsubscribe_removes_event(self):
        handler = self._make_handler()
        # Default has all subscribed
        handler._dispatch_command(
            json.dumps({"type": "unsubscribe", "events": ["AgentOutputEvent"]}).encode()
        )
        assert "AgentOutputEvent" not in handler._subscribed

    def test_invalid_json_replies_error(self):
        handler = self._make_handler()
        handler._dispatch_command(b"not valid json{{{")
        frame = handler._outbound.get_nowait()
        length = frame[1]
        payload = json.loads(frame[2 : 2 + length])
        assert payload["type"] == "error"

    def test_unknown_command_replies_error(self):
        handler = self._make_handler()
        handler._dispatch_command(json.dumps({"type": "frobnicate"}).encode())
        frame = handler._outbound.get_nowait()
        length = frame[1]
        payload = json.loads(frame[2 : 2 + length])
        assert payload["type"] == "error"
        assert "frobnicate" in payload["message"]

    def test_non_dict_json_replies_error(self):
        handler = self._make_handler()
        handler._dispatch_command(json.dumps([1, 2, 3]).encode())
        frame = handler._outbound.get_nowait()
        length = frame[1]
        payload = json.loads(frame[2 : 2 + length])
        assert payload["type"] == "error"
