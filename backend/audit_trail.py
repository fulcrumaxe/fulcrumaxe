"""
Audit trail — centralized state-change logger for the autonomous team.

Appends JSON Lines to the audit log named by ``state_paths.AUDIT_LOG``
(``$AUTONOMOUS_TEAM_STATE_DIR/audit.jsonl``). All significant state
mutations across blackboard, control_plane, registry, and budget are recorded
with source, action, key, old value, new value, actor, timestamp, and a
monotonically increasing sequence number.

Streaming search reads ``audit.jsonl.1`` (if present) then ``audit.jsonl``
once and applies all filters in a single pass. Expected throughput is
≥100k entries/sec on a typical SSD with no index required.

Usage (CLI):
    python backend/audit_trail.py tail              # last 20 entries
    python backend/audit_trail.py tail --n 50       # last 50 entries
    python backend/audit_trail.py query --source blackboard
    python backend/audit_trail.py query --source blackboard --since 2026-04-10T20:00:00Z
    python backend/audit_trail.py query --actor team-lead --limit 100
    python backend/audit_trail.py stats             # counts by source and action
    python backend/audit_trail.py search --discussion 396 --role project-manager --since 24h
    python backend/audit_trail.py search --verdict done --format json
    python backend/audit_trail.py search --since 2026-05-01T00:00:00Z --until 2026-05-02T00:00:00Z

Usage (library):
    from backend.audit_trail import get_audit_trail
    at = get_audit_trail()
    at.emit("blackboard", "write", "loop/status", "idle", "running", "team-lead")
    entries = at.tail(20)
    results = at.query(source="blackboard", since="2026-04-10T20:00:00Z")
    hits = at.search(discussion=396, role="project-manager", since_expr="4h")
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allow running as a script from the repo root: `python3 backend/audit_trail.py tail`.
# The default log path now resolves through backend.state_paths, so the package
# has to be importable even when this file is executed directly.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ROTATION_BYTES = 10 * 1024 * 1024  # 10 MB
_SEEN_DB_PATH = Path(".autonomous-team/hook-events/seen.sqlite")


def _resolve_seen_db_audit() -> Path:
    """Resolve seen.sqlite path relative to repo root."""
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    return repo_root / _SEEN_DB_PATH


def _check_seen_audit(event_id: str, hook: str) -> bool:
    """
    Return True if event_id already recorded for audit_trail (duplicate).
    Return False and register atomically if new.
    """
    if not event_id:
        return False
    db_path = _resolve_seen_db_audit()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_events "
            "(event_id TEXT PRIMARY KEY, hook TEXT, ts TEXT)"
        )
        conn.execute(
            "DELETE FROM seen_events WHERE ts < datetime('now','-7 days')"
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_events VALUES (?,?,datetime('now'))",
            (event_id, hook),
        )
        conn.commit()
        conn.close()
        return cur.rowcount == 0  # True == already seen
    except sqlite3.Error:
        return False

# Valid source and action values (informational — not enforced at runtime)
_VALID_SOURCES = frozenset({"blackboard", "control_plane", "registry", "budget", "agent", "gate"})
_VALID_ACTIONS = frozenset({
    "write", "delete", "cas", "set", "init",
    "spawn", "terminate", "transition",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Time-expression parser (shared helper)
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)(s|m|h|d|w)$")
_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _parse_time_expr(expr: str, now: datetime | None = None) -> datetime:
    """
    Parse a time expression into a UTC-aware datetime.

    Accepted forms:
      - ``now``                     → current UTC time
      - ``(\\d+)(s|m|h|d|w)``       → *now* minus the relative offset
      - ISO 8601 / RFC 3339 string  → parsed directly (Z accepted)

    Raises
    ------
    ValueError
        On unrecognised input.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    expr = expr.strip()

    if expr == "now":
        return now

    m = _RELATIVE_RE.match(expr)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(seconds=amount * _UNIT_SECONDS[unit])
        return now - delta

    # Try ISO 8601 / RFC 3339
    try:
        dt = datetime.fromisoformat(expr.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise ValueError(f"Cannot parse time expression: {expr!r}")


# ---------------------------------------------------------------------------
# Per-entry filter helpers
# ---------------------------------------------------------------------------


def _match_discussion(entry: dict, disc_str: str) -> bool:
    """Return True if *entry* is associated with the given discussion number."""
    new = entry.get("new") or {}
    if isinstance(new, dict):
        # Direct field match
        if str(new.get("discussion", "")) == disc_str:
            return True

    # Key-based pattern: key contains /N- or /N/ or -N-
    key = str(entry.get("key", ""))
    patterns = [f"/{disc_str}-", f"/{disc_str}/", f"-{disc_str}-", f"-{disc_str}/"]
    if any(p in key for p in patterns):
        return True

    return False


def _match_role(entry: dict, role: str) -> bool:
    """Return True if *entry* is associated with the given role/agent name."""
    if entry.get("actor") == role:
        return True
    new = entry.get("new") or {}
    if isinstance(new, dict):
        if new.get("role") == role or new.get("agent") == role:
            return True
    return False


def _match_verdict(entry: dict, verdict: str) -> bool:
    """Return True if *entry* carries the given verdict."""
    new = entry.get("new") or {}
    if isinstance(new, dict):
        if new.get("verdict") == verdict:
            return True
        tags = new.get("tags") or []
        if isinstance(tags, (list, tuple)) and verdict in tags:
            return True
    return False


# ---------------------------------------------------------------------------
# AuditTrail
# ---------------------------------------------------------------------------


class AuditTrail:
    """
    Append-only audit logger backed by a JSON Lines file.

    Thread-safe: a single threading.Lock serialises all writes.
    File rotation: when audit.jsonl exceeds 10 MB it is renamed to
    audit.jsonl.1 (one rotated file is kept; older ones are discarded).
    """

    def __init__(self, audit_path: Path | str | None = None):
        if audit_path is None:
            # The default used to be an in-repo path relative to the repo root.
            # On a set-up checkout that is a symlink into the state dir, so it
            # worked; in a fresh clone it is a real file, and the trail was
            # written straight into the tree (D#1967).
            # state_paths is the single resolver — no local spelling of the path.
            from backend import state_paths  # noqa: PLC0415
            self._path = state_paths.AUDIT_LOG
        else:
            self._path = Path(audit_path).resolve()

        self._lock = threading.Lock()
        self._seq = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(
        self,
        source: str,
        action: str,
        key: str,
        old_value: Any,
        new_value: Any,
        actor: str,
        event_id: str | None = None,
    ) -> None:
        """
        Append one audit entry to the log file.

        Parameters
        ----------
        source:    Module that made the change (e.g. "blackboard").
        action:    What happened (e.g. "write", "delete", "cas").
        key:       The key or identifier that changed.
        old_value: Value before the change (None if it didn't exist).
        new_value: Value after the change (None if it was deleted).
        actor:     Agent or process that triggered the change.
        event_id:  Optional idempotency key. If provided and already seen, call is a no-op.
        """
        # Idempotency dedup check
        if event_id and _check_seen_audit(event_id, "audit_trail"):
            return

        with self._lock:
            self._seq += 1
            entry = {
                "ts": _now_iso(),
                "source": source,
                "action": action,
                "key": key,
                "old": old_value,
                "new": new_value,
                "actor": actor,
                "seq": self._seq,
            }
            self._maybe_rotate()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")

    def query(
        self,
        source: str | None = None,
        action: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Filter audit entries and return up to *limit* matching results.

        All filter parameters are optional; omitting them returns everything
        (up to *limit*). *since* is an ISO 8601 timestamp string.
        """
        since_dt: datetime | None = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                pass

        results: list[dict] = []
        if not self._path.exists():
            return results

        with self._lock:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if source and entry.get("source") != source:
                        continue
                    if action and entry.get("action") != action:
                        continue
                    if actor and entry.get("actor") != actor:
                        continue
                    if since_dt:
                        ts = entry.get("ts", "")
                        try:
                            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if entry_dt < since_dt:
                                continue
                        except ValueError:
                            continue

                    results.append(entry)
                    if len(results) >= limit:
                        break

        return results

    def tail(self, n: int = 20) -> list[dict]:
        """Return the last *n* entries from the audit log."""
        if not self._path.exists():
            return []

        with self._lock:
            lines: list[str] = []
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        lines.append(stripped)

        # Take last n lines
        tail_lines = lines[-n:] if len(lines) > n else lines
        results: list[dict] = []
        for line in tail_lines:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return results

    def stats(self) -> dict:
        """
        Return counts grouped by source and action, plus a total.

        Returns:
            {
                "by_source": {"blackboard": 42, ...},
                "by_action": {"write": 38, ...},
                "total": 100,
            }
        """
        by_source: dict[str, int] = {}
        by_action: dict[str, int] = {}
        total = 0

        if not self._path.exists():
            return {"by_source": by_source, "by_action": by_action, "total": total}

        with self._lock:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    src = entry.get("source", "unknown")
                    act = entry.get("action", "unknown")
                    if not isinstance(src, (str, int)):
                        src = json.dumps(src, sort_keys=True)
                    if not isinstance(act, (str, int)):
                        act = json.dumps(act, sort_keys=True)
                    by_source[src] = by_source.get(src, 0) + 1
                    by_action[act] = by_action.get(act, 0) + 1

        return {"by_source": by_source, "by_action": by_action, "total": total}

    def search(
        self,
        *,
        discussion: int | str | None = None,
        role: str | None = None,
        verdict: str | None = None,
        since_expr: str | None = None,
        until_expr: str | None = None,
        source: str | None = None,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Stream-filter audit entries and return up to *limit* matches.

        All parameters are optional and combined with AND logic.

        Parameters
        ----------
        discussion: Match entries related to this discussion number.
            Checks ``entry.new.discussion``, and whether the key contains
            ``/<N>-``, ``/<N>/``, or ``-<N>-``.
        role: Match ``entry.actor``, ``entry.new.role``, or
            ``entry.new.agent``.
        verdict: Match ``entry.new.verdict`` or ``entry.new.tags`` list.
        since_expr: Relative (``30m``, ``4h``, ``2d``, ``1w``) or
            ISO 8601 string or ``now``.
        until_expr: Same format as *since_expr*; defaults to now.
        source/action/actor: Pass-through filters matching existing fields.
        limit: Stop after returning this many matches (default 100).
        """
        now = datetime.now(timezone.utc)
        since_dt: datetime | None = None
        until_dt: datetime | None = None
        if since_expr is not None:
            since_dt = _parse_time_expr(since_expr, now)
        if until_expr is not None:
            until_dt = _parse_time_expr(until_expr, now)

        disc_str = str(discussion) if discussion is not None else None

        results: list[dict] = []

        files_to_read: list[Path] = []
        # Resolve to the real path so we locate audit.jsonl.1 in the
        # canonical state dir even when self._path is a symlink.
        real_path = self._path.resolve()
        rotated = real_path.with_suffix(".jsonl.1")
        if rotated.exists():
            files_to_read.append(rotated)
        if self._path.exists():
            files_to_read.append(self._path)

        for fpath in files_to_read:
            if len(results) >= limit:
                break
            with self._lock:
                with fpath.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if len(results) >= limit:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # --- time window ---
                        ts_raw = entry.get("ts", "")
                        try:
                            entry_dt = datetime.fromisoformat(
                                ts_raw.replace("Z", "+00:00")
                            )
                        except ValueError:
                            entry_dt = None

                        if since_dt is not None:
                            if entry_dt is None or entry_dt < since_dt:
                                continue
                        if until_dt is not None:
                            if entry_dt is None or entry_dt > until_dt:
                                continue

                        # --- pass-through filters ---
                        if source and entry.get("source") != source:
                            continue
                        if action and entry.get("action") != action:
                            continue
                        if actor and entry.get("actor") != actor:
                            continue

                        # --- discussion ---
                        if disc_str is not None and not _match_discussion(
                            entry, disc_str
                        ):
                            continue

                        # --- role ---
                        if role is not None and not _match_role(entry, role):
                            continue

                        # --- verdict ---
                        if verdict is not None and not _match_verdict(
                            entry, verdict
                        ):
                            continue

                        results.append(entry)

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_rotate(self) -> None:
        """Rotate audit.jsonl to audit.jsonl.1 when it exceeds _ROTATION_BYTES.

        Caller must hold self._lock.

        When self._path is a symlink, we rotate the *real target* (resolved
        path), not the symlink itself.  This keeps the symlink intact so that
        future appends continue to land in the canonical state-dir file rather
        than creating a new real file next to the symlink.
        """
        if not self._path.exists():
            return
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < _ROTATION_BYTES:
            return
        # Resolve to the real path so a symlink is never moved.
        real_path = self._path.resolve()
        rotated = real_path.with_suffix(".jsonl.1")
        try:
            real_path.rename(rotated)
        except OSError:
            pass  # best-effort rotation


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: AuditTrail | None = None
_singleton_lock = threading.Lock()


def get_audit_trail(audit_path: Path | str | None = None) -> AuditTrail:
    """Return (or create) the module-level AuditTrail singleton."""
    global _singleton  # noqa: PLW0603
    with _singleton_lock:
        if _singleton is None:
            _singleton = AuditTrail(audit_path)
    return _singleton


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="audit_trail",
        description="Query the autonomous team audit log.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # tail
    tail_p = sub.add_parser("tail", help="Print last N entries (default: 20)")
    tail_p.add_argument("--n", type=int, default=20, metavar="N")

    # query
    q = sub.add_parser("query", help="Filter audit entries")
    q.add_argument("--source", default=None)
    q.add_argument("--action", default=None)
    q.add_argument("--actor", default=None)
    q.add_argument("--since", default=None, metavar="ISO8601")
    q.add_argument("--limit", type=int, default=50)

    # stats
    sub.add_parser("stats", help="Print counts by source and action")

    # search
    s = sub.add_parser("search", help="Filter entries by discussion, role, verdict, time")
    s.add_argument("--discussion", default=None, metavar="N",
                   help="Match entries for this discussion number")
    s.add_argument("--role", default=None, metavar="ROLE",
                   help="Match actor, new.role, or new.agent")
    s.add_argument("--verdict", default=None, metavar="V",
                   help="Match new.verdict or V in new.tags")
    s.add_argument("--since", default=None, metavar="EXPR",
                   help="Relative (30m, 4h, 2d, 1w) or ISO 8601 or 'now'")
    s.add_argument("--until", default=None, metavar="EXPR",
                   help="Same format as --since; defaults to now")
    s.add_argument("--source", default=None, metavar="SRC",
                   help="Pass-through filter on entry.source")
    s.add_argument("--action", default=None, metavar="ACTION",
                   help="Pass-through filter on entry.action")
    s.add_argument("--actor", default=None, metavar="ACTOR",
                   help="Pass-through filter on entry.actor")
    s.add_argument("--limit", type=int, default=100, metavar="N",
                   help="Maximum results to return (default: 100)")
    s.add_argument("--format", dest="fmt", choices=["human", "json"],
                   default="human", help="Output format (default: human)")

    # append — idempotent append with optional event_id
    app_p = sub.add_parser("append", help="Append one audit entry (idempotent with --event-id)")
    app_p.add_argument("--source", default="agent", metavar="SRC")
    app_p.add_argument("--action", default="write", metavar="ACTION")
    app_p.add_argument("--key", default="", metavar="KEY")
    app_p.add_argument("--actor", default="cli", metavar="ACTOR")
    app_p.add_argument("--event-id", default=None, metavar="ID",
                       help="Idempotency key — skip if already seen in seen.sqlite")

    return p


def _format_entry(e: dict) -> str:
    ts = e.get("ts", "?")
    source = e.get("source", "?")
    action = e.get("action", "?")
    key = e.get("key", "?")
    old = json.dumps(e.get("old"), default=str)
    new = json.dumps(e.get("new"), default=str)
    actor = e.get("actor", "?")
    seq = e.get("seq", "?")
    return f"[{seq}] {ts}  {source}/{action}  {key}  {old} -> {new}  (by {actor})"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    at = get_audit_trail()

    if args.command == "tail":
        entries = at.tail(args.n)
        if not entries:
            print("(no entries)")
            return 0
        for e in entries:
            print(_format_entry(e))
        return 0

    if args.command == "query":
        entries = at.query(
            source=args.source,
            action=args.action,
            actor=args.actor,
            since=args.since,
            limit=args.limit,
        )
        if not entries:
            print("(no matching entries)")
            return 0
        for e in entries:
            print(_format_entry(e))
        return 0

    if args.command == "stats":
        s = at.stats()
        print(f"Total entries: {s['total']}")
        print("By source:")
        for k, v in sorted(s["by_source"].items()):
            print(f"  {k:<20} {v}")
        print("By action:")
        for k, v in sorted(s["by_action"].items()):
            print(f"  {k:<20} {v}")
        return 0

    if args.command == "search":
        try:
            entries = at.search(
                discussion=args.discussion,
                role=args.role,
                verdict=args.verdict,
                since_expr=args.since,
                until_expr=args.until,
                source=args.source,
                action=args.action,
                actor=args.actor,
                limit=args.limit,
            )
        except ValueError as exc:
            # Identify which flag caused the bad time expression
            for flag, val in [("--since", args.since), ("--until", args.until)]:
                if val is not None:
                    try:
                        _parse_time_expr(val)
                    except ValueError:
                        print(f"error: bad time expression for {flag}: {exc}", file=sys.stderr)
                        return 2
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if args.fmt == "json":
            for e in entries:
                print(json.dumps(e, default=str))
        else:
            if not entries:
                print("(no matching entries)")
            else:
                for e in entries:
                    print(_format_entry(e))
        return 0

    if args.command == "append":
        event_id = getattr(args, "event_id", None)
        at.emit(
            source=args.source,
            action=args.action,
            key=args.key,
            old_value=None,
            new_value=None,
            actor=args.actor,
            event_id=event_id,
        )
        if event_id:
            print(f"appended (event_id={event_id})")
        else:
            print("appended")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
