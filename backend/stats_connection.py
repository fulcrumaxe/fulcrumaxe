"""stats_connection.py — per-call DuckDB read connection factory.

Opens a fresh read-only connection on every call and returns it to the caller.
The old singleton held an OS file lock for the life of the process, which
blocked writers (stats_writer, post-agent hook) until the dashboard process
restarted.  The per-call model releases the lock as soon as the caller closes
the connection.

IMPORTANT: Callers MUST call conn.close() explicitly (preferably in a
try/finally block) to release the DuckDB read lock promptly.  Relying on
Python GC to close the connection is unreliable inside long-lived threaded
HTTP servers — the lock can persist across requests and block external writers
(loop hooks, post-merge-hook) indefinitely.

Usage::

    from backend.stats_connection import get_read_connection

    conn = get_read_connection()
    try:
        rows = conn.execute("SELECT ...").fetchall()
    finally:
        conn.close()  # releases the DuckDB read lock immediately
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend import state_paths as _state_paths

#: Default bound (seconds) on how long _connect_with_retry() retries a
#: conflicting lock before giving up and raising. Sized with margin over the
#: measured longest contiguous hold (0.7s over a 90s sampling window; see
#: D#2524's frozen Spec F1/Implementation Notes) — a conflict is expected to
#: clear well inside this window, and a permanent conflict must not hang.
_DEFAULT_LOCK_RETRY_BUDGET_S = 2.0
_LOCK_RETRY_INTERVAL_S = 0.05


def _connect_with_retry(db_str: str, *, read_only: bool, retry_budget_s: float = _DEFAULT_LOCK_RETRY_BUDGET_S) -> Any:
    """Open a DuckDB connection, retrying a lock conflict up to retry_budget_s.

    DuckDB's file lock is acquired at connect() time and held for the whole
    life of the connection, not per statement — a conflicting connect() fails
    immediately with duckdb.IOException rather than blocking (confirmed
    empirically, D#2524 F1: a read_only holder blocks a read-write opener and
    vice versa; only reader+reader coexists). So retry belongs here, once,
    around the acquisition — not around individual statements or callers.

    Bounded: re-raises the last duckdb.IOException once retry_budget_s has
    elapsed rather than retrying indefinitely. A permanently-held conflicting
    lock must fail loudly, not hang.
    """
    import duckdb  # noqa: PLC0415

    deadline = time.monotonic() + retry_budget_s
    while True:
        try:
            return duckdb.connect(db_str, read_only=read_only)
        except duckdb.IOException:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_RETRY_INTERVAL_S)


def _db_path() -> Path:
    """Return the active DuckDB stats path.

    Delegates to backend.stats_writer._db_path() so that test patches on
    stats_writer._db_path are respected here too.  Falls back to the env-var
    resolver from state_paths if stats_writer is unavailable.
    """
    try:
        # Lazy import to avoid circular deps — stats_writer may import us.
        import backend.stats_writer as _sw  # noqa: PLC0415
        return _sw._db_path()
    except Exception:  # noqa: BLE001
        import os  # noqa: PLC0415
        env = os.environ.get("STATS_DB_PATH")
        if env:
            return Path(env)
        return _state_paths.STATS_DB


def get_read_connection(retry_budget_s: float = _DEFAULT_LOCK_RETRY_BUDGET_S) -> Any:
    """Open and return a fresh read-only DuckDB connection.

    Each call opens a new connection.  Callers MUST call conn.close() in a
    try/finally block to release the lock promptly — do not rely on GC.

    Thread-safe: DuckDB read-only connections are independent per-call and
    do not share state.

    Retries a lock conflict (a writer holding the file) with bounded backoff
    — see _connect_with_retry — rather than failing immediately on a
    sub-second writer window. Still raises duckdb.IOException if the
    conflicting lock outlives retry_budget_s.
    """
    try:
        import duckdb  # noqa: PLC0415, F401  — import kept for callers that expect it eagerly
    except ImportError as exc:
        raise RuntimeError(
            "duckdb not installed — run: pip install duckdb"
        ) from exc

    db_str = str(_db_path())
    db = Path(db_str)
    db.parent.mkdir(parents=True, exist_ok=True)
    return _connect_with_retry(db_str, read_only=True, retry_budget_s=retry_budget_s)


def close_all() -> None:
    """No-op — kept for API compatibility with callers that call close_all().

    With per-call connections there is no singleton to close.  Callers should
    call conn.close() directly on the connection returned by get_read_connection().
    """
