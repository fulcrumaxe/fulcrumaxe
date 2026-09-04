"""
SQLite abstraction layer for autonomous-team state persistence.

Provides a thin Database class backed by SQLite with WAL mode, thread-local
connections, and a simple CRUD + query interface. Three tables are defined:
blackboard, sessions, and notifications.

Usage:
    from backend.db import get_db
    db = get_db()
    db.put("blackboard", "loop/status", {"value": "idle", "version": 1})
    entry = db.get("blackboard", "loop/status")
"""

import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

def _resolve_db_path() -> Path:
    """Return the SQLite state.db path.

    state_paths.py is the single source of truth (D#1908 PR 3) — no legacy
    in-repo fallback here any more. See backend/state_paths.py for how
    AUTONOMOUS_TEAM_STATE_DIR is resolved.
    """
    from backend.state_paths import STATE_DB  # noqa: PLC0415
    return STATE_DB


def _self():
    return sys.modules[__name__]


def __getattr__(name: str):
    """PEP 562: ``_DB_PATH`` resolves at access time instead of being frozen
    at import time (D#1810 — the only instance of this exact freeze pattern
    outside backend/state_paths.py itself). Keeps ``backend.db._DB_PATH`` and
    ``mock.patch("backend.db._DB_PATH", ...)`` working unchanged for callers
    and tests: a direct assignment/patch shadows this and is picked up by
    ``_self()._DB_PATH`` below exactly like any other module attribute.
    """
    if name == "_DB_PATH":
        return _resolve_db_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS blackboard (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    locked_by  TEXT,
    locked_at  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    status     TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    channel    TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    success    INTEGER NOT NULL,
    message    TEXT,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS agent_lessons (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    discussion  INTEGER,
    role        TEXT NOT NULL,
    lesson_type TEXT NOT NULL,
    content     TEXT NOT NULL,
    files       TEXT,
    tags        TEXT,
    created_at  TEXT NOT NULL,
    relevance   REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_lessons_role ON agent_lessons(role);
CREATE INDEX IF NOT EXISTS idx_lessons_type ON agent_lessons(lesson_type);
CREATE INDEX IF NOT EXISTS idx_lessons_created ON agent_lessons(created_at);
"""

_KNOWN_TABLES = {"blackboard", "sessions", "notifications", "agent_lessons"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """
    Thin SQLite wrapper with WAL mode and thread-local connections.

    All public methods are thread-safe: each thread gets its own sqlite3
    connection. The underlying database file is shared.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else _self()._DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Initialise schema on the calling thread's connection.
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return a thread-local sqlite3 connection, creating it if needed."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,  # We enforce thread-locality ourselves.
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            # WAL mode for concurrent readers + a single writer.
            conn.execute("PRAGMA journal_mode=WAL")
            # Wait up to 5 s before raising OperationalError on a locked DB.
            conn.execute("PRAGMA busy_timeout=5000")
            conn.commit()
            self._local.conn = conn
        return self._local.conn

    def close(self) -> None:
        """Close this thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Transaction context manager
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that wraps operations in a single transaction."""
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Generic CRUD
    # ------------------------------------------------------------------

    def _validate_table(self, table: str) -> None:
        if table not in _KNOWN_TABLES:
            raise ValueError(f"Unknown table: {table!r}. Must be one of {_KNOWN_TABLES}.")

    def get(self, table: str, key: str) -> Optional[dict]:
        """
        Return the row for *key* as a plain dict, or None if not found.

        For the blackboard table the row includes: key, value (decoded JSON),
        updated_at, locked_by, locked_at.
        For the sessions table: id, data (decoded JSON), created_at, closed_at, status.
        """
        self._validate_table(table)
        conn = self._conn()
        pk = "key" if table == "blackboard" else "id"
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {pk} = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        # Decode JSON blobs
        for col in ("value", "data"):
            if col in result and result[col] is not None:
                try:
                    result[col] = json.loads(result[col])
                except (json.JSONDecodeError, TypeError):
                    pass
        return result

    def put(self, table: str, key: str, value: Any) -> None:
        """
        Upsert *value* under *key* (INSERT OR REPLACE semantics).

        *value* is stored as a JSON-encoded blob. For the blackboard table,
        pass the full entry dict (must contain at least a 'value' field).
        For the sessions table, pass the full session dict.
        """
        self._validate_table(table)
        conn = self._conn()
        now = _now_iso()

        if table == "blackboard":
            entry = value if isinstance(value, dict) else {"value": value}
            # Store the full entry dict as a JSON blob so callers can retrieve
            # version, updated_by, and other metadata alongside the value.
            conn.execute(
                """
                INSERT OR REPLACE INTO blackboard (key, value, updated_at, locked_by, locked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    key,
                    json.dumps(entry, ensure_ascii=False),
                    entry.get("updated_at", now),
                    entry.get("locked_by"),
                    entry.get("locked_at"),
                ),
            )
        elif table == "sessions":
            data = value if isinstance(value, dict) else {"data": value}
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions (id, data, created_at, closed_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    key,
                    json.dumps(data, ensure_ascii=False),
                    data.get("started_at", now),
                    data.get("ended_at"),
                    "closed" if data.get("ended_at") else "active",
                ),
            )
        else:
            raise ValueError(f"put() is not supported for table {table!r}; use insert_notification().")
        conn.commit()

    def delete(self, table: str, key: str) -> bool:
        """
        Remove the row for *key* from *table*.

        Returns True if a row was deleted, False if key was not found.
        """
        self._validate_table(table)
        conn = self._conn()
        pk = "key" if table == "blackboard" else "id"
        cursor = conn.execute(f"DELETE FROM {table} WHERE {pk} = ?", (key,))
        conn.commit()
        return cursor.rowcount > 0

    def query(self, table: str, where_clause: str, params: list | tuple = ()) -> list[dict]:
        """
        Run a SELECT * with the given WHERE clause and parameters.

        Example:
            db.query("sessions", "status = ?", ["active"])
        """
        self._validate_table(table)
        conn = self._conn()
        sql = f"SELECT * FROM {table} WHERE {where_clause}"
        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            for col in ("value", "data"):
                if col in d and d[col] is not None:
                    try:
                        d[col] = json.loads(d[col])
                    except (json.JSONDecodeError, TypeError):
                        pass
            results.append(d)
        return results

    def list_keys(self, table: str, prefix: Optional[str] = None) -> list[str]:
        """
        Return all primary keys from *table*, optionally filtered by *prefix*.

        Keys are returned sorted alphabetically.
        """
        self._validate_table(table)
        conn = self._conn()
        pk = "key" if table == "blackboard" else "id"
        if prefix:
            rows = conn.execute(
                f"SELECT {pk} FROM {table} WHERE {pk} LIKE ? ORDER BY {pk}",
                (prefix + "%",),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {pk} FROM {table} ORDER BY {pk}"
            ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Notifications helper
    # ------------------------------------------------------------------

    def insert_notification(
        self,
        event_type: str,
        channel: str,
        success: bool,
        message: Optional[str] = None,
        error: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> int:
        """Insert a row into the notifications table, returning the new row id."""
        conn = self._conn()
        cursor = conn.execute(
            """
            INSERT INTO notifications (event_type, channel, timestamp, success, message, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                channel,
                timestamp or _now_iso(),
                1 if success else 0,
                message,
                error,
            ),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_db_instance: Optional[Database] = None
_db_lock = threading.Lock()


def get_db(db_path: Path | str | None = None) -> Database:
    """Return the module-level singleton Database, creating it if needed."""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = Database(db_path)
    return _db_instance


def state_db_exists(db_path: Path | str | None = None) -> bool:
    """Return True if the SQLite state file exists on disk."""
    path = Path(db_path) if db_path else _self()._DB_PATH
    return path.exists()
