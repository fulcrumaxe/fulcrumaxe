/**
 * rpc/stats.ts — Native TS implementations of the stats.* RPC methods (batch 2).
 *
 * Mirrors the following Python RPC handlers exactly (1:1 parity):
 *   - stats.team_lead_tokens    → handleTeamLeadTokens()
 *   - stats.cost_spike_history  → handleCostSpikeHistory()
 *   - stats.role_success_rate   → handleRoleSuccessRate()
 *   - stats.role_retry_rate     → handleRoleRetryRate()
 *   - stats.avg_fix_rounds_per_pr → handleAvgFixRoundsPerPr()
 *   - stats.pre_write_burn      → handlePreWriteBurn()
 *   - stats.cosmetic_blocks     → handleCosmeticBlocks()
 *   - stats.loop_idle_ratio     → handleLoopIdleRatio()
 *   - stats.parity_trend        → handleParityTrend()
 *
 * All handlers are additive — Python runtime code is not modified.
 * Response shapes are 1:1 parity with Python (including quirks like
 * sample_size < 5 → null rates, NOW() - INTERVAL 24 HOURS logic, etc.)
 *
 * Design notes:
 *   - team_lead_tokens/cost_spike_history/avg_fix_rounds_per_pr use
 *     CAST(? AS TIMESTAMP) (naïve, not TIMESTAMPTZ) because Python's
 *     stats_writer stores timestamps in loop_metrics + metric_event as
 *     plain TIMESTAMP, not TIMESTAMPTZ. The cutoff_str format mirrors
 *     Python: "%Y-%m-%d %H:%M:%S.%f"[:-3] (millisecond precision, no tz).
 *   - role_success_rate and role_retry_rate use DuckDB NOW() - INTERVAL 24 HOURS
 *     (no placeholder) because Python's stats_writer.py uses that exact form
 *     for those two queries (not a cutoff string). Faithful mirror: same form.
 *   - pre_write_burn interpolates LIMIT as a safe integer (never raw string).
 *   - cosmetic_blocks/loop_idle_ratio/parity_trend are pure file readers —
 *     no DuckDB, no spawns.
 *   - Path resolution for file-based readers mirrors Python's env-var order:
 *     AF_HOOK_EVENTS_DIR, AUTONOMOUS_TEAM_STATE_DIR, repo-relative default.
 */

import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { openReadConn, closeConn, queryDicts } from "../duckdb-helpers.js";

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

/**
 * Format a Date as a Python-style cutoff string for CAST(? AS TIMESTAMP):
 *   "YYYY-MM-DD HH:MM:SS.mmm"  (millisecond precision, no tz suffix)
 * Mirrors Python:
 *   cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
 * (Python's %f is microseconds; [:-3] truncates to milliseconds.)
 */
function toCutoffStr(d: Date): string {
  // toISOString() → "YYYY-MM-DDTHH:MM:SS.mmmZ"
  // Replace T → space, drop trailing Z
  return d.toISOString().replace("T", " ").slice(0, 23);
}

// ---------------------------------------------------------------------------
// stats.team_lead_tokens
// ---------------------------------------------------------------------------

/**
 * Return avg / p50 / p95 of team_lead_tokens_per_iter over the last N hours.
 *
 * Params:
 *   since_hours (int, optional) — default 24
 *
 * Response: {"avg": float|null, "p50": float|null, "p95": float|null, "sample_size": int}
 * When sample_size < 5, avg/p50/p95 are null (mirrors Python's UI "N/A" rule).
 *
 * Mirrors: backend/stats_writer.team_lead_tokens_percentiles()
 *          backend/rpc/stats_team_lead_tokens.handle()
 */
export async function handleTeamLeadTokens(params: Record<string, unknown>): Promise<unknown> {
  const empty = { avg: null, p50: null, p95: null, sample_size: 0 };
  const sinceHours = parseInt(String(params["since_hours"] ?? 24), 10) || 24;

  const cutoff = new Date(Date.now() - sinceHours * 3600 * 1000);
  const cutoffStr = toCutoffStr(cutoff);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return empty;
  }

  try {
    // Python uses PERCENTILE_CONT(0.50) WITHIN GROUP ... — DuckDB also supports this form.
    // Table: loop_metrics (not metric_event).
    const rows = await queryDicts(
      h,
      `
      SELECT
          COUNT(*)                                                   AS sample_size,
          AVG(team_lead_tokens_per_iter)                             AS avg_tl,
          PERCENTILE_CONT(0.50) WITHIN GROUP
              (ORDER BY team_lead_tokens_per_iter)                   AS p50_tl,
          PERCENTILE_CONT(0.95) WITHIN GROUP
              (ORDER BY team_lead_tokens_per_iter)                   AS p95_tl
      FROM loop_metrics
      WHERE ts >= CAST(? AS TIMESTAMP)
      `,
      [cutoffStr]
    );

    if (!rows.length) return empty;
    const row = rows[0];

    // COUNT(*) → bigint in DuckDB
    const sampleSizeRaw = row["sample_size"];
    const sampleSize = typeof sampleSizeRaw === "bigint"
      ? Number(sampleSizeRaw)
      : (typeof sampleSizeRaw === "number" ? sampleSizeRaw : 0);

    if (!sampleSize || sampleSize === 0) return empty;

    // Mirror Python: when sample_size < 5, return all nulls with the count
    if (sampleSize < 5) {
      return { avg: null, p50: null, p95: null, sample_size: sampleSize };
    }

    const toFloat = (v: unknown): number | null => {
      if (v === null || v === undefined) return null;
      const n = typeof v === "number" ? v : Number(v);
      return isFinite(n) ? n : null;
    };

    return {
      avg: toFloat(row["avg_tl"]),
      p50: toFloat(row["p50_tl"]),
      p95: toFloat(row["p95_tl"]),
      sample_size: sampleSize,
    };
  } catch {
    return empty;
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats.cost_spike_history
// ---------------------------------------------------------------------------

/**
 * Return recent cost spike events, newest first.
 *
 * Params:
 *   hours (int, optional) — look-back window; default 24
 *
 * Response: {
 *   "spikes": [{"ts_iso": str, "value": float, "mu": float, "sigma": float}, ...],
 *   "count": int,
 *   "last_spike_iso": str|null
 * }
 *
 * Mirrors: backend/stats_writer.cost_spike_history()
 *          backend/rpc/stats_cost_spike_history.handle()
 */
export async function handleCostSpikeHistory(params: Record<string, unknown>): Promise<unknown> {
  const hours = parseInt(String(params["hours"] ?? 24), 10) || 24;
  const cutoff = new Date(Date.now() - hours * 3600 * 1000);
  const cutoffStr = toCutoffStr(cutoff);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { spikes: [], count: 0, last_spike_iso: null };
  }

  try {
    const rows = await queryDicts(
      h,
      `
      SELECT ts, value, tags
      FROM metric_event
      WHERE metric = 'cost_spike'
        AND ts >= CAST(? AS TIMESTAMP)
      ORDER BY ts DESC
      `,
      [cutoffStr]
    );

    const spikes = rows.map(row => {
      // ts comes back as ISO string from rowToDict/tsToIso
      const tsRaw = row["ts"] as string | null;

      // Python: ts_val.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts_val, 'strftime') else str(ts_val)
      // tsToIso already produces "YYYY-MM-DDTHH:MM:SSZ" — match exactly.
      const tsIso = tsRaw ?? "";

      // tags is stored as JSON string in metric_event
      let tags: Record<string, string> = {};
      const tagsRaw = row["tags"];
      if (typeof tagsRaw === "string") {
        try { tags = JSON.parse(tagsRaw); } catch { tags = {}; }
      } else if (tagsRaw && typeof tagsRaw === "object") {
        tags = tagsRaw as Record<string, string>;
      }

      const value = row["value"];
      const valueNum = typeof value === "number" ? value : Number(value);

      return {
        ts_iso: tsIso,
        // Python: round(float(value), 6)
        value: Math.round(valueNum * 1e6) / 1e6,
        mu: parseFloat(tags["mu"] ?? "0") || 0,
        sigma: parseFloat(tags["sigma"] ?? "0") || 0,
      };
    });

    return {
      spikes,
      count: spikes.length,
      last_spike_iso: spikes.length > 0 ? spikes[0]["ts_iso"] : null,
    };
  } catch {
    return { spikes: [], count: 0, last_spike_iso: null };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats.role_success_rate
// ---------------------------------------------------------------------------

/**
 * Return per-role success rates over the last 24 hours.
 *
 * Response: {"rows": [{"role": str, "success_rate": float|null, "sample_size": int}, ...]}
 * success_rate is null when sample_size < 5.
 * Sorted: lowest success_rate first, null-rate rows last.
 *
 * Mirrors: backend/stats_writer.role_success_rate_24h()
 *          backend/rpc/stats_role_success_rate.handle()
 *
 * NOTE: Python uses NOW() - INTERVAL 24 HOURS (not a placeholder cutoff string)
 * for this query. Faithful mirror: use the same DuckDB expression.
 */
export async function handleRoleSuccessRate(_params: Record<string, unknown>): Promise<unknown> {
  let h;
  try {
    h = await openReadConn();
  } catch {
    return { rows: [] };
  }

  try {
    const rows = await queryDicts(
      h,
      `
      SELECT
          json_extract_string(tags, '$.role')                       AS role,
          COUNT(*)                                                  AS sample_size,
          SUM(CASE WHEN json_extract_string(tags, '$.verdict')
                        IN ('pass', 'done')
                   THEN 1 ELSE 0 END)                              AS success_count
      FROM metric_event
      WHERE metric = 'role_verdict'
        AND ts >= NOW() - INTERVAL 24 HOURS
        AND json_extract_string(tags, '$.role') IS NOT NULL
      GROUP BY json_extract_string(tags, '$.role')
      `
    );

    const result = rows.map(row => {
      const sampleSize = typeof row["sample_size"] === "bigint"
        ? Number(row["sample_size"])
        : (row["sample_size"] as number | null) ?? 0;
      const successCount = typeof row["success_count"] === "bigint"
        ? Number(row["success_count"])
        : (row["success_count"] as number | null) ?? 0;

      const rate = sampleSize >= 5 ? successCount / sampleSize : null;
      return {
        role: row["role"] as string,
        success_rate: rate,
        sample_size: sampleSize,
      };
    });

    // Sort: lowest success_rate first, null rows last — mirrors Python's sort key
    result.sort((a, b) => {
      const aNull = a.success_rate === null;
      const bNull = b.success_rate === null;
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      return a.success_rate! - b.success_rate!;
    });

    return { rows: result };
  } catch {
    return { rows: [] };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats.role_retry_rate
// ---------------------------------------------------------------------------

/**
 * Return per-role retry rates over the last 24 hours.
 *
 * Response: {"rows": [{"role": str, "retry_rate": float|null, "sample_size": int}, ...]}
 * retry_rate = count(needs-fix|fail) / count(all) per role.
 * retry_rate is null when sample_size < 5.
 * Sorted: highest retry_rate first, null-rate rows last.
 *
 * Mirrors: backend/stats_writer.role_retry_rate_24h()
 *          backend/rpc/stats_role_retry_rate.handle()
 */
export async function handleRoleRetryRate(_params: Record<string, unknown>): Promise<unknown> {
  let h;
  try {
    h = await openReadConn();
  } catch {
    return { rows: [] };
  }

  try {
    const rows = await queryDicts(
      h,
      `
      SELECT
          json_extract_string(tags, '$.role')                       AS role,
          COUNT(*)                                                  AS sample_size,
          SUM(CASE WHEN json_extract_string(tags, '$.verdict')
                        IN ('needs-fix', 'fail')
                   THEN 1 ELSE 0 END)                              AS retry_count
      FROM metric_event
      WHERE metric = 'role_verdict'
        AND ts >= NOW() - INTERVAL 24 HOURS
        AND json_extract_string(tags, '$.role') IS NOT NULL
      GROUP BY json_extract_string(tags, '$.role')
      `
    );

    const result = rows.map(row => {
      const sampleSize = typeof row["sample_size"] === "bigint"
        ? Number(row["sample_size"])
        : (row["sample_size"] as number | null) ?? 0;
      const retryCount = typeof row["retry_count"] === "bigint"
        ? Number(row["retry_count"])
        : (row["retry_count"] as number | null) ?? 0;

      const rate = sampleSize >= 5 ? retryCount / sampleSize : null;
      return {
        role: row["role"] as string,
        retry_rate: rate,
        sample_size: sampleSize,
      };
    });

    // Sort: highest retry_rate first, null rows last — mirrors Python sort key:
    // key=lambda r: (r["retry_rate"] is None, -(r["retry_rate"] if ... else 0))
    result.sort((a, b) => {
      const aNull = a.retry_rate === null;
      const bNull = b.retry_rate === null;
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      // Descending by retry_rate
      return b.retry_rate! - a.retry_rate!;
    });

    return { rows: result };
  } catch {
    return { rows: [] };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats.avg_fix_rounds_per_pr
// ---------------------------------------------------------------------------

/**
 * Return avg fix rounds per merged PR over the last 24 hours.
 *
 * Response: {
 *   "avg_last_24h": float|null,  — null when sample_size < 5
 *   "sample_size": int,
 *   "distribution": {"0": N, "1": N, ...}
 * }
 *
 * Mirrors: backend/stats_writer.avg_fix_rounds_24h()
 *          backend/rpc/stats_avg_fix_rounds_per_pr.handle()
 */
export async function handleAvgFixRoundsPerPr(_params: Record<string, unknown>): Promise<unknown> {
  const empty = { avg_last_24h: null, sample_size: 0, distribution: {} };
  const cutoff = new Date(Date.now() - 24 * 3600 * 1000);
  const cutoffStr = toCutoffStr(cutoff);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return empty;
  }

  try {
    const rows = await queryDicts(
      h,
      `
      SELECT value
      FROM metric_event
      WHERE metric = 'fix_rounds_per_pr'
        AND ts >= CAST(? AS TIMESTAMP)
      ORDER BY ts
      `,
      [cutoffStr]
    );

    if (!rows.length) return empty;

    // Python: values = [int(r[0]) for r in rows]
    const values = rows.map(row => {
      const v = row["value"];
      return typeof v === "bigint" ? Number(v) : Math.round(Number(v));
    });

    const sampleSize = values.length;
    const avg = sampleSize >= 5 ? values.reduce((a, b) => a + b, 0) / sampleSize : null;

    // Python: round(avg, 2) if avg is not None else None
    const avgRounded = avg !== null ? Math.round(avg * 100) / 100 : null;

    // distribution: {str(v): count}
    const distribution: Record<string, number> = {};
    for (const v of values) {
      const key = String(v);
      distribution[key] = (distribution[key] ?? 0) + 1;
    }

    return {
      avg_last_24h: avgRounded,
      sample_size: sampleSize,
      distribution,
    };
  } catch {
    return empty;
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats.pre_write_burn
// ---------------------------------------------------------------------------

/**
 * Return executor agent_run rows where pre-Write turn ratio > 10%.
 *
 * Params:
 *   limit (int, optional) — max rows; default 20
 *
 * Response: {"rows": [{agent_id, role, discussion, pr, first_write_turn,
 *                       total_turns, ratio_pct, input_tok, event_id}, ...]}
 * Sorted by ratio_pct DESC (worst offenders first).
 *
 * Mirrors: backend/rpc/stats_pre_write_burn.pre_write_burn_rows()
 *          backend/rpc/stats_pre_write_burn.handle()
 */
export async function handlePreWriteBurn(params: Record<string, unknown>): Promise<unknown> {
  const limit = parseInt(String(params["limit"] ?? 20), 10) || 20;

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { rows: [] };
  }

  try {
    // LIMIT must be interpolated as a validated integer (not a bound param)
    // because DuckDB's prepared statement API only binds varchars here.
    // safe: limit is already parseInt'd above.
    const rows = await queryDicts(
      h,
      `
      SELECT
          agent_id,
          role,
          discussion,
          pr,
          first_write_turn,
          total_turns,
          ROUND(first_write_turn * 1.0 / total_turns * 100, 1) AS ratio_pct,
          input_tok,
          event_id
      FROM agent_run
      WHERE role = 'executor'
        AND first_write_turn IS NOT NULL
        AND total_turns IS NOT NULL
        AND total_turns > 0
        AND (first_write_turn * 1.0 / total_turns) > 0.10
      ORDER BY ratio_pct DESC
      LIMIT ${limit}
      `
    );

    return { rows };
  } catch {
    return { rows: [] };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats.cosmetic_blocks
// ---------------------------------------------------------------------------

/**
 * Resolve the hook-events directory.
 * Priority: AF_HOOK_EVENTS_DIR env → AUTONOMOUS_TEAM_STATE_DIR/hook-events
 *           → <REPO_ROOT>/.autonomous-team/hook-events (repo-relative fallback)
 *
 * Mirrors Python's backend/stats/cosmetic_blocks.py _default_hook_events_dir().
 */
function hookEventsDir(): string {
  const env = process.env.AF_HOOK_EVENTS_DIR;
  if (env) return env;

  const stateDir = process.env.AUTONOMOUS_TEAM_STATE_DIR;
  if (stateDir) return join(stateDir, "hook-events");

  // Walk up from ts-backend/src/rpc/stats.ts → 4 levels = repo root
  // repo_root/.autonomous-team/hook-events
  // In production AF: AF_REPO_ROOT env takes priority.
  const repoRoot = process.env.AF_REPO_ROOT
    || join(new URL(import.meta.url).pathname, "..", "..", "..", "..", "..");
  return join(repoRoot, ".autonomous-team", "hook-events");
}

/**
 * Parse a cosmetic-blocks JSONL line into an entry with a ts field.
 * Returns null when the line is malformed or ts is unparseable.
 */
function parseBlockEntry(line: string): { ts: Date; tsStr: string } | null {
  line = line.trim();
  if (!line) return null;
  let entry: Record<string, unknown>;
  try {
    entry = JSON.parse(line) as Record<string, unknown>;
  } catch {
    return null;
  }
  const tsStr = (entry["ts"] as string | undefined) ?? "";
  if (!tsStr) return null;
  try {
    return { ts: new Date(tsStr.replace("Z", "+00:00")), tsStr };
  } catch {
    return null;
  }
}

/**
 * Return hourly cosmetic block counts for the last 7 days.
 * Only hours with at least one block are included. Returns [] when no logs exist.
 *
 * Mirrors: backend/stats/cosmetic_blocks.blocks_per_hour()
 */
function cosmeticBlocksPerHour(eventsDir: string, sinceDays: number = 7): Array<{ hour_iso: string; count: number }> {
  const cutoff = new Date(Date.now() - sinceDays * 24 * 3600 * 1000);
  const hourly: Map<string, number> = new Map();

  for (let dayOffset = 0; dayOffset <= sinceDays; dayOffset++) {
    const day = new Date(Date.now() - dayOffset * 24 * 3600 * 1000);
    const dateStr = day.toISOString().slice(0, 10); // YYYY-MM-DD
    const logFile = join(eventsDir, `cosmetic-blocks-${dateStr}.jsonl`);
    if (!existsSync(logFile)) continue;

    let content: string;
    try {
      content = readFileSync(logFile, "utf-8");
    } catch {
      continue;
    }

    for (const line of content.split("\n")) {
      const entry = parseBlockEntry(line);
      if (!entry) continue;
      if (entry.ts < cutoff) continue;

      // Bucket to hour — "YYYY-MM-DDTHH:00:00Z"
      const d = entry.ts;
      const hourKey = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}T${String(d.getUTCHours()).padStart(2, "0")}:00:00Z`;
      hourly.set(hourKey, (hourly.get(hourKey) ?? 0) + 1);
    }
  }

  return Array.from(hourly.entries())
    .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
    .map(([hour_iso, count]) => ({ hour_iso, count }));
}

/**
 * Return total cosmetic block count in the last 24 hours.
 *
 * Mirrors: backend/stats/cosmetic_blocks.total_blocks_24h()
 */
function cosmeticTotalBlocks24h(eventsDir: string): number {
  const cutoff = new Date(Date.now() - 24 * 3600 * 1000);
  let total = 0;

  for (let dayOffset = 0; dayOffset < 2; dayOffset++) {
    const day = new Date(Date.now() - dayOffset * 24 * 3600 * 1000);
    const dateStr = day.toISOString().slice(0, 10);
    const logFile = join(eventsDir, `cosmetic-blocks-${dateStr}.jsonl`);
    if (!existsSync(logFile)) continue;

    let content: string;
    try {
      content = readFileSync(logFile, "utf-8");
    } catch {
      continue;
    }

    for (const line of content.split("\n")) {
      const entry = parseBlockEntry(line);
      if (!entry) continue;
      if (entry.ts >= cutoff) total++;
    }
  }
  return total;
}

/**
 * stats.cosmetic_blocks handler.
 *
 * Params: none (project routing is Python-side only; TS serves AF default)
 *
 * Response: {
 *   "total_24h": int,
 *   "hourly_7d": [{"hour_iso": str, "count": int}, ...]  — oldest first
 * }
 *
 * Mirrors: backend/rpc/stats_cosmetic_blocks.handle()
 */
export function handleCosmeticBlocks(_params: Record<string, unknown>): unknown {
  const eventsDir = hookEventsDir();
  return {
    total_24h: cosmeticTotalBlocks24h(eventsDir),
    hourly_7d: cosmeticBlocksPerHour(eventsDir),
  };
}

// ---------------------------------------------------------------------------
// stats.loop_idle_ratio
// ---------------------------------------------------------------------------

/**
 * Resolve the loop-metrics.jsonl path.
 * Python: Path(__file__).resolve().parent.parent / ".autonomous-team" / "loop-metrics.jsonl"
 * TS: AF_LOOP_METRICS_PATH env (test override) → repo-relative default.
 */
function loopMetricsPath(): string {
  const env = process.env.AF_LOOP_METRICS_PATH;
  if (env) return env;

  const repoRoot = process.env.AF_REPO_ROOT
    || join(new URL(import.meta.url).pathname, "..", "..", "..", "..", "..");
  return join(repoRoot, ".autonomous-team", "loop-metrics.jsonl");
}

/**
 * Return fraction of /loop iterations in the last 24h where agents_spawned == 0.
 *
 * Response: {"ratio": float|null, "idle_count": int, "sample_size": int}
 * ratio is null when sample_size < 5.
 *
 * Mirrors: backend/stats_writer.loop_idle_ratio_24h()
 *          backend/rpc/stats_loop_idle_ratio.handle()
 */
export function handleLoopIdleRatio(_params: Record<string, unknown>): unknown {
  const metricsPath = loopMetricsPath();

  if (!existsSync(metricsPath)) {
    return { ratio: null, idle_count: 0, sample_size: 0 };
  }

  const cutoff = new Date(Date.now() - 24 * 3600 * 1000);
  let total = 0;
  let idleCount = 0;

  let content: string;
  try {
    content = readFileSync(metricsPath, "utf-8");
  } catch {
    return { ratio: null, idle_count: 0, sample_size: 0 };
  }

  for (const raw of content.split("\n")) {
    const line = raw.trim();
    if (!line) continue;

    let row: Record<string, unknown>;
    try {
      row = JSON.parse(line) as Record<string, unknown>;
    } catch {
      continue;
    }

    const tsStr = (row["timestamp"] as string | undefined)
      || (row["ts"] as string | undefined)
      || "";
    if (!tsStr) continue;

    let ts: Date;
    try {
      ts = new Date(tsStr.replace("Z", "+00:00"));
      if (isNaN(ts.getTime())) continue;
    } catch {
      continue;
    }

    if (ts < cutoff) continue;

    // Python: if row.get("origin") == "test": continue
    if (row["origin"] === "test") continue;

    total++;
    // Python: is_idle = bool(row.get("idle", False)) or int(row.get("agents_spawned", -1)) == 0
    const isIdle = Boolean(row["idle"])
      || (parseInt(String(row["agents_spawned"] ?? -1), 10) === 0);
    if (isIdle) idleCount++;
  }

  if (total < 5) {
    return { ratio: null, idle_count: idleCount, sample_size: total };
  }

  return { ratio: idleCount / total, idle_count: idleCount, sample_size: total };
}

// ---------------------------------------------------------------------------
// stats.parity_trend
// ---------------------------------------------------------------------------

/**
 * Resolve the parity-history.jsonl path.
 * Mirrors Python's PARITY_HISTORY from backend/state_paths.py:
 *   PARITY_HISTORY_PATH env → <repo_root>/.autonomous-team/parity-history.jsonl
 */
function parityHistoryPath(): string {
  const env = process.env.PARITY_HISTORY_PATH;
  if (env) return env;

  const repoRoot = process.env.AF_REPO_ROOT
    || join(new URL(import.meta.url).pathname, "..", "..", "..", "..", "..");
  return join(repoRoot, ".autonomous-team", "parity-history.jsonl");
}

/**
 * Return per-role parity-experiment trend data.
 *
 * Params:
 *   limit (int, optional) — max recent runs; default 20
 *
 * Response: {
 *   "runs": [{"ts", "overall": {...}, "per_role": [...]}, ...],
 *   "total_runs": int,
 *   "history_path": str
 * }
 *
 * Mirrors: backend/rpc/stats_parity_trend.handle()
 */
export function handleParityTrend(params: Record<string, unknown>): unknown {
  const limit = parseInt(String(params["limit"] ?? 20), 10) || 20;
  const histPath = parityHistoryPath();

  if (!existsSync(histPath)) {
    return { runs: [], total_runs: 0, history_path: histPath };
  }

  let content: string;
  try {
    content = readFileSync(histPath, "utf-8");
  } catch {
    return { runs: [], total_runs: 0, history_path: histPath };
  }

  const records: unknown[] = [];
  for (const raw of content.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    try {
      records.push(JSON.parse(line));
    } catch {
      // Skip malformed lines — mirrors Python behavior
      continue;
    }
  }

  const total = records.length;
  // Python: recent = records[-limit:] if limit > 0 else records
  const recent = limit > 0 ? records.slice(-limit) : records;

  return { runs: recent, total_runs: total, history_path: histPath };
}
