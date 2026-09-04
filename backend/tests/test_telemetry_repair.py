"""Tests for D#919 telemetry repair — agent_run token + first_write_turn columns.

Acceptance criteria:
  AC-1  New agent_run rows have non-null input_tok and output_tok after complete_run().
  AC-2  cache_creation_tokens is stored correctly.
  AC-3  first_write_turn is stored from complete_run() for agents that wrote files.
  AC-4  first_write_turn is NULL for agents with no write calls.
  AC-5  _ensure_schema migration is idempotent (can be called twice without error).
  AC-6  Schema migration adds first_write_turn to an existing table that lacks it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure repo root is on path
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

duckdb = pytest.importorskip("duckdb", reason="duckdb not installed")

from backend.agent_run_tracker import (  # noqa: E402
    _ensure_schema,
    complete_run,
    start_run,
    _db_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_memory_conn():
    """Return a fresh in-memory DuckDB connection."""
    import duckdb as _duckdb  # noqa: PLC0415
    return _duckdb.connect(":memory:")


def _fresh_db(tmp_path: Path) -> Path:
    """Return path to a fresh stats.duckdb in tmp_path."""
    return tmp_path / "stats.duckdb"


# ---------------------------------------------------------------------------
# AC-5: _ensure_schema is idempotent
# ---------------------------------------------------------------------------

def test_ensure_schema_idempotent():
    """Calling _ensure_schema twice must not raise."""
    conn = _in_memory_conn()
    _ensure_schema(conn)
    _ensure_schema(conn)  # second call must be a no-op
    conn.close()


# ---------------------------------------------------------------------------
# AC-6: migration adds first_write_turn to existing table
# ---------------------------------------------------------------------------

def test_migration_adds_first_write_turn_column():
    """_ensure_schema must add first_write_turn to a table that doesn't have it.

    The old-style table matches the schema that existed before D#919: it includes
    all columns that were present in PR-635 but lacks first_write_turn.
    """
    conn = _in_memory_conn()
    # Create the pre-D#919 table (all original columns, no first_write_turn)
    conn.execute("""
        CREATE TABLE agent_run (
            agent_id               VARCHAR PRIMARY KEY,
            role                   VARCHAR NOT NULL,
            discussion             INTEGER,
            pr                     INTEGER,
            start_ts               TIMESTAMPTZ NOT NULL,
            end_ts                 TIMESTAMPTZ,
            duration_s             DOUBLE,
            verdict                VARCHAR,
            model                  VARCHAR,
            input_tok              INTEGER,
            output_tok             INTEGER,
            cache_read             INTEGER,
            cache_write            INTEGER,
            cache_creation_tokens  INTEGER,
            blocked_reason         VARCHAR,
            event_id               VARCHAR
        )
    """)
    # Running _ensure_schema should add the missing first_write_turn column
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='agent_run'"
        ).fetchall()
    }
    assert "first_write_turn" in cols, "first_write_turn column must be added by migration"
    conn.close()


# ---------------------------------------------------------------------------
# AC-1: input_tok / output_tok populated
# ---------------------------------------------------------------------------

def test_complete_run_populates_token_columns(tmp_path):
    """complete_run with token args must store non-null input_tok and output_tok."""
    import os  # noqa: PLC0415
    db = _fresh_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db)
    try:
        complete_run(
            agent_id="executor-919-test1",
            verdict="done",
            input_tok=62000,
            output_tok=8400,
        )

        import duckdb as _duckdb  # noqa: PLC0415
        conn = _duckdb.connect(str(db), read_only=True)
        row = conn.execute(
            "SELECT input_tok, output_tok FROM agent_run WHERE agent_id = ?",
            ["executor-919-test1"],
        ).fetchone()
        conn.close()
        assert row is not None, "Row must exist after complete_run"
        assert row[0] == 62000, f"input_tok must be 62000, got {row[0]}"
        assert row[1] == 8400, f"output_tok must be 8400, got {row[1]}"
    finally:
        del os.environ["STATS_DB_PATH"]


# ---------------------------------------------------------------------------
# AC-2: cache_creation_tokens populated
# ---------------------------------------------------------------------------

def test_complete_run_populates_cache_creation_tokens(tmp_path):
    """complete_run with cache_creation_tokens must store the value."""
    import os  # noqa: PLC0415
    db = _fresh_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db)
    try:
        complete_run(
            agent_id="executor-919-test2",
            verdict="done",
            input_tok=10000,
            output_tok=500,
            cache_creation_tokens=3500,
        )

        import duckdb as _duckdb  # noqa: PLC0415
        conn = _duckdb.connect(str(db), read_only=True)
        row = conn.execute(
            "SELECT cache_creation_tokens FROM agent_run WHERE agent_id = ?",
            ["executor-919-test2"],
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 3500, f"cache_creation_tokens must be 3500, got {row[0]}"
    finally:
        del os.environ["STATS_DB_PATH"]


# ---------------------------------------------------------------------------
# AC-3: first_write_turn populated when write calls exist
# ---------------------------------------------------------------------------

def test_complete_run_populates_first_write_turn(tmp_path):
    """complete_run with first_write_turn=5 must store 5."""
    import os  # noqa: PLC0415
    db = _fresh_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db)
    try:
        complete_run(
            agent_id="executor-919-test3",
            verdict="done",
            input_tok=50000,
            output_tok=6000,
            first_write_turn=5,
        )

        import duckdb as _duckdb  # noqa: PLC0415
        conn = _duckdb.connect(str(db), read_only=True)
        row = conn.execute(
            "SELECT first_write_turn FROM agent_run WHERE agent_id = ?",
            ["executor-919-test3"],
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 5, f"first_write_turn must be 5, got {row[0]}"
    finally:
        del os.environ["STATS_DB_PATH"]


# ---------------------------------------------------------------------------
# AC-4: first_write_turn is NULL for agents without writes
# ---------------------------------------------------------------------------

def test_complete_run_first_write_turn_null_when_no_writes(tmp_path):
    """complete_run without first_write_turn must leave the column NULL."""
    import os  # noqa: PLC0415
    db = _fresh_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db)
    try:
        complete_run(
            agent_id="reviewer-919-test4",
            verdict="pass",
            input_tok=30000,
            output_tok=2000,
            # no first_write_turn
        )

        import duckdb as _duckdb  # noqa: PLC0415
        conn = _duckdb.connect(str(db), read_only=True)
        row = conn.execute(
            "SELECT first_write_turn FROM agent_run WHERE agent_id = ?",
            ["reviewer-919-test4"],
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None, f"first_write_turn must be NULL, got {row[0]}"
    finally:
        del os.environ["STATS_DB_PATH"]


# ---------------------------------------------------------------------------
# BONUS: start_run + complete_run round-trip preserves all token fields
# ---------------------------------------------------------------------------

def test_start_then_complete_upserts_correctly(tmp_path):
    """start_run then complete_run must merge into one row with all fields."""
    import os  # noqa: PLC0415
    db = _fresh_db(tmp_path)
    os.environ["STATS_DB_PATH"] = str(db)
    try:
        start_run(
            agent_id="executor-919-roundtrip",
            role="executor",
            discussion=919,
        )
        complete_run(
            agent_id="executor-919-roundtrip",
            verdict="done",
            input_tok=100000,
            output_tok=9000,
            cache_creation_tokens=4000,
            first_write_turn=3,
        )

        import duckdb as _duckdb  # noqa: PLC0415
        conn = _duckdb.connect(str(db), read_only=True)
        row = conn.execute(
            """
            SELECT role, verdict, input_tok, output_tok,
                   cache_creation_tokens, first_write_turn
            FROM agent_run WHERE agent_id = ?
            """,
            ["executor-919-roundtrip"],
        ).fetchone()
        conn.close()
        assert row is not None
        role, verdict, input_tok, output_tok, cct, fwt = row
        assert role == "executor"
        assert verdict == "done"
        assert input_tok == 100000
        assert output_tok == 9000
        assert cct == 4000
        assert fwt == 3
    finally:
        del os.environ["STATS_DB_PATH"]
