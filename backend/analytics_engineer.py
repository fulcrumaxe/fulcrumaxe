"""
analytics_engineer.py — read-only DORA + KPI reader and snapshot emitter.

Aggregates DORA metrics (deploy frequency, lead time, change failure rate)
and KPI metrics (velocity, cycle time) over the trailing 7-day window and
writes a single markdown snapshot to wiki/analytics/<YYYY-MM-DD>.md.

This module is a PURE READER. It:
  - Reuses compute_dora_snapshot() from release_manager.py for deploy-freq
    and lead-time p50 — no independent recomputation.
  - Reuses kpi_engine functions (load_registry, compute_velocity,
    compute_pr_cycle_time) for velocity and cycle-time.
  - Does NOT write to state.db / stats.duckdb / any counter.
  - Does NOT spawn agents or mutate the blackboard.
  - Only writes wiki/analytics/<date>.md.

Change failure rate: computed by counting releases within the 7-day window
that had a [Bug] Discussion filed within 24h of their merged_at timestamp.
The bug-filing heuristic uses the GitHub Discussion list filtered by the
[Bug] label; if the gh CLI is unavailable the CFR falls back to "n/a".

Usage:
    python3 backend/analytics_engineer.py snapshot
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Reuse release_manager for deploy-freq + lead-time (no independent computation)
from backend.release_manager import compute_dora_snapshot  # noqa: E402

# Reuse kpi_engine for velocity + cycle-time
from backend.kpi_engine import (  # noqa: E402
    load_registry,
    compute_velocity,
    compute_pr_cycle_time,
)

_ANALYTICS_DIR = REPO_ROOT / "wiki" / "analytics"

_7D_SECONDS = 7 * 24 * 3600


def _releases_dir() -> Path:
    """Return the serving checkout's own releases directory.

    Computed on every call rather than cached as a module constant (D#2518):
    a pre-joined ``_RELEASES_DIR = REPO_ROOT / ...`` bound at import time is
    exactly what made ``stats.dora`` UNSCOPABLE — a per-request override had
    nothing to replace. ``_load_recent_releases(releases_dir=...)`` is how a
    caller reaches a different project's releases; this is only the
    no-project default.
    """
    return REPO_ROOT / ".autonomous-team" / "releases"


# ---------------------------------------------------------------------------
# Change failure rate helper
# ---------------------------------------------------------------------------

def _load_recent_releases(cutoff_ts: float, releases_dir: "Path | None" = None) -> list[dict]:
    """Return release records within the trailing 7-day window.

    Args:
        releases_dir: directory to glob ``*.json`` release records from, for
            per-project scoping. Defaults to the serving checkout's own
            releases dir when omitted.
    """
    effective_dir = releases_dir if releases_dir is not None else _releases_dir()
    releases: list[dict] = []
    if not effective_dir.exists():
        return releases
    for rf in effective_dir.glob("*.json"):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            # Explicitly skip records with null/missing merged_at —
            # they have no timestamp so cannot be placed in any window.
            if not data.get("merged_at"):
                continue
            merged_at = datetime.fromisoformat(
                data["merged_at"].replace("Z", "+00:00")
            )
            if merged_at.timestamp() >= cutoff_ts:
                releases.append(data)
        except Exception:
            pass
    return releases


def _compute_cfr(releases: list[dict], repo: "str | None" = None) -> str:
    """Compute change_failure_rate_pct using GitHub Bug discussions.

    Heuristic: a release "failed" if a Discussion labeled [Bug] was created
    within 24 hours of the release's merged_at timestamp.

    Args:
        repo: ``owner/name`` slug to query, for per-project scoping. Defaults
            to the serving checkout's own repo (``backend._repo.REPO``) when
            omitted — resolved lazily here, not at module import, so
            importing this module binds no repo slug (D#2518). A caller
            scoping to a *named* project must resolve that project's own
            slug itself (see backend/project_repo_slug.py) and pass it
            explicitly rather than relying on this default, which always
            means "the serving checkout".

    Returns a formatted percentage string, or "n/a" when the signal is
    unavailable (no releases, gh CLI error, or insufficient data).
    """
    if not releases:
        # No releases in window — CFR undefined.
        return "n/a"

    if repo is None:
        from backend._repo import REPO as _default_repo  # noqa: PLC0415
        repo = _default_repo

    # Fetch recent [Bug] Discussions from GitHub.
    # Uses analyst_bug_filer's category convention: label = "Bug" in the title.
    try:
        result = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", (
                    'query=query{'
                    f'repository(owner:"{repo.split("/")[0]}",name:"{repo.split("/")[1]}")' + "{"
                    'discussions(first:100,categoryId:null,filterBy:{labels:[]}){nodes{title createdAt}}'
                    "}"
                    "}"
                ),
            ],
            capture_output=True, text=True, check=False, timeout=20,
        )
        if result.returncode != 0:
            # gh CLI failed or not authenticated — graceful fallback.
            return "n/a"
        payload = json.loads(result.stdout)
        discussions = (
            payload.get("data", {})
            .get("repository", {})
            .get("discussions", {})
            .get("nodes", [])
        )
    except Exception:
        # Any error (timeout, JSON parse, etc.) → graceful fallback.
        return "n/a"

    # Filter Bug discussions by title prefix "[Bug]"
    bug_discussions: list[datetime] = []
    for d in discussions:
        if d.get("title", "").startswith("[Bug]"):
            try:
                bug_discussions.append(
                    datetime.fromisoformat(
                        d["createdAt"].replace("Z", "+00:00")
                    )
                )
            except Exception:
                pass

    if not bug_discussions:
        # No bug discussions found — CFR = 0%.
        return "0.0"

    # For each release, check whether a Bug discussion was filed within 24h.
    # Skip records with null/missing merged_at — they have no timestamp to compare.
    _24H = 24 * 3600
    failed = 0
    for rel in releases:
        if not rel.get("merged_at"):
            continue
        try:
            merged_ts = datetime.fromisoformat(
                rel["merged_at"].replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            continue
        for bug_dt in bug_discussions:
            diff = bug_dt.timestamp() - merged_ts
            if 0 <= diff <= _24H:
                failed += 1
                break  # count each release at most once

    pct = round(failed / len(releases) * 100, 1)
    return str(pct)


# ---------------------------------------------------------------------------
# Main snapshot function
# ---------------------------------------------------------------------------

def compute_snapshot(
    today: str | None = None,
    project_root: "Path | None" = None,
    repo: "str | None" = None,
) -> dict:
    """Compute the analytics snapshot dict for the trailing 7-day window.

    Parameters
    ----------
    today:
        Date string YYYY-MM-DD. Defaults to UTC today. Useful in tests.
    project_root:
        A project's own checkout root (the directory containing its
        ``.autonomous-team/``), for per-project scoping (D#2518). When
        given, releases and the registry are read from
        ``<project_root>/.autonomous-team/{releases,registry.json}``
        instead of the serving checkout's own. Omit for the serving
        checkout's own snapshot (existing behaviour).
    repo:
        ``owner/name`` GitHub repo slug to query for lead time and change
        failure rate, for per-project scoping. Omit for the serving
        checkout's own repo (existing behaviour). Callers scoping to a
        *named* project must resolve that project's slug themselves (see
        backend/project_repo_slug.py) — passing ``None`` here always means
        "the serving checkout", never "decline".

    Returns a dict with keys:
        date, deploy_frequency_per_day, lead_time_minutes_p50,
        change_failure_rate_pct, velocity_last_24h, velocity_all_time_per_day,
        cycle_time_median_hours
    """
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    now = datetime.now(timezone.utc)
    cutoff_ts = now.timestamp() - _7D_SECONDS

    releases_dir = (
        Path(project_root) / ".autonomous-team" / "releases"
        if project_root is not None
        else None
    )

    # --- DORA: deploy frequency + lead time (reuse release_manager) ---
    dora = compute_dora_snapshot(releases_dir=releases_dir, repo=repo)
    deploy_freq = dora["deploy_frequency_per_day"]
    lead_time = dora["lead_time_minutes_p50"]

    # --- DORA: change failure rate (computed here from releases + bug filings) ---
    recent_releases = _load_recent_releases(cutoff_ts, releases_dir=releases_dir)
    cfr = _compute_cfr(recent_releases, repo=repo)

    # --- KPI: velocity + cycle time (reuse kpi_engine) ---
    discussions = load_registry(repo_root=project_root)
    velocity = compute_velocity(discussions)
    cycle = compute_pr_cycle_time(discussions)

    return {
        "date": today,
        "deploy_frequency_per_day": deploy_freq,
        "lead_time_minutes_p50": lead_time,
        "change_failure_rate_pct": cfr,
        "velocity_last_24h": velocity["last_24h"],
        "velocity_all_time_per_day": velocity["all_time_per_day"],
        "cycle_time_median_hours": cycle["median_hours"],
    }


def emit_snapshot(snap: dict) -> Path:
    """Write the markdown snapshot to wiki/analytics/<date>.md.

    Returns the path that was written.
    """
    _ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    out = _ANALYTICS_DIR / f"{snap['date']}.md"

    def _fmt(v: object) -> str:
        if v is None:
            return "n/a"
        if isinstance(v, float):
            return f"{v:.2f}" if v >= 0 else "n/a"
        return str(v)

    deploy = _fmt(snap["deploy_frequency_per_day"])
    lead = (
        _fmt(snap["lead_time_minutes_p50"])
        if snap["lead_time_minutes_p50"] != -1.0
        else "n/a"
    )
    cfr = snap["change_failure_rate_pct"]
    vel_24h = snap["velocity_last_24h"]
    vel_atd = _fmt(snap["velocity_all_time_per_day"])
    cycle = _fmt(snap["cycle_time_median_hours"])

    lines = [
        f"# Analytics Snapshot — {snap['date']}",
        "",
        "## DORA Metrics (trailing 7 days)",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Deploy frequency (deploys/day) | {deploy} |",
        f"| Lead time p50 (minutes) | {lead} |",
        f"| Change failure rate (%) | {cfr} |",
        "",
        "## KPI Metrics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Velocity (tasks last 24h) | {vel_24h} |",
        f"| Velocity (all-time tasks/day) | {vel_atd} |",
        f"| PR cycle time p50 (hours) | {cycle} |",
        "",
        f"> Generated by `analytics_engineer.py snapshot` at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
    ]

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def cmd_snapshot(args: argparse.Namespace) -> None:  # noqa: ARG001
    snap = compute_snapshot()
    out = emit_snapshot(snap)
    print(f"Snapshot written: {out}")
    print(f"  deploy_frequency_per_day : {snap['deploy_frequency_per_day']}")
    print(f"  lead_time_minutes_p50    : {snap['lead_time_minutes_p50']}")
    print(f"  change_failure_rate_pct  : {snap['change_failure_rate_pct']}")
    print(f"  velocity_last_24h        : {snap['velocity_last_24h']}")
    print(f"  velocity_all_time_per_day: {snap['velocity_all_time_per_day']}")
    print(f"  cycle_time_median_hours  : {snap['cycle_time_median_hours']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="analytics-engineer: DORA + KPI snapshot emitter (read-only)"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("snapshot", help="Compute and emit a snapshot to wiki/analytics/")

    args = parser.parse_args()
    if args.command == "snapshot":
        cmd_snapshot(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
