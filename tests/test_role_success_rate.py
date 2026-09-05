"""tests/test_role_success_rate.py — role_success_rate metric (Discussion #540).

Simulates post-agent-hook invocations across mixed roles/verdicts, then
verifies that role_success_rate_24h() returns the expected per-role ratios.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

_seq = 0  # monotonic counter to ensure unique timestamps per emit call


@pytest.fixture()
def isolated_db(tmp_path):
    """Point stats_writer at a throwaway DuckDB for each test."""
    db = tmp_path / "test_stats.duckdb"
    old = os.environ.get("STATS_DB_PATH")
    os.environ["STATS_DB_PATH"] = str(db)
    yield db
    if old is None:
        os.environ.pop("STATS_DB_PATH", None)
    else:
        os.environ["STATS_DB_PATH"] = old


def _emit(role: str, verdict: str) -> None:
    """Thin wrapper that ensures unique timestamps to avoid PK dedup."""
    global _seq
    from backend.stats_writer import emit_verdict  # noqa: PLC0415
    now = datetime.now(timezone.utc) + timedelta(milliseconds=_seq)
    _seq += 1
    emit_verdict(role, verdict, ts=now)


def test_role_success_rate_basic(isolated_db):
    """Simulates 10 post-agent-hook invocations across 4 roles."""
    from backend.stats_writer import role_success_rate_24h  # noqa: PLC0415

    # executor: 3x done, 1x fail  → sample_size=4 → N/A (< 5)
    _emit("executor", "done")
    _emit("executor", "done")
    _emit("executor", "done")
    _emit("executor", "fail")

    # code-reviewer: 4x pass, 1x needs-fix → success_rate=0.80, sample_size=5
    _emit("code-reviewer", "pass")
    _emit("code-reviewer", "pass")
    _emit("code-reviewer", "pass")
    _emit("code-reviewer", "pass")
    _emit("code-reviewer", "needs-fix")

    # security-reviewer: 3x pass → sample_size=3 → N/A (< 5)
    _emit("security-reviewer", "pass")
    _emit("security-reviewer", "pass")
    _emit("security-reviewer", "pass")

    # impl-coordinator: 5x done → success_rate=1.00, sample_size=5
    _emit("impl-coordinator", "done")
    _emit("impl-coordinator", "done")
    _emit("impl-coordinator", "done")
    _emit("impl-coordinator", "done")
    _emit("impl-coordinator", "done")

    rows = role_success_rate_24h()
    by_role = {r["role"]: r for r in rows}

    # Roles with sample_size >= 5 must have a numeric success_rate
    cr = by_role["code-reviewer"]
    assert cr["sample_size"] == 5
    assert cr["success_rate"] is not None
    assert abs(cr["success_rate"] - 0.8) < 1e-9

    ic = by_role["impl-coordinator"]
    assert ic["sample_size"] == 5
    assert ic["success_rate"] is not None
    assert abs(ic["success_rate"] - 1.0) < 1e-9

    # Roles with sample_size < 5 must return success_rate=None
    ex = by_role["executor"]
    assert ex["sample_size"] == 4
    assert ex["success_rate"] is None

    sr = by_role["security-reviewer"]
    assert sr["sample_size"] == 3
    assert sr["success_rate"] is None


def test_sort_order(isolated_db):
    """Rows with a rate come first (lowest first); None rows are last."""
    from backend.stats_writer import role_success_rate_24h  # noqa: PLC0415

    # role-a: 5/5 done → 100%
    for _ in range(5):
        _emit("role-a", "done")

    # role-b: 3/5 pass → 60%
    for _ in range(3):
        _emit("role-b", "pass")
    for _ in range(2):
        _emit("role-b", "fail")

    # role-c: 2 events → N/A
    _emit("role-c", "pass")
    _emit("role-c", "fail")

    rows = role_success_rate_24h()
    roles = [r["role"] for r in rows]

    # role-b (60%) must appear before role-a (100%), both before role-c (N/A)
    assert roles.index("role-b") < roles.index("role-a")
    assert roles.index("role-a") < roles.index("role-c")


def test_empty_db(isolated_db):
    """No data returns empty list without error."""
    from backend.stats_writer import role_success_rate_24h  # noqa: PLC0415
    assert role_success_rate_24h() == []


def test_emit_verdict_cli(isolated_db):
    """emit-verdict subcommand works end-to-end via subprocess."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable, "backend/stats_writer.py", "emit-verdict",
            "--role", "executor", "--verdict", "done",
        ],
        env={**os.environ, "STATS_DB_PATH": str(isolated_db)},
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0, result.stderr
    assert "emit_verdict" in result.stdout
