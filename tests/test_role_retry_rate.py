"""tests/test_role_retry_rate.py — role_retry_rate metric (Discussion #540).

Synthetic test: emit 10 verdict events (3 needs-fix, 7 done) for role=executor
→ retry_rate = 0.3. Verifies RPC shape and N/A behaviour for sample_size < 5.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

_seq = 0  # monotonic counter to avoid PK dedup collisions


@pytest.fixture()
def isolated_db(tmp_path):
    """Point stats_writer at a throwaway DuckDB for each test."""
    db = tmp_path / "test_retry_stats.duckdb"
    old = os.environ.get("STATS_DB_PATH")
    os.environ["STATS_DB_PATH"] = str(db)
    yield db
    if old is None:
        os.environ.pop("STATS_DB_PATH", None)
    else:
        os.environ["STATS_DB_PATH"] = old


def _emit(role: str, verdict: str) -> None:
    global _seq
    from backend.stats_writer import emit_verdict  # noqa: PLC0415
    now = datetime.now(timezone.utc) + timedelta(milliseconds=_seq)
    _seq += 1
    emit_verdict(role, verdict, ts=now)


def test_retry_rate_basic(isolated_db):
    """10 events for executor: 3 needs-fix + 7 done → retry_rate = 0.3."""
    from backend.stats_writer import role_retry_rate_24h  # noqa: PLC0415

    for _ in range(7):
        _emit("executor", "done")
    for _ in range(3):
        _emit("executor", "needs-fix")

    rows = role_retry_rate_24h()
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "executor"
    assert row["sample_size"] == 10
    assert row["retry_rate"] is not None
    assert abs(row["retry_rate"] - 0.3) < 1e-9


def test_retry_rate_fail_verdicts(isolated_db):
    """fail verdict also counts as a retry event."""
    from backend.stats_writer import role_retry_rate_24h  # noqa: PLC0415

    for _ in range(4):
        _emit("code-reviewer", "pass")
    for _ in range(1):
        _emit("code-reviewer", "fail")

    rows = role_retry_rate_24h()
    by_role = {r["role"]: r for r in rows}
    cr = by_role["code-reviewer"]
    assert cr["sample_size"] == 5
    assert cr["retry_rate"] is not None
    assert abs(cr["retry_rate"] - 0.2) < 1e-9


def test_retry_rate_na_below_5(isolated_db):
    """Roles with sample_size < 5 return retry_rate = None."""
    from backend.stats_writer import role_retry_rate_24h  # noqa: PLC0415

    _emit("security-reviewer", "pass")
    _emit("security-reviewer", "needs-fix")
    _emit("security-reviewer", "pass")

    rows = role_retry_rate_24h()
    assert len(rows) == 1
    assert rows[0]["retry_rate"] is None
    assert rows[0]["sample_size"] == 3


def test_retry_rate_empty_db(isolated_db):
    """Empty DB returns empty list."""
    from backend.stats_writer import role_retry_rate_24h  # noqa: PLC0415
    assert role_retry_rate_24h() == []


def test_retry_rate_sort_order(isolated_db):
    """Rows are sorted highest retry_rate first; None rows last."""
    from backend.stats_writer import role_retry_rate_24h  # noqa: PLC0415

    # role-a: 1 needs-fix, 4 done → 20% retry
    for _ in range(4):
        _emit("role-a", "done")
    _emit("role-a", "needs-fix")

    # role-b: 4 needs-fix, 1 done → 80% retry
    for _ in range(1):
        _emit("role-b", "done")
    for _ in range(4):
        _emit("role-b", "needs-fix")

    # role-c: 2 events → N/A
    _emit("role-c", "pass")
    _emit("role-c", "fail")

    rows = role_retry_rate_24h()
    roles = [r["role"] for r in rows]

    # role-b (80%) must appear before role-a (20%), both before role-c (N/A)
    assert roles.index("role-b") < roles.index("role-a")
    assert roles.index("role-a") < roles.index("role-c")


def test_retry_rate_zero_retries(isolated_db):
    """All pass/done → retry_rate = 0.0."""
    from backend.stats_writer import role_retry_rate_24h  # noqa: PLC0415

    for _ in range(5):
        _emit("impl-coordinator", "done")

    rows = role_retry_rate_24h()
    assert len(rows) == 1
    assert rows[0]["retry_rate"] == 0.0
