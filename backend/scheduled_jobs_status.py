"""
scheduled_jobs_status.py — per-job status summary for the scheduled-jobs framework.

Reports: last-run-at, next-run-at, last-exit-code, consecutive-failures per job.
Reads from runs.jsonl (fast path) and/or the scheduled_job_runs DuckDB table.

Usage:
    python3 backend/scheduled_jobs_status.py
    python3 backend/scheduled_jobs_status.py --json
    python3 backend/scheduled_jobs_status.py --job heartbeat
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Path helpers ──────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run_log_path() -> Path:
    return _repo_root() / ".autonomous-team" / "scheduled-jobs" / "runs.jsonl"


def _breaker_path(job_name: str) -> Path:
    return Path(f"/tmp/autonomous-sched-breaker-{job_name}.json")


def _manifest_path() -> Path:
    return _repo_root() / "scripts" / "schedule" / "jobs.yaml"


# ── Next-run calculation ──────────────────────────────────────────────────────

def next_run_at(schedule: str, after: datetime) -> str | None:
    """Return ISO8601 string of next run time after `after`, or None if error."""
    try:
        # Walk forward minute by minute up to 1 week
        import importlib.util
        parse_jobs_path = _repo_root() / "scripts" / "schedule" / "parse_jobs.py"
        spec = importlib.util.spec_from_file_location("parse_jobs", parse_jobs_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        from datetime import timedelta
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(10080):  # 1 week of minutes
            if mod._cron_matches(schedule, candidate):  # type: ignore[attr-defined]
                return candidate.strftime("%Y-%m-%dT%H:%M:00Z")
            candidate += timedelta(minutes=1)
        return None
    except Exception:
        return None


# ── Read run log ──────────────────────────────────────────────────────────────

def read_run_log() -> list[dict]:
    """Read all rows from runs.jsonl. Returns list of dicts, newest first."""
    run_log = _run_log_path()
    if not run_log.exists():
        return []
    rows = []
    with run_log.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return rows


# ── Per-job status ────────────────────────────────────────────────────────────

def job_status(job_name: str, schedule: str | None = None) -> dict:
    """Return status dict for a single job."""
    rows = [r for r in read_run_log() if r.get("job") == job_name]

    last_run_at = rows[0].get("started_at") if rows else None
    last_exit_code = rows[0].get("exit_code") if rows else None

    # Consecutive failures from breaker file (most accurate)
    consecutive_failures = 0
    breaker_file = _breaker_path(job_name)
    if breaker_file.exists():
        try:
            b = json.loads(breaker_file.read_text())
            consecutive_failures = b.get("consecutive_failures", 0)
        except Exception:
            pass

    # Fall back to computing from run log
    if not breaker_file.exists() and rows:
        for r in rows:
            if r.get("exit_code", 0) == 0:
                break
            consecutive_failures += 1

    now = datetime.now(timezone.utc)
    next_run = next_run_at(schedule, now) if schedule else None

    return {
        "job": job_name,
        "last_run_at": last_run_at,
        "next_run_at": next_run,
        "last_exit_code": last_exit_code,
        "consecutive_failures": consecutive_failures,
        "total_runs": len(rows),
    }


# ── Load manifest jobs ────────────────────────────────────────────────────────

def load_manifest_jobs() -> list[dict]:
    """Return list of job dicts from jobs.yaml."""
    manifest = _manifest_path()
    if not manifest.exists():
        return []
    try:
        import importlib.util
        parse_jobs_path = _repo_root() / "scripts" / "schedule" / "parse_jobs.py"
        spec = importlib.util.spec_from_file_location("parse_jobs", parse_jobs_path)
        if spec is None or spec.loader is None:
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.validate_manifest(manifest, _repo_root() / "scripts" / "schedule" / "jobs")  # type: ignore[attr-defined]
    except Exception:
        return []


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Scheduled jobs status")
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    ap.add_argument("--job", default=None, help="Show status for a specific job only")
    args = ap.parse_args()

    manifest_jobs = load_manifest_jobs()
    job_schedules = {j["name"]: j["schedule"] for j in manifest_jobs}

    if args.job:
        names = [args.job]
    else:
        # All jobs with any run history + all manifest jobs
        all_names = set(job_schedules.keys())
        rows = read_run_log()
        for r in rows:
            job = r.get("job", "")
            if job and job != "dispatcher":
                all_names.add(job)
        names = sorted(all_names)

    statuses = [job_status(n, job_schedules.get(n)) for n in names]

    if args.json:
        print(json.dumps(statuses, indent=2))
        return

    if not statuses:
        print("No scheduled jobs found.")
        return

    print(f"{'JOB':<20} {'LAST RUN':<22} {'NEXT RUN':<22} {'EXIT':>5} {'FAILURES':>8}")
    print("-" * 80)
    for s in statuses:
        last = (s["last_run_at"] or "never")[:19]
        nxt = (s["next_run_at"] or "unknown")[:19]
        code = str(s["last_exit_code"]) if s["last_exit_code"] is not None else "-"
        fails = str(s["consecutive_failures"])
        print(f"{s['job']:<20} {last:<22} {nxt:<22} {code:>5} {fails:>8}")


if __name__ == "__main__":
    main()
