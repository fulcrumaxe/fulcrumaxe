"""stats_reader.py — query layer for the stats.duckdb metric store.

Subcommands:
    summary                          last value of every metric
    series <metric> [--since 7d]     time-series JSON for one metric
    distribution <metric>            P50/P90/P99 stats, optional --tag and --since

All output is JSON (one JSON document printed to stdout).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running as a script from the repo root: `python3 backend/stats_reader.py`.
# Every path now resolves through backend.state_paths, so the package has to be
# importable before the first _db_path() call, not just inside _open_conn().
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


def _open_conn():
    db = _db_path()
    if not db.exists():
        raise SystemExit(f"No stats database found at {db}. Run stats collection first.")
    try:
        from backend.stats_connection import get_read_connection  # noqa: PLC0415
        return get_read_connection()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def _parse_since(since: str | None) -> datetime | None:
    """Parse '7d', '24h', '30m', '2026-05-11' into a UTC datetime or None."""
    if since is None:
        return None
    since = since.strip()
    m = re.match(r"^(\d+)([dhm])$", since)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return datetime.now(timezone.utc) - delta
    # Try ISO date
    try:
        return datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"Cannot parse --since value: {since!r}. Use '7d', '24h', '30m', or ISO date.")


def cmd_summary(args: argparse.Namespace) -> None:
    """Print last value of every metric."""
    conn = _open_conn()
    try:
        rows = conn.execute("""
            SELECT metric, value, unit, tags, ts
            FROM (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY metric ORDER BY ts DESC) AS rn
                FROM metric_event
            ) t
            WHERE rn = 1
            ORDER BY metric
        """).fetchall()
    finally:
        conn.close()

    result = []
    for (metric, value, unit, tags, ts) in rows:
        entry: dict = {"metric": metric, "value": value, "unit": unit}
        if tags:
            try:
                entry["tags"] = json.loads(tags) if isinstance(tags, str) else tags
            except Exception:
                entry["tags"] = tags
        entry["ts"] = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        result.append(entry)

    print(json.dumps(result, indent=2))


def cmd_series(args: argparse.Namespace) -> None:
    """Print time-series rows for one metric."""
    since = _parse_since(args.since)
    conn = _open_conn()
    try:
        if since:
            rows = conn.execute(
                """
                SELECT ts, value, unit, tags, source
                FROM metric_event
                WHERE metric = ?
                  AND ts >= CAST(? AS TIMESTAMP)
                ORDER BY ts
                """,
                [args.metric, since.strftime("%Y-%m-%d %H:%M:%S")],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT ts, value, unit, tags, source
                FROM metric_event
                WHERE metric = ?
                ORDER BY ts
                """,
                [args.metric],
            ).fetchall()
    finally:
        conn.close()

    result = []
    for (ts, value, unit, tags, source) in rows:
        entry: dict = {
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "value": value,
            "unit": unit,
        }
        if tags:
            try:
                entry["tags"] = json.loads(tags) if isinstance(tags, str) else tags
            except Exception:
                entry["tags"] = tags
        if source:
            entry["source"] = source
        result.append(entry)

    print(json.dumps({"metric": args.metric, "since": args.since, "rows": result}, indent=2))


def cmd_distribution(args: argparse.Namespace) -> None:
    """Print P50/P90/P99 for one metric, with optional --tag and --since filters."""
    since = _parse_since(args.since)
    conn = _open_conn()

    # Build WHERE clause
    params: list = [args.metric]
    where_parts = ["metric = ?"]

    if since:
        where_parts.append("ts >= CAST(? AS TIMESTAMP)")
        params.append(since.strftime("%Y-%m-%d %H:%M:%S"))

    tag_filter = None
    if args.tag:
        # args.tag like "tag=Bug" or "pr=42"
        if "=" not in args.tag:
            conn.close()
            raise SystemExit("--tag must be KEY=VALUE, e.g. --tag 'tag=Bug'")
        k, v = args.tag.split("=", 1)
        tag_filter = (k.strip(), v.strip())
        if not re.fullmatch(r"[a-zA-Z0-9_]+", tag_filter[0]):
            conn.close()
            raise ValueError(
                f"Invalid tag key {tag_filter[0]!r}: only [a-zA-Z0-9_] are allowed"
            )
        # DuckDB JSON extraction: json_extract_string(tags, '$.key') = 'value'
        where_parts.append(f"json_extract_string(tags, '$.{tag_filter[0]}') = ?")
        params.append(tag_filter[1])

    where_sql = " AND ".join(where_parts)

    try:
        agg = conn.execute(
            f"""
            SELECT
                COUNT(*) AS n,
                MIN(value) AS min_val,
                MAX(value) AS max_val,
                AVG(value) AS mean_val,
                APPROX_QUANTILE(value, 0.50) AS p50,
                APPROX_QUANTILE(value, 0.90) AS p90,
                APPROX_QUANTILE(value, 0.99) AS p99
            FROM metric_event
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
    finally:
        conn.close()

    if agg is None or agg[0] == 0:
        print(json.dumps({"metric": args.metric, "n": 0, "note": "no data"}))
        return

    n, min_v, max_v, mean_v, p50, p90, p99 = agg
    result = {
        "metric": args.metric,
        "n": n,
        "min": round(float(min_v), 4) if min_v is not None else None,
        "max": round(float(max_v), 4) if max_v is not None else None,
        "mean": round(float(mean_v), 4) if mean_v is not None else None,
        "p50": round(float(p50), 4) if p50 is not None else None,
        "p90": round(float(p90), 4) if p90 is not None else None,
        "p99": round(float(p99), 4) if p99 is not None else None,
    }
    if args.since:
        result["since"] = args.since
    if tag_filter:
        result["tag_filter"] = f"{tag_filter[0]}={tag_filter[1]}"

    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Unit corrections — backward-compat bridge for rows written with stale units
# ---------------------------------------------------------------------------

_UNIT_CORRECTIONS: dict[tuple[str, str], str] = {
    # PR #1040 fixed the producer to write unit='count'.
    # PR #1062 added a migration script, but it may not have run on every
    # deployment yet.  Until it has, rows with the old unit='ratio' remain
    # the most-recent row for this metric.  Correct at the API boundary so
    # the display layer never sees the stale value.
    ("orphan_worktree_rate", "ratio"): "count",
}


def _correct_unit(metric: str, unit: str) -> str:
    """Return the canonical unit for *metric*, correcting known stale values."""
    return _UNIT_CORRECTIONS.get((metric, unit), unit)


def summary() -> list[dict]:
    """Return the latest value for every metric name.

    Returns a list of dicts with keys: name, value, unit, updated_at_iso.
    Returns [] if the database does not exist or has no rows.
    """
    try:
        conn = _open_conn()
    except SystemExit:
        return []
    try:
        rows = conn.execute("""
            SELECT metric, value, unit, ts
            FROM (
                SELECT metric, value, unit, ts,
                       ROW_NUMBER() OVER (PARTITION BY metric ORDER BY ts DESC) AS rn
                FROM metric_event
            ) t
            WHERE rn = 1
            ORDER BY metric
        """).fetchall()
    finally:
        conn.close()

    result = []
    for (metric, value, unit, ts) in rows:
        corrected_unit = _correct_unit(metric, unit)
        result.append({
            "name": metric,
            "value": value,
            "unit": corrected_unit,
            "updated_at_iso": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        })
    return result


def series(name: str, since_hours: int = 168) -> list[dict]:
    """Return time-ordered points for one metric over the past since_hours hours.

    Returns a list of dicts with keys: ts_iso, value.
    Returns [] if the database does not exist or has no matching rows.
    """
    try:
        conn = _open_conn()
    except SystemExit:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = conn.execute(
            """
            SELECT ts, value
            FROM metric_event
            WHERE metric = ?
              AND ts >= CAST(? AS TIMESTAMP)
            ORDER BY ts
            """,
            [name, cutoff_str],
        ).fetchall()
    finally:
        conn.close()

    return [
        {"ts_iso": ts.isoformat() if hasattr(ts, "isoformat") else str(ts), "value": value}
        for ts, value in rows
    ]


def scan_to_spawn(window_iterations: int = 10) -> dict:
    """Return the most recent N scan_to_spawn_ratio values + rolling mean.

    Returns a dict with keys:
        points  — list of {ts_iso, value} in chronological order (up to window_iterations)
        mean    — float rolling mean over the window, or None if no data
        n       — number of data points in the window

    Returns {"points": [], "mean": None, "n": 0} if the database does not
    exist or has no scan_to_spawn_ratio rows.
    """
    try:
        conn = _open_conn()
    except SystemExit:
        return {"points": [], "mean": None, "n": 0}
    try:
        rows = conn.execute(
            """
            SELECT ts, value
            FROM metric_event
            WHERE metric = 'scan_to_spawn_ratio'
            ORDER BY ts DESC
            LIMIT ?
            """,
            [window_iterations],
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"points": [], "mean": None, "n": 0}

    # Reverse to chronological order
    rows = list(reversed(rows))
    points = [
        {"ts_iso": ts.isoformat() if hasattr(ts, "isoformat") else str(ts), "value": value}
        for ts, value in rows
    ]
    values = [r[1] for r in rows]
    mean_val = round(sum(values) / len(values), 4) if values else None
    return {"points": points, "mean": mean_val, "n": len(points)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stats_reader.py",
        description="Query the stats.duckdb metric store.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # summary
    sub.add_parser("summary", help="Last value of every metric")

    # series <metric> [--since N]
    p_series = sub.add_parser("series", help="Time-series for one metric")
    p_series.add_argument("metric", help="Metric name")
    p_series.add_argument("--since", default=None, help="e.g. '7d', '24h', '30m', or ISO date")

    # distribution <metric> [--tag k=v] [--since N]
    p_dist = sub.add_parser("distribution", help="P50/P90/P99 stats for one metric")
    p_dist.add_argument("metric", help="Metric name")
    p_dist.add_argument("--tag", default=None, help="Filter by tag, e.g. 'tag=Bug'")
    p_dist.add_argument("--since", default=None, help="e.g. '7d', '24h', '30m', or ISO date")

    args = parser.parse_args(argv)

    if args.cmd == "summary":
        cmd_summary(args)
    elif args.cmd == "series":
        cmd_series(args)
    elif args.cmd == "distribution":
        cmd_distribution(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
