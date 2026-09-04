"""
Agent memory persistence — dual-store for lessons from past runs.

Lessons are written to both the blackboard (backward compat) and a dedicated
``agent_lessons`` SQLite table that survives session boundaries. New agents
query the SQL table by default, which supports cross-session retrieval,
relevance decay, and richer filtering.

Usage (CLI):
    python backend/agent_memory.py record --discussion 14 --role executor --type failure \
        --content "TypeScript type error..." --files "a.ts,b.ts" --tags "type-error"
    python backend/agent_memory.py query --files "a.ts" --tags "type-error" --limit 5
    python backend/agent_memory.py query --session-only
    python backend/agent_memory.py context --files "a.ts,b.ts"
    python backend/agent_memory.py prune --max-age 30 --max-entries 200
    python backend/agent_memory.py migrate
    python backend/agent_memory.py decay
    python backend/agent_memory.py stats

Usage (library):
    from backend.agent_memory import AgentMemory
    mem = AgentMemory()
    key = mem.record_lesson(discussion=14, role="executor", lesson_type="failure",
                            content="...", files=["a.ts"], tags=["type-error"])
    lessons = mem.query_lessons(files=["a.ts"], limit=5)
    lessons = mem.query_lessons(cross_session=False)  # current session only
    block = mem.get_context_block(files=["a.ts"])
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Allow running as a script from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.blackboard import Blackboard  # noqa: E402
from backend.db import get_db  # noqa: E402
from backend.session_manager import SessionManager  # noqa: E402

_MEMORY_PREFIX = "memory/"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string into an aware datetime."""
    return datetime.fromisoformat(ts)


def _days_since(ts: str) -> float:
    """Return fractional days since the given ISO timestamp."""
    try:
        recorded = _parse_iso(ts)
        delta = datetime.now(timezone.utc) - recorded
        return delta.total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 0.0


class AgentMemory:
    """
    Stores and retrieves structured lessons from agent runs.

    Lessons are written to both the blackboard (backward compat) and the
    ``agent_lessons`` SQLite table. Queries default to the SQL table, which
    supports cross-session retrieval and relevance decay.
    """

    def __init__(self, bb: Optional[Blackboard] = None, db=None):
        if bb is None:
            bb = Blackboard()
        self._bb = bb
        self._db = db if db is not None else get_db()

    def _current_session_id(self) -> Optional[str]:
        """Return the current session ID, or None if no active session."""
        try:
            sm = SessionManager()
            session = sm.current_session()
            return session["session_id"] if session else None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_lesson(
        self,
        discussion: int,
        role: str,
        lesson_type: str,
        content: str,
        files: list,
        tags: list,
    ) -> str:
        """
        Store a lesson in both the blackboard and the agent_lessons SQL table.

        Returns the blackboard key for the stored lesson (backward compat).
        """
        ts = _now_iso()
        safe_ts = ts.replace(":", "-")
        key = f"{_MEMORY_PREFIX}{discussion}-{safe_ts}"
        lesson_id = str(uuid.uuid4())
        session_id = self._current_session_id()

        lesson = {
            "id": lesson_id,
            "discussion": discussion,
            "role": role,
            "lesson_type": lesson_type,
            "content": content,
            "files": list(files),
            "tags": list(tags),
            "recorded_at": ts,
        }

        # Backward-compat: write to blackboard
        self._bb.write(key, lesson, updated_by=role)

        # Primary store: write to SQL agent_lessons table
        conn = self._db._conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_lessons
              (id, session_id, discussion, role, lesson_type, content, files, tags, created_at, relevance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0)
            """,
            (
                lesson_id,
                session_id,
                discussion,
                role,
                lesson_type,
                content,
                json.dumps(list(files), ensure_ascii=False),
                json.dumps(list(tags), ensure_ascii=False),
                ts,
            ),
        )
        conn.commit()

        return key

    def query_lessons(
        self,
        files: Optional[list] = None,
        tags: Optional[list] = None,
        role: Optional[str] = None,
        lesson_type: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 5,
        cross_session: bool = True,
    ) -> list:
        """
        Find lessons from the agent_lessons SQL table.

        Filter by files (any overlap), tags (any overlap), role, lesson_type.
        When cross_session=False, restricts to the current session only.
        Results are ordered by relevance * recency_factor DESC.
        """
        conn = self._db._conn()
        rows = conn.execute(
            "SELECT * FROM agent_lessons ORDER BY created_at DESC"
        ).fetchall()

        lessons = []
        for row in rows:
            d = dict(row)
            try:
                d["files"] = json.loads(d["files"] or "[]")
            except (json.JSONDecodeError, TypeError):
                d["files"] = []
            try:
                d["tags"] = json.loads(d["tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
            lessons.append(d)

        # Session filter
        if not cross_session:
            current_sid = session_id or self._current_session_id()
            if current_sid:
                lessons = [l for l in lessons if l.get("session_id") == current_sid]

        # Role filter
        if role is not None:
            lessons = [l for l in lessons if l.get("role") == role]

        # Lesson type filter
        if lesson_type is not None:
            lessons = [l for l in lessons if l.get("lesson_type") == lesson_type]

        # File/tag filters
        if files is not None or tags is not None:
            file_set = set(files) if files else set()
            tag_set = set(tags) if tags else set()
            filtered = []
            for lesson in lessons:
                lesson_files = set(lesson.get("files", []))
                lesson_tags = set(lesson.get("tags", []))
                matches_files = bool(file_set & lesson_files)
                matches_tags = bool(tag_set & lesson_tags)
                if files is not None and tags is not None:
                    if matches_files or matches_tags:
                        filtered.append(lesson)
                elif files is not None:
                    if matches_files:
                        filtered.append(lesson)
                else:
                    if matches_tags:
                        filtered.append(lesson)
            lessons = filtered

        # Sort by relevance * recency_factor DESC
        def _score(lesson: dict) -> float:
            rel = lesson.get("relevance", 1.0) or 1.0
            days = _days_since(lesson.get("created_at", ""))
            recency = 1.0 / (1.0 + days)
            return rel * recency

        lessons.sort(key=_score, reverse=True)
        return lessons[:limit]

    def get_context_block(
        self,
        files: list,
        role: Optional[str] = None,
    ) -> str:
        """
        Return a formatted markdown block for injection into an agent prompt.

        Queries lessons matching the given files (and optionally role) and formats
        them with session, age, and tag information.
        """
        lessons = self.query_lessons(files=files, role=role)
        if not lessons:
            return ""

        lines = ["## Relevant lessons from past sessions", ""]
        for lesson in lessons:
            lesson_type = lesson.get("lesson_type", "unknown")
            discussion = lesson.get("discussion", "?")
            lesson_role = lesson.get("role", "?")
            content = lesson.get("content", "")
            lesson_files = lesson.get("files", [])
            lesson_tags = lesson.get("tags", [])
            session_id = lesson.get("session_id", "")
            created_at = lesson.get("created_at", "")

            days = _days_since(created_at)
            if days < 1:
                age_str = "today"
            elif days < 2:
                age_str = "1 day ago"
            else:
                age_str = f"{int(days)} days ago"

            sid_short = session_id[:6] if session_id else "unknown"

            lines.append(
                f"### [{lesson_type}] {lesson_role} in Discussion #{discussion}"
                f" ({age_str}, session {sid_short})"
            )
            lines.append(content)
            parts = []
            if lesson_files:
                parts.append("Files: " + ", ".join(lesson_files))
            if lesson_tags:
                parts.append("Tags: " + ", ".join(lesson_tags))
            if parts:
                lines.append(" | ".join(parts))
            lines.append("")

        # Remove trailing blank line
        while lines and lines[-1] == "":
            lines.pop()

        return "\n".join(lines)

    def migrate_from_blackboard(self) -> int:
        """
        Move existing blackboard memory/* entries into the agent_lessons table.

        Marks migrated blackboard entries with migrated_to_sql=True to prevent
        double-migration. Returns the number of entries migrated.
        """
        all_keys = self._bb.list_keys(_MEMORY_PREFIX)
        migrated = 0
        conn = self._db._conn()

        for key in all_keys:
            raw = self._bb.read(key)
            if not isinstance(raw, dict):
                continue
            # Skip already-migrated entries
            if raw.get("migrated_to_sql"):
                continue

            lesson_id = raw.get("id") or str(uuid.uuid4())
            session_id = raw.get("session_id")
            discussion = raw.get("discussion")
            role = raw.get("role", "unknown")
            lesson_type = raw.get("lesson_type", "pattern")
            content = raw.get("content", "")
            files = raw.get("files", [])
            tags = raw.get("tags", [])
            created_at = raw.get("recorded_at") or raw.get("created_at") or _now_iso()

            # Check if already in SQL (idempotent)
            existing = conn.execute(
                "SELECT id FROM agent_lessons WHERE id = ?", (lesson_id,)
            ).fetchone()
            if existing:
                # Mark blackboard entry as migrated without re-inserting
                raw["migrated_to_sql"] = True
                self._bb.write(key, raw, updated_by="migrate")
                continue

            conn.execute(
                """
                INSERT OR REPLACE INTO agent_lessons
                  (id, session_id, discussion, role, lesson_type, content, files, tags, created_at, relevance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0)
                """,
                (
                    lesson_id,
                    session_id,
                    discussion,
                    role,
                    lesson_type,
                    content,
                    json.dumps(list(files), ensure_ascii=False),
                    json.dumps(list(tags), ensure_ascii=False),
                    created_at,
                ),
            )
            # Mark blackboard entry as migrated (keep it — read-only fallback)
            raw["migrated_to_sql"] = True
            self._bb.write(key, raw, updated_by="migrate")
            migrated += 1

        conn.commit()
        return migrated

    def decay_relevance(self) -> int:
        """
        Reduce relevance of old lessons.

        - Lessons older than 7 days:  relevance *= 0.9
        - Lessons older than 30 days: relevance *= 0.7
        - Lessons older than 90 days: relevance *= 0.5

        Returns the number of rows updated.
        """
        now = datetime.now(timezone.utc)
        cutoff_7  = (now - timedelta(days=7)).isoformat(timespec="seconds")
        cutoff_30 = (now - timedelta(days=30)).isoformat(timespec="seconds")
        cutoff_90 = (now - timedelta(days=90)).isoformat(timespec="seconds")

        conn = self._db._conn()
        total = 0

        for cutoff, factor in [
            (cutoff_90, 0.5),
            (cutoff_30, 0.7),
            (cutoff_7,  0.9),
        ]:
            cursor = conn.execute(
                "UPDATE agent_lessons SET relevance = relevance * ? WHERE created_at < ?",
                (factor, cutoff),
            )
            total += cursor.rowcount

        conn.commit()
        return total

    def stats(self) -> dict:
        """
        Return lesson counts grouped by role and lesson_type.

        Returns a dict with keys:
          - total: int
          - by_role: {role: count}
          - by_type: {lesson_type: count}
          - by_session: {session_id: count}
        """
        conn = self._db._conn()
        total = conn.execute("SELECT COUNT(*) FROM agent_lessons").fetchone()[0]

        by_role: dict = {}
        for row in conn.execute(
            "SELECT role, COUNT(*) as cnt FROM agent_lessons GROUP BY role"
        ).fetchall():
            by_role[row[0]] = row[1]

        by_type: dict = {}
        for row in conn.execute(
            "SELECT lesson_type, COUNT(*) as cnt FROM agent_lessons GROUP BY lesson_type"
        ).fetchall():
            by_type[row[0]] = row[1]

        by_session: dict = {}
        for row in conn.execute(
            "SELECT COALESCE(session_id, 'none'), COUNT(*) as cnt"
            " FROM agent_lessons GROUP BY session_id"
        ).fetchall():
            by_session[row[0]] = row[1]

        return {
            "total": total,
            "by_role": by_role,
            "by_type": by_type,
            "by_session": by_session,
        }

    def prune_old(
        self,
        max_age_days: int = 30,
        max_entries: int = 200,
    ) -> int:
        """
        Remove entries older than max_age_days or exceeding max_entries (oldest first).

        Prunes both the blackboard and the SQL table.
        Returns the number of entries removed.
        """
        removed = 0

        # --- SQL prune ---
        conn = self._db._conn()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=max_age_days)).isoformat(timespec="seconds")

        cursor = conn.execute(
            "DELETE FROM agent_lessons WHERE created_at < ?", (cutoff,)
        )
        removed += cursor.rowcount

        # Prune oldest beyond max_entries
        total = conn.execute("SELECT COUNT(*) FROM agent_lessons").fetchone()[0]
        if total > max_entries:
            excess = total - max_entries
            old_ids = conn.execute(
                "SELECT id FROM agent_lessons ORDER BY created_at ASC LIMIT ?", (excess,)
            ).fetchall()
            for (old_id,) in old_ids:
                conn.execute("DELETE FROM agent_lessons WHERE id = ?", (old_id,))
                removed += 1

        conn.commit()

        # --- Blackboard prune (original logic) ---
        all_keys = self._bb.list_keys(_MEMORY_PREFIX)
        entries: list = []
        for key in all_keys:
            value = self._bb.read(key)
            if not isinstance(value, dict):
                continue
            ts = value.get("recorded_at", "")
            entries.append((ts, key))

        entries.sort(key=lambda x: x[0])

        now = datetime.now(timezone.utc)
        bb_cutoff = now - timedelta(days=max_age_days)
        keys_to_delete: set = set()

        for ts, key in entries:
            if ts:
                try:
                    recorded_at = _parse_iso(ts)
                    if recorded_at < bb_cutoff:
                        keys_to_delete.add(key)
                except (ValueError, TypeError):
                    pass

        surviving = [key for _, key in entries if key not in keys_to_delete]
        if len(surviving) > max_entries:
            excess = len(surviving) - max_entries
            keys_to_delete.update(surviving[:excess])

        for key in keys_to_delete:
            if self._bb.delete(key):
                removed += 1

        return removed


# ------------------------------------------------------------------
# Module-level convenience functions
# ------------------------------------------------------------------


def record_lesson(
    discussion: int,
    role: str,
    lesson_type: str,
    content: str,
    files: list,
    tags: list,
) -> str:
    """Store a lesson using the default stores. Returns the blackboard key."""
    return AgentMemory().record_lesson(discussion, role, lesson_type, content, files, tags)


def query_lessons(
    files: Optional[list] = None,
    tags: Optional[list] = None,
    role: Optional[str] = None,
    limit: int = 5,
    cross_session: bool = True,
) -> list:
    """Query lessons from SQL using the default store."""
    return AgentMemory().query_lessons(
        files=files, tags=tags, role=role, limit=limit, cross_session=cross_session
    )


def get_context_block(files: list, role: Optional[str] = None) -> str:
    """Return a formatted context block using the default store."""
    return AgentMemory().get_context_block(files=files, role=role)


def prune_old(max_age_days: int = 30, max_entries: int = 200) -> int:
    """Prune old entries. Returns count removed."""
    return AgentMemory().prune_old(max_age_days=max_age_days, max_entries=max_entries)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent_memory",
        description="Cross-session memory store for agent lessons.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # record
    r = sub.add_parser("record", help="Record a lesson from an agent run")
    r.add_argument("--discussion", type=int, required=True, metavar="N")
    r.add_argument("--role", required=True, help="Agent role (executor, code-reviewer, ...)")
    r.add_argument("--type", dest="lesson_type", required=True,
                   choices=["failure", "success", "pattern", "workaround"],
                   help="Lesson type")
    r.add_argument("--content", required=True, help="Human-readable lesson content")
    r.add_argument("--files", default="",
                   help="Comma-separated list of file paths involved")
    r.add_argument("--tags", default="",
                   help="Comma-separated list of tags (e.g. type-error,import-error)")

    # query
    q = sub.add_parser("query", help="Query lessons by files, tags, or role")
    q.add_argument("--files", default="",
                   help="Comma-separated file paths to match")
    q.add_argument("--tags", default="",
                   help="Comma-separated tags to match")
    q.add_argument("--role", default="",
                   help="Filter by agent role")
    q.add_argument("--limit", type=int, default=5,
                   help="Maximum number of results (default: 5)")
    q_session = q.add_mutually_exclusive_group()
    q_session.add_argument("--cross-session", dest="cross_session", action="store_true",
                            default=True, help="Query across all sessions (default)")
    q_session.add_argument("--session-only", dest="cross_session", action="store_false",
                            help="Restrict to current session only")

    # context
    ctx = sub.add_parser("context", help="Print a prompt-injection context block")
    ctx.add_argument("--files", required=True,
                     help="Comma-separated file paths to match")
    ctx.add_argument("--role", default="",
                     help="Filter by agent role")

    # prune
    pr = sub.add_parser("prune", help="Remove old or excess entries")
    pr.add_argument("--max-age", type=int, default=30, metavar="DAYS",
                    help="Remove entries older than this many days (default: 30)")
    pr.add_argument("--max-entries", type=int, default=200,
                    help="Keep at most this many entries (default: 200)")

    # migrate
    sub.add_parser("migrate", help="Migrate blackboard memory/* entries to SQL")

    # decay
    sub.add_parser("decay", help="Run relevance decay on old lessons")

    # stats
    sub.add_parser("stats", help="Show lesson counts by role, type, and session")

    return p


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    mem = AgentMemory()

    if args.command == "record":
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        key = mem.record_lesson(
            discussion=args.discussion,
            role=args.role,
            lesson_type=args.lesson_type,
            content=args.content,
            files=files,
            tags=tags,
        )
        print(key)
        return 0

    if args.command == "query":
        files = [f.strip() for f in args.files.split(",") if f.strip()] or None
        tags = [t.strip() for t in args.tags.split(",") if t.strip()] or None
        role = args.role.strip() or None
        lessons = mem.query_lessons(
            files=files, tags=tags, role=role, limit=args.limit,
            cross_session=args.cross_session,
        )
        print(json.dumps(lessons, indent=2, ensure_ascii=False))
        return 0

    if args.command == "context":
        files = [f.strip() for f in args.files.split(",") if f.strip()]
        role = args.role.strip() or None
        block = mem.get_context_block(files=files, role=role)
        print(block)
        return 0

    if args.command == "prune":
        count = mem.prune_old(max_age_days=args.max_age, max_entries=args.max_entries)
        print(f"pruned {count} entries")
        return 0

    if args.command == "migrate":
        count = mem.migrate_from_blackboard()
        print(f"migrated {count} entries from blackboard to SQL")
        return 0

    if args.command == "decay":
        count = mem.decay_relevance()
        print(f"updated relevance on {count} entries")
        return 0

    if args.command == "stats":
        data = mem.stats()
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
