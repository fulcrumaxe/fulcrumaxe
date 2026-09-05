"""tests/orchestrator/test_auto_routed_marker.py — Tests for auto_routed audit column.

Covers D#1364 Spec item #3:
- agent_run schema has auto_routed BOOLEAN column after _ensure_schema
- idempotent migration adds auto_routed to tables created before D#1364
- complete_run(auto_routed=True) persists True; False persists False; None stays NULL
- COALESCE: a second complete_run without auto_routed does not overwrite the first value
- _write_agent_run propagates RunResult.auto_routed to DuckDB
- dispatch.route() with SDK_AUTO_ROUTE=1 + eligible role → auto_routed=True in DB
- dispatch.route() with explicit sdk_eligible=True (no auto-route gate) → auto_routed=False
- dispatch.route() for CC (ineligible role, no gate) → auto_routed NULL
- old rows (pre-D#1364) stay NULL after migration

No real Anthropic API calls. DuckDB is isolated via STATS_DB_PATH.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Isolated DuckDB stats file, wired via STATS_DB_PATH."""
    db = tmp_path / "test_stats.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(db))
    return db


def _connect(db: Path):
    import duckdb
    return duckdb.connect(str(db))


def _fetch_run(db: Path, agent_id: str) -> dict | None:
    """Return the agent_run row as a dict, or None."""
    conn = _connect(db)
    try:
        row = conn.execute(
            "SELECT * FROM agent_run WHERE agent_id = ?", [agent_id]
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_run' ORDER BY ordinal_position"
        ).fetchall()]
        return dict(zip(cols, row))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchemaHasAutoRouted:
    """_ensure_schema creates the auto_routed BOOLEAN column."""

    def test_new_table_has_auto_routed_column(self, tmp_db):
        import duckdb
        from backend.agent_run_tracker import _ensure_schema

        conn = duckdb.connect(str(tmp_db))
        try:
            _ensure_schema(conn)
            cols = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name='agent_run'"
            ).fetchall()}
        finally:
            conn.close()

        assert "auto_routed" in cols

    def test_migration_adds_auto_routed_to_old_table(self, tmp_db):
        """A table created without auto_routed gains it on next _ensure_schema call."""
        import duckdb
        from backend.agent_run_tracker import _ensure_schema

        # Create pre-D#1364 table without auto_routed
        conn = duckdb.connect(str(tmp_db))
        try:
            conn.execute("""
                CREATE TABLE agent_run (
                    agent_id   VARCHAR PRIMARY KEY,
                    role       VARCHAR NOT NULL,
                    discussion INTEGER,
                    pr         INTEGER,
                    start_ts   TIMESTAMPTZ NOT NULL,
                    end_ts     TIMESTAMPTZ,
                    duration_s DOUBLE,
                    verdict    VARCHAR,
                    routed_via TEXT
                )
            """)
        finally:
            conn.close()

        conn2 = duckdb.connect(str(tmp_db))
        try:
            _ensure_schema(conn2)
            cols = {r[0] for r in conn2.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name='agent_run'"
            ).fetchall()}
        finally:
            conn2.close()

        assert "auto_routed" in cols

    def test_old_rows_stay_null_after_migration(self, tmp_db):
        """Pre-D#1364 rows keep auto_routed=NULL after migration."""
        import duckdb
        from datetime import datetime, timezone
        from backend.agent_run_tracker import _ensure_schema

        conn = duckdb.connect(str(tmp_db))
        try:
            # Old table without auto_routed (but with pr so _ensure_schema index doesn't fail)
            conn.execute("""
                CREATE TABLE agent_run (
                    agent_id   VARCHAR PRIMARY KEY,
                    role       VARCHAR NOT NULL,
                    pr         INTEGER,
                    start_ts   TIMESTAMPTZ NOT NULL,
                    routed_via TEXT
                )
            """)
            now = datetime.now(timezone.utc)
            conn.execute(
                "INSERT INTO agent_run (agent_id, role, start_ts, routed_via) VALUES (?, ?, ?, ?)",
                ["old-row-1", "executor", now, "cc"],
            )
        finally:
            conn.close()

        conn2 = duckdb.connect(str(tmp_db))
        try:
            _ensure_schema(conn2)
            row = conn2.execute(
                "SELECT auto_routed FROM agent_run WHERE agent_id = 'old-row-1'"
            ).fetchone()
        finally:
            conn2.close()

        assert row is not None
        assert row[0] is None  # old rows stay NULL


# ---------------------------------------------------------------------------
# complete_run writes auto_routed
# ---------------------------------------------------------------------------


class TestCompleteRunAutoRouted:
    """complete_run persists auto_routed correctly."""

    def test_complete_run_writes_true(self, tmp_db):
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="ar-true-1", role="docs-writer", event_id="ar-true-1")
        complete_run(agent_id="ar-true-1", verdict="done", routed_via="sdk", auto_routed=True)

        row = _fetch_run(tmp_db, "ar-true-1")
        assert row is not None
        assert row["auto_routed"] is True

    def test_complete_run_writes_false(self, tmp_db):
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="ar-false-1", role="docs-writer", event_id="ar-false-1")
        complete_run(agent_id="ar-false-1", verdict="done", routed_via="sdk", auto_routed=False)

        row = _fetch_run(tmp_db, "ar-false-1")
        assert row is not None
        assert row["auto_routed"] is False

    def test_complete_run_none_stays_null(self, tmp_db):
        """CC runs and pre-D#1364 rows have auto_routed=NULL."""
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="ar-null-1", role="executor", event_id="ar-null-1")
        complete_run(agent_id="ar-null-1", verdict="done", routed_via="cc")

        row = _fetch_run(tmp_db, "ar-null-1")
        assert row is not None
        assert row["auto_routed"] is None

    def test_complete_run_coalesce_preserves_existing_value(self, tmp_db):
        """COALESCE: a second complete_run without auto_routed does not overwrite True."""
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="ar-coalesce-1", role="docs-writer", event_id="ar-coalesce-1")
        complete_run(agent_id="ar-coalesce-1", verdict="done", routed_via="sdk", auto_routed=True)
        # Second call without auto_routed — should not overwrite True to NULL
        complete_run(agent_id="ar-coalesce-1", verdict="done")

        row = _fetch_run(tmp_db, "ar-coalesce-1")
        assert row is not None
        assert row["auto_routed"] is True


# ---------------------------------------------------------------------------
# _write_agent_run propagates auto_routed
# ---------------------------------------------------------------------------


class TestWriteAgentRunAutoRouted:
    """_write_agent_run writes RunResult.auto_routed to the DB row."""

    def test_auto_routed_true_written_to_db(self, tmp_db):
        from backend.orchestrator.sdk_runner import RunResult, _write_agent_run

        result = RunResult(
            agent_id="war-auto-1",
            role="docs-writer",
            discussion=1364,
            pr=None,
            verdict="done",
            final_text="ok",
            input_tokens=100,
            output_tokens=50,
            tool_calls_count=2,
            prompt_sha256="abc123",
            start_ts="2026-05-21T00:00:00Z",
            end_ts="2026-05-21T00:01:00Z",
            routed_via="sdk",
            auto_routed=True,
        )
        _write_agent_run(result)

        row = _fetch_run(tmp_db, "war-auto-1")
        assert row is not None
        assert row["auto_routed"] is True

    def test_explicit_sdk_lane_writes_false(self, tmp_db):
        from backend.orchestrator.sdk_runner import RunResult, _write_agent_run

        result = RunResult(
            agent_id="war-explicit-1",
            role="run-analyst",
            discussion=1364,
            pr=None,
            verdict="done",
            final_text="ok",
            input_tokens=80,
            output_tokens=30,
            tool_calls_count=1,
            prompt_sha256="def456",
            start_ts="2026-05-21T00:00:00Z",
            end_ts="2026-05-21T00:01:00Z",
            routed_via="sdk",
            auto_routed=False,
        )
        _write_agent_run(result)

        row = _fetch_run(tmp_db, "war-explicit-1")
        assert row is not None
        assert row["auto_routed"] is False

    def test_cc_run_writes_null(self, tmp_db):
        from backend.orchestrator.sdk_runner import RunResult, _write_agent_run

        result = RunResult(
            agent_id="war-cc-1",
            role="executor",
            discussion=1364,
            pr=None,
            verdict="done",
            final_text="ok",
            input_tokens=200,
            output_tokens=100,
            tool_calls_count=0,
            prompt_sha256="ghi789",
            start_ts="2026-05-21T00:00:00Z",
            end_ts="2026-05-21T00:01:00Z",
            routed_via="cc",
            auto_routed=None,
        )
        _write_agent_run(result)

        row = _fetch_run(tmp_db, "war-cc-1")
        assert row is not None
        assert row["auto_routed"] is None


# ---------------------------------------------------------------------------
# dispatch.route() stamps auto_routed correctly
# ---------------------------------------------------------------------------


def _make_tracker(remaining: float = 100.0) -> MagicMock:
    t = MagicMock()
    t.remaining_usd.return_value = remaining
    t.soft_cap_breached.return_value = False
    return t


def _make_run_result_mock(agent_id: str = "dispatch-ar-1") -> MagicMock:
    r = MagicMock()
    r.verdict = "done"
    r.agent_id = agent_id
    r.error = None
    r.input_tokens = 100
    r.output_tokens = 50
    r.auto_routed = None  # runner default; dispatch will override
    return r


class TestDispatchAutoRoutedStamping:
    """dispatch.route() stamps auto_routed=True for auto-route, False for explicit opt-in."""

    def test_auto_route_gate_on_stamps_true(self, tmp_db, monkeypatch):
        """SDK_AUTO_ROUTE=1 + eligible role → auto_routed=True persists to agent_run DB row."""
        monkeypatch.setenv("SDK_AUTO_ROUTE", "1")

        mock_tracker = _make_tracker()
        mock_result = _make_run_result_mock("dispatch-ar-auto-1")

        # Simulate the fixed runner: accepts auto_routed kwarg, sets it on the result,
        # then calls the real _write_agent_run so the value lands in the DB.
        async def _fake_run(spec, auto_routed=None):
            from backend.orchestrator.sdk_runner import _write_agent_run, RunResult
            mock_result.auto_routed = auto_routed
            mock_result.routed_via = "sdk"
            # Coerce the MagicMock into a real RunResult so _write_agent_run works
            real_result = RunResult(
                agent_id=mock_result.agent_id,
                role="docs-writer",
                discussion=1364,
                pr=None,
                verdict=mock_result.verdict,
                final_text="ok",
                input_tokens=mock_result.input_tokens,
                output_tokens=mock_result.output_tokens,
                tool_calls_count=0,
                prompt_sha256="abc",
                start_ts="2026-05-21T00:00:00Z",
                end_ts="2026-05-21T00:01:00Z",
                routed_via="sdk",
                auto_routed=auto_routed,
            )
            _write_agent_run(real_result)
            return mock_result

        mock_runner = MagicMock()
        mock_runner.run = _fake_run
        mock_hook = MagicMock()
        mock_hook.pre_spawn.return_value = True

        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "discussion": 1364,
            # no sdk_eligible — auto-route gate should fire
        }

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook):
            from backend.orchestrator.dispatch import route
            result = route(spec)

        assert result["route"] == "sdk"
        # Verify auto_routed=True actually persisted to the DB row (not NULL)
        row = _fetch_run(tmp_db, "dispatch-ar-auto-1")
        assert row is not None, "agent_run row not found"
        assert row["auto_routed"] is True, f"Expected True, got {row['auto_routed']!r}"

    def test_explicit_sdk_lane_stamps_false(self, tmp_db, monkeypatch):
        """Explicit sdk_eligible=True (no auto-route gate) → auto_routed=False persists to DB."""
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)

        mock_tracker = _make_tracker()
        mock_result = _make_run_result_mock("dispatch-ar-explicit-1")

        # Simulate the fixed runner: accepts auto_routed kwarg, sets it on the result,
        # then calls the real _write_agent_run so the value lands in the DB.
        async def _fake_run(spec, auto_routed=None):
            from backend.orchestrator.sdk_runner import _write_agent_run, RunResult
            real_result = RunResult(
                agent_id=mock_result.agent_id,
                role="docs-writer",
                discussion=1364,
                pr=None,
                verdict=mock_result.verdict,
                final_text="ok",
                input_tokens=mock_result.input_tokens,
                output_tokens=mock_result.output_tokens,
                tool_calls_count=0,
                prompt_sha256="def",
                start_ts="2026-05-21T00:00:00Z",
                end_ts="2026-05-21T00:01:00Z",
                routed_via="sdk",
                auto_routed=auto_routed,
            )
            _write_agent_run(real_result)
            return mock_result

        mock_runner = MagicMock()
        mock_runner.run = _fake_run
        mock_hook = MagicMock()
        mock_hook.pre_spawn.return_value = True

        spec = {
            "role": "docs-writer",
            "task_prompt": "generate docs",
            "discussion": 1364,
            "sdk_eligible": True,  # explicit --sdk-lane opt-in
        }

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"), \
             patch("backend.orchestrator.dispatch._select_sdk_backend", return_value=mock_runner), \
             patch("backend.orchestrator.dispatch.HookRunner", return_value=mock_hook):
            from backend.orchestrator.dispatch import route
            result = route(spec)

        assert result["route"] == "sdk"
        # Verify auto_routed=False actually persisted to the DB row (not NULL)
        row = _fetch_run(tmp_db, "dispatch-ar-explicit-1")
        assert row is not None, "agent_run row not found"
        assert row["auto_routed"] is False, f"Expected False, got {row['auto_routed']!r}"

    def test_cc_route_auto_routed_null(self, tmp_db, monkeypatch):
        """CC-routed run (ineligible role) → auto_routed=NULL in agent_run."""
        monkeypatch.delenv("SDK_AUTO_ROUTE", raising=False)

        mock_tracker = _make_tracker()

        spec = {
            "role": "executor",
            "task_prompt": "implement",
            "discussion": 1364,
        }

        with patch("backend.orchestrator.dispatch.CreditTracker", return_value=mock_tracker), \
             patch("backend.orchestrator.dispatch._SHADOW_MODE", "default"):
            from backend.orchestrator.dispatch import route
            result = route(spec)

        assert result["route"] == "cc"
        # The CC record is written via _record_cc_route; auto_routed is NULL (not passed)
        row = _fetch_run(tmp_db, result["run_id"])
        assert row is not None
        assert row["auto_routed"] is None
