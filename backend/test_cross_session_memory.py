"""
Tests for cross-session memory — agent_lessons SQLite persistence.

Covers:
  1. Table creation on db init
  2. record_lesson writes to both blackboard and SQL
  3. cross-session query returns lessons from multiple sessions
  4. session-only query restricts to current session
  5. migrate_from_blackboard idempotency
  6. decay_relevance math
  7. get_context_block formatting
  8. /memory/lessons API endpoint
  9. /memory/stats API endpoint
 10. query ordering by relevance * recency
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import Database
from backend.blackboard import Blackboard


def _make_db(tmp_path: Path) -> Database:
    """Create an in-process Database backed by a temp file."""
    return Database(db_path=tmp_path / "state.db")


def _make_memory(db: Database, session_id: str | None = "test-session-001",
                 bb_root: Path | None = None):
    """Return an AgentMemory wired to the given in-process Database."""
    from backend.agent_memory import AgentMemory
    from backend.blackboard import Blackboard

    bb = Blackboard(root=bb_root) if bb_root else Blackboard(root=db._path.parent / "blackboard")
    mem = AgentMemory(bb=bb)
    mem._db = db
    # Patch current_session_id
    mem._current_session_id = lambda: session_id
    return mem


class TestTableCreation(unittest.TestCase):
    """Test 1: agent_lessons table is created automatically."""

    def test_table_exists_after_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            conn = db._conn()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_lessons'"
            ).fetchall()
            self.assertEqual(len(rows), 1)

    def test_indexes_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            conn = db._conn()
            indexes = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='agent_lessons'"
                ).fetchall()
            ]
            self.assertIn("idx_lessons_role", indexes)
            self.assertIn("idx_lessons_type", indexes)
            self.assertIn("idx_lessons_created", indexes)


class TestRecordLesson(unittest.TestCase):
    """Test 2: record_lesson writes to both blackboard and SQL."""

    def test_record_writes_to_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem = _make_memory(db)
            mem.record_lesson(
                discussion=10, role="executor", lesson_type="failure",
                content="Test content", files=["a.ts"], tags=["type-error"],
            )
            conn = db._conn()
            rows = conn.execute("SELECT * FROM agent_lessons").fetchall()
            self.assertEqual(len(rows), 1)
            row = dict(rows[0])
            self.assertEqual(row["role"], "executor")
            self.assertEqual(row["lesson_type"], "failure")
            self.assertEqual(row["content"], "Test content")
            self.assertEqual(json.loads(row["files"]), ["a.ts"])

    def test_record_writes_to_blackboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem = _make_memory(db)
            key = mem.record_lesson(
                discussion=10, role="executor", lesson_type="success",
                content="Worked fine", files=[], tags=[],
            )
            val = mem._bb.read(key)
            self.assertIsNotNone(val)
            self.assertEqual(val["role"], "executor")

    def test_record_stores_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem = _make_memory(db, session_id="session-abc")
            mem.record_lesson(
                discussion=5, role="code-reviewer", lesson_type="pattern",
                content="Pattern found", files=[], tags=[],
            )
            conn = db._conn()
            row = dict(conn.execute("SELECT session_id FROM agent_lessons").fetchone())
            self.assertEqual(row["session_id"], "session-abc")


class TestCrossSessionQuery(unittest.TestCase):
    """Test 3: query_lessons(cross_session=True) returns lessons from multiple sessions."""

    def test_cross_session_returns_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem_a = _make_memory(db, session_id="session-aaa")
            mem_a.record_lesson(10, "executor", "failure", "session A lesson", ["x.ts"], [])
            mem_b = _make_memory(db, session_id="session-bbb")
            mem_b.record_lesson(11, "executor", "success", "session B lesson", ["y.ts"], [])

            # Query from any instance with cross_session=True
            results = mem_a.query_lessons(cross_session=True, limit=10)
            sessions = {r["session_id"] for r in results}
            self.assertIn("session-aaa", sessions)
            self.assertIn("session-bbb", sessions)


class TestSessionOnlyQuery(unittest.TestCase):
    """Test 4: query_lessons(cross_session=False) restricts to current session."""

    def test_session_only_excludes_other_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem_a = _make_memory(db, session_id="session-aaa")
            mem_a.record_lesson(10, "executor", "failure", "session A lesson", [], [])
            mem_b = _make_memory(db, session_id="session-bbb")
            mem_b.record_lesson(11, "executor", "success", "session B lesson", [], [])

            results = mem_a.query_lessons(cross_session=False, limit=10)
            session_ids = {r["session_id"] for r in results}
            self.assertIn("session-aaa", session_ids)
            self.assertNotIn("session-bbb", session_ids)


class TestMigration(unittest.TestCase):
    """Test 5: migrate_from_blackboard is idempotent."""

    def test_migrate_moves_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            bb = Blackboard(root=Path(tmp) / "blackboard")
            # Plant a raw blackboard memory entry
            bb.write(
                "memory/7-2026-01-01T00-00-00",
                {
                    "id": "test-id-001",
                    "discussion": 7,
                    "role": "executor",
                    "lesson_type": "pattern",
                    "content": "old lesson",
                    "files": ["old.ts"],
                    "tags": ["old"],
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                },
                updated_by="executor",
            )
            mem = _make_memory(db, bb_root=Path(tmp) / "blackboard")
            count = mem.migrate_from_blackboard()
            self.assertEqual(count, 1)
            conn = db._conn()
            rows = conn.execute("SELECT * FROM agent_lessons WHERE id = 'test-id-001'").fetchall()
            self.assertEqual(len(rows), 1)

    def test_migrate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            bb = Blackboard(root=Path(tmp) / "blackboard")
            bb.write(
                "memory/8-2026-01-01T00-00-00",
                {
                    "id": "test-id-002",
                    "discussion": 8,
                    "role": "executor",
                    "lesson_type": "failure",
                    "content": "duplicate test",
                    "files": [],
                    "tags": [],
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                },
                updated_by="executor",
            )
            mem = _make_memory(db, bb_root=Path(tmp) / "blackboard")
            first = mem.migrate_from_blackboard()
            second = mem.migrate_from_blackboard()
            # Second run should migrate 0 (already marked)
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            conn = db._conn()
            rows = conn.execute(
                "SELECT * FROM agent_lessons WHERE id = 'test-id-002'"
            ).fetchall()
            self.assertEqual(len(rows), 1)


class TestDecayRelevance(unittest.TestCase):
    """Test 6: decay_relevance reduces relevance for old lessons."""

    def _insert_lesson(self, conn, lesson_id: str, created_at: str, relevance: float = 1.0):
        conn.execute(
            """INSERT INTO agent_lessons
               (id, session_id, discussion, role, lesson_type, content, files, tags, created_at, relevance)
               VALUES (?, 'session-x', 1, 'executor', 'pattern', 'test', '[]', '[]', ?, ?)""",
            (lesson_id, created_at, relevance),
        )
        conn.commit()

    def test_decay_reduces_old_lessons(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            conn = db._conn()
            now = datetime.now(timezone.utc)
            ts_8d = (now - timedelta(days=8)).isoformat(timespec="seconds")
            self._insert_lesson(conn, "old-8d", ts_8d, 1.0)

            mem = _make_memory(db)
            mem.decay_relevance()

            row = dict(conn.execute("SELECT relevance FROM agent_lessons WHERE id='old-8d'").fetchone())
            self.assertAlmostEqual(row["relevance"], 0.9, places=5)

    def test_decay_30d_and_90d(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            conn = db._conn()
            now = datetime.now(timezone.utc)
            ts_35d = (now - timedelta(days=35)).isoformat(timespec="seconds")
            ts_95d = (now - timedelta(days=95)).isoformat(timespec="seconds")
            self._insert_lesson(conn, "old-35d", ts_35d, 1.0)
            self._insert_lesson(conn, "old-95d", ts_95d, 1.0)

            mem = _make_memory(db)
            mem.decay_relevance()

            row35 = dict(conn.execute("SELECT relevance FROM agent_lessons WHERE id='old-35d'").fetchone())
            row95 = dict(conn.execute("SELECT relevance FROM agent_lessons WHERE id='old-95d'").fetchone())

            # 35d falls in 30d bucket: 0.7 applied. Also in 7d bucket: 0.9 applied.
            # Order in decay_relevance: 90d (0.5), 30d (0.7), 7d (0.9)
            # For 35d: only 30d and 7d brackets apply → 1.0 * 0.7 * 0.9 = 0.63
            self.assertAlmostEqual(row35["relevance"], 0.63, places=5)

            # For 95d: all three brackets → 1.0 * 0.5 * 0.7 * 0.9 = 0.315
            self.assertAlmostEqual(row95["relevance"], 0.315, places=5)


class TestContextBlock(unittest.TestCase):
    """Test 7: get_context_block formats a markdown block."""

    def test_context_block_includes_lesson_type_and_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem = _make_memory(db, session_id="session-ctx")
            mem.record_lesson(
                discussion=20, role="executor", lesson_type="failure",
                content="Import failed", files=["b.ts"], tags=["import-error"],
            )
            block = mem.get_context_block(files=["b.ts"])
            self.assertIn("[failure]", block)
            self.assertIn("executor", block)
            self.assertIn("Import failed", block)
            self.assertIn("b.ts", block)
            self.assertIn("import-error", block)

    def test_context_block_empty_when_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem = _make_memory(db)
            block = mem.get_context_block(files=["nonexistent.ts"])
            self.assertEqual(block, "")


class TestAPILessons(unittest.TestCase):
    """Test 8: /memory/lessons returns a valid JSON array."""

    def test_memory_lessons_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem = _make_memory(db, session_id="api-session")
            mem.record_lesson(5, "executor", "success", "works", ["c.ts"], ["ok"])

            # Simulate the API handler logic directly
            from backend.agent_memory import AgentMemory
            from backend.blackboard import Blackboard as BB
            bb = BB(root=Path(tmp) / "blackboard")
            api_mem = AgentMemory(bb=bb)
            api_mem._db = db
            api_mem._current_session_id = lambda: "api-session"
            lessons = api_mem.query_lessons(cross_session=True, limit=20)

            self.assertIsInstance(lessons, list)
            self.assertEqual(len(lessons), 1)
            self.assertEqual(lessons[0]["role"], "executor")


class TestAPIStats(unittest.TestCase):
    """Test 9: /memory/stats returns grouped counts."""

    def test_stats_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            mem = _make_memory(db, session_id="stats-session")
            mem.record_lesson(1, "executor", "failure", "fail", [], [])
            mem.record_lesson(2, "executor", "success", "ok", [], [])
            mem.record_lesson(3, "code-reviewer", "pattern", "pat", [], [])

            stats = mem.stats()
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["by_role"].get("executor", 0), 2)
            self.assertEqual(stats["by_role"].get("code-reviewer", 0), 1)
            self.assertEqual(stats["by_type"].get("failure", 0), 1)
            self.assertEqual(stats["by_type"].get("success", 0), 1)
            self.assertEqual(stats["by_type"].get("pattern", 0), 1)


class TestQueryOrdering(unittest.TestCase):
    """Test 10: query_lessons orders by relevance * recency DESC."""

    def test_recent_lesson_ranks_above_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(Path(tmp))
            conn = db._conn()
            now = datetime.now(timezone.utc)
            recent_ts = (now - timedelta(hours=1)).isoformat(timespec="seconds")
            old_ts = (now - timedelta(days=60)).isoformat(timespec="seconds")
            # Insert old lesson with high relevance, recent with low relevance
            conn.execute(
                "INSERT INTO agent_lessons (id, session_id, discussion, role, lesson_type, content, files, tags, created_at, relevance) "
                "VALUES ('old-high', 's', 1, 'executor', 'pattern', 'old high relevance', '[]', '[]', ?, 0.9)",
                (old_ts,),
            )
            conn.execute(
                "INSERT INTO agent_lessons (id, session_id, discussion, role, lesson_type, content, files, tags, created_at, relevance) "
                "VALUES ('recent-low', 's', 2, 'executor', 'pattern', 'recent low relevance', '[]', '[]', ?, 0.1)",
                (recent_ts,),
            )
            conn.commit()
            from backend.agent_memory import AgentMemory
            from backend.blackboard import Blackboard as BB
            bb = BB(root=Path(tmp) / "blackboard")
            mem = AgentMemory(bb=bb)
            mem._db = db
            mem._current_session_id = lambda: "s"
            results = mem.query_lessons(cross_session=True, limit=10)
            # The recent lesson (even with low relevance) should beat the old high-relevance one
            # recent score: 0.1 * (1/(1+0.04)) ≈ 0.096
            # old score: 0.9 * (1/(1+60)) ≈ 0.0147
            self.assertEqual(results[0]["id"], "recent-low")
            self.assertEqual(results[1]["id"], "old-high")


if __name__ == "__main__":
    unittest.main()
