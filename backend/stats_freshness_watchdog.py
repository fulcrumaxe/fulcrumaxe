"""stats_freshness_watchdog.py — periodic watchdog for metric_event freshness.

Scans every distinct metric_name in the DuckDB stats database and warns when
any metric has not been updated within the expected window.

CLI:
    check              — print human-readable summary
    check --json       — machine-readable JSON output
    check --file-bugs  — also auto-file [Bug] Discussions for age >= 24h
    check --dry-run    — skip team-log writes and Discussion filing

Thresholds:
    WARN_AGE_SECONDS = 7200   (2 hours)
    BUG_AGE_SECONDS  = 86400  (24 hours)

Metrics listed in INTERMITTENT_METRICS get bug-filing threshold raised to 72h.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a script from repo root: `python3 backend/stats_freshness_watchdog.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend._repo import REPO, REPO_OWNER, REPO_NAME

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WARN_AGE_SECONDS: int = 7200    # 2 hours — emit team-log warning
BUG_AGE_SECONDS: int = 86400    # 24 hours — auto-file [Bug] Discussion

# Known-intermittent metrics: bug-filing threshold raised to 72h.
# Add metric names here to suppress noisy bug filings during weekend lulls.
INTERMITTENT_METRICS: set[str] = set()
INTERMITTENT_BUG_AGE_SECONDS: int = 259200  # 72 hours

# Hard timeout for the entire DuckDB query (seconds)
QUERY_TIMEOUT_SECONDS: float = 5.0



# ---------------------------------------------------------------------------
# Per-process dedup: set of (iteration_ts_minute, metric_name) already warned.
# "iteration_ts_minute" = current UTC time truncated to the minute boundary.
# ---------------------------------------------------------------------------
_warned_this_process: set[tuple[str, str]] = set()

_REPO_ID_CACHE: dict[str, str] = {}
_CATEGORY_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# DuckDB helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


def _query_freshness() -> list[dict[str, Any]]:
    """Run the DuckDB query and return raw rows.

    Returns a list of dicts: {metric_name, last_ts, age_seconds, monitored}.
    Raises if the DB is missing or query fails — callers handle exceptions.
    """
    import duckdb

    from backend.stats.freshness import is_monitored, to_utc

    db_path = _db_path()
    if not db_path.exists():
        return []

    now = datetime.now(tz=timezone.utc)
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT metric AS metric_name, MAX(ts) AS last_ts "
            "FROM metric_event GROUP BY metric"
        ).fetchall()
    finally:
        conn.close()

    result = []
    for metric_name, last_ts in rows:
        if last_ts is None:
            continue
        # metric_event.ts is a naive TIMESTAMP already holding UTC wall-clock.
        # Labelling it UTC is correct; astimezone() on a naive value would
        # reinterpret it as local and push it into the future — see
        # backend/stats/freshness.py.
        last_ts_aware = to_utc(last_ts)
        age = (now - last_ts_aware).total_seconds()
        result.append({
            "metric_name": metric_name,
            "last_ts": last_ts_aware.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_seconds": int(age),
            "monitored": is_monitored(metric_name),
        })
    return result


# ---------------------------------------------------------------------------
# Human-readable helpers
# ---------------------------------------------------------------------------

def _human_age(seconds: int) -> str:
    """Format an age in seconds as a short human string, e.g. '3h 12m'."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check() -> list[dict[str, Any]]:
    """Run the freshness scan with a hard 5s timeout.

    Returns every metric row (fresh or stale). Never raises — exceptions are
    caught, logged to stderr, and an empty list is returned.

    Each row: {metric_name, last_ts, age_seconds, monitored}
    """
    result: list[dict[str, Any]] = []
    exc_holder: list[Exception] = []

    def _run() -> None:
        try:
            result.extend(_query_freshness())
        except Exception as exc:  # noqa: BLE001
            exc_holder.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=QUERY_TIMEOUT_SECONDS)

    if t.is_alive():
        print(
            "[stats_freshness_watchdog] WARNING: DuckDB query timed out after "
            f"{QUERY_TIMEOUT_SECONDS}s — skipping freshness check",
            file=sys.stderr,
        )
        return []

    if exc_holder:
        print(
            f"[stats_freshness_watchdog] WARNING: query failed: {exc_holder[0]}",
            file=sys.stderr,
        )
        return []

    return result


def monitored_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows whose metric still has a live writer.

    Staleness is only assertable about these. A one-shot marker with no
    registered writer keeps its real age in the payload but never drives a
    warning, a Discussion, or the dashboard banner — see
    ``backend/stats/freshness.is_monitored``. Rows from an older payload with
    no ``monitored`` key are treated as monitored, which is the safe default.
    """
    return [r for r in rows if r.get("monitored", True)]


def warn_stale(rows: list[dict[str, Any]], dry_run: bool = False) -> None:
    """Post one team-log warning per (iteration_minute, metric_name) pair.

    Deduped within the process lifetime — second call with same metric is a no-op.
    """
    now = datetime.now(tz=timezone.utc)
    iteration_minute = now.strftime("%Y-%m-%dT%H:%M")

    stale = [r for r in monitored_rows(rows) if r["age_seconds"] >= WARN_AGE_SECONDS]
    for row in stale:
        key = (iteration_minute, row["metric_name"])
        if key in _warned_this_process:
            continue
        _warned_this_process.add(key)

        human = _human_age(row["age_seconds"])
        msg = (
            f"[{now.strftime('%H:%M')}] stats-freshness: WARN "
            f"metric={row['metric_name']} age={human} last_seen={row['last_ts']}"
        )
        if dry_run:
            print(f"[dry-run] Would post to team-log: {msg}")
        else:
            _post_team_log(msg)


def file_bugs(rows: list[dict[str, Any]], dry_run: bool = False) -> list[dict[str, Any]]:
    """Auto-file a [Bug] Discussion for every metric older than BUG_AGE_SECONDS.

    Idempotent — uses marker ``<!-- stats-freshness:{metric_name} -->``.
    Returns a list of {metric_name, url, filed} dicts.
    """
    results = []
    for row in monitored_rows(rows):
        threshold = (
            INTERMITTENT_BUG_AGE_SECONDS
            if row["metric_name"] in INTERMITTENT_METRICS
            else BUG_AGE_SECONDS
        )
        if row["age_seconds"] < threshold:
            continue
        marker = f"<!-- stats-freshness:{row['metric_name']} -->"
        url = _file_stale_bug(row, marker, dry_run=dry_run)
        results.append({
            "metric_name": row["metric_name"],
            "url": url,
            "filed": url is not None,
        })
    return results


# ---------------------------------------------------------------------------
# GitHub API helpers (vendored inline — same pattern as analyst_bug_filer.py)
# ---------------------------------------------------------------------------

def _get_repo_id() -> str:
    if "id" in _REPO_ID_CACHE:
        return _REPO_ID_CACHE["id"]
    query = f'query{{repository(owner:"{REPO_OWNER}",name:"{REPO_NAME}"){{id}}}}'
    try:
        r = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            rid = json.loads(r.stdout)["data"]["repository"]["id"]
            _REPO_ID_CACHE["id"] = rid
            return rid
    except Exception:  # noqa: BLE001
        pass
    return ""


def _get_category_id(name: str = "General") -> str:
    if name in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[name]
    query = (
        f'query{{repository(owner:"{REPO_OWNER}",name:"{REPO_NAME}")'
        f'{{discussionCategories(first:20){{nodes{{id name}}}}}}}}'
    )
    try:
        r = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            nodes = json.loads(r.stdout)["data"]["repository"]["discussionCategories"]["nodes"]
            for node in nodes:
                _CATEGORY_CACHE[node["name"]] = node["id"]
            if name in _CATEGORY_CACHE:
                return _CATEGORY_CACHE[name]
            if "General" in _CATEGORY_CACHE:
                return _CATEGORY_CACHE["General"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _marker_exists(marker: str) -> bool:
    query = (
        f'query{{repository(owner:"{REPO_OWNER}",name:"{REPO_NAME}")'
        f'{{discussions(first:100){{nodes{{body}}}}}}}}'
    )
    try:
        r = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            nodes = json.loads(r.stdout)["data"]["repository"]["discussions"]["nodes"]
            return any(marker in (n.get("body") or "") for n in nodes)
    except Exception:  # noqa: BLE001
        pass
    return False


def _create_discussion(title: str, body: str, repo_id: str, cat_id: str) -> str | None:
    mutation = (
        "mutation CreateDiscussion($repoId:ID!,$catId:ID!,$title:String!,$body:String!){"
        "createDiscussion(input:{repositoryId:$repoId,categoryId:$catId,"
        "title:$title,body:$body}){discussion{url}}}"
    )
    try:
        r = subprocess.run(
            [
                "gh", "api", "graphql",
                "-f", f"query={mutation}",
                "-f", f"repoId={repo_id}",
                "-f", f"catId={cat_id}",
                "-f", f"title={title}",
                "-f", f"body={body}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)["data"]["createDiscussion"]["discussion"]["url"]
        print(f"[stats_freshness_watchdog] GraphQL error: {r.stderr[:200]}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[stats_freshness_watchdog] Exception filing Discussion: {exc}", file=sys.stderr)
    return None


def _file_stale_bug(row: dict[str, Any], marker: str, dry_run: bool = False) -> str | None:
    metric = row["metric_name"]
    human = _human_age(row["age_seconds"])
    title = f"[Bug] Stats metric stale >{human}: {metric}"
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = "\n".join([
        f"<!-- STATUS:DISCUSSING SINCE:{now_iso} -->",
        "",
        "## Summary",
        "",
        f"The `{metric}` metric has not been updated for **{human}**.",
        f"Last seen: `{row['last_ts']}`",
        "",
        "This Discussion was filed automatically by `backend/stats_freshness_watchdog.py`",
        "because the metric exceeded the 24h stale threshold.",
        "",
        "## Steps to investigate",
        "",
        f"1. Check the stats writer for `{metric}` — is it still being called?",
        "2. Run `python3 backend/stats_freshness_watchdog.py check --json` to see current state.",
        "3. Check the loop log for recent errors in the subsystem that records this metric.",
        "",
        "*Filed automatically by `backend/stats_freshness_watchdog.py`.*",
        "",
        marker,
    ])

    if dry_run:
        print(f"[dry-run] Would file Discussion: {title!r}")
        return f"https://github.com/{REPO}/discussions/DRY-RUN"

    if _marker_exists(marker):
        print(f"[stats_freshness_watchdog] Skipped — Discussion already exists for {metric!r}")
        return None

    repo_id = _get_repo_id()
    cat_id = _get_category_id("General")
    if not repo_id or not cat_id:
        print("[stats_freshness_watchdog] Could not resolve repo_id/cat_id — skipping", file=sys.stderr)
        return None

    url = _create_discussion(title, body, repo_id, cat_id)
    if url:
        print(f"[stats_freshness_watchdog] Filed: {url}")
    return url


def _post_team_log(msg: str) -> None:
    """Post msg to the team-log via rotate-team-log.sh (best-effort)."""
    script = Path(__file__).parent.parent / "scripts" / "rotate-team-log.sh"
    try:
        subprocess.run(
            ["bash", str(script), "comment", msg],
            capture_output=True, timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check DuckDB metric_event freshness and alert on stale metrics."
    )
    sub = parser.add_subparsers(dest="command")
    chk = sub.add_parser("check", help="Run freshness check")
    chk.add_argument("--json", dest="as_json", action="store_true",
                     help="Emit machine-readable JSON instead of human text")
    chk.add_argument("--file-bugs", action="store_true",
                     help="Auto-file [Bug] Discussions for metrics stale > 24h")
    chk.add_argument("--dry-run", action="store_true",
                     help="Never post to team-log or file Discussions")
    args = parser.parse_args()

    if args.command != "check":
        parser.print_help()
        return 1

    rows = check()
    stale_warn = [r for r in monitored_rows(rows) if r["age_seconds"] >= WARN_AGE_SECONDS]

    if not args.dry_run:
        try:
            warn_stale(rows, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[stats_freshness_watchdog] warn_stale failed: {exc}", file=sys.stderr)

    bug_results: list[dict[str, Any]] = []
    if args.file_bugs and not args.dry_run:
        try:
            bug_results = file_bugs(rows, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[stats_freshness_watchdog] file_bugs failed: {exc}", file=sys.stderr)
    elif args.file_bugs and args.dry_run:
        try:
            bug_results = file_bugs(rows, dry_run=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[stats_freshness_watchdog] file_bugs (dry-run) failed: {exc}", file=sys.stderr)

    if args.as_json:
        output = {
            "rows": rows,
            "stale_count": len(stale_warn),
            "bug_results": bug_results,
        }
        print(json.dumps(output, indent=2))
    else:
        if not rows:
            print("No metrics found in DuckDB (database may not exist yet).")
            return 0
        print(f"Stats freshness check — {len(rows)} metrics, {len(stale_warn)} stale:")
        for r in rows:
            flag = ""
            if not r.get("monitored", True):
                flag = " [one-shot / not monitored]"
            elif r["age_seconds"] >= BUG_AGE_SECONDS:
                flag = " [BUG-THRESHOLD]"
            elif r["age_seconds"] >= WARN_AGE_SECONDS:
                flag = " [STALE]"
            print(f"  {r['metric_name']}: age={_human_age(r['age_seconds'])} last_seen={r['last_ts']}{flag}")
        if bug_results:
            print("\nBug filing results:")
            for b in bug_results:
                status = b["url"] or "skipped (already exists)"
                print(f"  {b['metric_name']}: {status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
