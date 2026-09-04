"""
backend/stats/scheduled_jobs.py — per-feature stats module for scheduled_job_runs.

Owns the scheduled_job_runs table in the DuckDB stats store.
Single writer: dispatcher.sh (via run log ingest). Readers use stats_writer ingest
path to avoid DuckDB write contention.

Table schema:
    scheduled_job_runs(
        job TEXT,
        started_at TIMESTAMPTZ,
        ended_at TIMESTAMPTZ,
        exit_code INTEGER,
        stdout_head TEXT,
        stdout_tail TEXT,
        tokens_spent BIGINT
    )
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a script from the repo root: the DB path now resolves through
# backend.state_paths, so the package has to be importable either way.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── Path helpers ──────────────────────────────────────────────────────────────

def _run_log_path() -> Path:
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / ".autonomous-team" / "scheduled-jobs" / "runs.jsonl"


def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


# ── Table DDL ─────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    job           TEXT         NOT NULL,
    started_at    TIMESTAMPTZ  NOT NULL,
    ended_at      TIMESTAMPTZ,
    exit_code     INTEGER      NOT NULL DEFAULT 0,
    stdout_head   TEXT         DEFAULT '',
    stdout_tail   TEXT         DEFAULT '',
    tokens_spent  BIGINT       DEFAULT 0,
    note          TEXT         DEFAULT ''
);
"""

INSERT_SQL = """
INSERT INTO scheduled_job_runs
    (job, started_at, ended_at, exit_code, stdout_head, stdout_tail, tokens_spent, note)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


def ensure_table() -> None:
    """Create scheduled_job_runs table if it doesn't exist."""
    try:
        import duckdb  # type: ignore[import]
    except ImportError:
        return
    db = _db_path()
    if not db.parent.exists():
        return
    try:
        con = duckdb.connect(str(db))
        try:
            con.execute(CREATE_TABLE_SQL)
        finally:
            con.close()
    except Exception:
        pass


def ingest_run_log() -> int:
    """
    Read runs.jsonl and ingest any rows not yet in scheduled_job_runs.
    Returns number of rows inserted. Safe to call repeatedly (idempotent via started_at check).
    """
    try:
        import duckdb  # type: ignore[import]
    except ImportError:
        return 0

    run_log = _run_log_path()
    if not run_log.exists():
        return 0

    ensure_table()
    db = _db_path()
    if not db.parent.exists():
        return 0

    rows_inserted = 0
    try:
        con = duckdb.connect(str(db))
        try:
            with run_log.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    job = row.get("job", "")
                    started_at = row.get("started_at", "")
                    ended_at = row.get("ended_at", started_at)
                    exit_code = int(row.get("exit_code", 0))
                    note = row.get("note", "")

                    # Idempotency: skip if already present
                    existing = con.execute(
                        "SELECT 1 FROM scheduled_job_runs WHERE job=? AND started_at=?",
                        [job, started_at]
                    ).fetchone()
                    if existing:
                        continue

                    con.execute(INSERT_SQL, [job, started_at, ended_at, exit_code, "", "", 0, note])
                    rows_inserted += 1
        finally:
            con.close()
    except Exception:
        pass
    return rows_inserted


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Scheduled jobs stats module")
    ap.add_argument("command", choices=["ensure-table", "ingest", "status"])
    args = ap.parse_args()

    if args.command == "ensure-table":
        ensure_table()
        print("table ensured")
    elif args.command == "ingest":
        n = ingest_run_log()
        print(f"inserted {n} row(s)")
    elif args.command == "status":
        try:
            import duckdb  # type: ignore[import]
            con = duckdb.connect(str(_db_path()))
            try:
                rows = con.execute(
                    "SELECT job, started_at, exit_code, note FROM scheduled_job_runs "
                    "ORDER BY started_at DESC LIMIT 20"
                ).fetchall()
            finally:
                con.close()
            for r in rows:
                print(r)
        except Exception as exc:
            print(f"error: {exc}")
