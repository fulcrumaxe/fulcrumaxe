"""Tests for backend/stats/spawn_total_today.py (D#2319).

Every case builds a real DuckDB file with the real ``agent_run`` schema and
reads it through the real function — no stub returns the answer under test.

The distinction these tests exist to hold is between a *stale or unreadable
source* and a *genuinely small count*. Before this module both came back as a
plain integer, and on the operator host that integer was ``0`` in the first case
and ``0`` in the second — which is why nobody noticed the source had been silent
since morning.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

duckdb = pytest.importorskip("duckdb")

from backend.stats import spawn_total_today as sut  # noqa: E402

NOW = datetime(2026, 9, 6, 15, 0, 0, tzinfo=timezone.utc)

_SCHEMA = """
CREATE TABLE agent_run (
    agent_id   TEXT PRIMARY KEY,
    role       TEXT,
    start_ts   TIMESTAMPTZ NOT NULL,
    end_ts     TIMESTAMPTZ
)
"""


def _make_db(path, run_offsets):
    """Write a DuckDB file with one agent_run row per (hours-before-NOW) offset."""
    conn = duckdb.connect(str(path))
    conn.execute(_SCHEMA)
    for i, hours in enumerate(run_offsets):
        conn.execute(
            "INSERT INTO agent_run (agent_id, role, start_ts, end_ts) VALUES (?, ?, ?, ?)",
            [f"agent-{i}", "executor", NOW - timedelta(hours=hours), None],
        )
    conn.close()
    return path


def test_stale_source_returns_unknown_not_a_number(tmp_path):
    """Newest run 17h old — the exact operator-host condition. Must not be a number."""
    db = _make_db(tmp_path / "stats.duckdb", [17, 18, 19])

    result = sut.total_today(now=NOW, db_path=db)

    assert result.count is None
    assert result.reason is not None
    assert "stale" in result.reason
    # The reason has to be actionable: it names the age and the threshold.
    assert "17h00m" in result.reason
    assert "3h00m" in result.reason
    # And the result carries the source and the newest row's timestamp.
    assert result.source == "agent_run"
    assert result.newest_ts == "2026-09-05T22:00:00Z"


def test_fresh_source_with_three_runs_today_returns_three(tmp_path):
    """The other half of the distinction: a real, fresh, small count is a count."""
    db = _make_db(tmp_path / "stats.duckdb", [0.1, 0.5, 2.0])

    result = sut.total_today(now=NOW, db_path=db)

    assert result.count == 3
    assert result.reason is None
    assert result.source == "agent_run"


def test_rows_from_previous_days_are_not_counted_but_do_establish_freshness(tmp_path):
    """A run yesterday is not "today", yet it still proves the source is alive."""
    db = _make_db(tmp_path / "stats.duckdb", [0.5, 20, 30])

    result = sut.total_today(now=NOW, db_path=db)

    assert result.count == 1


def test_unreadable_source_returns_unknown_with_the_error_not_zero(tmp_path):
    """A corrupt store is a read failure. The old code answered 0 for this."""
    db = tmp_path / "stats.duckdb"
    db.write_bytes(b"this is not a duckdb database" * 64)

    result = sut.total_today(now=NOW, db_path=db)

    assert result.count is None  # the old code answered 0 here
    assert result.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits")
def test_permission_denied_returns_unknown_with_the_error_not_zero(tmp_path):
    db = _make_db(tmp_path / "stats.duckdb", [0.1, 0.2, 0.3])
    db.chmod(0o000)
    try:
        result = sut.total_today(now=NOW, db_path=db)
    finally:
        db.chmod(0o644)

    assert result.count is None
    assert result.reason


def test_missing_store_returns_unknown_not_zero(tmp_path):
    result = sut.total_today(now=NOW, db_path=tmp_path / "absent.duckdb")

    assert result.count is None
    assert "no metrics store" in (result.reason or "")


def test_store_without_agent_run_table_returns_unknown(tmp_path):
    db = tmp_path / "stats.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE something_else (x INTEGER)")
    conn.close()

    result = sut.total_today(now=NOW, db_path=db)

    assert result.count is None
    assert result.reason


def test_empty_store_returns_unknown_rather_than_a_confident_zero(tmp_path):
    db = _make_db(tmp_path / "stats.duckdb", [])

    result = sut.total_today(now=NOW, db_path=db)

    assert result.count is None
    assert "no runs recorded" in (result.reason or "")


def test_to_dict_omits_reason_when_the_answer_is_known(tmp_path):
    db = _make_db(tmp_path / "stats.duckdb", [0.1])

    d = sut.total_today(now=NOW, db_path=db).to_dict()

    assert d == {
        "count": 1,
        "source": "agent_run",
        "newestTs": "2026-09-06T14:54:00Z",
    }


def test_to_dict_carries_reason_when_the_answer_is_unknown(tmp_path):
    db = _make_db(tmp_path / "stats.duckdb", [17])

    d = sut.total_today(now=NOW, db_path=db).to_dict()

    assert d["count"] is None
    assert d["source"] == "agent_run"
    assert d["newestTs"] == "2026-09-05T22:00:00Z"
    assert "reason" in d


def test_threshold_boundary_is_not_stale_just_under_and_is_stale_just_over(tmp_path):
    under = _make_db(tmp_path / "under.duckdb", [2.9])
    over = _make_db(tmp_path / "over.duckdb", [3.1])

    assert sut.total_today(now=NOW, db_path=under).count == 1
    assert sut.total_today(now=NOW, db_path=over).count is None
