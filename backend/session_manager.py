"""
Session manager — persists loop session history in .autonomous-team/sessions/.

Each session is stored as a separate JSON file named {session_id}.json.
The SessionManager handles create, read, close, list, and compare operations.
All file writes are atomic (write-to-temp-then-rename) to prevent corruption.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path(__file__).resolve().parent.parent / ".autonomous-team" / "sessions"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


def _write_atomic(path: Path, data: dict) -> None:
    """Write JSON to *path* atomically via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class SessionManager:
    """Manages per-session JSON files in SESSIONS_DIR."""

    def __init__(self, sessions_dir: Path = SESSIONS_DIR) -> None:
        self._dir = sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _read(self, session_id: str) -> Optional[dict]:
        p = self._path(session_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def start_session(self) -> dict:
        """Create a new session, closing any currently-open one first."""
        self.close_session()  # no-op if nothing is open

        session_id = str(uuid.uuid4())
        data: dict = {
            "session_id": session_id,
            "started_at": _now_iso(),
            "ended_at": None,
            "iteration_count": 0,
            "prs_merged": [],
            "discussions_completed": [],
        }
        _write_atomic(self._path(session_id), data)
        return data

    def current_session(self) -> Optional[dict]:
        """Return the session with ended_at == null, or None."""
        for p in self._dir.glob("*.json"):
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("ended_at") is None and "session_id" in data:
                return data
        return None

    def _update_current(self, mutate) -> Optional[dict]:
        """Read current session, apply mutate(data), write back atomically."""
        session = self.current_session()
        if session is None:
            return None
        mutate(session)
        _write_atomic(self._path(session["session_id"]), session)
        return session

    def record_iteration(self) -> None:
        """Increment iteration_count on the current session (no-op if none)."""
        self._update_current(lambda d: d.update({"iteration_count": d["iteration_count"] + 1}))

    def record_pr_merged(self, pr_number: int) -> None:
        """Append pr_number to prs_merged on the current session."""
        def _add(d: dict) -> None:
            if pr_number not in d["prs_merged"]:
                d["prs_merged"].append(pr_number)
        self._update_current(_add)

    def record_discussion_completed(self, discussion_number: int) -> None:
        """Append discussion_number to discussions_completed on the current session."""
        def _add(d: dict) -> None:
            if discussion_number not in d["discussions_completed"]:
                d["discussions_completed"].append(discussion_number)
        self._update_current(_add)

    def close_session(self) -> Optional[dict]:
        """Set ended_at on the current open session. Returns the closed session, or None."""
        return self._update_current(lambda d: d.update({"ended_at": _now_iso()}))

    def get_session(self, session_id: str) -> Optional[dict]:
        """Return a single session by ID, or None if not found."""
        return self._read(session_id)

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """Return sessions sorted newest-first (by started_at)."""
        sessions: list[dict] = []
        for p in self._dir.glob("*.json"):
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if "session_id" in data:
                sessions.append(data)

        sessions.sort(
            key=lambda s: s.get("started_at", ""),
            reverse=True,
        )
        return sessions[:limit]

    def compare_sessions(self, id_a: str, id_b: str) -> dict:
        """Compare two sessions and return their data plus arithmetic deltas."""
        a = self._read(id_a)
        b = self._read(id_b)
        if a is None:
            raise ValueError(f"session '{id_a}' not found")
        if b is None:
            raise ValueError(f"session '{id_b}' not found")

        def _duration_minutes(session: dict) -> Optional[float]:
            start = _parse_dt(session.get("started_at", ""))
            end_raw = session.get("ended_at")
            end = _parse_dt(end_raw) if end_raw else datetime.now(timezone.utc)
            if start is None:
                return None
            return (end - start).total_seconds() / 60

        dur_a = _duration_minutes(a)
        dur_b = _duration_minutes(b)
        if dur_a is not None and dur_b is not None:
            duration_delta: Optional[float] = round(dur_a - dur_b, 2)
        else:
            duration_delta = None

        return {
            "a": a,
            "b": b,
            "delta": {
                "iterations": a["iteration_count"] - b["iteration_count"],
                "prs": len(a["prs_merged"]) - len(b["prs_merged"]),
                "discussions": len(a["discussions_completed"]) - len(b["discussions_completed"]),
                "duration_minutes": duration_delta,
            },
        }


# ------------------------------------------------------------------
# SQLite-backed implementation
# ------------------------------------------------------------------


class SqliteSessionManager:
    """
    Drop-in replacement for SessionManager backed by SQLite via backend.db.

    Sessions are stored as JSON blobs in the 'sessions' table. The public API
    is identical to SessionManager so callers need no changes.
    """

    def __init__(self, db=None) -> None:
        if db is None:
            from backend.db import get_db  # local import
            db = get_db()
        self._db = db

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read(self, session_id: str) -> Optional[dict]:
        row = self._db.get("sessions", session_id)
        if row is None:
            return None
        data = row.get("data")
        return data if isinstance(data, dict) else None

    def _write(self, data: dict) -> None:
        self._db.put("sessions", data["session_id"], data)

    def _update_current(self, mutate) -> Optional[dict]:
        session = self.current_session()
        if session is None:
            return None
        mutate(session)
        self._write(session)
        return session

    # ------------------------------------------------------------------
    # Public API (mirrors SessionManager)
    # ------------------------------------------------------------------

    def start_session(self) -> dict:
        """Create a new session, closing any currently-open one first."""
        self.close_session()
        session_id = str(uuid.uuid4())
        data: dict = {
            "session_id": session_id,
            "started_at": _now_iso(),
            "ended_at": None,
            "iteration_count": 0,
            "prs_merged": [],
            "discussions_completed": [],
        }
        self._write(data)
        return data

    def current_session(self) -> Optional[dict]:
        """Return the open session (ended_at is None), or None."""
        rows = self._db.query("sessions", "status = ?", ["active"])
        for row in rows:
            data = row.get("data")
            if isinstance(data, dict) and "session_id" in data and data.get("ended_at") is None:
                return data
        return None

    def record_iteration(self) -> None:
        self._update_current(lambda d: d.update({"iteration_count": d["iteration_count"] + 1}))

    def record_pr_merged(self, pr_number: int) -> None:
        def _add(d: dict) -> None:
            if pr_number not in d["prs_merged"]:
                d["prs_merged"].append(pr_number)
        self._update_current(_add)

    def record_discussion_completed(self, discussion_number: int) -> None:
        def _add(d: dict) -> None:
            if discussion_number not in d["discussions_completed"]:
                d["discussions_completed"].append(discussion_number)
        self._update_current(_add)

    def close_session(self) -> Optional[dict]:
        """Set ended_at on the current open session."""
        return self._update_current(lambda d: d.update({"ended_at": _now_iso()}))

    def get_session(self, session_id: str) -> Optional[dict]:
        return self._read(session_id)

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """Return sessions sorted newest-first."""
        rows = self._db.query("sessions", "1=1", [])
        sessions: list[dict] = []
        for row in rows:
            data = row.get("data")
            if isinstance(data, dict) and "session_id" in data:
                sessions.append(data)
        sessions.sort(key=lambda s: s.get("started_at", ""), reverse=True)
        return sessions[:limit]

    def compare_sessions(self, id_a: str, id_b: str) -> dict:
        a = self._read(id_a)
        b = self._read(id_b)
        if a is None:
            raise ValueError(f"session '{id_a}' not found")
        if b is None:
            raise ValueError(f"session '{id_b}' not found")

        def _duration_minutes(session: dict) -> Optional[float]:
            start = _parse_dt(session.get("started_at", ""))
            end_raw = session.get("ended_at")
            end = _parse_dt(end_raw) if end_raw else datetime.now(timezone.utc)
            if start is None:
                return None
            return (end - start).total_seconds() / 60

        dur_a = _duration_minutes(a)
        dur_b = _duration_minutes(b)
        duration_delta: Optional[float] = (
            round(dur_a - dur_b, 2) if dur_a is not None and dur_b is not None else None
        )
        return {
            "a": a,
            "b": b,
            "delta": {
                "iterations": a["iteration_count"] - b["iteration_count"],
                "prs": len(a["prs_merged"]) - len(b["prs_merged"]),
                "discussions": len(a["discussions_completed"]) - len(b["discussions_completed"]),
                "duration_minutes": duration_delta,
            },
        }


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def get_session_manager() -> "SessionManager | SqliteSessionManager":
    """
    Return the appropriate SessionManager implementation.

    Returns SqliteSessionManager when state.db exists, else falls back to
    the file-based SessionManager for backward compatibility.
    """
    from backend.db import state_db_exists  # local import
    if state_db_exists():
        return SqliteSessionManager()
    return SessionManager()
