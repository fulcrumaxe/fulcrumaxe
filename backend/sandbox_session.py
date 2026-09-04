"""
sandbox_session.py — in-memory session state for the sandbox server.

Sessions are ephemeral: they live only for the lifetime of the container
process.  No persistence or database required.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Permission:
    """Tracks a single pending/resolved permission request."""

    def __init__(self, perm_id: str, tool: str, input_text: str) -> None:
        self.perm_id = perm_id
        self.tool = tool
        self.input_text = input_text
        self.decision: str | None = None
        self.reason: str | None = None
        self.decided_by: str | None = None
        # Coroutines waiting for this permission to be resolved wait on this event.
        self._event: asyncio.Event = asyncio.Event()

    @property
    def resolved(self) -> bool:
        return self.decision is not None

    async def wait_for_decision(self) -> str:
        """Block until this permission is resolved, then return the decision."""
        await self._event.wait()
        assert self.decision is not None
        return self.decision

    def resolve(self, decision: str, decided_by: str, reason: str | None = None) -> None:
        if self.resolved:
            raise ValueError("already resolved")
        self.decision = decision
        self.decided_by = decided_by
        self.reason = reason
        self._event.set()


class Session:
    """Represents a single agent session within the sandbox."""

    def __init__(self, session_id: str, role: str, system_prompt: str, working_dir: str) -> None:
        self.session_id = session_id
        self.role = role
        self.system_prompt = system_prompt
        self.working_dir = working_dir
        self.events: list[dict[str, Any]] = []
        self.permissions: dict[str, Permission] = {}
        # SSE subscribers: each is a queue of JSON-encoded event strings
        self._subscribers: list[asyncio.Queue[str | None]] = []

    def _push_to_subscribers(self, event: dict[str, Any]) -> None:
        import json
        data = json.dumps(event)
        for q in self._subscribers:
            q.put_nowait(data)

    def add_subscriber(self) -> asyncio.Queue[str | None]:
        q: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers.append(q)
        # Replay historical events to the new subscriber
        import json
        for ev in self.events:
            q.put_nowait(json.dumps(ev))
        return q

    def remove_subscriber(self, q: asyncio.Queue[str | None]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass
        # Sentinel to unblock the consumer
        q.put_nowait(None)

    def add_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self._push_to_subscribers(event)


class SessionManager:
    """Thread-safe (asyncio-safe) registry of active sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create_session(self, role: str, system_prompt: str, working_dir: str) -> Session:
        session_id = str(uuid.uuid4())
        session = Session(session_id, role, system_prompt, working_dir)
        self._sessions[session_id] = session

        # Emit the session.started event immediately
        session.add_event({
            "type": "session.started",
            "session_id": session_id,
            "ts": _now_iso(),
            "role": role,
        })
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def add_event(self, session_id: str, event: dict[str, Any]) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        session.add_event(event)

    def request_permission(
        self,
        session_id: str,
        tool: str,
        input_text: str,
    ) -> str:
        """Create a pending permission request and emit permission.requested."""
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")

        perm_id = str(uuid.uuid4())
        perm = Permission(perm_id, tool, input_text)
        session.permissions[perm_id] = perm

        session.add_event({
            "type": "permission.requested",
            "session_id": session_id,
            "perm_id": perm_id,
            "ts": _now_iso(),
            "tool": tool,
            "input": input_text,
        })
        return perm_id

    def resolve_permission(
        self,
        session_id: str,
        perm_id: str,
        decision: str,
        decided_by: str,
        reason: str | None = None,
    ) -> bool:
        """Resolve a permission and emit permission.resolved.  Returns True on success."""
        session = self._sessions.get(session_id)
        if session is None:
            return False

        perm = session.permissions.get(perm_id)
        if perm is None:
            return False

        if perm.resolved:
            raise ValueError("already resolved")

        perm.resolve(decision, decided_by, reason)
        session.add_event({
            "type": "permission.resolved",
            "session_id": session_id,
            "perm_id": perm_id,
            "ts": _now_iso(),
            "decision": decision,
            "decided_by": decided_by,
            **({"reason": reason} if reason else {}),
        })
        return True

    @property
    def session_count(self) -> int:
        return len(self._sessions)
