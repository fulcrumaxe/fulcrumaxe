"""
Health monitor — detects stale loop runs and fires GitHub Issue alerts.

Usage:
    python backend/health_monitor.py check   # print health JSON, exit 0/1
    python backend/health_monitor.py alert   # check + create needs-boss Issue if stale
"""

from __future__ import annotations

if __name__ == '__main__' and __package__ is None:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_METRICS_PATH = _REPO_ROOT / ".autonomous-team" / "loop-metrics.jsonl"
_DEFAULT_THRESHOLD_MINUTES = 30

_ALERT_TITLE_PREFIX = "[Alert] Loop stale"
_NEEDS_BOSS_LABEL = "needs-boss"

from backend._repo import REPO as _REPO  # noqa: E402 (after path constants)
from backend.loop_metrics_ts import parse_loop_metrics_ts, report_skipped_row  # noqa: E402


def get_loop_metrics(
    n_entries: int = 10,
    metrics_path: Path | None = None,
) -> dict[str, Any]:
    """Read loop-metrics.jsonl and return summary fields for the /health endpoint.

    Returns a dict with:
        loop_last_run    — ISO timestamp of most recent run, or None
        loop_duration_s  — duration_seconds of most recent run, or None
        loop_idle_rate   — fraction of last n_entries runs where idle=true (0.0–1.0), or None
        malformed_lines  — count of lines that failed JSON parsing (int, always present)

    Malformed lines are skipped and counted; a single bad line does not raise or
    flip any health flag. Only if *every* non-empty line is malformed will
    loop_last_run / loop_duration_s be None.
    """
    if metrics_path is None:
        metrics_path = _DEFAULT_METRICS_PATH

    if not metrics_path.exists() or metrics_path.stat().st_size == 0:
        return {"loop_last_run": None, "loop_duration_s": None, "loop_idle_rate": None, "malformed_lines": 0}

    raw_lines: list[str] = []
    with metrics_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                raw_lines.append(stripped)

    if not raw_lines:
        return {"loop_last_run": None, "loop_duration_s": None, "loop_idle_rate": None, "malformed_lines": 0}

    # Parse all lines, collecting valid entries and counting malformed ones.
    parsed: list[dict[str, Any]] = []
    malformed_lines = 0
    for raw in raw_lines:
        try:
            entry = json.loads(raw)
            if isinstance(entry, dict):
                parsed.append(entry)
            else:
                malformed_lines += 1
        except (json.JSONDecodeError, ValueError):
            malformed_lines += 1
            import logging as _logging  # noqa: PLC0415
            _logging.getLogger(__name__).warning(
                "health_monitor: skipping malformed line in loop-metrics.jsonl: %.80s", raw
            )

    if not parsed:
        return {"loop_last_run": None, "loop_duration_s": None, "loop_idle_rate": None, "malformed_lines": malformed_lines}

    # Use last valid parsed entry for last_run and duration.
    last_entry = parsed[-1]

    # Support both field names: "timestamp" (old cron format) and "ts" (interactive /loop format)
    _raw_last_run = last_entry.get("timestamp") or last_entry.get("ts") or None
    if _raw_last_run is not None and parse_loop_metrics_ts(_raw_last_run) is None:
        # Present but unparseable (e.g. a raw epoch int, D#2315) -- don't leak
        # a non-string into a field declared `str | None`, and don't silently
        # drop it either: report the skip, same as every other reader of
        # this file.
        report_skipped_row(
            (metrics_path or _DEFAULT_METRICS_PATH).name, None, _raw_last_run, prefix="health_monitor"
        )
        loop_last_run: str | None = None
    else:
        loop_last_run = _raw_last_run
    # Support both "duration_seconds" (old) and "duration_s" (interactive /loop format)
    _dur = last_entry.get("duration_seconds") or last_entry.get("duration_s")
    loop_duration_s: int | None = int(_dur) if _dur is not None else None

    # Compute idle rate from last n_entries of *valid* parsed entries
    recent = parsed[-n_entries:]
    idle_count = sum(1 for e in recent if e.get("idle"))
    valid_count = len(recent)

    loop_idle_rate: float | None = (
        round(idle_count / valid_count, 4) if valid_count > 0 else None
    )

    return {
        "loop_last_run": loop_last_run,
        "loop_duration_s": loop_duration_s,
        "loop_idle_rate": loop_idle_rate,
        "malformed_lines": malformed_lines,
    }


_LOOP_RUNS_DIR = _REPO_ROOT / ".autonomous-team" / "loop-runs"
_STALE_WARNING_MINUTES = 60  # >30 min → warning, >60 min → error


def _get_latest_loop_run_mtime() -> float | None:
    """Return the max mtime (as POSIX float) across .autonomous-team/loop-runs/*/*.log.

    Returns None when no log files exist.
    """
    import glob  # noqa: PLC0415
    pattern = str(_LOOP_RUNS_DIR / "*" / "*.log")
    files = glob.glob(pattern)
    if not files:
        return None
    try:
        return max(Path(f).stat().st_mtime for f in files)
    except OSError:
        return None


def get_loop_health_dashboard(
    metrics_path: Path | None = None,
) -> dict[str, Any]:
    """Return loop health in the shape expected by the dashboard LoopHealth interface.

    Freshness comes from check_loop_health() which reads loop-runs/*/*.log mtime.
    Duration is read from loop-metrics.jsonl (best effort).

    Returns a dict with:
        lastRun   — ISO timestamp string
        status    — 'ok' | 'error' | 'idle' | 'warning'
        duration  — duration_seconds as int
    """
    health = check_loop_health(metrics_path=metrics_path)
    metrics = get_loop_metrics(metrics_path=metrics_path)

    # Use the most recent timestamp from either source:
    # - loop-runs mtime signal (check_loop_health)
    # - loop-metrics.jsonl last-entry timestamp (get_loop_metrics)
    # This ensures interactive /loop runs that write loop-metrics but not loop-runs
    # files still show as recent.
    loop_runs_at = health.get("lastRunAt") or health.get("last_run")
    metrics_at = metrics.get("loop_last_run")

    def _parse_ts(ts: str | None) -> float:
        if not ts:
            return 0.0
        parsed = parse_loop_metrics_ts(ts)
        if parsed is None:
            # Present but unparseable -- report it rather than silently
            # returning 0.0 with no signal (D#2315 Spec item 10). In
            # practice get_loop_metrics() above already screens
            # metrics_at for this, so this branch mainly guards
            # loop_runs_at (a non-loop-metrics source) against the same
            # class of bad input.
            report_skipped_row(
                (metrics_path or _DEFAULT_METRICS_PATH).name, None, ts, prefix="health_monitor"
            )
            return 0.0
        return parsed.timestamp()

    loop_runs_epoch = _parse_ts(loop_runs_at)
    metrics_epoch = _parse_ts(metrics_at)

    if metrics_epoch > loop_runs_epoch:
        last_run_at = metrics_at
        # Re-derive health status from loop-metrics freshness
        import time as _time  # noqa: PLC0415
        age_minutes = (_time.time() - metrics_epoch) / 60
        if age_minutes < 30:
            health_status = "healthy"
        elif age_minutes < 60:
            health_status = "warning"
        else:
            health_status = "error"
    else:
        last_run_at = loop_runs_at
        health_status = health.get("status", "error")

    duration = metrics.get("loop_duration_s")

    # Map health status → dashboard status string
    idle_rate = metrics.get("loop_idle_rate")
    if health_status == "healthy":
        if idle_rate is not None and idle_rate >= 1.0:
            status = "idle"
        else:
            status = "ok"
    elif health_status == "warning":
        status = "warning"
    else:
        status = "error"

    result: dict[str, Any] = {
        "lastRun": last_run_at or "",
        "status": status,
        "duration": duration if duration is not None else 0,
    }
    return result


def check_loop_health(
    threshold_minutes: int | None = None,
    metrics_path: Path | None = None,  # kept for backward-compat; no longer primary
) -> dict[str, Any]:
    """Return a health dict describing whether the loop has run recently.

    Primary freshness signal is the max mtime across
    ``.autonomous-team/loop-runs/*/*.log``.  The old ``loop.log`` SUMMARY-line
    approach is no longer used as the primary source because interactive-session
    loop iterations write per-run files under loop-runs/ but do not append to
    the legacy loop.log.

    Status thresholds:
      - mtime < 30 min ago  → healthy=True,  status="healthy"
      - mtime 30–60 min ago → healthy=False, status="warning"
      - mtime > 60 min OR no files → healthy=False, status="error"

    Args:
        threshold_minutes: Minutes before a loop run is considered stale.
            Defaults to the ``AF_LOOP_STALE_MINUTES`` env var or 30.
            A run is ``healthy`` only when age < threshold_minutes.
        metrics_path: Unused (kept for API backward compatibility).

    Returns:
        A dict with at minimum the keys ``healthy``, ``threshold_minutes``,
        ``status``, ``lastRunAt`` (ISO string or None), and ``age_minutes``.
    """
    if threshold_minutes is None:
        threshold_minutes = int(
            os.environ.get("AF_LOOP_STALE_MINUTES", _DEFAULT_THRESHOLD_MINUTES)
        )

    now = datetime.now(tz=timezone.utc)
    latest_mtime = _get_latest_loop_run_mtime()

    if latest_mtime is None:
        return {
            "healthy": False,
            "status": "error",
            "last_run": None,
            "lastRunAt": None,
            "age_minutes": None,
            "threshold_minutes": threshold_minutes,
            "reason": "no loop-runs logs found",
        }

    last_run_dt = datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
    age_seconds = (now - last_run_dt).total_seconds()
    age_minutes = round(age_seconds / 60, 1)
    last_run_iso = last_run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    if age_minutes <= threshold_minutes:
        status = "healthy"
        healthy = True
    elif age_minutes <= _STALE_WARNING_MINUTES:
        status = "warning"
        healthy = False
    else:
        status = "error"
        healthy = False

    return {
        "healthy": healthy,
        "status": status,
        "last_run": last_run_iso,
        "lastRunAt": last_run_iso,
        "age_minutes": age_minutes,
        "threshold_minutes": threshold_minutes,
    }


def _open_stale_alert_exists(age_minutes: float) -> bool:
    """Return True if a matching open needs-boss Issue already exists."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--label", _NEEDS_BOSS_LABEL,
                "--state", "open",
                "--json", "title",
                "--repo", _REPO,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        issues = json.loads(result.stdout or "[]")
        return any(
            issue.get("title", "").startswith(_ALERT_TITLE_PREFIX)
            for issue in issues
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def create_alert_issue(health: dict[str, Any]) -> str | None:
    """Create a needs-boss GitHub Issue for a stale loop.

    Returns the Issue URL on success, or None if skipped (duplicate) or
    creation failed.
    """
    age = health.get("age_minutes")
    age_str = f"{age}" if age is not None else "unknown"
    title = f"{_ALERT_TITLE_PREFIX} — last run {age_str} minutes ago"

    if _open_stale_alert_exists(age or 0):
        return None  # duplicate — skip

    last_run = health.get("last_run") or "unknown"
    reason = health.get("reason", "")
    body_parts = [
        "The autonomous loop appears to have stalled.",
        f"**Last recorded run:** {last_run}",
        f"**Age:** {age_str} minutes (threshold: {health.get('threshold_minutes')} minutes)",
    ]
    if reason:
        body_parts.append(f"**Reason:** {reason}")
    body_parts.append(
        "\nInvestigate the cron job and process watchdog. "
        "Check `.autonomous-team/loop.lock` and `.autonomous-team/loop.log`."
    )
    body = "\n".join(body_parts)

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--title", title,
                "--body", body,
                "--label", _NEEDS_BOSS_LABEL,
                "--repo", _REPO,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    threshold = getattr(args, "threshold", None)
    metrics = Path(args.metrics) if getattr(args, "metrics", None) else None
    health = check_loop_health(threshold_minutes=threshold, metrics_path=metrics)
    print(json.dumps(health))
    return 0 if health["healthy"] else 1


def _cmd_alert(args: argparse.Namespace) -> int:
    threshold = getattr(args, "threshold", None)
    metrics = Path(args.metrics) if getattr(args, "metrics", None) else None
    health = check_loop_health(threshold_minutes=threshold, metrics_path=metrics)
    print(json.dumps(health))
    if not health["healthy"]:
        url = create_alert_issue(health)
        if url:
            print(f"Alert issue created: {url}", file=sys.stderr)
        else:
            print("Alert issue skipped (duplicate or creation failed).", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Health monitor for the autonomous loop"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("check", "Print health JSON and exit 0 (healthy) or 1 (stale/missing)."),
        ("alert", "Check and create a needs-boss Issue if stale."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--threshold",
            type=int,
            default=None,
            metavar="MINUTES",
            help="Override stale threshold in minutes (default: AF_LOOP_STALE_MINUTES or 30).",
        )
        p.add_argument(
            "--metrics",
            default=None,
            metavar="PATH",
            help="Override path to loop-metrics.jsonl.",
        )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {"check": _cmd_check, "alert": _cmd_alert}
    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
