"""Tests for scripts/migrate-orphan-worktree-rate-unit.sh

Simulates the bad state (orphan_worktree_rate rows with unit='ratio') and
confirms the migration script fixes them to unit='count'.

Also verifies idempotency and dry-run behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate-orphan-worktree-rate-unit.sh"

sys.path.insert(0, str(REPO_ROOT))

try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="duckdb not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_db(db_path: Path, rows: list[dict]) -> None:
    """Write metric_event rows directly to a temp DuckDB.

    Each dict must have: metric, value, unit, tags (optional), ts (optional).
    """
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metric_event (
            ts      TIMESTAMP NOT NULL,
            metric  TEXT      NOT NULL,
            tags    JSON,
            value   DOUBLE    NOT NULL,
            unit    TEXT      NOT NULL,
            source  TEXT,
            PRIMARY KEY (ts, metric, tags)
        )
    """)
    base_ts = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    for i, row in enumerate(rows):
        ts = row.get("ts", datetime(2026, 5, 10, 12, i, 0, tzinfo=timezone.utc))
        tags_json = "{}"  # default empty tags
        conn.execute(
            "INSERT OR IGNORE INTO metric_event (ts, metric, tags, value, unit) VALUES (?, ?, ?, ?, ?)",
            [ts, row["metric"], tags_json, row["value"], row["unit"]],
        )
    conn.close()


def _read_units(db_path: Path, metric: str) -> list[str]:
    conn = duckdb.connect(str(db_path), read_only=True)
    rows = conn.execute(
        "SELECT unit FROM metric_event WHERE metric = ? ORDER BY ts", [metric]
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _run_migrate(db_path: Path, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["STATS_DB_PATH"] = str(db_path)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = str(db_path.parent)
    cmd = ["bash", str(MIGRATE_SCRIPT)] + (extra_args or [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrateOrphanWorktreeRateUnit:

    def test_script_exists_and_is_executable_via_bash(self, tmp_path):
        """The migration script exists and can be invoked by bash."""
        assert MIGRATE_SCRIPT.exists(), f"Missing: {MIGRATE_SCRIPT}"

    def test_fixes_ratio_rows_to_count(self, tmp_path):
        """Bad rows (unit='ratio') become unit='count' after migration."""
        db = tmp_path / "stats.duckdb"
        _seed_db(db, [
            {"metric": "orphan_worktree_rate", "value": 7200000.0, "unit": "ratio"},
            {"metric": "orphan_worktree_rate", "value": 3600000.0, "unit": "ratio"},
        ])

        result = _run_migrate(db)
        assert result.returncode == 0, f"script failed:\n{result.stderr}\n{result.stdout}"

        units = _read_units(db, "orphan_worktree_rate")
        assert all(u == "count" for u in units), f"Expected all 'count', got {units}"
        assert "2 row" in result.stdout or "updated" in result.stdout

    def test_skips_already_correct_rows(self, tmp_path):
        """Rows already with unit='count' are untouched."""
        db = tmp_path / "stats.duckdb"
        _seed_db(db, [
            {"metric": "orphan_worktree_rate", "value": 2.0, "unit": "count"},
        ])

        result = _run_migrate(db)
        assert result.returncode == 0, f"script failed:\n{result.stderr}\n{result.stdout}"

        units = _read_units(db, "orphan_worktree_rate")
        assert units == ["count"]
        assert "0 row" in result.stdout or "already clean" in result.stdout

    def test_idempotent(self, tmp_path):
        """Running the migration twice produces the same result and no error."""
        db = tmp_path / "stats.duckdb"
        _seed_db(db, [
            {"metric": "orphan_worktree_rate", "value": 5000.0, "unit": "ratio"},
        ])

        # First run
        r1 = _run_migrate(db)
        assert r1.returncode == 0, f"First run failed:\n{r1.stderr}"
        units_after_first = _read_units(db, "orphan_worktree_rate")
        assert units_after_first == ["count"]

        # Second run — must succeed and report 0 rows updated
        r2 = _run_migrate(db)
        assert r2.returncode == 0, f"Second run failed:\n{r2.stderr}"
        units_after_second = _read_units(db, "orphan_worktree_rate")
        assert units_after_second == ["count"]
        assert "0 row" in r2.stdout or "already clean" in r2.stdout

    def test_dry_run_does_not_mutate(self, tmp_path):
        """--dry-run reports count of affected rows but makes no changes."""
        db = tmp_path / "stats.duckdb"
        _seed_db(db, [
            {"metric": "orphan_worktree_rate", "value": 1000.0, "unit": "ratio"},
        ])

        result = _run_migrate(db, extra_args=["--dry-run"])
        assert result.returncode == 0, f"dry-run failed:\n{result.stderr}"

        # Rows must still have unit='ratio' — nothing changed
        units = _read_units(db, "orphan_worktree_rate")
        assert units == ["ratio"], f"dry-run mutated the DB: {units}"
        assert "DRY RUN" in result.stdout or "dry-run" in result.stdout.lower()

    def test_does_not_touch_other_metrics(self, tmp_path):
        """The migration only targets orphan_worktree_rate, not other ratio metrics."""
        db = tmp_path / "stats.duckdb"
        _seed_db(db, [
            {"metric": "orphan_worktree_rate", "value": 999.0, "unit": "ratio"},
            {"metric": "scan_to_spawn_ratio", "value": 0.75, "unit": "ratio"},
            {"metric": "budget_usage", "value": 0.5, "unit": "ratio"},
        ])

        result = _run_migrate(db)
        assert result.returncode == 0

        orphan_units = _read_units(db, "orphan_worktree_rate")
        assert orphan_units == ["count"], "orphan_worktree_rate should be 'count'"

        scan_units = _read_units(db, "scan_to_spawn_ratio")
        assert scan_units == ["ratio"], "scan_to_spawn_ratio should be untouched"

        budget_units = _read_units(db, "budget_usage")
        assert budget_units == ["ratio"], "budget_usage should be untouched"

    def test_handles_empty_db(self, tmp_path):
        """An empty (but valid) DuckDB should not cause errors."""
        db = tmp_path / "stats.duckdb"
        conn = duckdb.connect(str(db))
        conn.execute("""
            CREATE TABLE metric_event (
                ts TIMESTAMP NOT NULL, metric TEXT NOT NULL, tags JSON,
                value DOUBLE NOT NULL, unit TEXT NOT NULL, source TEXT,
                PRIMARY KEY (ts, metric, tags)
            )
        """)
        conn.close()

        result = _run_migrate(db)
        assert result.returncode == 0, f"script failed on empty DB:\n{result.stderr}"
