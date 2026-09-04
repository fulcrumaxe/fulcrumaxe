"""stats_writer.py — append-only metric event writer backed by DuckDB.

Public API:
    record(metric, value, unit, tags=None, source=None)
    record_loop_iter(ts, duration_s, team_lead_input_tokens, team_lead_output_tokens,
                     team_lead_cache_read, team_lead_cache_write)

Storage: state_paths.STATS_DB ($AUTONOMOUS_TEAM_STATE_DIR/stats.duckdb)
Schema:
    metric_event(ts TIMESTAMP, metric TEXT, tags JSON, value DOUBLE,
                 unit TEXT, source TEXT, PRIMARY KEY(ts, metric, tags))

    loop_metrics(ts TIMESTAMP PRIMARY KEY, duration_s DOUBLE,
                 team_lead_input_tokens BIGINT DEFAULT 0,
                 team_lead_output_tokens BIGINT DEFAULT 0,
                 team_lead_cache_read BIGINT DEFAULT 0,
                 team_lead_cache_write BIGINT DEFAULT 0,
                 team_lead_tokens_per_iter BIGINT DEFAULT 0)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a script from repo root: `python3 backend/stats_writer.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _db_path() -> Path:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


def _ensure_schema(conn: Any) -> None:
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_time ON metric_event(metric, ts)"
    )


def record(
    metric: str,
    value: float,
    unit: str,
    tags: dict[str, str] | None = None,
    source: str | None = None,
    ts: datetime | None = None,
) -> None:
    """Write one metric event row.

    Duplicate (ts, metric, tags) rows are silently skipped (INSERT OR IGNORE).

    Args:
        metric: metric name, e.g. "time_to_merge_seconds"
        value:  numeric value
        unit:   unit string, e.g. "seconds", "usd", "ratio", "count"
        tags:   optional dict of key/value labels, e.g. {"tag": "Bug", "pr": "42"}
        source: optional source identifier, e.g. "post-merge-hook"
        ts:     optional timestamp (defaults to now UTC)
    """
    try:
        import duckdb  # noqa: PLC0415  # lazy import — only fail at call time if missing
    except ImportError as exc:
        raise RuntimeError(
            "duckdb not installed — run: pip install duckdb"
        ) from exc

    if ts is None:
        ts = datetime.now(timezone.utc)

    tags_json = json.dumps(tags or {}, sort_keys=True)
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # millisecond precision

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = duckdb.connect(str(db))
    except duckdb.IOException as exc:
        raise IOError(f"stats_writer: lock conflict on {db}: {exc}") from exc
    try:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO metric_event (ts, metric, tags, value, unit, source)
            VALUES (CAST(? AS TIMESTAMP), ?, CAST(? AS JSON), ?, ?, ?)
            """,
            [ts_str, metric, tags_json, float(value), unit, source],
        )
    finally:
        conn.close()


def record_many(rows: list[dict[str, Any]]) -> None:
    """Bulk-write metric events. Each dict must have keys: metric, value, unit.
    Optional keys: tags, source, ts.
    """
    for row in rows:
        record(
            metric=row["metric"],
            value=row["value"],
            unit=row["unit"],
            tags=row.get("tags"),
            source=row.get("source"),
            ts=row.get("ts"),
        )


# ---------------------------------------------------------------------------
# loop_metrics table — per-iteration Team Lead token tracking
# ---------------------------------------------------------------------------

def _ensure_loop_metrics_schema(conn: Any) -> None:
    """Create loop_metrics table and apply any pending column migrations."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_metrics (
            ts                          TIMESTAMP PRIMARY KEY,
            duration_s                  DOUBLE    NOT NULL DEFAULT 0,
            team_lead_input_tokens      BIGINT    NOT NULL DEFAULT 0,
            team_lead_output_tokens     BIGINT    NOT NULL DEFAULT 0,
            team_lead_cache_read        BIGINT    NOT NULL DEFAULT 0,
            team_lead_cache_write       BIGINT    NOT NULL DEFAULT 0,
            team_lead_tokens_per_iter   BIGINT    NOT NULL DEFAULT 0
        )
    """)
    # Migration: add team_lead_tokens_per_iter if missing (for DBs created before PR-b).
    # DuckDB does not support ADD COLUMN IF NOT EXISTS nor ADD COLUMN with NOT NULL
    # constraints on pre-existing tables in all versions, so we check the column list
    # first and only ALTER when the column is absent.
    existing_cols = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='loop_metrics'"
        ).fetchall()
    }
    if "team_lead_tokens_per_iter" not in existing_cols:
        try:
            conn.execute(
                "ALTER TABLE loop_metrics ADD COLUMN team_lead_tokens_per_iter BIGINT DEFAULT 0"
            )
        except Exception:  # noqa: BLE001
            pass  # column may have been added by a concurrent writer — safe to ignore


def record_loop_iter(
    ts: datetime | None = None,
    duration_s: float = 0.0,
    team_lead_input_tokens: int = 0,
    team_lead_output_tokens: int = 0,
    team_lead_cache_read: int = 0,
    team_lead_cache_write: int = 0,
) -> None:
    """Write one loop_metrics row for the current /loop iteration.

    team_lead_tokens_per_iter is computed as input + output (excluding cache),
    matching the spec's definition.

    Duplicate timestamps are silently ignored (INSERT OR IGNORE).

    Args:
        ts:                         Iteration timestamp (defaults to now UTC).
        duration_s:                 Iteration wall-clock duration in seconds.
        team_lead_input_tokens:     TL regular input tokens this iteration.
        team_lead_output_tokens:    TL output tokens this iteration.
        team_lead_cache_read:       TL cache-read tokens this iteration.
        team_lead_cache_write:      TL cache-creation tokens this iteration.
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("duckdb not installed — run: pip install duckdb") from exc

    if ts is None:
        ts = datetime.now(timezone.utc)

    tokens_per_iter = max(0, team_lead_input_tokens) + max(0, team_lead_output_tokens)
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = duckdb.connect(str(db))
    except duckdb.IOException as exc:
        raise IOError(f"stats_writer: lock conflict on {db}: {exc}") from exc
    try:
        _ensure_loop_metrics_schema(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO loop_metrics (
                ts, duration_s,
                team_lead_input_tokens, team_lead_output_tokens,
                team_lead_cache_read, team_lead_cache_write,
                team_lead_tokens_per_iter
            ) VALUES (CAST(? AS TIMESTAMP), ?, ?, ?, ?, ?, ?)
            """,
            [
                ts_str,
                float(duration_s),
                int(team_lead_input_tokens),
                int(team_lead_output_tokens),
                int(team_lead_cache_read),
                int(team_lead_cache_write),
                int(tokens_per_iter),
            ],
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Team Lead token percentile helpers (Discussion #569 PR-c)
# ---------------------------------------------------------------------------


def team_lead_tokens_percentiles(since_hours: int = 24) -> dict:
    """Return avg / p50 / p95 of team_lead_tokens_per_iter over the last N hours.

    Returns::

        {
            "avg":         float | None,
            "p50":         float | None,
            "p95":         float | None,
            "sample_size": int,
        }

    When sample_size < 5, avg/p50/p95 are all None (UI renders "N/A" per D#586 rule).
    When the loop_metrics table does not exist or duckdb is absent, returns zeros.
    """
    db = _db_path()
    if not db.exists():
        return {"avg": None, "p50": None, "p95": None, "sample_size": 0}

    # Compute the cutoff as a Python datetime to avoid DuckDB's INTERVAL ? HOURS
    # parameter-binding limitation (placeholders not supported inside INTERVAL literals).
    from datetime import timedelta  # noqa: PLC0415
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    try:
        from backend.stats_connection import get_read_connection  # noqa: PLC0415
        conn = get_read_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    COUNT(*)                                           AS sample_size,
                    AVG(team_lead_tokens_per_iter)                    AS avg_tl,
                    PERCENTILE_CONT(0.50) WITHIN GROUP
                        (ORDER BY team_lead_tokens_per_iter)          AS p50_tl,
                    PERCENTILE_CONT(0.95) WITHIN GROUP
                        (ORDER BY team_lead_tokens_per_iter)          AS p95_tl
                FROM loop_metrics
                WHERE ts >= CAST(? AS TIMESTAMP)
                """,
                [cutoff_str],
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {"avg": None, "p50": None, "p95": None, "sample_size": 0}

    if not rows or rows[0][0] is None or rows[0][0] == 0:
        return {"avg": None, "p50": None, "p95": None, "sample_size": 0}

    sample_size, avg_tl, p50_tl, p95_tl = rows[0]
    sample_size = int(sample_size)

    if sample_size < 5:
        return {"avg": None, "p50": None, "p95": None, "sample_size": sample_size}

    return {
        "avg": float(avg_tl) if avg_tl is not None else None,
        "p50": float(p50_tl) if p50_tl is not None else None,
        "p95": float(p95_tl) if p95_tl is not None else None,
        "sample_size": sample_size,
    }


# ---------------------------------------------------------------------------
# Live-analyst observability helpers
# ---------------------------------------------------------------------------

def record_live_analyst_intervention(
    agent_id: str,
    classifier: str,
    intervention_number: int,
    ts: datetime | None = None,
) -> None:
    """Record one live-analyst intervention event.

    Emits three stats metrics (all needed for the dashboard tile to compute
    averages and per-classifier breakdowns):

      intervention_count            — raw count for time-series charts
      interventions_per_classifier  — per-classifier count (tagged by classifier)
      interventions_per_agent_avg   — rolling input to average calculation
    """
    now = ts or datetime.now(timezone.utc)
    shared_tags = {"agent_id": agent_id, "classifier": classifier}

    record_many([
        {
            "metric": "intervention_count",
            "value": 1.0,
            "unit": "count",
            "tags": shared_tags,
            "source": "live-analyst",
            "ts": now,
        },
        {
            "metric": "interventions_per_classifier",
            "value": 1.0,
            "unit": "count",
            "tags": {"classifier": classifier},
            "source": "live-analyst",
            "ts": now,
        },
        {
            "metric": "interventions_per_agent_avg",
            # Store the per-agent count at time of intervention so the
            # dashboard can compute rolling average across agents.
            "value": float(intervention_number),
            "unit": "count",
            "tags": {"agent_id": agent_id},
            "source": "live-analyst",
            "ts": now,
        },
    ])


def record_intervention_outcome(
    agent_id: str,
    classifier: str,
    self_corrected: bool,
    ts: datetime | None = None,
) -> None:
    """Record whether the 5 turns after an intervention changed behaviour.

    self_corrected=True means the agent pivoted (different tool, different
    command, or an explicit acknowledgment) — used to compute
    intervention_to_self_correction_rate.
    """
    now = ts or datetime.now(timezone.utc)
    record(
        metric="intervention_to_self_correction_rate",
        value=1.0 if self_corrected else 0.0,
        unit="ratio",
        tags={"agent_id": agent_id, "classifier": classifier},
        source="live-analyst",
        ts=now,
    )


# ---------------------------------------------------------------------------
# Role success-rate helpers (Discussion #540)
# ---------------------------------------------------------------------------


def emit_verdict(role: str, verdict: str, ts: datetime | None = None) -> None:
    """Record one agent verdict event for success-rate aggregation.

    Stores a raw role_verdict row tagged with role + verdict.
    Aggregation happens at read time via role_success_rate_24h().
    """
    record(
        metric="role_verdict",
        value=1.0,
        unit="event",
        tags={"role": role, "verdict": verdict},
        source="post-agent-hook",
        ts=ts,
    )


def role_retry_rate_24h() -> list[dict]:
    """Return per-role retry rates over the last 24 hours.

    Returns a list of dicts:
        [{role, retry_rate, sample_size}, ...]

    Rules:
    - retry_rate = count(needs-fix|fail) / count(all) for that role
    - Roles with sample_size < 5 have retry_rate = None (shown as N/A in UI)
    - Sorted: highest retry_rate first, None-rate rows last
    """
    db = _db_path()
    if not db.exists():
        return []

    try:
        from backend.stats_connection import get_read_connection  # noqa: PLC0415
        conn = get_read_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    json_extract_string(tags, '$.role')                   AS role,
                    COUNT(*)                                              AS sample_size,
                    SUM(CASE WHEN json_extract_string(tags, '$.verdict')
                                  IN ('needs-fix', 'fail')
                             THEN 1 ELSE 0 END)                           AS retry_count
                FROM metric_event
                WHERE metric = 'role_verdict'
                  AND ts >= NOW() - INTERVAL 24 HOURS
                  AND json_extract_string(tags, '$.role') IS NOT NULL
                GROUP BY json_extract_string(tags, '$.role')
                """,
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

    result = []
    for role, sample_size, retry_count in rows:
        rate = (retry_count / sample_size) if sample_size >= 5 else None
        result.append(
            {"role": role, "retry_rate": rate, "sample_size": int(sample_size)}
        )

    # Sort: highest retry_rate first, None rows last
    result.sort(
        key=lambda r: (
            r["retry_rate"] is None,
            -(r["retry_rate"] if r["retry_rate"] is not None else 0),
        )
    )
    return result


def role_success_rate_24h() -> list[dict]:
    """Return per-role success rates over the last 24 hours.

    Returns a list of dicts:
        [{role, success_rate, sample_size}, ...]

    Rules:
    - success_rate = count(pass|done) / count(all) for that role
    - Roles with sample_size < 5 have success_rate = None (shown as N/A in UI)
    - Sorted: lowest success_rate first, None-rate rows last
    """
    db = _db_path()
    if not db.exists():
        return []

    try:
        from backend.stats_connection import get_read_connection  # noqa: PLC0415
        conn = get_read_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    json_extract_string(tags, '$.role')                   AS role,
                    COUNT(*)                                              AS sample_size,
                    SUM(CASE WHEN json_extract_string(tags, '$.verdict')
                                  IN ('pass', 'done')
                             THEN 1 ELSE 0 END)                           AS success_count
                FROM metric_event
                WHERE metric = 'role_verdict'
                  AND ts >= NOW() - INTERVAL 24 HOURS
                  AND json_extract_string(tags, '$.role') IS NOT NULL
                GROUP BY json_extract_string(tags, '$.role')
                """,
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

    result = []
    for role, sample_size, success_count in rows:
        rate = (success_count / sample_size) if sample_size >= 5 else None
        result.append(
            {"role": role, "success_rate": rate, "sample_size": int(sample_size)}
        )

    # Sort: lowest success_rate first, None rows last
    result.sort(
        key=lambda r: (
            r["success_rate"] is None,
            r["success_rate"] if r["success_rate"] is not None else 0,
        )
    )
    return result


# ---------------------------------------------------------------------------
# Loop idle ratio (Discussion #540 P2 metric #2)
# ---------------------------------------------------------------------------


def loop_idle_ratio_24h(metrics_path: str | None = None) -> dict:
    """Return fraction of /loop iterations in the last 24h where agents_spawned == 0.

    Reads loop-metrics.jsonl directly (reader-time aggregation — no double-write).
    An iteration counts as idle when the row has ``idle: true`` OR ``agents_spawned == 0``.

    Returns:
        {"ratio": float | None, "idle_count": int, "sample_size": int}

    When sample_size < 5, ratio is None (UI renders "N/A").
    """
    from datetime import timedelta  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    from backend.loop_metrics_ts import (  # noqa: PLC0415
        parse_loop_metrics_ts,
        report_skipped_row,
        row_ts,
    )

    if metrics_path is None:
        metrics_path = str(
            Path(__file__).resolve().parent.parent
            / ".autonomous-team"
            / "loop-metrics.jsonl"
        )

    path = Path(metrics_path)
    if not path.exists():
        return {"ratio": None, "idle_count": 0, "sample_size": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    total = 0
    idle_count = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = _json.loads(raw)
            except _json.JSONDecodeError:
                continue

            raw_ts = row_ts(row)
            if raw_ts is None:
                continue
            ts = parse_loop_metrics_ts(raw_ts)
            if ts is None:
                # Present but unparseable (e.g. a raw epoch int) -- skip
                # loudly rather than raising or silently dropping (D#2315).
                report_skipped_row(path.name, lineno, raw_ts, prefix="stats_writer")
                continue

            if ts < cutoff:
                continue

            if row.get("origin") == "test":
                continue

            total += 1
            is_idle = bool(row.get("idle", False)) or int(row.get("agents_spawned", -1)) == 0
            if is_idle:
                idle_count += 1

    if total < 5:
        return {"ratio": None, "idle_count": idle_count, "sample_size": total}

    return {"ratio": idle_count / total, "idle_count": idle_count, "sample_size": total}


# ---------------------------------------------------------------------------
# Cost spike helpers (Discussion #540 metric #22)
# ---------------------------------------------------------------------------


def record_cost_spike(value: float, mu: float, sigma: float, ts: "datetime | None" = None) -> None:
    """Record one cost spike event.

    Emits a 'cost_spike' metric row tagged with mu and sigma so the dashboard
    tile can display the threshold and severity context.
    """
    now = ts or datetime.now(timezone.utc)
    record(
        metric="cost_spike",
        value=float(value),
        unit="usd",
        tags={"mu": str(round(mu, 6)), "sigma": str(round(sigma, 6))},
        source="team-lead-iteration",
        ts=now,
    )


def record_iteration_cost(value: float, ts: "datetime | None" = None) -> None:
    """Record the total USD cost of agents that completed in this loop iteration.

    Used by detect_cost_spike() as the baseline data source.
    """
    now = ts or datetime.now(timezone.utc)
    record(
        metric="iteration_cost_usd",
        value=float(value),
        unit="usd",
        tags={},
        source="team-lead-iteration",
        ts=now,
    )


def cost_spike_history(hours: int = 24) -> list[dict]:
    """Return spike events from the last `hours` hours, newest first.

    Returns a list of dicts:
        [{ts_iso, value, mu, sigma}, ...]
    """
    from datetime import timedelta  # noqa: PLC0415

    db = _db_path()
    if not db.exists():
        return []

    # Compute cutoff in Python to avoid DuckDB NOW() local-tz drift.
    # Stored timestamps are UTC strings cast as plain TIMESTAMP — compare
    # against a plain UTC string so timezone offsets do not affect filtering.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    try:
        from backend.stats_connection import get_read_connection  # noqa: PLC0415
        conn = get_read_connection()
        try:
            rows = conn.execute(
                """
                SELECT ts, value, tags
                FROM metric_event
                WHERE metric = 'cost_spike'
                  AND ts >= CAST(? AS TIMESTAMP)
                ORDER BY ts DESC
                """,
                [cutoff_str],
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

    result = []
    for ts_val, value, tags_raw in rows:
        try:
            tags = json.loads(tags_raw) if isinstance(tags_raw, str) else (tags_raw or {})
        except Exception:  # noqa: BLE001
            tags = {}
        result.append({
            "ts_iso": ts_val.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts_val, "strftime") else str(ts_val),
            "value": round(float(value), 6),
            "mu": float(tags.get("mu", 0)),
            "sigma": float(tags.get("sigma", 0)),
        })
    return result


# ---------------------------------------------------------------------------
# Fix-rounds aggregation helpers (Discussion #540 Phase 3)
# ---------------------------------------------------------------------------


def avg_fix_rounds_24h() -> dict:
    """Return avg fix rounds per merged PR over the last 24 hours.

    Reads all fix_rounds_per_pr rows in the last 24h and computes:
        avg_last_24h  — float average, or None when sample_size < 5
        sample_size   — number of PRs merged in the window
        distribution  — dict mapping rounds (as string "0", "1", ...) to count

    Returns {"avg_last_24h": None, "sample_size": 0, "distribution": {}} when
    the database is empty or does not exist.
    """
    db = _db_path()
    if not db.exists():
        return {"avg_last_24h": None, "sample_size": 0, "distribution": {}}

    from datetime import timedelta  # noqa: PLC0415

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    try:
        from backend.stats_connection import get_read_connection  # noqa: PLC0415
        conn = get_read_connection()
        try:
            rows = conn.execute(
                """
                SELECT value
                FROM metric_event
                WHERE metric = 'fix_rounds_per_pr'
                  AND ts >= CAST(? AS TIMESTAMP)
                ORDER BY ts
                """,
                [cutoff_str],
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {"avg_last_24h": None, "sample_size": 0, "distribution": {}}

    if not rows:
        return {"avg_last_24h": None, "sample_size": 0, "distribution": {}}

    values = [int(r[0]) for r in rows]
    sample_size = len(values)
    avg = sum(values) / sample_size if sample_size >= 5 else None

    distribution: dict[str, int] = {}
    for v in values:
        key = str(v)
        distribution[key] = distribution.get(key, 0) + 1

    return {
        "avg_last_24h": round(avg, 2) if avg is not None else None,
        "sample_size": sample_size,
        "distribution": distribution,
    }


# ---------------------------------------------------------------------------
# Writer registry (Discussion #1153)
# ---------------------------------------------------------------------------

#: Canonical set of metric names that have an active writer in this module.
#: Add an entry here whenever a new ``record()`` call is introduced, and remove
#: it when the writer is deleted.  The freshness test asserts that every metric
#: watched by ``stats_freshness_watchdog`` has a corresponding entry here.
REGISTERED_WRITERS: frozenset[str] = frozenset({
    # stats_writer.py — direct record() calls
    "role_verdict",
    "intervention_count",
    "interventions_per_classifier",
    "interventions_per_agent_avg",
    "intervention_to_self_correction_rate",
    "cost_spike",
    "iteration_cost_usd",
    "fix_rounds_per_pr",
})


def registered_metrics() -> frozenset[str]:
    """Return the set of metric names that have an active writer in stats_writer.

    Used by ``tests/test_health_metrics_freshness.py`` to assert that every
    metric watched by the freshness checker has a corresponding writer so dead
    metrics (with no writer) can never accumulate in the database again.

    Also covers metrics written by external scripts that call
    ``stats_writer.record()`` directly — those are included here.
    """
    # External writers that call stats_writer.record() but live outside this file:
    _external: frozenset[str] = frozenset({
        "orphan_worktree_rate",             # scripts/reap-worktrees.sh
        "loop_iteration_duration_seconds",  # scripts/append-loop-metrics.sh
        "scan_to_spawn_ratio",              # scripts/team-lead-iteration.sh
        "wasted_tokens_ratio",              # scripts/spawn-hourly-stats.sh
        "impersonation_rate",               # scripts/spawn-hourly-stats.sh
        "hard_rule_violation_count",        # scripts/spawn-hourly-stats.sh
        # post-merge-hook.sh writers:
        "time_to_merge_seconds",
        "fix_cycle_count",
        "cost_per_merged_pr_usd",
        "cost_attribution_unresolved_count",  # D#2282 — suppression counter when the resolver isn't agent_run
        "pr_file_conflict_score",
        "spec_to_first_pr_latency_seconds",
        "acceptance_criteria_pass_rate",
        "reviewer_acceptance_latency_seconds",
    })
    return REGISTERED_WRITERS | _external


if __name__ == "__main__":
    import argparse as _argparse

    _parser = _argparse.ArgumentParser(description="stats_writer CLI")
    _sub = _parser.add_subparsers(dest="cmd")

    _ev = _sub.add_parser("emit-verdict", help="Record one agent verdict event")
    _ev.add_argument("--role", required=True)
    _ev.add_argument("--verdict", required=True)

    _sub.add_parser("role-success-rate", help="Print per-role success rates (24h) as JSON")
    _sub.add_parser("role-retry-rate", help="Print per-role retry rates (24h) as JSON")
    _sub.add_parser("loop-idle-ratio", help="Print loop idle ratio (24h) as JSON")
    _sub.add_parser("avg-fix-rounds", help="Print avg fix rounds per PR (24h) as JSON")

    _args = _parser.parse_args()

    if _args.cmd == "emit-verdict":
        emit_verdict(_args.role, _args.verdict)
        print(f"emit_verdict({_args.role!r}, {_args.verdict!r}) OK")
    elif _args.cmd == "role-success-rate":
        import json as _json
        print(_json.dumps(role_success_rate_24h(), indent=2))
    elif _args.cmd == "role-retry-rate":
        import json as _json
        print(_json.dumps(role_retry_rate_24h(), indent=2))
    elif _args.cmd == "loop-idle-ratio":
        import json as _json
        print(_json.dumps(loop_idle_ratio_24h(), indent=2))
    elif _args.cmd == "avg-fix-rounds":
        import json as _json
        print(_json.dumps(avg_fix_rounds_24h(), indent=2))
    else:
        # Quick smoke-test (legacy __main__ behaviour)
        record("test_metric", 1.0, "count", {"env": "test"}, source="smoke-test")
        print("record() OK")
