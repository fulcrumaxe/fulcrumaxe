"""
Tests for backend/spawn_activity.py

Isolation: every test points STATS_DB_PATH at a fresh tmp DuckDB file so the
real ~/.autonomous-forever-state/stats.duckdb is never touched.

Run bounded:
    timeout 120 python3 -m pytest backend/tests/test_spawn_activity.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_db(db_path: Path, rows: list[dict]) -> None:
    """Insert test rows into agent_run via agent_run_tracker's _ensure_schema + raw insert."""
    import duckdb
    from backend.agent_run_tracker import _ensure_schema  # noqa: PLC0415

    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    for r in rows:
        conn.execute(
            """
            INSERT INTO agent_run
                (agent_id, role, start_ts, end_ts, verdict, input_tok, output_tok,
                 model, discussion, pr, duration_s, blocked_reason, event_id,
                 cache_read, cache_write)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                r["agent_id"],
                r["role"],
                r.get("start_ts", datetime.now(timezone.utc) - timedelta(hours=1)),
                r.get("end_ts"),
                r.get("verdict"),
                r.get("input_tok"),
                r.get("output_tok"),
                r.get("model", "claude-sonnet-4-6"),
                r.get("discussion"),
                r.get("pr"),
                r.get("duration_s"),
                r.get("blocked_reason"),
                r.get("event_id", r["agent_id"]),
                r.get("cache_read"),
                r.get("cache_write"),
            ],
        )
    conn.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hours_ago(h: float) -> datetime:
    return _now() - timedelta(hours=h)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Isolated DuckDB file; STATS_DB_PATH env var set to point at it."""
    p = tmp_path / "test_spawn_activity.duckdb"
    monkeypatch.setenv("STATS_DB_PATH", str(p))
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRollupCounting:
    """Core counting-rule tests against a seeded fixture DB."""

    def test_basic_counts_two_roles(self, db_path):
        """Spawns / done / fail split correctly across two roles."""
        _seed_db(
            db_path,
            [
                # executor: 1 done, 1 fail, 1 in-flight (NULL verdict + NULL end_ts)
                {
                    "agent_id": "ex-done-1",
                    "role": "executor",
                    "verdict": "done",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 10_000,
                    "output_tok": 1_000,
                },
                {
                    "agent_id": "ex-fail-1",
                    "role": "executor",
                    "verdict": "fail",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 5_000,
                    "output_tok": 500,
                },
                {
                    "agent_id": "ex-inflight-1",
                    "role": "executor",
                    "verdict": None,
                    "end_ts": None,
                    "input_tok": None,
                    "output_tok": None,
                },
                # code-reviewer: 1 pass (= done bucket)
                {
                    "agent_id": "cr-pass-1",
                    "role": "code-reviewer",
                    "verdict": "pass",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 20_000,
                    "output_tok": 2_000,
                },
            ],
        )

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        by_role = {r["role"]: r for r in result}

        assert set(by_role) == {"executor", "code-reviewer"}

        ex = by_role["executor"]
        assert ex["spawns"] == 3
        assert ex["done"] == 1
        assert ex["fail"] == 1

        cr = by_role["code-reviewer"]
        assert cr["spawns"] == 1
        assert cr["done"] == 1
        assert cr["fail"] == 0

    def test_in_flight_excluded_from_done_and_fail(self, db_path):
        """In-flight row (no end_ts, no verdict) must NOT appear in done or fail."""
        _seed_db(
            db_path,
            [
                {
                    "agent_id": "inflight-only-1",
                    "role": "executor",
                    "verdict": None,
                    "end_ts": None,
                    "input_tok": None,
                    "output_tok": None,
                }
            ],
        )

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        assert len(result) == 1
        r = result[0]
        assert r["spawns"] == 1
        assert r["done"] == 0
        assert r["fail"] == 0

    def test_out_of_window_row_excluded(self, db_path):
        """A row older than --since is not counted."""
        _seed_db(
            db_path,
            [
                # This row is 12 hours old — outside a 6h window.
                {
                    "agent_id": "old-1",
                    "role": "executor",
                    "start_ts": _hours_ago(12),
                    "verdict": "done",
                    "end_ts": _hours_ago(11),
                    "input_tok": 1_000,
                    "output_tok": 100,
                },
                # This row is 1 hour old — inside the window.
                {
                    "agent_id": "recent-1",
                    "role": "executor",
                    "start_ts": _hours_ago(1),
                    "verdict": "done",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 2_000,
                    "output_tok": 200,
                },
            ],
        )

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        assert len(result) == 1
        r = result[0]
        assert r["spawns"] == 1, "Only the recent row should be counted"

    def test_needs_fix_verdict_not_in_done_or_fail(self, db_path):
        """needs-fix verdict counts in spawns only."""
        _seed_db(
            db_path,
            [
                {
                    "agent_id": "nf-1",
                    "role": "code-reviewer",
                    "verdict": "needs-fix",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 8_000,
                    "output_tok": 800,
                }
            ],
        )

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        r = result[0]
        assert r["spawns"] == 1
        assert r["done"] == 0
        assert r["fail"] == 0

    def test_empty_db_returns_empty_list(self, db_path):
        """Empty (schema-only) database returns []."""
        # Create schema but no rows.
        import duckdb
        from backend.agent_run_tracker import _ensure_schema  # noqa: PLC0415

        conn = duckdb.connect(str(db_path))
        _ensure_schema(conn)
        conn.close()

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        assert result == []

    def test_nonexistent_db_returns_empty_list(self, tmp_path, monkeypatch):
        """When STATS_DB_PATH points to a nonexistent file, rollup returns []."""
        monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "does_not_exist.duckdb"))

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        assert result == []

    def test_avg_tokens_correct(self, db_path):
        """avg_tokens is mean(input_tok + output_tok) over rows with token data."""
        _seed_db(
            db_path,
            [
                {
                    "agent_id": "tok-1",
                    "role": "executor",
                    "verdict": "done",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 10_000,
                    "output_tok": 2_000,  # total = 12_000
                },
                {
                    "agent_id": "tok-2",
                    "role": "executor",
                    "verdict": "done",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 6_000,
                    "output_tok": 2_000,  # total = 8_000
                },
            ],
        )

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        r = result[0]
        assert r["avg_tokens"] == 10_000  # (12_000 + 8_000) / 2

    def test_total_usd_uses_cost_pricing(self, db_path):
        """total_usd is computed via cost_pricing.cost_usd, not a hardcoded rate."""
        from backend.cost_pricing import cost_usd  # noqa: PLC0415

        input_tok = 10_000
        output_tok = 1_000
        model = "claude-sonnet-4-6"
        expected_usd = round(cost_usd(input_tok, output_tok, model=model), 4)

        _seed_db(
            db_path,
            [
                {
                    "agent_id": "usd-1",
                    "role": "executor",
                    "verdict": "done",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": input_tok,
                    "output_tok": output_tok,
                    "model": model,
                }
            ],
        )

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        assert result[0]["total_usd"] == expected_usd

    def test_rows_sorted_by_role_ascending(self, db_path):
        """Output rows are sorted alphabetically by role."""
        _seed_db(
            db_path,
            [
                {
                    "agent_id": "z-role-1",
                    "role": "security-reviewer",
                    "verdict": "pass",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 1000,
                    "output_tok": 100,
                },
                {
                    "agent_id": "a-role-1",
                    "role": "executor",
                    "verdict": "done",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 1000,
                    "output_tok": 100,
                },
            ],
        )

        from backend.spawn_activity import rollup  # noqa: PLC0415

        result = rollup(6)
        assert [r["role"] for r in result] == ["executor", "security-reviewer"]


class TestParseSince:
    """Unit tests for the _parse_since helper."""

    def test_valid_6h(self):
        from backend.spawn_activity import _parse_since  # noqa: PLC0415
        assert _parse_since("6h") == 6

    def test_valid_24h(self):
        from backend.spawn_activity import _parse_since  # noqa: PLC0415
        assert _parse_since("24h") == 24

    def test_missing_h_suffix_raises(self):
        from backend.spawn_activity import _parse_since  # noqa: PLC0415
        with pytest.raises(ValueError, match="form Nh"):
            _parse_since("6")

    def test_minutes_suffix_raises(self):
        from backend.spawn_activity import _parse_since  # noqa: PLC0415
        with pytest.raises(ValueError, match="form Nh"):
            _parse_since("6m")

    def test_alpha_raises(self):
        from backend.spawn_activity import _parse_since  # noqa: PLC0415
        with pytest.raises(ValueError):
            _parse_since("banana")

    def test_zero_hours_raises(self):
        from backend.spawn_activity import _parse_since  # noqa: PLC0415
        with pytest.raises(ValueError, match="> 0"):
            _parse_since("0h")


class TestCLI:
    """End-to-end CLI tests calling the subprocess directly."""

    def test_json_flag_empty_db(self, db_path):
        """--json on an empty schema DB prints [] and exits 0."""
        import duckdb
        from backend.agent_run_tracker import _ensure_schema  # noqa: PLC0415

        conn = duckdb.connect(str(db_path))
        _ensure_schema(conn)
        conn.close()

        env = os.environ.copy()
        env["STATS_DB_PATH"] = str(db_path)
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "spawn_activity.py"), "--since=6h", "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout.strip()) == []

    def test_bad_since_exits_nonzero(self, db_path):
        """Bad --since value exits non-zero with a stderr message."""
        env = os.environ.copy()
        env["STATS_DB_PATH"] = str(db_path)
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "spawn_activity.py"), "--since=banana", "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode != 0
        assert "ERROR" in result.stderr or "banana" in result.stderr

    def test_json_output_matches_seeded_rows(self, db_path):
        """--since=6h --json rollup matches seeded agent_run rows."""
        _seed_db(
            db_path,
            [
                {
                    "agent_id": "cli-done-1",
                    "role": "executor",
                    "verdict": "done",
                    "end_ts": _hours_ago(0.5),
                    "input_tok": 10_000,
                    "output_tok": 1_000,
                    "model": "claude-sonnet-4-6",
                },
                {
                    "agent_id": "cli-fail-1",
                    "role": "executor",
                    "verdict": "fail",
                    "end_ts": _hours_ago(1),
                    "input_tok": 5_000,
                    "output_tok": 500,
                    "model": "claude-sonnet-4-6",
                },
                {
                    "agent_id": "cli-inflight-1",
                    "role": "executor",
                    "verdict": None,
                    "end_ts": None,
                    "input_tok": None,
                    "output_tok": None,
                },
            ],
        )

        env = os.environ.copy()
        env["STATS_DB_PATH"] = str(db_path)
        result = subprocess.run(
            [sys.executable, str(_REPO_ROOT / "backend" / "spawn_activity.py"), "--since=6h", "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout.strip())
        assert len(data) == 1
        r = data[0]
        assert r["role"] == "executor"
        assert r["spawns"] == 3
        assert r["done"] == 1
        assert r["fail"] == 1
