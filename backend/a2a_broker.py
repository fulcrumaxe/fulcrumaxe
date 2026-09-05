"""
a2a_broker.py — localhost Agent-to-Agent message broker for autonomous-forever.

Binds to 127.0.0.1:8830.  Provides:
  POST /a2a/register          (admin-token, loopback only) — register (agent_id, token_sha256)
  POST /a2a/message           (Bearer token) — send a message
  GET  /a2a/inbox/<agent_id>  (Bearer token) — fetch unread messages (204 when empty)
  POST /a2a/ack/<msg_id>      (Bearer token) — explicit ack
  GET  /a2a/stream/<agent_id> (SSE, dashboard token) — push channel
  GET  /a2a/tasks             (no auth) — list active task-cards

Design decisions:
- No external dependencies — uses stdlib http.server + threading.
- JSONL persistence in $AUTONOMOUS_TEAM_STATE_DIR/a2a/; chmod 600 on inbox files.
- Audit log records sha256 of body, never the body itself.
- Empty inbox returns 204; hook injects nothing → zero idle token cost.
- Per-agent Bearer tokens; per-kind authz matrix (broadcast/interrupt-request restricted
  to team-lead-* senders).
- Survives kill+restart by replaying unread tail.
- Rate-limit: 1 status message per 2 minutes per sender.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import logging
import os
import re
import socketserver
import stat
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s a2a_broker %(levelname)s %(message)s",
)
log = logging.getLogger("a2a_broker")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BROKER_HOST = "127.0.0.1"
BROKER_PORT = int(os.environ.get("A2A_PORT", "8830"))
ADMIN_PORT = int(os.environ.get("A2A_ADMIN_PORT", "8831"))

# Maximum body size in bytes (2 KB per spec)
MAX_BODY_BYTES = 2048

# Rotate daily; drop messages older than this many seconds
MSG_MAX_AGE_SECONDS = 30 * 60  # 30 minutes (hook drops stale)

# Rate-limit: status messages per sender
STATUS_RATE_LIMIT_SECONDS = 120  # 2 minutes per spec

# SSE concurrent connection cap
SSE_MAX_CONNECTIONS = 16

# Admin token (env-provided or generated at startup)
_ADMIN_TOKEN: str = os.environ.get("A2A_ADMIN_TOKEN", "")

# ---------------------------------------------------------------------------
# State (in-process; persisted to JSONL for restart recovery)
# ---------------------------------------------------------------------------

# agent_id → sha256(token)
_registrations: dict[str, str] = {}
_registrations_lock = threading.Lock()

# agent_id → list of {id, from, to, kind, body, in_reply_to, ts, read}
_inboxes: dict[str, list[dict]] = {}
_inboxes_lock = threading.Lock()

# msg_id → message dict (for ack)
_messages_by_id: dict[str, dict] = {}

# rate-limit tracker: agent_id → last_status_ts
_status_rate: dict[str, float] = {}
_status_rate_lock = threading.Lock()

# SSE subscribers: agent_id → list of queue-like objects
_sse_subscribers: dict[str, list] = {}
_sse_lock = threading.Lock()
_sse_connection_count = 0

# ---------------------------------------------------------------------------
# State directory
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    """Return the a2a sub-directory inside the runtime state dir.

    Routed through backend.state_paths (D#1908 PR 3) rather than
    ``os.environ.get("AUTONOMOUS_TEAM_STATE_DIR", <default>)``: that idiom's
    default only fires when the var is *missing*, not when it is set to an
    empty string — a set-but-empty value used to resolve to ``Path("")``,
    i.e. the process cwd, and ``mkdir`` a stray ``a2a/`` directory there.
    """
    from backend.state_paths import STATE_DIR  # noqa: PLC0415

    d = STATE_DIR / "a2a"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _inbox_path(agent_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", agent_id)
    return _state_dir() / f"inbox-{safe}.jsonl"


def _audit_path() -> Path:
    return _state_dir() / "messages.jsonl"


def _registrations_path() -> Path:
    return _state_dir() / "registrations.json"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_registrations() -> None:
    """Replay registrations from disk on startup."""
    p = _registrations_path()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
        with _registrations_lock:
            _registrations.update(data)
        log.info("Loaded %d registrations from disk", len(data))
    except Exception as exc:
        log.warning("Failed to load registrations: %s", exc)


def _save_registrations() -> None:
    p = _registrations_path()
    try:
        with _registrations_lock:
            snap = dict(_registrations)
        p.write_text(json.dumps(snap, indent=2))
        p.chmod(0o600)
    except Exception as exc:
        log.warning("Failed to save registrations: %s", exc)


def _append_inbox(agent_id: str, msg: dict) -> None:
    """Write a message to the agent's JSONL inbox file (chmod 600)."""
    p = _inbox_path(agent_id)
    line = json.dumps(msg) + "\n"
    try:
        with open(p, "a") as f:
            f.write(line)
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception as exc:
        log.warning("Failed to append inbox for %s: %s", agent_id, exc)


def _append_audit(msg: dict) -> None:
    """Write an audit entry — body replaced by sha256."""
    entry = {k: v for k, v in msg.items() if k != "body"}
    entry["body_sha256"] = hashlib.sha256(msg.get("body", "").encode()).hexdigest()
    line = json.dumps(entry) + "\n"
    audit = _audit_path()
    try:
        with open(audit, "a") as f:
            f.write(line)
        os.chmod(audit, stat.S_IRUSR | stat.S_IWUSR)  # 600 — audit log is sensitive
    except Exception as exc:
        log.warning("Failed to append audit: %s", exc)


def _load_unread_inbox(agent_id: str) -> list[dict]:
    """Replay unread messages from JSONL on restart."""
    p = _inbox_path(agent_id)
    if not p.exists():
        return []
    msgs = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                    if not m.get("read"):
                        msgs.append(m)
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        log.warning("Failed to replay inbox for %s: %s", agent_id, exc)
    return msgs


def _ensure_inbox_loaded(agent_id: str) -> None:
    """Lazily load JSONL inbox into memory on first access."""
    with _inboxes_lock:
        if agent_id not in _inboxes:
            msgs = _load_unread_inbox(agent_id)
            _inboxes[agent_id] = msgs
            for m in msgs:
                _messages_by_id[m["id"]] = m


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify_bearer(headers: Any, required_agent_id: str | None = None) -> str | None:
    """Return agent_id if token is valid, else None."""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):]
    token_hash = _sha256(token)
    with _registrations_lock:
        for aid, thash in _registrations.items():
            if thash == token_hash:
                if required_agent_id and aid != required_agent_id:
                    return None
                return aid
    return None


def _verify_admin(headers: Any) -> bool:
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer "):] == _ADMIN_TOKEN


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

VALID_KINDS = {
    "question", "answer", "status", "broadcast", "task-card",
    "interrupt-request", "interrupt-ack",
}


def _validate_message(data: dict) -> str | None:
    """Return error string if invalid, else None."""
    for field in ("from", "to", "kind", "body"):
        if field not in data:
            return f"Missing field: {field}"
    if data["kind"] not in VALID_KINDS:
        return f"Unknown kind: {data['kind']}"
    if len(data["body"].encode()) > MAX_BODY_BYTES:
        return f"Body exceeds {MAX_BODY_BYTES} bytes"
    return None


# ---------------------------------------------------------------------------
# Per-kind authz
# ---------------------------------------------------------------------------

def _check_kind_authz(sender_id: str, kind: str, claimed_from: str) -> str | None:
    """Return 403 message if forbidden, else None."""
    # The Bearer token must match the 'from' claim
    if sender_id != claimed_from:
        return f"Token agent_id {sender_id!r} does not match 'from' claim {claimed_from!r}"
    # broadcast and interrupt-request are team-lead only
    if kind in ("broadcast", "interrupt-request"):
        if not sender_id.startswith("team-lead-"):
            return f"Kind {kind!r} is restricted to team-lead-* agents"
    return None


# ---------------------------------------------------------------------------
# SSE notification
# ---------------------------------------------------------------------------

def _notify_sse(agent_id: str, msg: dict) -> None:
    """Push a message to all SSE subscribers for this agent."""
    with _sse_lock:
        subs = _sse_subscribers.get(agent_id, [])
    data = json.dumps(msg)
    for q in subs:
        try:
            q.append(data)
        except Exception:
            pass
    # Also notify "*" subscribers (dashboard global view)
    with _sse_lock:
        subs_all = _sse_subscribers.get("*", [])
    for q in subs_all:
        try:
            q.append(data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class A2AHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: Any) -> None:  # type: ignore[override]
        log.debug(fmt, *args)

    def _is_loopback(self) -> bool:
        addr = self.client_address[0]
        return addr in ("127.0.0.1", "::1", "localhost")

    def _send_json(self, code: int, data: dict | None) -> None:
        if data is None:
            self.send_response(code)
            self.end_headers()
            return
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY_BYTES * 4)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------ #
    # Route dispatch
    # ------------------------------------------------------------------ #

    def do_POST(self) -> None:  # noqa: N802
        if not self._is_loopback():
            self._send_json(403, {"error": "non-loopback connection rejected"})
            return

        if self.path == "/a2a/register":
            self._handle_register()
        elif self.path == "/a2a/message":
            self._handle_message()
        elif self.path.startswith("/a2a/ack/"):
            msg_id = self.path[len("/a2a/ack/"):]
            self._handle_ack(msg_id)
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if not self._is_loopback():
            self._send_json(403, {"error": "non-loopback connection rejected"})
            return

        if self.path.startswith("/a2a/inbox/"):
            agent_id = self.path[len("/a2a/inbox/"):]
            # strip query params
            if "?" in agent_id:
                agent_id, qs = agent_id.split("?", 1)
                peek = "peek=1" in qs
                head = "head=1" in qs
            else:
                peek = False
                head = False
            self._handle_inbox(agent_id, peek=peek, head=head)
        elif self.path.startswith("/a2a/stream/"):
            agent_id = self.path[len("/a2a/stream/"):]
            if "?" in agent_id:
                agent_id = agent_id.split("?", 1)[0]
            self._handle_sse(agent_id)
        elif self.path == "/a2a/tasks":
            self._handle_tasks()
        elif self.path == "/a2a/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------------ #
    # /a2a/register (admin-token, loopback)
    # ------------------------------------------------------------------ #

    def _handle_register(self) -> None:
        if not _verify_admin(self.headers):
            self._send_json(401, {"error": "invalid admin token"})
            return
        data = self._read_json()
        if data is None:
            self._send_json(400, {"error": "invalid JSON"})
            return
        agent_id = data.get("agent_id", "")
        token_sha256 = data.get("token_sha256", "")
        if not agent_id or not token_sha256:
            self._send_json(400, {"error": "agent_id and token_sha256 required"})
            return
        with _registrations_lock:
            _registrations[agent_id] = token_sha256
        _save_registrations()
        log.info("Registered agent: %s", agent_id)
        self._send_json(200, {"registered": agent_id})

    # ------------------------------------------------------------------ #
    # POST /a2a/message
    # ------------------------------------------------------------------ #

    def _handle_message(self) -> None:
        sender_id = _verify_bearer(self.headers)
        if not sender_id:
            self._send_json(401, {"error": "invalid or missing Bearer token"})
            return

        data = self._read_json()
        if data is None:
            self._send_json(400, {"error": "invalid JSON"})
            return

        err = _validate_message(data)
        if err:
            self._send_json(400, {"error": err})
            return

        authz_err = _check_kind_authz(sender_id, data["kind"], data["from"])
        if authz_err:
            self._send_json(403, {"error": authz_err})
            return

        # Rate-limit for status kind
        if data["kind"] == "status":
            now = time.time()
            with _status_rate_lock:
                last = _status_rate.get(sender_id, 0.0)
                if now - last < STATUS_RATE_LIMIT_SECONDS:
                    self._send_json(429, {"error": "rate limit: 1 status per 2 minutes"})
                    return
                _status_rate[sender_id] = now

        msg_id = "msg-" + str(uuid.uuid4())
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        msg = {
            "id": msg_id,
            "from": data["from"],
            "to": data["to"],
            "kind": data["kind"],
            "body": data["body"],
            "in_reply_to": data.get("in_reply_to"),
            "ts": ts,
            "read": False,
        }

        # Deliver to recipient(s)
        recipients: list[str] = []
        if data["to"] == "*":
            # broadcast: deliver to all registered agents
            with _registrations_lock:
                recipients = list(_registrations.keys())
        else:
            recipients = [data["to"]]

        for recipient in recipients:
            _ensure_inbox_loaded(recipient)
            with _inboxes_lock:
                if recipient not in _inboxes:
                    _inboxes[recipient] = []
                msg_copy = dict(msg)
                _inboxes[recipient].append(msg_copy)
                _messages_by_id[msg_id] = msg_copy
            _append_inbox(recipient, msg)
            _notify_sse(recipient, msg)

        _append_audit(msg)
        log.info("Message %s from %s → %s (%s)", msg_id, sender_id, data["to"], data["kind"])
        self._send_json(200, {"id": msg_id})

    # ------------------------------------------------------------------ #
    # GET /a2a/inbox/<agent_id>
    # ------------------------------------------------------------------ #

    def _handle_inbox(self, agent_id: str, peek: bool = False, head: bool = False) -> None:
        caller = _verify_bearer(self.headers, required_agent_id=agent_id)
        if not caller:
            # Also allow admin token for team-lead polling other inboxes
            if not _verify_admin(self.headers):
                self._send_json(401, {"error": "invalid Bearer token"})
                return

        _ensure_inbox_loaded(agent_id)

        now = time.time()
        with _inboxes_lock:
            unread = [
                m for m in _inboxes.get(agent_id, [])
                if not m.get("read")
                and _msg_age_seconds(m["ts"], now) < MSG_MAX_AGE_SECONDS
            ]

        if not unread:
            # 204 No Content — hook treats this as empty, injects nothing
            self._send_response_no_body(204)
            return

        if head:
            # head=1: just indicate non-empty (for hook optimization)
            self._send_response_no_body(200)
            return

        # Mark as read (unless peek)
        if not peek:
            with _inboxes_lock:
                for m in unread:
                    m["read"] = True

        self._send_json(200, {"messages": unread, "count": len(unread)})

    def _send_response_no_body(self, code: int) -> None:
        self.send_response(code)
        self.end_headers()

    # ------------------------------------------------------------------ #
    # POST /a2a/ack/<msg_id>
    # ------------------------------------------------------------------ #

    def _handle_ack(self, msg_id: str) -> None:
        caller = _verify_bearer(self.headers)
        if not caller:
            self._send_json(401, {"error": "invalid Bearer token"})
            return

        with _inboxes_lock:
            msg = _messages_by_id.get(msg_id)
            if msg is None:
                self._send_json(404, {"error": "message not found"})
                return
            # Verify caller is the recipient
            if msg["to"] != caller and msg["to"] != "*":
                self._send_json(403, {"error": "not the recipient"})
                return
            msg["read"] = True

        self._send_json(200, {"acked": msg_id})

    # ------------------------------------------------------------------ #
    # GET /a2a/stream/<agent_id>  (SSE)
    # ------------------------------------------------------------------ #

    def _handle_sse(self, agent_id: str) -> None:
        global _sse_connection_count
        with _sse_lock:
            if _sse_connection_count >= SSE_MAX_CONNECTIONS:
                self._send_json(503, {"error": "SSE connection cap reached"})
                return
            _sse_connection_count += 1

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            queue: list[str] = []
            with _sse_lock:
                if agent_id not in _sse_subscribers:
                    _sse_subscribers[agent_id] = []
                _sse_subscribers[agent_id].append(queue)

            try:
                cutoff = time.time() - 60  # drop messages older than 60s
                while True:
                    if queue:
                        data = queue.pop(0)
                        try:
                            msg = json.loads(data)
                            # Drop old messages on reconnect / replay
                            msg_ts = _parse_ts(msg.get("ts", ""))
                            if msg_ts > 0 and msg_ts < cutoff:
                                continue
                        except Exception:
                            pass
                        line = f"data: {data}\n\n".encode()
                        self.wfile.write(line)
                        self.wfile.flush()
                    else:
                        # heartbeat every 15s to keep connection alive
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _sse_lock:
                    if agent_id in _sse_subscribers:
                        try:
                            _sse_subscribers[agent_id].remove(queue)
                        except ValueError:
                            pass
        finally:
            with _sse_lock:
                _sse_connection_count -= 1

    # ------------------------------------------------------------------ #
    # GET /a2a/tasks
    # ------------------------------------------------------------------ #

    def _handle_tasks(self) -> None:
        """Return all active task-card messages across all inboxes.

        Body is omitted from the response — callers get only the envelope
        fields needed for routing/display, not the full message content.
        """
        tasks = []
        now = time.time()
        _SAFE_FIELDS = {"id", "from", "to", "kind", "ts"}
        with _inboxes_lock:
            for agent_id, msgs in _inboxes.items():
                for m in msgs:
                    if m.get("kind") == "task-card" and not m.get("read"):
                        if _msg_age_seconds(m["ts"], now) < MSG_MAX_AGE_SECONDS:
                            tasks.append({k: v for k, v in m.items() if k in _SAFE_FIELDS})
        self._send_json(200, {"tasks": tasks, "count": len(tasks)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg_age_seconds(ts_str: str, now: float) -> float:
    """Return age of message in seconds, or 0 if ts unparseable."""
    t = _parse_ts(ts_str)
    if t == 0:
        return 0
    return max(0.0, now - t)


def _parse_ts(ts_str: str) -> float:
    """Parse ISO8601 UTC timestamp to epoch float."""
    try:
        import datetime
        dt = datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread."""
    allow_reuse_address = True
    daemon_threads = True


def run_broker(
    host: str = BROKER_HOST,
    port: int = BROKER_PORT,
    admin_token: str = "",
) -> None:
    """Start the A2A broker (blocking)."""
    global _ADMIN_TOKEN
    if admin_token:
        _ADMIN_TOKEN = admin_token
    elif not _ADMIN_TOKEN:
        _ADMIN_TOKEN = os.urandom(16).hex()

    _load_registrations()
    server = ThreadedHTTPServer((host, port), A2AHandler)
    log.info("A2A broker running on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("A2A broker stopped")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="A2A message broker")
    parser.add_argument("--host", default=BROKER_HOST)
    parser.add_argument("--port", type=int, default=BROKER_PORT)
    parser.add_argument("--admin-token", default="", help="Admin token (generated if empty)")
    args = parser.parse_args()

    if args.admin_token:
        print(f"Admin token: {args.admin_token}", file=sys.stderr)

    run_broker(host=args.host, port=args.port, admin_token=args.admin_token)
