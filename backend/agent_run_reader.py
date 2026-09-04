"""
backend/agent_run_reader.py — Read-side helpers for the agent_run DuckDB table.

All functions return empty / zero / None on an empty table or missing dependency
so callers don't need to special-case the "no data yet" state.

Public API
----------
by_role(role, since_iso) -> list[dict]
duration_percentiles(role, since_iso) -> dict
stuck_runs(threshold_seconds) -> list[dict]
roundtrip_latency(pr) -> float | None
concurrent_active(since_iso, until_iso) -> list[dict]
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from backend.agent_run_verdicts import is_agent_reported

logger = logging.getLogger(__name__)

# Rendered in place of a NON_AGENT_VERDICTS value (e.g. "reconciled-stale")
# so a caller reading agent_run.verdict can't mistake a reconciler/sweeper
# placeholder for a real agent-reported outcome.
NON_AGENT_VERDICT_MARKER = "(no verdict recorded — run never completed)"


# ---------------------------------------------------------------------------
# DB path — one resolver, shared with agent_run_tracker via state_paths
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    """Return the DuckDB stats path — same single resolver the writer uses."""
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


def _connect() -> Any:
    """Open a read-only DuckDB connection to stats.duckdb.

    Raises ImportError when duckdb is not installed.
    Raises FileNotFoundError when the database file does not exist.
    """
    import duckdb  # noqa: PLC0415

    db = _db_path()
    if not db.exists():
        raise FileNotFoundError(f"stats.duckdb not found at {db}")

    return duckdb.connect(str(db), read_only=True)


def _row_to_dict(cursor_description: list, row: tuple) -> dict:
    """Convert a DuckDB cursor row to a dict using column names.

    datetime objects are converted to ISO-8601 strings so the result is
    always JSON-serializable (Python's json module cannot encode datetime).
    """
    result = {}
    for col, val in zip(cursor_description, row):
        if isinstance(val, datetime):
            # Ensure UTC offset is present so consumers can parse unambiguously.
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)
            val = val.isoformat()
        if col[0] == "verdict" and val and not is_agent_reported(val):
            val = NON_AGENT_VERDICT_MARKER
        result[col[0]] = val
    return result


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def by_role(role: str, since_iso: str | None = None) -> list[dict]:
    """Return all agent_run rows for *role* since *since_iso*.

    Parameters
    ----------
    role:       Agent role name, e.g. "executor".
    since_iso:  ISO-8601 timestamp (UTC).  Defaults to 24h ago when omitted.

    Returns
    -------
    List of row dicts ordered by start_ts descending.  Empty list on any error.
    """
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader.by_role: %s", exc)
        return []

    try:
        if since_iso is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))

        # routed_via column was added in D#1331; use COALESCE for older schemas.
        # Older tables without the column will have NULL for routed_via.
        try:
            col_names = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name='agent_run'"
                ).fetchall()
            }
            has_routed_via = "routed_via" in col_names
        except Exception:  # noqa: BLE001
            has_routed_via = False

        routed_via_expr = "routed_via" if has_routed_via else "NULL AS routed_via"

        cur = conn.execute(
            f"""
            SELECT agent_id, role, discussion, pr, start_ts, end_ts,
                   duration_s, verdict, model, input_tok, output_tok,
                   cache_read, cache_write, blocked_reason, event_id,
                   {routed_via_expr}
            FROM agent_run
            WHERE role = ?
              AND start_ts >= ?
            ORDER BY start_ts DESC
            """,
            [role, since],
        )
        desc = cur.description
        rows = cur.fetchall()
        return [_row_to_dict(desc, row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader.by_role failed: %s", exc)
        return []
    finally:
        conn.close()


def duration_percentiles(
    role: str | None = None,
    since_iso: str | None = None,
) -> dict:
    """Return p50 / p95 / p99 duration percentiles (in seconds) across completed runs.

    Parameters
    ----------
    role:       Filter to one role.  None means all roles.
    since_iso:  ISO-8601 start bound (UTC).  Defaults to 7 days.

    Returns
    -------
    {"p50": float|None, "p95": float|None, "p99": float|None, "sample_size": int}
    All percentile fields are None when sample_size == 0.
    """
    empty = {"p50": None, "p95": None, "p99": None, "sample_size": 0}
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader.duration_percentiles: %s", exc)
        return empty

    try:
        if since_iso is None:
            since = datetime.now(timezone.utc) - timedelta(days=7)
        else:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))

        if role is None:
            cur = conn.execute(
                """
                SELECT
                    quantile_cont(duration_s, 0.50) AS p50,
                    quantile_cont(duration_s, 0.95) AS p95,
                    quantile_cont(duration_s, 0.99) AS p99,
                    count(*) AS cnt
                FROM agent_run
                WHERE duration_s IS NOT NULL
                  AND start_ts >= ?
                """,
                [since],
            )
        else:
            cur = conn.execute(
                """
                SELECT
                    quantile_cont(duration_s, 0.50) AS p50,
                    quantile_cont(duration_s, 0.95) AS p95,
                    quantile_cont(duration_s, 0.99) AS p99,
                    count(*) AS cnt
                FROM agent_run
                WHERE duration_s IS NOT NULL
                  AND role = ?
                  AND start_ts >= ?
                """,
                [role, since],
            )

        row = cur.fetchone()
        if row is None or row[3] == 0:
            return empty

        p50, p95, p99, cnt = row
        return {
            "p50": float(p50) if p50 is not None else None,
            "p95": float(p95) if p95 is not None else None,
            "p99": float(p99) if p99 is not None else None,
            "sample_size": int(cnt),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader.duration_percentiles failed: %s", exc)
        return empty
    finally:
        conn.close()


DEFAULT_STUCK_THRESHOLD_SECONDS: int = 900  # 15 minutes — shared with agent_feed screen


def stuck_runs(threshold_seconds: int = DEFAULT_STUCK_THRESHOLD_SECONDS) -> list[dict]:
    """Return in-flight runs that have been open longer than *threshold_seconds*.

    A "stuck" run has end_ts IS NULL and start_ts older than threshold_seconds ago.

    Parameters
    ----------
    threshold_seconds:  Age threshold.  Default 900 (15 minutes).

    Returns
    -------
    List of row dicts ordered by start_ts ascending (oldest first).
    Empty list when no stuck runs or on any error.
    """
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader.stuck_runs: %s", exc)
        return []

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold_seconds)
        cur = conn.execute(
            """
            SELECT agent_id, role, discussion, pr, start_ts, end_ts,
                   duration_s, verdict, model, input_tok, output_tok,
                   cache_read, cache_write, blocked_reason, event_id
            FROM agent_run
            WHERE end_ts IS NULL
              AND start_ts < ?
              AND agent_id NOT LIKE 'idem-test%'
              AND agent_id NOT LIKE 'test-%'
            ORDER BY start_ts ASC
            """,
            [cutoff],
        )
        desc = cur.description
        rows = cur.fetchall()
        return [_row_to_dict(desc, row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader.stuck_runs failed: %s", exc)
        return []
    finally:
        conn.close()


def roundtrip_latency(pr: int) -> float | None:
    """Return executor-done → reviewer-started latency in seconds for a PR.

    Looks for the latest completed executor run and the earliest completed
    reviewer (code-reviewer / security-reviewer) start on the same PR.
    Returns None when either endpoint cannot be found.

    Parameters
    ----------
    pr: GitHub PR number.
    """
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader.roundtrip_latency: %s", exc)
        return None

    try:
        # Latest executor end_ts on this PR
        cur = conn.execute(
            """
            SELECT MAX(end_ts)
            FROM agent_run
            WHERE pr = ?
              AND role = 'executor'
              AND end_ts IS NOT NULL
            """,
            [pr],
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        executor_done = row[0]
        if isinstance(executor_done, str):
            executor_done = datetime.fromisoformat(executor_done.replace("Z", "+00:00"))
        if hasattr(executor_done, "tzinfo") and executor_done.tzinfo is None:
            executor_done = executor_done.replace(tzinfo=timezone.utc)

        # Earliest reviewer start_ts on this PR that came AFTER executor_done
        cur = conn.execute(
            """
            SELECT MIN(start_ts)
            FROM agent_run
            WHERE pr = ?
              AND role IN ('code-reviewer', 'security-reviewer')
              AND start_ts > ?
            """,
            [pr, executor_done],
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        reviewer_start = row[0]
        if isinstance(reviewer_start, str):
            reviewer_start = datetime.fromisoformat(reviewer_start.replace("Z", "+00:00"))
        if hasattr(reviewer_start, "tzinfo") and reviewer_start.tzinfo is None:
            reviewer_start = reviewer_start.replace(tzinfo=timezone.utc)

        return (reviewer_start - executor_done).total_seconds()

    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader.roundtrip_latency failed: %s", exc)
        return None
    finally:
        conn.close()


def _recent(limit: int = 50, since_iso: str | None = None) -> list[dict]:
    """Return the *limit* most-recent rows across all roles since *since_iso*.

    This is an internal helper called by rpc/agent_runs.py handle_recent.
    Not part of the public spec API but follows the same non-fatal pattern.
    """
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader._recent: %s", exc)
        return []

    try:
        if since_iso is None:
            since = datetime.now(timezone.utc) - timedelta(days=7)
        else:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))

        cur = conn.execute(
            """
            SELECT agent_id, role, discussion, pr, start_ts, end_ts,
                   duration_s, verdict, model, input_tok, output_tok,
                   cache_read, cache_write, blocked_reason, event_id
            FROM agent_run
            WHERE start_ts >= ?
            ORDER BY start_ts DESC
            LIMIT ?
            """,
            [since, limit],
        )
        desc = cur.description
        rows = cur.fetchall()
        return [_row_to_dict(desc, row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader._recent failed: %s", exc)
        return []
    finally:
        conn.close()


def run_detail(agent_id: str) -> dict:
    """Return the full agent_run row for *agent_id* as a dict.

    Parameters
    ----------
    agent_id:  The agent_id to look up.

    Returns
    -------
    Row dict, or empty dict when not found or on any error.
    """
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader.run_detail: %s", exc)
        return {}

    try:
        cur = conn.execute(
            """
            SELECT agent_id, role, discussion, pr, start_ts, end_ts,
                   duration_s, verdict, model, input_tok, output_tok,
                   cache_read, cache_write, blocked_reason, event_id
            FROM agent_run
            WHERE agent_id = ?
            LIMIT 1
            """,
            [agent_id],
        )
        desc = cur.description
        row = cur.fetchone()
        if row is None:
            return {}
        return _row_to_dict(desc, row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader.run_detail failed: %s", exc)
        return {}
    finally:
        conn.close()


def duration_percentiles_by_role(since_iso: str | None = None) -> list[dict]:
    """Return [{role, p50_ms, p95_ms, n}, ...] for each distinct role.

    Only includes completed runs (duration_s IS NOT NULL).
    Ordered by role name ascending.

    Parameters
    ----------
    since_iso:  ISO-8601 start bound (UTC).  Defaults to 24h ago.

    Returns
    -------
    List of dicts.  Empty list on any error or empty table.
    """
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader.duration_percentiles_by_role: %s", exc)
        return []

    try:
        if since_iso is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))

        cur = conn.execute(
            """
            SELECT
                role,
                quantile_cont(duration_s, 0.50) AS p50_s,
                quantile_cont(duration_s, 0.95) AS p95_s,
                count(*) AS n
            FROM agent_run
            WHERE duration_s IS NOT NULL
              AND start_ts >= ?
            GROUP BY role
            ORDER BY role ASC
            """,
            [since],
        )
        rows = cur.fetchall()
        result = []
        for role, p50_s, p95_s, n in rows:
            if n == 0:
                continue
            result.append({
                "role": role,
                "p50_ms": round(float(p50_s) * 1000) if p50_s is not None else None,
                "p95_ms": round(float(p95_s) * 1000) if p95_s is not None else None,
                "n": int(n),
            })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader.duration_percentiles_by_role failed: %s", exc)
        return []
    finally:
        conn.close()


def concurrent_active(
    since_iso: str | None = None,
    until_iso: str | None = None,
    bucket_seconds: int = 60,
) -> list[dict]:
    """Return a time-series of concurrent active agent counts.

    Counts how many runs were in-flight (started before ts, ended after ts or
    still open) at each *bucket_seconds* interval between *since_iso* and
    *until_iso*.

    Parameters
    ----------
    since_iso:      Start of the window (UTC ISO-8601).  Default: 24h ago.
    until_iso:      End of the window (UTC ISO-8601).    Default: now.
    bucket_seconds: Granularity.  Default: 60 (1-min buckets).

    Returns
    -------
    List of {"ts": "<iso>", "count": int} ordered by ts ascending.
    Empty list on any error.
    """
    try:
        conn = _connect()
    except (ImportError, FileNotFoundError) as exc:
        logger.debug("agent_run_reader.concurrent_active: %s", exc)
        return []

    try:
        now = datetime.now(timezone.utc)
        if since_iso is None:
            since = now - timedelta(hours=24)
        else:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        if until_iso is None:
            until = now
        else:
            until = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))

        # Build bucket timestamps in Python — avoids DuckDB generate_series compat issues
        buckets: list[datetime] = []
        ts = since
        while ts <= until:
            buckets.append(ts)
            ts = ts + timedelta(seconds=bucket_seconds)

        if not buckets:
            return []

        # For open (in-flight) runs, use actual current time as effective end —
        # not the `until` parameter — so future windows don't incorrectly count them.
        cur = conn.execute(
            """
            SELECT start_ts, COALESCE(end_ts, ?) AS end_ts_eff
            FROM agent_run
            WHERE start_ts <= ?
              AND (end_ts IS NULL OR end_ts >= ?)
            """,
            [now, until, since],
        )
        runs = cur.fetchall()

        results = []
        for bucket_ts in buckets:
            count = 0
            for start, end in runs:
                # Normalise timezone
                if isinstance(start, str):
                    start = datetime.fromisoformat(start.replace("Z", "+00:00"))
                if isinstance(end, str):
                    end = datetime.fromisoformat(end.replace("Z", "+00:00"))
                if hasattr(start, "tzinfo") and start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                if hasattr(end, "tzinfo") and end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                if start <= bucket_ts <= end:
                    count += 1
            results.append({
                "ts": bucket_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": count,
            })

        return results

    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_reader.concurrent_active failed: %s", exc)
        return []
    finally:
        conn.close()
