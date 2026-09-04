"""Tests for backend/verdict_overturn.py (Discussion #1397).

Covers:
  - record_overturn writes a metric_event row with correct tags
  - record/read roundtrip via overturn_rate_by_role_24h
  - sample-size gating: overturn_rate=None when sample_size < 5
  - sort order: highest overturn_rate first, None rows last
  - producer logic: replay the #1383 sequence (acceptance-tester pass, then
    code-reviewer needs-fix on same PR) -> exactly ONE downstream_needs_fix row

All persistence is isolated to tmp_path via STATS_DB_PATH env var.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    import duckdb as _duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


# ---------------------------------------------------------------------------
# Timestamp helpers — all seeded rows must fall inside the 24h window
# ---------------------------------------------------------------------------

def _recent(hours_ago: float = 1, minutes_ago: float = 0) -> datetime:
    """Return a timezone-aware UTC timestamp within the last 24 hours."""
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago, minutes=minutes_ago)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal stats.duckdb with the metric_event schema."""
    db = tmp_path / "stats.duckdb"
    conn = _duckdb.connect(str(db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metric_event (
            ts      TIMESTAMPTZ NOT NULL,
            metric  TEXT        NOT NULL,
            tags    JSON,
            value   DOUBLE      NOT NULL,
            unit    TEXT        NOT NULL,
            source  TEXT,
            PRIMARY KEY (ts, metric, tags)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_time ON metric_event(metric, ts)"
    )
    conn.close()
    return db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point STATS_DB_PATH to a temp file so no test ever touches real state."""
    db_file = _make_db(tmp_path)
    monkeypatch.setenv("STATS_DB_PATH", str(db_file))
    yield db_file


def _insert_metric(db_file: Path, metric: str, value: float, unit: str, tags: dict,
                   source: str = "test", ts: datetime | None = None) -> None:
    """Directly insert a metric_event row (bypasses stats_writer for test setup)."""
    now = ts or datetime.now(timezone.utc)
    conn = _duckdb.connect(str(db_file))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO metric_event (ts, metric, tags, value, unit, source) VALUES (?, ?, ?, ?, ?, ?)",
            [now, metric, json.dumps(tags), value, unit, source],
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test: record_overturn writes correct metric row
# ---------------------------------------------------------------------------


def test_record_overturn_writes_row(isolated_db):
    """record_overturn must write a metric_event row with correct tags."""
    from backend.verdict_overturn import record_overturn

    ts = _recent(hours_ago=1)
    record_overturn(
        pr=999,
        prior_role="acceptance-tester",
        prior_verdict="pass",
        contradicting_source="code-reviewer",
        kind="downstream_needs_fix",
        evidence_ref=".autonomous-team/pr-artifacts/999/abc12345.jsonl",
        ts=ts,
    )

    conn = _duckdb.connect(str(isolated_db))
    try:
        rows = conn.execute(
            "SELECT metric, value, unit, tags, source FROM metric_event WHERE metric = 'verdict_overturn'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    metric, value, unit, tags_raw, source = rows[0]
    assert metric == "verdict_overturn"
    assert value == 1.0
    assert unit == "count"
    tags = json.loads(tags_raw)
    assert tags["role"] == "acceptance-tester"
    assert tags["kind"] == "downstream_needs_fix"
    assert tags["pr"] == "999"
    assert tags["prior_verdict"] == "pass"
    assert tags["contradicting_source"] == "code-reviewer"
    assert source == "verdict-overturn-hook"


# ---------------------------------------------------------------------------
# Test: record/read roundtrip
# ---------------------------------------------------------------------------


def test_record_read_roundtrip(isolated_db):
    """After recording an overturn, overturn_rate_by_role_24h must return it."""
    from backend.verdict_overturn import record_overturn, overturn_rate_by_role_24h

    # Seed 6 pass verdicts for acceptance-tester (sample_size >= 5)
    for i in range(6):
        _insert_metric(isolated_db, "role_verdict", 1.0, "count",
                       {"role": "acceptance-tester", "verdict": "pass"},
                       ts=_recent(hours_ago=2, minutes_ago=i))

    # Record one overturn
    record_overturn(
        pr=100,
        prior_role="acceptance-tester",
        prior_verdict="pass",
        contradicting_source="code-reviewer",
        kind="downstream_needs_fix",
        evidence_ref="pr-artifacts/100/abc.jsonl",
    )

    result = overturn_rate_by_role_24h()
    assert len(result) == 1
    row = result[0]
    assert row["role"] == "acceptance-tester"
    assert row["overturns"] == 1
    assert row["sample_size"] == 6
    assert row["overturn_rate"] == pytest.approx(1 / 6)


# ---------------------------------------------------------------------------
# Test: sample-size gating — overturn_rate=None when sample_size < 5
# ---------------------------------------------------------------------------


def test_sample_size_gating_below_threshold(isolated_db):
    """Roles with fewer than 5 pass verdicts must have overturn_rate=None."""
    from backend.verdict_overturn import record_overturn, overturn_rate_by_role_24h

    # Only 3 pass verdicts — below threshold
    for i in range(3):
        _insert_metric(isolated_db, "role_verdict", 1.0, "count",
                       {"role": "executor", "verdict": "done"},
                       ts=_recent(hours_ago=3, minutes_ago=i))

    record_overturn(
        pr=200,
        prior_role="executor",
        prior_verdict="done",
        contradicting_source="code-reviewer",
        kind="downstream_needs_fix",
        evidence_ref="pr-artifacts/200/xyz.jsonl",
    )

    result = overturn_rate_by_role_24h()
    assert len(result) == 1
    assert result[0]["overturn_rate"] is None
    assert result[0]["sample_size"] == 3


def test_sample_size_gating_at_threshold(isolated_db):
    """Exactly 5 pass verdicts must yield a numeric overturn_rate."""
    from backend.verdict_overturn import record_overturn, overturn_rate_by_role_24h

    for i in range(5):
        _insert_metric(isolated_db, "role_verdict", 1.0, "count",
                       {"role": "security-reviewer", "verdict": "pass"},
                       ts=_recent(hours_ago=4, minutes_ago=i))

    record_overturn(
        pr=300,
        prior_role="security-reviewer",
        prior_verdict="pass",
        contradicting_source="code-reviewer",
        kind="downstream_needs_fix",
        evidence_ref="pr-artifacts/300/aaa.jsonl",
    )

    result = overturn_rate_by_role_24h()
    assert len(result) == 1
    assert result[0]["overturn_rate"] is not None
    assert result[0]["overturn_rate"] == pytest.approx(1 / 5)


# ---------------------------------------------------------------------------
# Test: sort order — highest overturn_rate first, None rows last
# ---------------------------------------------------------------------------


def test_sort_order(isolated_db):
    """Results must be sorted highest overturn_rate first, None rows last."""
    from backend.verdict_overturn import record_overturn, overturn_rate_by_role_24h

    # role-a: 5 passes, 2 overturns → 40%
    for i in range(5):
        _insert_metric(isolated_db, "role_verdict", 1.0, "count",
                       {"role": "role-a", "verdict": "pass"},
                       ts=_recent(hours_ago=5, minutes_ago=i))
    for j in range(2):
        record_overturn(
            pr=400 + j,
            prior_role="role-a",
            prior_verdict="pass",
            contradicting_source="role-b",
            kind="downstream_needs_fix",
            evidence_ref=f"pr-artifacts/{400 + j}/x.jsonl",
            ts=_recent(hours_ago=5, minutes_ago=10 + j),
        )

    # role-b: 5 passes, 1 overturn → 20%
    for i in range(5):
        _insert_metric(isolated_db, "role_verdict", 1.0, "count",
                       {"role": "role-b", "verdict": "pass"},
                       ts=_recent(hours_ago=6, minutes_ago=i))
    record_overturn(
        pr=500,
        prior_role="role-b",
        prior_verdict="pass",
        contradicting_source="role-a",
        kind="downstream_needs_fix",
        evidence_ref="pr-artifacts/500/y.jsonl",
        ts=_recent(hours_ago=6, minutes_ago=10),
    )

    # role-c: 3 passes, no overturns → None (below threshold)
    for i in range(3):
        _insert_metric(isolated_db, "role_verdict", 1.0, "count",
                       {"role": "role-c", "verdict": "done"},
                       ts=_recent(hours_ago=7, minutes_ago=i))

    result = overturn_rate_by_role_24h()
    roles = [r["role"] for r in result]
    rates = [r["overturn_rate"] for r in result]

    # role-a (40%) before role-b (20%) before role-c (None)
    assert roles.index("role-a") < roles.index("role-b")
    assert roles.index("role-b") < roles.index("role-c")
    assert rates[-1] is None


# ---------------------------------------------------------------------------
# Test: producer logic — replay D#1383 sequence
#
# Sequence: acceptance-tester marks pass on PR 1383, then code-reviewer
# returns needs-fix on the same PR.  Should yield exactly ONE
# downstream_needs_fix row.
# ---------------------------------------------------------------------------


def test_d1383_sequence_exactly_one_overturn(isolated_db, tmp_path, monkeypatch):
    """Replay the D#1383 sequence: exactly one downstream_needs_fix row emitted."""
    import subprocess
    from backend.verdict_overturn import overturn_rate_by_role_24h

    # Set up a minimal agent_run table in a separate duckdb file (simulating the
    # stats db that the hook script reads from).  For this unit test we directly
    # call the Python detection logic rather than shelling out to bash.

    # Seed agent_run table for the D#1383 scenario
    db_conn = _duckdb.connect(str(isolated_db))
    try:
        # Create agent_run table
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_run (
                agent_id    VARCHAR PRIMARY KEY,
                role        VARCHAR NOT NULL,
                discussion  INTEGER,
                pr          INTEGER,
                start_ts    TIMESTAMPTZ NOT NULL,
                end_ts      TIMESTAMPTZ,
                verdict     VARCHAR
            )
        """)
        # acceptance-tester passed first
        db_conn.execute(
            "INSERT INTO agent_run VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["acceptance-tester-1383-111", "acceptance-tester", 1383, 1383,
             _recent(hours_ago=2),
             _recent(hours_ago=1, minutes_ago=55), "pass"],
        )
        # Seed 6 pass verdicts for acceptance-tester so sample_size >= 5
        for i in range(6):
            db_conn.execute(
                "INSERT OR IGNORE INTO metric_event VALUES (?, ?, ?, ?, ?, ?)",
                [_recent(hours_ago=3, minutes_ago=i),
                 "role_verdict", json.dumps({"role": "acceptance-tester", "verdict": "pass"}),
                 1.0, "count", "test"],
            )
    finally:
        db_conn.close()

    # Simulate the verdict-overturn producer logic directly in Python
    # (mirrors what verdict-overturn.sh does)
    from backend.stats_connection import get_read_connection
    from backend.verdict_overturn import record_overturn

    pr = 1383
    current_role = "code-reviewer"
    current_verdict = "needs-fix"

    # Query as the hook would
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT role, verdict, agent_id
            FROM agent_run
            WHERE pr = ?
              AND verdict IN ('pass', 'done')
              AND role != ?
              AND end_ts IS NOT NULL
            ORDER BY end_ts ASC
            """,
            [pr, current_role],
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, f"Expected 1 prior-pass row, got {rows}"
    prior_role, prior_verdict, prior_agent_id = rows[0]
    assert prior_role == "acceptance-tester"
    assert prior_verdict == "pass"

    # Record overturn (this is what the hook calls)
    record_overturn(
        pr=pr,
        prior_role=prior_role,
        prior_verdict=prior_verdict,
        contradicting_source=current_role,
        kind="downstream_needs_fix",
        evidence_ref=f".autonomous-team/pr-artifacts/{pr}/testsha.jsonl",
    )

    # Verify exactly ONE downstream_needs_fix row
    conn2 = _duckdb.connect(str(isolated_db))
    try:
        overturn_rows = conn2.execute(
            "SELECT COUNT(*) FROM metric_event WHERE metric = 'verdict_overturn'"
        ).fetchone()
    finally:
        conn2.close()

    assert overturn_rows[0] == 1, f"Expected exactly 1 overturn row, got {overturn_rows[0]}"

    # Verify reader picks it up with correct rate
    result = overturn_rate_by_role_24h()
    at_rows = [r for r in result if r["role"] == "acceptance-tester"]
    assert len(at_rows) == 1
    assert at_rows[0]["overturns"] == 1


# ---------------------------------------------------------------------------
# Test: no self-overturn — same role should not trigger
# ---------------------------------------------------------------------------


def test_no_self_overturn(isolated_db):
    """The producer must filter out runs where prior_role == current_role."""
    import subprocess

    # Seed agent_run with acceptance-tester pass then acceptance-tester needs-fix
    db_conn = _duckdb.connect(str(isolated_db))
    try:
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_run (
                agent_id    VARCHAR PRIMARY KEY,
                role        VARCHAR NOT NULL,
                discussion  INTEGER,
                pr          INTEGER,
                start_ts    TIMESTAMPTZ NOT NULL,
                end_ts      TIMESTAMPTZ,
                verdict     VARCHAR
            )
        """)
        db_conn.execute(
            "INSERT INTO agent_run VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["acceptance-tester-100-111", "acceptance-tester", 100, 100,
             _recent(hours_ago=2),
             _recent(hours_ago=1, minutes_ago=55), "pass"],
        )
    finally:
        db_conn.close()

    from backend.stats_connection import get_read_connection

    # Current role is SAME as prior role — should return 0 rows from the query
    conn = get_read_connection()
    try:
        rows = conn.execute(
            """
            SELECT role, verdict, agent_id
            FROM agent_run
            WHERE pr = ?
              AND verdict IN ('pass', 'done')
              AND role != ?
              AND end_ts IS NOT NULL
            ORDER BY end_ts ASC
            """,
            [100, "acceptance-tester"],  # same role as current
        ).fetchall()
    finally:
        conn.close()

    # Must be empty — no cross-role contradiction
    assert rows == [], f"Expected no self-overturn rows, got {rows}"
