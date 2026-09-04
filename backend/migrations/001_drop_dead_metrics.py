"""backend/migrations/001_drop_dead_metrics.py

Idempotent migration: delete dead metric rows from metric_event.

Three metric names were written by test scaffolding and were never tied to a
real writer. They sit in the DB with stale timestamps and cause false-positive
stale-metric alerts in stats_freshness_watchdog.py.

Dead names:
  - test_write_with_dashboard_running
  - acceptance_test_gate2
  - test_write_gate2

Running this migration more than once is safe — DELETE WHERE is idempotent.

Usage:
    python3 backend/migrations/001_drop_dead_metrics.py [--dry-run]

Exit codes:
    0  — success (rows deleted or already absent)
    1  — unexpected error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Dead metric names — no writer exists for any of these.
DEAD_METRICS: tuple[str, ...] = (
    "test_write_with_dashboard_running",
    "acceptance_test_gate2",
    "test_write_gate2",
)


def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


def run(dry_run: bool = False) -> int:
    """Delete dead metric rows.  Returns count deleted (or would-delete in dry-run)."""
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        print("ERROR: duckdb not installed — run: pip install duckdb", file=sys.stderr)
        return -1

    db_path = _db_path()
    if not db_path.exists():
        print(f"[migration-001] stats.duckdb not found at {db_path} — nothing to delete")
        return 0

    placeholders = ", ".join("?" for _ in DEAD_METRICS)
    count_sql = f"SELECT COUNT(*) FROM metric_event WHERE metric IN ({placeholders})"
    delete_sql = f"DELETE FROM metric_event WHERE metric IN ({placeholders})"
    params = list(DEAD_METRICS)

    try:
        conn = duckdb.connect(str(db_path), read_only=dry_run)
        try:
            (count,) = conn.execute(count_sql, params).fetchone()
            if dry_run:
                print(
                    f"[migration-001] dry-run: would delete {count} row(s) "
                    f"for metrics: {', '.join(DEAD_METRICS)}"
                )
                return count

            if count == 0:
                print("[migration-001] no dead-metric rows found — already clean")
                return 0

            conn.execute(delete_sql, params)
            conn.commit()
            print(
                f"[migration-001] deleted {count} dead-metric row(s) "
                f"for: {', '.join(DEAD_METRICS)}"
            )
            return count
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[migration-001] ERROR: {exc}", file=sys.stderr)
        return -1


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete dead metric rows from DuckDB.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without modifying the database.",
    )
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    sys.exit(0 if result >= 0 else 1)


if __name__ == "__main__":
    main()
