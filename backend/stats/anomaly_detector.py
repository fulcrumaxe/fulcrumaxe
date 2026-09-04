"""backend/stats/anomaly_detector.py — lightweight stat regression detector.

Detects when a metric's value swings by more than a configurable ratio
between consecutive readings (iteration-over-iteration).

Design:
  - Pure core: detect() takes two plain dicts and a config dict → list[Anomaly].
    No I/O, fully unit-testable.
  - I/O layer: run_detection() reads metric_event from DuckDB, writes detected
    anomalies to stat_anomalies, and posts a team-log comment when anomalies
    are found.

Table DDL (managed here):

    stat_anomalies(
        ts              TIMESTAMPTZ NOT NULL,
        metric          TEXT        NOT NULL,
        project_tag     TEXT        NOT NULL DEFAULT '',
        prev_value      DOUBLE,
        current_value   DOUBLE,
        ratio           DOUBLE,
        threshold       DOUBLE,
        PRIMARY KEY (ts, metric, project_tag)
    )

False-positive guards:
  - Skip when either value is 0 (division by zero / zero-start transition).
  - Skip when there is no prior row for a metric (first iteration).
  - Skip when prev or current value is None / NaN.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.stats.anomaly_config import threshold_for

log = logging.getLogger(__name__)

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class MetricRow:
    """One (metric, project_tag, value) reading at a point in time."""

    metric: str
    project_tag: str  # value of the 'project' tag, or '' if absent
    value: float
    ts: str  # ISO-8601 string


@dataclass
class Anomaly:
    """A detected value-swing anomaly."""

    metric: str
    project_tag: str
    prev_value: float
    current_value: float
    ratio: float
    threshold: float
    ts: str  # when the anomaly was detected (current reading's ts)

    def format_log_line(self) -> str:
        """One-liner suitable for team-log comments."""
        return (
            f"anomaly: {self.metric} "
            f"{self.prev_value:.4g}→{self.current_value:.4g} "
            f"({self.ratio:.1f}x, threshold {self.threshold:.1f}x)"
        )


# ── Pure detection logic ───────────────────────────────────────────────────────


def detect(
    prev_row: dict[str, Any],
    current_row: dict[str, Any],
    config: dict[str, float] | None = None,
) -> list[Anomaly]:
    """Compare two metric readings and return a list of Anomaly objects.

    Parameters
    ----------
    prev_row:
        Dict with keys ``metric``, ``value``, ``ts``, and optionally
        ``project_tag``.  Represents the *previous* reading.
    current_row:
        Same shape, represents the *current* reading.
    config:
        Optional override mapping ``metric_name → threshold``.  When absent,
        thresholds are read from :mod:`anomaly_config`.

    Returns
    -------
    list[Anomaly]
        Empty list when no anomaly is detected.  At most one Anomaly per call
        (one pair of rows → one verdict).

    False-positive guards:
      - Returns [] when either value is 0 (avoids ÷0 and zero-start noise).
      - Returns [] when either value is NaN / Inf.
      - Returns [] when metric names differ (programming error guard).
    """
    metric = current_row.get("metric", "")
    prev_val = prev_row.get("value")
    curr_val = current_row.get("value")
    project_tag = current_row.get("project_tag", "")
    ts = current_row.get("ts", "")

    # Metric name mismatch guard
    if prev_row.get("metric", "") != metric:
        return []

    # None / NaN / Inf guard
    if prev_val is None or curr_val is None:
        return []
    try:
        prev_val = float(prev_val)
        curr_val = float(curr_val)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(prev_val) or not math.isfinite(curr_val):
        return []

    # Zero guard — skip to avoid division-by-zero and first-write false positives
    if prev_val == 0.0 or curr_val == 0.0:
        return []

    # Compute ratio (always ≥ 1.0 — we care about magnitude not direction).
    # Use abs() on both sides so a sign flip across zero (e.g. -1 → +11)
    # is treated as an 11x magnitude swing rather than producing a negative
    # ratio that the inversion branch mis-handles.
    ratio = abs(curr_val) / abs(prev_val)
    if ratio < 1.0:
        ratio = 1.0 / ratio

    # Threshold lookup
    if config is not None and metric in config:
        threshold = float(config[metric])
    else:
        threshold = threshold_for(metric)

    if ratio > threshold:
        return [
            Anomaly(
                metric=metric,
                project_tag=project_tag,
                prev_value=prev_val,
                current_value=curr_val,
                ratio=ratio,
                threshold=threshold,
                ts=ts,
            )
        ]
    return []


# ── DuckDB path helper ─────────────────────────────────────────────────────────


def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


# ── DuckDB table management ────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stat_anomalies (
    ts              TIMESTAMPTZ NOT NULL,
    metric          TEXT        NOT NULL,
    project_tag     TEXT        NOT NULL DEFAULT '',
    prev_value      DOUBLE,
    current_value   DOUBLE,
    ratio           DOUBLE,
    threshold       DOUBLE,
    PRIMARY KEY (ts, metric, project_tag)
);
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO stat_anomalies
    (ts, metric, project_tag, prev_value, current_value, ratio, threshold)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def ensure_table(con: Any) -> None:
    """Create stat_anomalies table if it doesn't exist."""
    con.execute(_CREATE_TABLE_SQL)


# ── I/O layer ─────────────────────────────────────────────────────────────────


def _fetch_last_two_rows(con: Any) -> list[dict[str, Any]]:
    """Return at most 2 recent rows per (metric, project_tag) from metric_event.

    Reads the two most recent timestamps available across ALL metrics and
    returns one row per (metric, project_tag, ts).  This gives us
    (current, prev) pairs for each metric.

    Returns rows as list of dicts with keys: metric, project_tag, value, ts.
    """
    # Get the two most recent distinct timestamps in the DB
    ts_rows = con.execute(
        "SELECT DISTINCT ts FROM metric_event ORDER BY ts DESC LIMIT 2"
    ).fetchall()

    if len(ts_rows) < 2:
        # Not enough history — nothing to compare
        return []

    current_ts = ts_rows[0][0]
    prev_ts = ts_rows[1][0]

    rows: list[dict[str, Any]] = []
    for ts in (current_ts, prev_ts):
        result = con.execute(
            """
            SELECT metric,
                   COALESCE(JSON_EXTRACT_STRING(tags, '$.project'), '') AS project_tag,
                   value,
                   ts::TEXT AS ts_str
            FROM metric_event
            WHERE ts = ?
            ORDER BY metric, project_tag
            """,
            [ts],
        ).fetchall()
        for r in result:
            rows.append({
                "metric": r[0],
                "project_tag": r[1] or "",
                "value": r[2],
                "ts": r[3],
            })
    return rows


def _write_anomalies(con: Any, anomalies: list[Anomaly]) -> int:
    """Insert anomalies into stat_anomalies table. Returns count inserted."""
    inserted = 0
    for a in anomalies:
        try:
            con.execute(_INSERT_SQL, [
                a.ts, a.metric, a.project_tag,
                a.prev_value, a.current_value, a.ratio, a.threshold,
            ])
            inserted += 1
        except Exception as exc:
            log.warning("stat_anomalies insert failed for %s: %s", a.metric, exc)
    return inserted


def _post_team_log(anomalies: list[Anomaly], repo: str) -> None:
    """Post a team-log comment listing detected anomalies."""
    import subprocess  # noqa: PLC0415

    lines = [a.format_log_line() for a in anomalies]
    body = "[anomaly-detector] " + " | ".join(lines)
    try:
        log_issue = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", repo,
                "--label", "team-log",
                "--state", "open",
                "--json", "number",
                "--jq", ".[0].number",
            ],
            capture_output=True, text=True, timeout=10,
        )
        issue_num = log_issue.stdout.strip()
        if not issue_num:
            log.warning("anomaly-detector: no team-log issue found — skipping comment")
            return
        subprocess.run(
            [
                "gh", "issue", "comment", issue_num,
                "--repo", repo,
                "--body", body,
            ],
            capture_output=True, text=True, timeout=15,
        )
        log.info("anomaly-detector: posted team-log comment with %d anomaly(ies)", len(anomalies))
    except Exception as exc:
        log.warning("anomaly-detector: team-log comment failed: %s", exc)


def run_detection(
    repo: str | None = None,
    post_team_log: bool = True,
    db_path: Path | None = None,
) -> list[Anomaly]:
    """Run anomaly detection against the live DuckDB stats store.

    1. Opens stats.duckdb.
    2. Ensures stat_anomalies table exists.
    3. Fetches the two most recent readings per (metric, project_tag).
    4. Calls detect() on each pair.
    5. Writes detected anomalies to stat_anomalies.
    6. Posts a team-log comment if any anomalies were found.

    Parameters
    ----------
    repo:
        GitHub ``owner/name`` for team-log comments.
    post_team_log:
        Set False in tests or when called outside the loop.
    db_path:
        Override stats.duckdb path (used in tests).

    Returns
    -------
    list[Anomaly]
        All anomalies detected in this run.  May be empty.
    """
    if repo is None:
        from backend._repo import REPO as _DEFAULT_REPO
        repo = _DEFAULT_REPO

    try:
        import duckdb  # type: ignore[import]
    except ImportError:
        log.warning("anomaly-detector: duckdb not installed — skipping")
        return []

    path = db_path or _db_path()
    if not path.exists():
        log.debug("anomaly-detector: stats.duckdb not found at %s — skipping", path)
        return []

    all_anomalies: list[Anomaly] = []
    try:
        con = duckdb.connect(str(path))
        try:
            ensure_table(con)
            rows = _fetch_last_two_rows(con)
        finally:
            con.close()
    except Exception as exc:
        log.warning("anomaly-detector: DB read failed: %s", exc)
        return []

    if not rows:
        log.debug("anomaly-detector: not enough history — skipping")
        return []

    # Group into current / prev by the two timestamps seen
    # rows are in order: current_ts first, prev_ts second (from the query)
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["metric"], row["project_tag"])
        by_key.setdefault(key, []).append(row)

    for key, pair in by_key.items():
        if len(pair) != 2:
            continue  # only one reading — no prev to compare
        current_row, prev_row = pair[0], pair[1]
        anomalies = detect(prev_row, current_row)
        all_anomalies.extend(anomalies)

    if all_anomalies:
        try:
            con2 = duckdb.connect(str(path))
            try:
                ensure_table(con2)
                _write_anomalies(con2, all_anomalies)
            finally:
                con2.close()
        except Exception as exc:
            log.warning("anomaly-detector: failed to write anomalies: %s", exc)

        if post_team_log:
            _post_team_log(all_anomalies, repo)

    log.info(
        "anomaly-detector: checked %d metric(s), found %d anomaly(ies)",
        len(by_key),
        len(all_anomalies),
    )
    return all_anomalies


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="Stat regression detector")
    ap.add_argument(
        "--no-team-log",
        action="store_true",
        help="Skip posting team-log comment (dry-run mode)",
    )
    ap.add_argument(
        "--repo",
        default=None,
        help="GitHub repo slug for team-log comments (default: resolved from _repo.py)",
    )
    args = ap.parse_args()

    detected = run_detection(repo=args.repo, post_team_log=not args.no_team_log)
    if detected:
        print(f"Detected {len(detected)} anomaly(ies):")
        for a in detected:
            print(" ", a.format_log_line())
    else:
        print("No anomalies detected.")
