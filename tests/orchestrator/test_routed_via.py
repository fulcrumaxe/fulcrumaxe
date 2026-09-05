"""tests/orchestrator/test_routed_via.py — Tests for routed_via telemetry column.

Covers (D#1331):
- agent_run schema has routed_via TEXT column after _ensure_schema
- idempotent migration adds routed_via to existing table without it
- complete_run writes routed_via="sdk" / "cc" to the row
- _write_agent_run propagates RunResult.routed_via through to DuckDB
- SDKRunner.run() sets routed_via="sdk" on the RunResult it creates
- dispatch._record_cc_route writes routed_via="cc" for CC-routed spawns
- _routing_counts reads sdk/cc counts from real column; null rows fall back
- _routing_counts works on pre-D#1331 tables lacking the column (proxy)

No real SDK call is made. DuckDB is isolated to tmp_path via STATS_DB_PATH.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestSchemaHasRoutedVia:
    """_ensure_schema creates the routed_via column."""

    def test_new_table_has_routed_via_column(self, tmp_db):
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

        assert "routed_via" in cols

    def test_migration_adds_routed_via_to_old_table(self, tmp_db):
        """A table created without routed_via gains it on next _ensure_schema call."""
        import duckdb
        from backend.agent_run_tracker import _ensure_schema

        # Create old-style table without routed_via
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
                    model      VARCHAR,
                    input_tok  INTEGER,
                    output_tok INTEGER
                )
            """)
        finally:
            conn.close()

        # Now call _ensure_schema — it should add routed_via
        conn2 = duckdb.connect(str(tmp_db))
        try:
            _ensure_schema(conn2)
            cols = {r[0] for r in conn2.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name='agent_run'"
            ).fetchall()}
        finally:
            conn2.close()

        assert "routed_via" in cols


# ---------------------------------------------------------------------------
# complete_run writes routed_via
# ---------------------------------------------------------------------------


class TestCompleteRunRoutedVia:
    """complete_run persists routed_via correctly."""

    def test_complete_run_writes_sdk(self, tmp_db):
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="test-sdk-1", role="docs-writer", event_id="test-sdk-1")
        complete_run(agent_id="test-sdk-1", verdict="done", routed_via="sdk")

        row = _fetch_run(tmp_db, "test-sdk-1")
        assert row is not None
        assert row["routed_via"] == "sdk"

    def test_complete_run_writes_cc(self, tmp_db):
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="test-cc-1", role="executor", event_id="test-cc-1")
        complete_run(agent_id="test-cc-1", verdict="done", routed_via="cc")

        row = _fetch_run(tmp_db, "test-cc-1")
        assert row is not None
        assert row["routed_via"] == "cc"

    def test_complete_run_none_routed_via_leaves_null(self, tmp_db):
        """Rows written without routed_via stay NULL (old pre-D#1331 behaviour)."""
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="test-null-1", role="executor", event_id="test-null-1")
        complete_run(agent_id="test-null-1", verdict="done")  # no routed_via

        row = _fetch_run(tmp_db, "test-null-1")
        assert row is not None
        assert row["routed_via"] is None

    def test_complete_run_coalesce_does_not_overwrite_existing_value(self, tmp_db):
        """COALESCE means a second complete_run without routed_via keeps the first value."""
        from backend.agent_run_tracker import start_run, complete_run

        start_run(agent_id="test-coalesce-1", role="docs-writer", event_id="test-coalesce-1")
        complete_run(agent_id="test-coalesce-1", verdict="done", routed_via="sdk")
        # Second call without routed_via — should not overwrite "sdk" to NULL
        complete_run(agent_id="test-coalesce-1", verdict="done")

        row = _fetch_run(tmp_db, "test-coalesce-1")
        assert row is not None
        assert row["routed_via"] == "sdk"


# ---------------------------------------------------------------------------
# _write_agent_run propagates routed_via
# ---------------------------------------------------------------------------


class TestWriteAgentRunRoutedVia:
    """_write_agent_run writes RunResult.routed_via to the DB row."""

    def test_sdk_result_writes_sdk_routed_via(self, tmp_db):
        from backend.orchestrator.sdk_runner import RunResult, _write_agent_run

        result = RunResult(
            agent_id="wrun-sdk-1",
            role="docs-writer",
            discussion=1331,
            pr=None,
            verdict="done",
            final_text="ok",
            input_tokens=100,
            output_tokens=50,
            tool_calls_count=2,
            prompt_sha256="abc123",
            start_ts="2026-05-20T00:00:00Z",
            end_ts="2026-05-20T00:01:00Z",
            routed_via="sdk",
        )
        _write_agent_run(result)

        row = _fetch_run(tmp_db, "wrun-sdk-1")
        assert row is not None
        assert row["routed_via"] == "sdk"

    def test_cc_result_writes_cc_routed_via(self, tmp_db):
        from backend.orchestrator.sdk_runner import RunResult, _write_agent_run

        result = RunResult(
            agent_id="wrun-cc-1",
            role="executor",
            discussion=1331,
            pr=None,
            verdict="done",
            final_text="ok",
            input_tokens=100,
            output_tokens=50,
            tool_calls_count=0,
            prompt_sha256="def456",
            start_ts="2026-05-20T00:00:00Z",
            end_ts="2026-05-20T00:01:00Z",
            routed_via="cc",
        )
        _write_agent_run(result)

        row = _fetch_run(tmp_db, "wrun-cc-1")
        assert row is not None
        assert row["routed_via"] == "cc"


# ---------------------------------------------------------------------------
# SDKRunner sets routed_via="sdk"
# ---------------------------------------------------------------------------


class TestSDKRunnerSetsRoutedVia:
    """SDKRunner.run() produces RunResult with routed_via='sdk'."""

    def test_sdk_runner_sets_routed_via_sdk(self, tmp_db):
        """SDKRunner.run() creates a RunResult with routed_via='sdk'."""
        from backend.orchestrator.sdk_runner import SDKRunner, SpawnSpec, RunResult

        # Patch the anthropic client so no real API call is made
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.content = []
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        captured: list[RunResult] = []

        with patch("backend.orchestrator.sdk_runner.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            with patch("backend.orchestrator.sdk_runner._write_agent_run",
                       side_effect=captured.append):
                import asyncio
                runner = SDKRunner(api_key="test-key-not-real")
                spec = SpawnSpec(
                    role="docs-writer",
                    task_prompt="summarize",
                    tool_whitelist=["Read"],
                    discussion=1331,
                    sdk_eligible=True,
                )
                asyncio.run(runner.run(spec))

        assert len(captured) == 1
        assert captured[0].routed_via == "sdk"


# ---------------------------------------------------------------------------
# dispatch._record_cc_route writes routed_via="cc"
# ---------------------------------------------------------------------------


class TestRecordCcRoute:
    """_record_cc_route stamps routed_via='cc' for dispatcher CC-routed spawns."""

    def test_record_cc_route_writes_cc(self, tmp_db):
        from backend.orchestrator.dispatch import _record_cc_route

        spec_dict = {
            "role": "executor",
            "discussion": 1331,
            "sdk_eligible": False,
        }
        _record_cc_route("cc-test-agent-1", spec_dict)

        row = _fetch_run(tmp_db, "cc-test-agent-1")
        assert row is not None
        assert row["routed_via"] == "cc"


# ---------------------------------------------------------------------------
# _routing_counts reads real column + proxy fallback
# ---------------------------------------------------------------------------


class TestRoutingCountsRealColumn:
    """_routing_counts reads routed_via when present; falls back to proxy for NULLs."""

    def _make_db_with_routes(self, tmp_path, sdk_count: int, cc_count: int, null_count: int):
        """Create a stats.duckdb with agent_run rows including routed_via."""
        import duckdb
        from datetime import datetime, timezone
        from backend.agent_run_tracker import _ensure_schema

        db = tmp_path / "stats.duckdb"
        conn = duckdb.connect(str(db))
        try:
            _ensure_schema(conn)
            now = datetime.now(timezone.utc)
            idx = 0

            def _insert(route):
                nonlocal idx
                idx += 1
                conn.execute(
                    "INSERT INTO agent_run (agent_id, role, start_ts, routed_via)"
                    " VALUES (?, ?, ?, ?)",
                    [f"agent-{idx}", "docs-writer", now, route],
                )

            for _ in range(sdk_count):
                _insert("sdk")
            for _ in range(cc_count):
                _insert("cc")
            for _ in range(null_count):
                _insert(None)
        finally:
            conn.close()
        return db

    def test_counts_sdk_and_cc_from_real_column(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        db = self._make_db_with_routes(tmp_path, sdk_count=3, cc_count=7, null_count=2)

        from backend.orchestrator.sdk_status import _routing_counts
        rc = _routing_counts(db_path=db)

        assert rc["sdk_runs"] == 3
        assert rc["cc_runs"] == 7
        assert rc["null_route_runs"] == 2
        assert rc["total_runs_all_time"] == 12
        assert "routed_via column present" in rc["note"]

    def test_sdk_runs_estimate_uses_real_count_when_available(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        db = self._make_db_with_routes(tmp_path, sdk_count=2, cc_count=1, null_count=0)

        from backend.orchestrator.sdk_status import _routing_counts
        # Patch credit tracker to return 0 so we verify estimate comes from real column
        with patch("backend.orchestrator.credit_tracker.CreditTracker") as mock_ct:
            mock_ct.return_value.used_usd.return_value = 0.0
            rc = _routing_counts(db_path=db)

        assert rc["sdk_runs"] == 2
        assert "2 SDK run(s)" in rc["sdk_runs_estimate"]

    def test_null_rows_report_zero_sdk_runs(self, tmp_path, monkeypatch):
        """Old rows with NULL routed_via don't count as sdk runs."""
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        db = self._make_db_with_routes(tmp_path, sdk_count=0, cc_count=0, null_count=5)

        from backend.orchestrator.sdk_status import _routing_counts
        with patch("backend.orchestrator.credit_tracker.CreditTracker") as mock_ct:
            mock_ct.return_value.used_usd.return_value = 0.0
            rc = _routing_counts(db_path=db)

        assert rc["sdk_runs"] == 0
        assert rc["cc_runs"] == 0
        assert rc["null_route_runs"] == 5
        assert "0 SDK runs" in rc["sdk_runs_estimate"]

    def test_proxy_fallback_when_no_db(self, tmp_path, monkeypatch):
        """When stats.duckdb doesn't exist, falls back to proxy (credit_tracker)."""
        monkeypatch.setenv("AUTONOMOUS_TEAM_STATE_DIR", str(tmp_path))
        # Don't create the DB — it won't exist

        from backend.orchestrator.sdk_status import _routing_counts
        with patch("backend.orchestrator.credit_tracker.CreditTracker") as mock_ct:
            mock_ct.return_value.used_usd.return_value = 1.23
            rc = _routing_counts()

        assert rc["total_runs_all_time"] == 0
        assert rc["sdk_runs"] == 0  # no DB = no real counts
        # proxy is used
        assert "at least 1" in rc["sdk_runs_estimate"]
