"""Tests for backend/stats/sdk_vs_cc.py and backend/rpc/stats_sdk_vs_cc.py.

Uses an in-memory DuckDB fixture to verify:
- Correct grouping by (role, routed_via)
- Graceful no-data handling (missing file / empty table / absent column)
- RPC handler delegates correctly
"""

from __future__ import annotations

import sys
from pathlib import Path
import tempfile

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_with_data(path: Path) -> None:
    """Create a stats.duckdb with agent_run rows including routed_via."""
    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE agent_run (
            agent_id     TEXT,
            role         TEXT,
            discussion   INTEGER,
            pr           INTEGER,
            start_ts     TIMESTAMP,
            end_ts       TIMESTAMP,
            duration_s   FLOAT,
            verdict      TEXT,
            model        TEXT,
            input_tok    INTEGER,
            output_tok   INTEGER,
            cache_read   INTEGER,
            cache_write  INTEGER,
            blocked_reason TEXT,
            event_id     TEXT,
            routed_via   TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO agent_run VALUES
          -- executor: 2 sdk runs (1 pass, 1 fail)
          ('a1','executor',1,1,'2026-05-01','2026-05-01',120,'done','claude-sonnet',10000,1500,0,0,NULL,NULL,'sdk'),
          ('a2','executor',2,2,'2026-05-01','2026-05-01',180,'fail','claude-sonnet',12000,2000,0,0,NULL,NULL,'sdk'),
          -- executor: 1 cc run (pass)
          ('a3','executor',3,3,'2026-05-01','2026-05-01',90,'done','claude-sonnet',8000,1000,0,0,NULL,NULL,'cc'),
          -- code-reviewer: 2 sdk runs (both pass)
          ('a4','code-reviewer',4,4,'2026-05-01','2026-05-01',60,'pass','claude-sonnet',5000,800,0,0,NULL,NULL,'sdk'),
          ('a5','code-reviewer',5,5,'2026-05-01','2026-05-01',70,'pass','claude-sonnet',6000,900,0,0,NULL,NULL,'sdk'),
          -- row with NULL routed_via (pre-D#1331) — should be excluded
          ('a6','executor',6,6,'2026-05-01','2026-05-01',100,'done','claude-sonnet',9000,1200,0,0,NULL,NULL,NULL)
        """
    )
    conn.close()


def _make_db_no_routed_via(path: Path) -> None:
    """Create a stats.duckdb without the routed_via column."""
    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE agent_run (
            agent_id TEXT, role TEXT, discussion INTEGER, pr INTEGER,
            start_ts TIMESTAMP, end_ts TIMESTAMP, duration_s FLOAT,
            verdict TEXT, model TEXT, input_tok INTEGER, output_tok INTEGER,
            cache_read INTEGER, cache_write INTEGER, blocked_reason TEXT, event_id TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_run VALUES ('a1','executor',1,1,'2026-05-01','2026-05-01',120,'done','m',10000,1500,0,0,NULL,NULL)"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Tests: backend/stats/sdk_vs_cc.py
# ---------------------------------------------------------------------------


def test_sdk_vs_cc_no_file():
    """Missing stats.duckdb returns empty rows with no error."""
    from backend.stats.sdk_vs_cc import sdk_vs_cc_by_role

    result = sdk_vs_cc_by_role(db_path=Path("/nonexistent/stats.duckdb"))
    assert result["rows"] == []
    assert result["error"] is None
    assert result["has_routed_via"] is False
    assert result["generated_at"]  # non-empty ISO string


def test_sdk_vs_cc_absent_column():
    """DB without routed_via column returns has_routed_via=False, rows=[]."""
    from backend.stats.sdk_vs_cc import sdk_vs_cc_by_role

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stats.duckdb"
        _make_db_no_routed_via(db_path)
        result = sdk_vs_cc_by_role(db_path=db_path)

    assert result["has_routed_via"] is False
    assert result["rows"] == []
    assert result["error"] is None


def test_sdk_vs_cc_correct_grouping():
    """Rows are grouped correctly by (role, routed_via); NULL rows excluded."""
    from backend.stats.sdk_vs_cc import sdk_vs_cc_by_role

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stats.duckdb"
        _make_db_with_data(db_path)
        result = sdk_vs_cc_by_role(db_path=db_path)

    assert result["has_routed_via"] is True
    assert result["error"] is None

    rows_by_key = {(r["role"], r["route"]): r for r in result["rows"]}

    # executor/sdk: 2 runs, 50% pass rate, median input=11000
    exec_sdk = rows_by_key[("executor", "sdk")]
    assert exec_sdk["run_count"] == 2
    assert exec_sdk["pass_rate"] == pytest.approx(0.5, abs=0.01)
    assert exec_sdk["median_input_tok"] == 11000  # median of 10000, 12000

    # executor/cc: 1 run, 100% pass rate
    exec_cc = rows_by_key[("executor", "cc")]
    assert exec_cc["run_count"] == 1
    assert exec_cc["pass_rate"] == pytest.approx(1.0, abs=0.01)

    # code-reviewer/sdk: 2 runs, 100% pass rate
    cr_sdk = rows_by_key[("code-reviewer", "sdk")]
    assert cr_sdk["run_count"] == 2
    assert cr_sdk["pass_rate"] == pytest.approx(1.0, abs=0.01)

    # NULL routed_via row should not appear in results
    assert ("executor", None) not in rows_by_key
    assert ("executor", "unknown") not in rows_by_key


def test_sdk_vs_cc_empty_table():
    """Empty agent_run table returns has_routed_via=True, rows=[]."""
    import duckdb

    from backend.stats.sdk_vs_cc import sdk_vs_cc_by_role

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stats.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute(
            "CREATE TABLE agent_run (role TEXT, routed_via TEXT, verdict TEXT,"
            " input_tok INTEGER, output_tok INTEGER)"
        )
        conn.close()
        result = sdk_vs_cc_by_role(db_path=db_path)

    assert result["has_routed_via"] is True
    assert result["rows"] == []
    assert result["error"] is None


# ---------------------------------------------------------------------------
# Tests: backend/rpc/stats_sdk_vs_cc.py
# ---------------------------------------------------------------------------


def test_rpc_handler_delegates(monkeypatch):
    """RPC handle() calls sdk_vs_cc_by_role and returns its result."""
    from backend.rpc import stats_sdk_vs_cc

    sentinel = {"rows": [], "has_routed_via": False, "generated_at": "X", "error": None}
    monkeypatch.setattr("backend.stats.sdk_vs_cc.sdk_vs_cc_by_role", lambda **_: sentinel)

    result = stats_sdk_vs_cc.handle({})
    assert result == sentinel


def test_rpc_handler_with_real_data():
    """RPC handle() returns correct structure when db has data."""
    import os
    from backend.rpc import stats_sdk_vs_cc

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "stats.duckdb"
        _make_db_with_data(db_path)

        # Point the stats module to our temp DB via env var
        old = os.environ.get("STATS_DB_PATH")
        os.environ["STATS_DB_PATH"] = str(db_path)
        try:
            result = stats_sdk_vs_cc.handle({})
        finally:
            if old is None:
                del os.environ["STATS_DB_PATH"]
            else:
                os.environ["STATS_DB_PATH"] = old

    assert "rows" in result
    assert "has_routed_via" in result
    assert "generated_at" in result
    assert result["has_routed_via"] is True
    assert len(result["rows"]) > 0
