"""tests/test_stats_pre_write_burn.py — unit tests for stats_pre_write_burn RPC.

Covers:
  AC-1  handle() returns {"rows": []} when agent_run table is empty.
  AC-3  A row with first_write_turn=12, total_turns=80 (ratio 15%) IS included.
  AC-4  Rows with first_write_turn IS NULL are excluded.
  AC-5  RPC handler wraps pre_write_burn_rows correctly.
  EXTRA Rows at/below the 10% threshold (ratio <= 10%) are excluded.
  EXTRA Rows are returned sorted by ratio_pct DESC.
"""
from __future__ import annotations

import importlib
import os
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# Fixture: isolated DuckDB + reload the RPC module against it
# ---------------------------------------------------------------------------

@pytest.fixture
def handler(tmp_path, monkeypatch):
    """Reload stats_pre_write_burn with STATS_DB_PATH pointing at a temp file."""
    db = str(tmp_path / "test_pre_write_burn.duckdb")
    monkeypatch.setenv("STATS_DB_PATH", db)
    import backend.rpc.stats_pre_write_burn as mod
    importlib.reload(mod)
    return mod


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    """Reload agent_run_tracker with the same STATS_DB_PATH."""
    import backend.agent_run_tracker as mod
    importlib.reload(mod)
    return mod


def _seed_row(
    tracker,
    db_path: str,
    agent_id: str,
    first_write_turn: int | None,
    total_turns: int | None,
    verdict: str = "done",
) -> None:
    """Insert a completed executor run with given first_write_turn / total_turns."""
    import duckdb

    now = datetime.now(timezone.utc)
    tracker.start_run(agent_id=agent_id, role="executor")

    conn = duckdb.connect(db_path)
    try:
        conn.execute(
            "UPDATE agent_run SET start_ts = ? WHERE agent_id = ?",
            [now - timedelta(seconds=60), agent_id],
        )
    finally:
        conn.close()

    tracker.complete_run(
        agent_id=agent_id,
        end_ts=now,
        duration_s=60.0,
        verdict=verdict,
        input_tok=5000,
        output_tok=1000,
        first_write_turn=first_write_turn,
        total_turns=total_turns,
    )


# ---------------------------------------------------------------------------
# AC-1: empty DB returns empty rows
# ---------------------------------------------------------------------------

class TestEmptyDB:
    def test_empty_table_returns_empty_rows(self, handler):
        result = handler.handle({})
        assert result == {"rows": []}

    def test_pre_write_burn_rows_returns_empty_list(self, handler):
        rows = handler.pre_write_burn_rows()
        assert rows == []


# ---------------------------------------------------------------------------
# AC-3: row with first_write_turn=12, total_turns=80 (15%) IS flagged
# ---------------------------------------------------------------------------

class TestFlaggedRow:
    def test_15pct_ratio_is_included(self, handler, tracker, tmp_path):
        db = os.environ["STATS_DB_PATH"]
        # Ensure schema
        tracker.start_run(agent_id="__schema__", role="probe")
        import duckdb
        conn = duckdb.connect(db)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__schema__'")
        conn.close()

        _seed_row(tracker, db, "exec-flagged", first_write_turn=12, total_turns=80)

        rows = handler.pre_write_burn_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["agent_id"] == "exec-flagged"
        assert row["first_write_turn"] == 12
        assert row["total_turns"] == 80
        # 12/80 = 15%
        assert abs(row["ratio_pct"] - 15.0) < 0.2

    def test_handle_wraps_rows(self, handler, tracker, tmp_path):
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="__schema2__", role="probe")
        import duckdb
        conn = duckdb.connect(db)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__schema2__'")
        conn.close()

        _seed_row(tracker, db, "exec-wrap", first_write_turn=12, total_turns=80)

        result = handler.handle({})
        assert "rows" in result
        assert len(result["rows"]) == 1


# ---------------------------------------------------------------------------
# AC-4: rows with first_write_turn IS NULL are excluded
# ---------------------------------------------------------------------------

class TestNullExclusion:
    def test_null_first_write_turn_excluded(self, handler, tracker, tmp_path):
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="__schema3__", role="probe")
        import duckdb
        conn = duckdb.connect(db)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__schema3__'")
        conn.close()

        # Row with NULL first_write_turn — must NOT appear
        _seed_row(tracker, db, "exec-null-fwt", first_write_turn=None, total_turns=80)
        # Row with NULL total_turns — must NOT appear
        _seed_row(tracker, db, "exec-null-tt", first_write_turn=10, total_turns=None)

        rows = handler.pre_write_burn_rows()
        agent_ids = {r["agent_id"] for r in rows}
        assert "exec-null-fwt" not in agent_ids
        assert "exec-null-tt" not in agent_ids


# ---------------------------------------------------------------------------
# EXTRA: rows at/below 10% threshold are excluded
# ---------------------------------------------------------------------------

class TestThreshold:
    def test_exactly_10pct_excluded(self, handler, tracker, tmp_path):
        """first_write_turn=10, total_turns=100 → ratio exactly 10% → excluded."""
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="__schema4__", role="probe")
        import duckdb
        conn = duckdb.connect(db)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__schema4__'")
        conn.close()

        _seed_row(tracker, db, "exec-10pct", first_write_turn=10, total_turns=100)
        rows = handler.pre_write_burn_rows()
        agent_ids = {r["agent_id"] for r in rows}
        assert "exec-10pct" not in agent_ids

    def test_above_threshold_included(self, handler, tracker, tmp_path):
        """first_write_turn=11, total_turns=100 → ratio 11% → included."""
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="__schema5__", role="probe")
        import duckdb
        conn = duckdb.connect(db)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__schema5__'")
        conn.close()

        _seed_row(tracker, db, "exec-11pct", first_write_turn=11, total_turns=100)
        rows = handler.pre_write_burn_rows()
        agent_ids = {r["agent_id"] for r in rows}
        assert "exec-11pct" in agent_ids


# ---------------------------------------------------------------------------
# EXTRA: sorting — worst ratio first
# ---------------------------------------------------------------------------

class TestSorting:
    def test_sorted_by_ratio_desc(self, handler, tracker, tmp_path):
        db = os.environ["STATS_DB_PATH"]
        tracker.start_run(agent_id="__schema6__", role="probe")
        import duckdb
        conn = duckdb.connect(db)
        conn.execute("DELETE FROM agent_run WHERE agent_id = '__schema6__'")
        conn.close()

        # Ratios: exec-hi=50%, exec-lo=20%
        _seed_row(tracker, db, "exec-hi", first_write_turn=50, total_turns=100)
        _seed_row(tracker, db, "exec-lo", first_write_turn=20, total_turns=100)

        rows = handler.pre_write_burn_rows()
        assert len(rows) == 2
        assert rows[0]["agent_id"] == "exec-hi"
        assert rows[1]["agent_id"] == "exec-lo"
