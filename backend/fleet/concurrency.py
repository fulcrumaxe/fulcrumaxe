"""backend/fleet/concurrency.py — sqlite WAL fleet concurrency enforcement.

Tracks active agents across all coldstarted projects in a shared sqlite
database at ~/.autonomous-fleet-state/fleet.db.  Uses WAL mode and
BEGIN IMMEDIATE to handle concurrent writes from multiple project processes
without data loss or double-allocation.

Schema::

    CREATE TABLE agents (
        project_name TEXT NOT NULL,
        agent_id     TEXT NOT NULL,
        role         TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        pid          INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (project_name, agent_id)
    );

The ``pid`` column enables process-liveness reaping: ``reap_stale()`` removes
rows whose registering process no longer exists (``/proc/<pid>`` absent) AND
whose ``started_at`` is older than 60 s (to avoid racing with mid-fork spawns).
The original 2-hour ``started_at``-based reap is kept as a backstop for legacy
rows where ``pid = 0``.

Fleet cap default is 8 — sourced from CLAUDE.md memory
``feedback_concurrency_caps.md``: "max 4 executors + max 4 other agents = 8
total; bottleneck is state.db/GH API/preflight, not subscription quota."
The value is overridden by ``~/.autonomous-fleet-state/config.json``.

Usage::

    from backend.fleet.concurrency import register, unregister, count_fleet, fleet_cap

    ok = register("autonomous-forever", "agent-abc123", "executor")
    if not ok:
        raise SystemExit("fleet cap exceeded")
    ...
    unregister("autonomous-forever", "agent-abc123")

CLI (used by pre-spawn-check.sh and post-agent-hook.sh)::

    python3 -m backend.fleet.concurrency register <project> <agent_id> <role>
    python3 -m backend.fleet.concurrency unregister <project> <agent_id>
    python3 -m backend.fleet.concurrency count_fleet
    python3 -m backend.fleet.concurrency count_project <project>
    python3 -m backend.fleet.concurrency count_project_capped <project>
    python3 -m backend.fleet.concurrency active_agents <project>
    python3 -m backend.fleet.concurrency fleet_cap
    python3 -m backend.fleet.concurrency reap_stale [max_age_seconds]
    python3 -m backend.fleet.concurrency list
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────

FLEET_STATE_DIR = Path(os.environ.get("AUTONOMOUS_FLEET_STATE_DIR",
                                       Path.home() / ".autonomous-fleet-state"))
FLEET_DB_PATH = FLEET_STATE_DIR / "fleet.db"
FLEET_CONFIG_PATH = FLEET_STATE_DIR / "config.json"

DEFAULT_FLEET_CAP = 8  # from feedback_concurrency_caps.md

# p99 hold-time target (ms) from perf-expert consensus
_HOLD_TIME_WARN_MS = 50

# TTL reaper: entries older than this are considered stale (crashed/unhook-fired agents).
# Override via env AUTONOMOUS_FLEET_MAX_AGE_SECONDS; default 2h.
MAX_AGE_SECONDS: int = int(os.environ.get("AUTONOMOUS_FLEET_MAX_AGE_SECONDS", 7200))

# PID-liveness reaper: grace period before a dead-PID row may be reaped.
# Prevents races where register() runs but the child process hasn't fully launched yet.
PID_GRACE_SECONDS: int = 60

# D#2314 F1: rows registered by hooks/fleet_register.py for the Agent()-tool
# spawn path carry this prefix. They are keyed on the long-lived Team Lead
# *session* PID (os.getppid() at hook-invocation time) rather than a PID
# that dies when the individual spawned agent finishes -- there is no such
# PID available to a PreToolUse hook, which fires before the spawned agent
# exists. Two things follow from that:
#   - they are excluded from every cap-check COUNT(*) in this module
#     (register()'s fleet-wide check, count_project_capped()) -- they must
#     never consume a spawn-agent.sh-lane concurrency slot. This is also
#     what makes reap_stale()'s existing pid-liveness check sufficient for
#     them (see below) rather than a gap needing its own mechanism: a
#     leaked row can no longer block anything, so there is no pressure to
#     collect it faster than that.
#   - a missed hooks/fleet_unregister.py call (crash, hook error, race)
#     leaves the row registered under the session pid until that pid dies,
#     at which point reap_stale() collects it exactly like any other row --
#     verified empirically (dead pid, 5 minutes old, reaped). An earlier
#     version of this comment described a separate age-based sweep as the
#     backstop for that case; it was removed (D#2314 security re-review,
#     finding N1) because deleting purely on age with no liveness condition
#     is indistinguishable from deleting a still-running agent's row --
#     measured to do exactly that, including a two-spawn cascade where
#     sweeping agent A's row let agent B's SubagentStop evict B's own row
#     while B was still running. reap_stale()'s pid check is the only
#     backstop these rows need or get.
AGENT_TOOL_ID_PREFIX = "agent-tool-"


# ── Init ──────────────────────────────────────────────────────────────────────

def _ensure_fleet_state_dir() -> None:
    """Create ~/.autonomous-fleet-state/ and seed config.json on first run."""
    FLEET_STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not FLEET_CONFIG_PATH.exists():
        FLEET_CONFIG_PATH.write_text(
            json.dumps({"fleet_cap": DEFAULT_FLEET_CAP}, indent=2) + "\n"
        )


def _open_db() -> sqlite3.Connection:
    """Open (or create) fleet.db with WAL mode enabled."""
    _ensure_fleet_state_dir()
    conn = sqlite3.connect(str(FLEET_DB_PATH), timeout=5.0,
                           isolation_level=None)  # autocommit for manual txn control
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            project_name TEXT NOT NULL,
            agent_id     TEXT NOT NULL,
            role         TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            pid          INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_name, agent_id)
        )
    """)
    # Idempotent migration: add pid column to pre-existing databases.
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN pid INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists — normal on all but the very first open after upgrade.
    return conn


def _pid_alive(pid: int) -> bool:
    """Return True if process *pid* is still running.

    Uses /proc/<pid> for speed — no subprocess, no signal.  Falls back to True
    (conservative: don't reap) when /proc is unavailable (e.g. macOS), so the
    2-hour started_at backstop still fires.
    """
    if pid <= 0:
        return False  # pid=0 sentinel → treat as dead
    proc_path = f"/proc/{pid}"
    if not os.path.exists("/proc"):
        return True  # non-Linux: assume alive; 2h TTL is the backstop
    return os.path.exists(proc_path)


# ── Public API ─────────────────────────────────────────────────────────────────

def fleet_cap() -> int:
    """Return the configured fleet cap (default 8).

    Reads from ``~/.autonomous-fleet-state/config.json``.  Falls back to the
    compiled-in default if the file is absent or malformed.
    """
    _ensure_fleet_state_dir()
    try:
        data = json.loads(FLEET_CONFIG_PATH.read_text())
        return int(data.get("fleet_cap", DEFAULT_FLEET_CAP))
    except Exception:
        return DEFAULT_FLEET_CAP


def count_fleet() -> int:
    """Return total registered agents across all projects."""
    conn = _open_db()
    try:
        row = conn.execute("SELECT COUNT(*) FROM agents").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def count_project(project_name: str) -> int:
    """Return registered agent count for a single project."""
    conn = _open_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE project_name = ?",
            (project_name,)
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def count_project_capped(project_name: str) -> int:
    """Like ``count_project()``, but excludes ``agent-tool-`` rows (D#2314 S2).

    Those rows are observe-only registration coverage for the ``Agent()``
    tool path (see ``AGENT_TOOL_ID_PREFIX`` above) and must never consume a
    spawn-agent.sh-lane concurrency slot -- a busy consensus panel (5
    specialists + researcher + PM) can put up to 7 such rows against a
    default cap of 8. ``scripts/pre-spawn-check.sh``'s per-project cap check
    calls this; ``count_project()`` itself is unchanged for other,
    purely-observational consumers (e.g. the Fleet page RPC).
    """
    conn = _open_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE project_name = ? AND agent_id NOT LIKE ?",
            (project_name, f"{AGENT_TOOL_ID_PREFIX}%")
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _connect_ro(immutable: bool = False) -> sqlite3.Connection:
    suffix = "&immutable=1" if immutable else ""
    return sqlite3.connect(f"file:{FLEET_DB_PATH}?mode=ro{suffix}", uri=True, timeout=5.0)


def active_agents(project_name: str) -> list[dict]:
    """Read-only, PID-filtered agent rows for *project_name* (D#2314).

    For a polling endpoint (the dashboard's liveness probe): connects
    ``mode=ro``, runs no ``CREATE TABLE`` / ``ALTER TABLE`` (unlike every
    other function here via ``_open_db()``), and performs no write — never
    takes a write lock. Filters dead-PID rows in Python rather than calling
    ``reap_stale()``, which mutates the table and is only safe from the
    register() path.

    Returns ``[]`` when fleet.db doesn't exist yet, or has no ``agents``
    table yet, or has no rows for *project_name* — "nothing registered" is
    not an error. Raises for a genuine read failure (e.g. a corrupt
    database file) so callers can distinguish "no agents" from "couldn't
    tell" (D#2314's idle-vs-unknown distinction).

    WAL-mode subtlety this deliberately works around: SQLite persists
    ``journal_mode=WAL`` in the database file header, so *every* connection
    to it -- including a fresh ``mode=ro`` one -- must create the
    ``-shm``/``-wal`` sidecar files the first time anything ever reads or
    writes the file, and creating them needs a writable directory. On a
    normally-writable fleet dir this is invisible (some writer created them
    long ago). On a fleet dir made read-only before anything ever touched
    it, the plain ``mode=ro`` connect raises "attempt to write a readonly
    database" on the *first* query even though this function never issues
    a write of its own. When that happens, retry once with ``immutable=1``,
    which tells SQLite to skip WAL bookkeeping entirely and read the main
    file directly. That fallback is only reached once the directory has
    already proven itself unwritable -- and a directory no reader can write
    to is a directory no *writer* can register into either, so there is by
    construction no concurrent writer whose uncheckpointed WAL content this
    fallback could miss.
    """
    try:
        conn = _connect_ro()
    except sqlite3.OperationalError:
        # fleet.db (or its parent dir) doesn't exist yet — no agent has ever
        # registered anywhere on this host. Zero rows, not a failure.
        return []
    try:
        try:
            rows = conn.execute(
                "SELECT project_name, agent_id, role, started_at, pid "
                "FROM agents WHERE project_name = ? ORDER BY started_at",
                (project_name,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            msg = str(exc)
            if "no such table" in msg:
                # A freshly-created empty file — same "nothing registered
                # yet" case as a missing file.
                return []
            if "readonly database" in msg:
                # See the WAL-mode note above — fall back to a connection
                # that never touches -shm/-wal at all.
                conn.close()
                conn = _connect_ro(immutable=True)
                try:
                    rows = conn.execute(
                        "SELECT project_name, agent_id, role, started_at, pid "
                        "FROM agents WHERE project_name = ? ORDER BY started_at",
                        (project_name,),
                    ).fetchall()
                except sqlite3.OperationalError as exc2:
                    if "no such table" in str(exc2):
                        return []
                    raise
            else:
                raise
    finally:
        conn.close()
    return [
        {"project_name": r[0], "agent_id": r[1], "role": r[2], "started_at": r[3], "pid": r[4]}
        for r in rows
        if _pid_alive(r[4])
    ]


def reap_stale(max_age_seconds: int = MAX_AGE_SECONDS) -> int:
    """Delete fleet entries whose registering process is dead (PID liveness check).

    Primary criterion: row is reaped when its registering PID no longer exists
    AND its ``started_at`` is older than ``PID_GRACE_SECONDS`` (60 s).  The
    grace window prevents reaping rows mid-fork before the child process is
    fully alive.

    Backstop: rows with ``pid = 0`` (legacy rows written before this migration)
    are reaped when ``started_at`` is older than *max_age_seconds* (default 2 h),
    preserving the original PR #975 TTL behaviour.

    Best-effort: if the DELETE fails (e.g. transient SQLite lock), logs a
    warning to stderr and returns 0.  Never raises — callers must not fail
    the spawn because of a reaper error.

    Returns the number of rows deleted.
    """
    now = time.time()
    pid_cutoff = datetime.fromtimestamp(
        now - PID_GRACE_SECONDS, tz=timezone.utc
    ).isoformat()
    age_cutoff = datetime.fromtimestamp(
        now - max_age_seconds, tz=timezone.utc
    ).isoformat()

    try:
        conn = _open_db()
        try:
            # Fetch all rows to evaluate liveness in Python (avoids SQL UDF complexity).
            rows = conn.execute(
                "SELECT project_name, agent_id, pid, started_at FROM agents"
            ).fetchall()

            to_delete: list[tuple[str, str]] = []
            for project_name, agent_id, pid, started_at in rows:
                if pid != 0:
                    # PID-liveness path: reap if dead AND past grace window.
                    if not _pid_alive(pid) and started_at < pid_cutoff:
                        to_delete.append((project_name, agent_id))
                else:
                    # Legacy backstop: no PID recorded — fall back to age-based reap.
                    if started_at < age_cutoff:
                        to_delete.append((project_name, agent_id))

            count = 0
            for project_name, agent_id in to_delete:
                conn.execute(
                    "DELETE FROM agents WHERE project_name = ? AND agent_id = ?",
                    (project_name, agent_id),
                )
                count += 1

        finally:
            conn.close()

        if count > 0:
            print(
                f"[fleet/concurrency] reaped {count} stale entr{'y' if count == 1 else 'ies'} (pid-liveness + {max_age_seconds}s backstop)",
                file=sys.stderr,
            )
        return count
    except Exception as exc:
        print(f"[fleet/concurrency] WARN: reap_stale failed (non-fatal): {exc}", file=sys.stderr)
        return 0


def register(project_name: str, agent_id: str, role: str,
             pid: Optional[int] = None) -> bool:
    """Register a new agent.  Returns True on success, False if fleet cap would be exceeded.

    *pid* is the PID of the registering process and is used by ``reap_stale()``
    to detect dead agents via /proc/<pid> liveness checks.  Defaults to
    ``os.getpid()`` — pass an explicit value only in tests or when the
    relevant PID differs from the caller's PID.

    Uses ``BEGIN IMMEDIATE`` to serialise concurrent writes.  The entire
    count-check + insert is atomic so two concurrent callers cannot both
    succeed when only one slot remains.  p99 hold time is logged to stderr
    when it exceeds the 50 ms target.
    """
    if pid is None:
        pid = os.getpid()
    reap_stale()  # Best-effort TTL pruning — runs before cap check
    cap = fleet_cap()
    conn = _open_db()
    t0 = time.monotonic()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # D#2314 S2: agent-tool- rows (Agent()-tool registration coverage,
        # AGENT_TOOL_ID_PREFIX above) are observational, not a real
        # spawn-agent.sh-lane slot, so a registration under that prefix skips
        # the cap check entirely — it must never be denied (Spec item 11) —
        # and, symmetrically, never counts toward *other* registrations' cap
        # check either. However many pile up, they can't exhaust the fleet.
        if not agent_id.startswith(AGENT_TOOL_ID_PREFIX):
            row = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE agent_id NOT LIKE ?",
                (f"{AGENT_TOOL_ID_PREFIX}%",)
            ).fetchone()
            current = int(row[0]) if row else 0
            if current >= cap:
                conn.execute("ROLLBACK")
                return False
        conn.execute(
            "INSERT OR IGNORE INTO agents (project_name, agent_id, role, started_at, pid) VALUES (?, ?, ?, ?, ?)",
            (project_name, agent_id, role, datetime.now(timezone.utc).isoformat(), pid)
        )
        conn.execute("COMMIT")
        return True
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        hold_ms = (time.monotonic() - t0) * 1000
        if hold_ms > _HOLD_TIME_WARN_MS:
            print(
                f"[fleet/concurrency] WARN: BEGIN IMMEDIATE hold time {hold_ms:.1f}ms > {_HOLD_TIME_WARN_MS}ms",
                file=sys.stderr
            )
        conn.close()


def unregister(project_name: str, agent_id: str) -> None:
    """Remove an agent registration.  No-op if the row doesn't exist."""
    conn = _open_db()
    try:
        conn.execute(
            "DELETE FROM agents WHERE project_name = ? AND agent_id = ?",
            (project_name, agent_id)
        )
    finally:
        conn.close()


def list_agents() -> list[dict]:
    """Return all active agent rows as a list of dicts."""
    conn = _open_db()
    try:
        rows = conn.execute(
            "SELECT project_name, agent_id, role, started_at, pid FROM agents ORDER BY started_at"
        ).fetchall()
        return [
            {"project_name": r[0], "agent_id": r[1], "role": r[2], "started_at": r[3], "pid": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def _main() -> None:
    import sys

    args = sys.argv[1:]
    if not args:
        print("Usage: python3 -m backend.fleet.concurrency <command> [args]")
        print("Commands: register <project> <agent_id> <role>")
        print("          unregister <project> <agent_id>")
        print("          count_fleet")
        print("          count_project <project>")
        print("          count_project_capped <project>")
        print("          active_agents <project>")
        print("          fleet_cap")
        print("          reap_stale [max_age_seconds]")
        print("          list")
        sys.exit(1)

    cmd = args[0]

    if cmd == "register":
        if len(args) not in (4, 5):
            print("Usage: register <project> <agent_id> <role> [pid]", file=sys.stderr)
            sys.exit(1)
        # D#2314 S2 follow-up: agent_id is caller-supplied here (e.g. from
        # pre-spawn-check.sh's --event-id), and AGENT_TOOL_ID_PREFIX rows are
        # excluded from the cap check below. Without this guard, a caller
        # could forge that prefix on the real spawn-agent.sh lane to bypass
        # the fleet cap entirely. hooks/fleet_register.py's own internal
        # register() call (not this CLI) is trusted code and is unaffected.
        if args[2].startswith(AGENT_TOOL_ID_PREFIX):
            print(
                f"ERROR: agent_id may not start with reserved prefix "
                f"{AGENT_TOOL_ID_PREFIX!r} (D#2314 cap-exclusion guard)",
                file=sys.stderr,
            )
            sys.exit(1)
        pid_arg: Optional[int] = int(args[4]) if len(args) == 5 else None
        ok = register(args[1], args[2], args[3], pid=pid_arg)
        if ok:
            print("registered")
        else:
            print(f"denied: fleet cap ({fleet_cap()}) reached", file=sys.stderr)
            sys.exit(1)

    elif cmd == "unregister":
        if len(args) != 3:
            print("Usage: unregister <project> <agent_id>", file=sys.stderr)
            sys.exit(1)
        unregister(args[1], args[2])
        print("unregistered")

    elif cmd == "count_fleet":
        print(count_fleet())

    elif cmd == "count_project":
        if len(args) != 2:
            print("Usage: count_project <project>", file=sys.stderr)
            sys.exit(1)
        print(count_project(args[1]))

    elif cmd == "count_project_capped":
        if len(args) != 2:
            print("Usage: count_project_capped <project>", file=sys.stderr)
            sys.exit(1)
        print(count_project_capped(args[1]))

    elif cmd == "active_agents":
        if len(args) != 2:
            print("Usage: active_agents <project>", file=sys.stderr)
            sys.exit(1)
        import json as _json
        print(_json.dumps(active_agents(args[1]), indent=2))

    elif cmd == "fleet_cap":
        print(fleet_cap())

    elif cmd == "reap_stale":
        max_age = int(args[1]) if len(args) > 1 else MAX_AGE_SECONDS
        count = reap_stale(max_age)
        print(f"reaped {count}")

    elif cmd == "list":
        import json as _json
        print(_json.dumps(list_agents(), indent=2))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _main()
