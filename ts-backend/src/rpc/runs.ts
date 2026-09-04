/**
 * rpc/runs.ts — Native TS implementations of the runs.* RPC methods.
 *
 * Mirrors backend/rpc/agent_runs.py + backend/agent_run_reader.py exactly:
 *   - runs.by_role       → handleByRole()
 *   - runs.percentiles   → handlePercentiles()
 *   - runs.stuck         → handleStuck()
 *   - runs.roundtrip     → handleRoundtrip()
 *   - runs.active_over_time → handleActiveOverTime()
 *   - runs.recent        → handleRecent()
 *
 * All handlers are additive — Python runtime code is not modified.
 * Response shapes are 1:1 parity with Python (including error-return shapes).
 *
 * Design notes (documented as overrides of Implementation Notes):
 *   - Timestamp params are passed as ISO strings and bound via CAST(? AS TIMESTAMPTZ)
 *     in SQL, matching the SPIKE-1 harness Q4 pattern already proven in P3.
 *   - Integer params (limit, threshold_seconds, pr) are interpolated into SQL
 *     as validated integers (never raw user strings) — safe because we call
 *     parseInt() on them before use.
 *   - concurrent_active builds bucket timestamps in JS (same approach as Python)
 *     to avoid DuckDB generate_series compatibility issues (see Python comment).
 *   - routed_via schema-probe in by_role mirrors Python's try/except probe exactly:
 *     if the column exists it is selected; otherwise NULL AS routed_via.
 */

import { openReadConn, closeConn, queryDicts } from "../duckdb-helpers.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Format a JS Date as "YYYY-MM-DD HH:MM:SS+00:00" for DuckDB CAST(? AS TIMESTAMPTZ).
 *
 * agent_run.start_ts / end_ts are stored as TIMESTAMP WITH TIME ZONE.
 * DuckDB's CAST(? AS TIMESTAMP) (no tz) treats the string as local time — wrong on
 * non-UTC machines. CAST(? AS TIMESTAMPTZ) with the +00:00 suffix forces UTC
 * comparison, matching Python's behavior of passing datetime objects with UTC tzinfo.
 *
 * metric_event.ts is TIMESTAMP (no tz), so existing P3 routes still use
 * toDbTimestampNaive(). This function is for agent_run queries only.
 */
function toDbTimestamp(d: Date): string {
  return d.toISOString().replace("T", " ").slice(0, 19) + "+00:00";
}

/**
 * Parse an ISO-8601 string (with or without trailing Z/offset) to a Date.
 * Mirrors Python: datetime.fromisoformat(s.replace("Z", "+00:00"))
 */
function parseIso(s: string): Date {
  return new Date(s.replace("Z", "+00:00"));
}

// ---------------------------------------------------------------------------
// runs.by_role
// ---------------------------------------------------------------------------

/**
 * Return all agent_run rows for a given role.
 *
 * Params:
 *   role (str, required)
 *   since_iso (str, optional) — ISO-8601 lower bound; default 24h ago
 *
 * Response: {"runs": [...]}
 * Mirrors: agent_run_reader.by_role() + rpc/agent_runs.handle_by_role()
 */
export async function handleByRole(params: Record<string, unknown>): Promise<unknown> {
  const role = (params["role"] as string | undefined) ?? "";
  if (!role) {
    throw new Error("'role' parameter is required");
  }
  const sinceIso = (params["since_iso"] as string | undefined) || null;

  const since = sinceIso ? parseIso(sinceIso) : new Date(Date.now() - 24 * 3600 * 1000);
  const sinceStr = toDbTimestamp(since);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { runs: [] };
  }

  try {
    // Probe for routed_via column — mirrors Python's schema probe in by_role().
    // If the column doesn't exist, use NULL AS routed_via.
    let hasRoutedVia = false;
    try {
      const schemaCols = await queryDicts(
        h,
        `SELECT column_name FROM information_schema.columns WHERE table_name='agent_run'`
      );
      hasRoutedVia = schemaCols.some(r => r["column_name"] === "routed_via");
    } catch {
      hasRoutedVia = false;
    }

    const routedViaExpr = hasRoutedVia ? "routed_via" : "NULL AS routed_via";

    const rows = await queryDicts(
      h,
      `
      SELECT agent_id, role, discussion, pr, start_ts, end_ts,
             duration_s, verdict, model, input_tok, output_tok,
             cache_read, cache_write, blocked_reason, event_id,
             ${routedViaExpr}
      FROM agent_run
      WHERE role = ?
        AND start_ts >= CAST(? AS TIMESTAMPTZ)
      ORDER BY start_ts DESC
      `,
      [role, sinceStr]
    );

    return { runs: rows };
  } catch {
    return { runs: [] };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// runs.percentiles
// ---------------------------------------------------------------------------

/**
 * Return duration percentiles across completed runs.
 *
 * Params:
 *   role (str, optional) — filter to one role; omit for all roles
 *   since_iso (str, optional) — ISO-8601 lower bound; default 7d
 *
 * Response: {"p50": float|null, "p95": float|null, "p99": float|null, "sample_size": int}
 * Mirrors: agent_run_reader.duration_percentiles() + rpc/agent_runs.handle_percentiles()
 */
export async function handlePercentiles(params: Record<string, unknown>): Promise<unknown> {
  const empty = { p50: null, p95: null, p99: null, sample_size: 0 };

  const role = (params["role"] as string | undefined) || null;
  const sinceIso = (params["since_iso"] as string | undefined) || null;

  const since = sinceIso ? parseIso(sinceIso) : new Date(Date.now() - 7 * 24 * 3600 * 1000);
  const sinceStr = toDbTimestamp(since);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return empty;
  }

  try {
    let rows: Record<string, unknown>[];
    if (role === null) {
      rows = await queryDicts(
        h,
        `
        SELECT
            quantile_cont(duration_s, 0.50) AS p50,
            quantile_cont(duration_s, 0.95) AS p95,
            quantile_cont(duration_s, 0.99) AS p99,
            count(*) AS cnt
        FROM agent_run
        WHERE duration_s IS NOT NULL
          AND start_ts >= CAST(? AS TIMESTAMPTZ)
        `,
        [sinceStr]
      );
    } else {
      rows = await queryDicts(
        h,
        `
        SELECT
            quantile_cont(duration_s, 0.50) AS p50,
            quantile_cont(duration_s, 0.95) AS p95,
            quantile_cont(duration_s, 0.99) AS p99,
            count(*) AS cnt
        FROM agent_run
        WHERE duration_s IS NOT NULL
          AND role = ?
          AND start_ts >= CAST(? AS TIMESTAMPTZ)
        `,
        [role, sinceStr]
      );
    }

    if (!rows.length) return empty;

    const row = rows[0];
    const cnt = typeof row["cnt"] === "bigint"
      ? Number(row["cnt"])
      : (row["cnt"] as number | null) ?? 0;

    if (cnt === 0) return empty;

    const toFloat = (v: unknown): number | null => {
      if (v === null || v === undefined) return null;
      const n = typeof v === "number" ? v : Number(v);
      return isFinite(n) ? n : null;
    };

    return {
      p50: toFloat(row["p50"]),
      p95: toFloat(row["p95"]),
      p99: toFloat(row["p99"]),
      sample_size: cnt,
    };
  } catch {
    return empty;
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// runs.stuck
// ---------------------------------------------------------------------------

/**
 * Return in-flight runs older than threshold_seconds with no end_ts.
 *
 * Params:
 *   threshold_seconds (int, optional) — default 1800 (30 min)
 *     Note: matches rpc/agent_runs.py handle_stuck() default (1800),
 *     not agent_run_reader.stuck_runs() default (900).
 *
 * Response: {"runs": [...]} — list of row dicts, oldest first
 * Mirrors: agent_run_reader.stuck_runs() + rpc/agent_runs.handle_stuck()
 */
export async function handleStuck(params: Record<string, unknown>): Promise<unknown> {
  const thresholdSeconds = parseInt(
    String(params["threshold_seconds"] ?? 1800), 10
  ) || 1800;

  const cutoff = new Date(Date.now() - thresholdSeconds * 1000);
  const cutoffStr = toDbTimestamp(cutoff);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { runs: [] };
  }

  try {
    const rows = await queryDicts(
      h,
      `
      SELECT agent_id, role, discussion, pr, start_ts, end_ts,
             duration_s, verdict, model, input_tok, output_tok,
             cache_read, cache_write, blocked_reason, event_id
      FROM agent_run
      WHERE end_ts IS NULL
        AND start_ts < CAST(? AS TIMESTAMPTZ)
        AND agent_id NOT LIKE 'idem-test%'
        AND agent_id NOT LIKE 'test-%'
      ORDER BY start_ts ASC
      `,
      [cutoffStr]
    );
    return { runs: rows };
  } catch {
    return { runs: [] };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// runs.roundtrip
// ---------------------------------------------------------------------------

/**
 * Return executor-done → reviewer-started latency for a PR.
 *
 * Params:
 *   pr (int, required) — GitHub PR number
 *
 * Response: {"pr": int, "latency_seconds": float|null}
 * Mirrors: agent_run_reader.roundtrip_latency() + rpc/agent_runs.handle_roundtrip()
 */
export async function handleRoundtrip(params: Record<string, unknown>): Promise<unknown> {
  const prRaw = params["pr"];
  if (prRaw === undefined || prRaw === null) {
    throw new Error("'pr' parameter is required");
  }
  const pr = parseInt(String(prRaw), 10);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { pr, latency_seconds: null };
  }

  try {
    // Latest executor end_ts on this PR
    const prStr = String(pr);
    const executorRows = await queryDicts(
      h,
      `
      SELECT MAX(end_ts) AS max_end
      FROM agent_run
      WHERE pr = CAST(? AS INTEGER)
        AND role = 'executor'
        AND end_ts IS NOT NULL
      `,
      [prStr]
    );

    const rawEnd = executorRows[0]?.["max_end"];
    if (rawEnd === null || rawEnd === undefined) {
      return { pr, latency_seconds: null };
    }

    // rawEnd is a string (tsToIso converted it) e.g. "2026-05-23T17:39:14Z"
    const executorDoneStr = rawEnd as string;
    const executorDone = new Date(executorDoneStr);

    // Earliest reviewer start_ts on this PR that came AFTER executor_done
    const reviewerRows = await queryDicts(
      h,
      `
      SELECT MIN(start_ts) AS min_start
      FROM agent_run
      WHERE pr = CAST(? AS INTEGER)
        AND role IN ('code-reviewer', 'security-reviewer')
        AND start_ts > CAST(? AS TIMESTAMPTZ)
      `,
      [prStr, toDbTimestamp(executorDone)]
    );

    const rawStart = reviewerRows[0]?.["min_start"];
    if (rawStart === null || rawStart === undefined) {
      return { pr, latency_seconds: null };
    }

    const reviewerStart = new Date(rawStart as string);
    const latencySeconds = (reviewerStart.getTime() - executorDone.getTime()) / 1000;

    return { pr, latency_seconds: latencySeconds };
  } catch {
    return { pr, latency_seconds: null };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// runs.active_over_time
// ---------------------------------------------------------------------------

/**
 * Return time-series of concurrent active agent counts.
 *
 * Params:
 *   since_iso (str, optional) — default 24h ago
 *   until_iso (str, optional) — default now
 *   bucket_seconds (int, optional) — default 60
 *
 * Response: {"points": [{"ts": str, "count": int}, ...]}
 * Mirrors: agent_run_reader.concurrent_active() + rpc/agent_runs.handle_active_over_time()
 */
export async function handleActiveOverTime(params: Record<string, unknown>): Promise<unknown> {
  const sinceIso = (params["since_iso"] as string | undefined) || null;
  const untilIso = (params["until_iso"] as string | undefined) || null;
  const bucketSeconds = parseInt(String(params["bucket_seconds"] ?? 60), 10) || 60;

  const now = new Date();
  const since = sinceIso ? parseIso(sinceIso) : new Date(now.getTime() - 24 * 3600 * 1000);
  const until = untilIso ? parseIso(untilIso) : now;

  // Build bucket timestamps in JS — mirrors Python's approach to avoid
  // DuckDB generate_series compatibility issues (same comment in Python source).
  const buckets: Date[] = [];
  let ts = new Date(since.getTime());
  while (ts.getTime() <= until.getTime()) {
    buckets.push(new Date(ts.getTime()));
    ts = new Date(ts.getTime() + bucketSeconds * 1000);
  }

  if (buckets.length === 0) {
    return { points: [] };
  }

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { points: [] };
  }

  try {
    // For open (in-flight) runs, use actual current time as effective end —
    // not the `until` parameter — so future windows don't incorrectly count them.
    // Mirrors Python: COALESCE(end_ts, ?) where ? = now
    const rows = await queryDicts(
      h,
      `
      SELECT start_ts, COALESCE(end_ts, CAST(? AS TIMESTAMPTZ)) AS end_ts_eff
      FROM agent_run
      WHERE start_ts <= CAST(? AS TIMESTAMPTZ)
        AND (end_ts IS NULL OR end_ts >= CAST(? AS TIMESTAMPTZ))
      `,
      [toDbTimestamp(now), toDbTimestamp(until), toDbTimestamp(since)]
    );

    // Parse runs into [startMs, endMs] pairs
    const runPairs: [number, number][] = rows.map(row => {
      const startStr = row["start_ts"] as string;
      const endStr = row["end_ts_eff"] as string;
      return [new Date(startStr).getTime(), new Date(endStr).getTime()];
    });

    // Count active runs per bucket — mirrors Python's O(buckets × runs) loop
    const points = buckets.map(bucketTs => {
      const bucketMs = bucketTs.getTime();
      let count = 0;
      for (const [startMs, endMs] of runPairs) {
        if (startMs <= bucketMs && bucketMs <= endMs) {
          count++;
        }
      }
      // Format as "%Y-%m-%dT%H:%M:%SZ" — mirrors Python's strftime
      const tsStr = bucketTs.toISOString().replace(/\.\d{3}Z$/, "Z");
      return { ts: tsStr, count };
    });

    return { points };
  } catch {
    return { points: [] };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// runs.recent
// ---------------------------------------------------------------------------

/**
 * Return the most recent completed agent_run rows across all roles.
 *
 * Params:
 *   limit (int, optional) — max rows returned; default 50
 *   since_iso (str, optional) — lower bound; default 7 days
 *
 * Response: {"runs": [...]}
 * Mirrors: agent_run_reader._recent() + rpc/agent_runs.handle_recent()
 */
export async function handleRecent(params: Record<string, unknown>): Promise<unknown> {
  const limit = parseInt(String(params["limit"] ?? 50), 10) || 50;
  const sinceIso = (params["since_iso"] as string | undefined) || null;

  const since = sinceIso ? parseIso(sinceIso) : new Date(Date.now() - 7 * 24 * 3600 * 1000);
  const sinceStr = toDbTimestamp(since);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { runs: [] };
  }

  try {
    const rows = await queryDicts(
      h,
      `
      SELECT agent_id, role, discussion, pr, start_ts, end_ts,
             duration_s, verdict, model, input_tok, output_tok,
             cache_read, cache_write, blocked_reason, event_id
      FROM agent_run
      WHERE start_ts >= CAST(? AS TIMESTAMPTZ)
      ORDER BY start_ts DESC
      LIMIT ${limit}
      `,
      [sinceStr]
    );
    return { runs: rows };
  } catch {
    return { runs: [] };
  } finally {
    closeConn(h);
  }
}
