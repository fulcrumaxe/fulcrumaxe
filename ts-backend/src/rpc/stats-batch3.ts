/**
 * rpc/stats-batch3.ts — Native TS implementations of stats.* RPC methods (batch 3).
 *
 * Mirrors the following Python RPC handlers exactly (1:1 parity):
 *   - stats.freshness_list   → handleFreshnessList()
 *   - stats.weekly_velocity  → handleWeeklyVelocity()
 *   - stats.sdk_vs_cc        → handleSdkVsCc()
 *   - stats_duckdb_writers   → handleDuckdbWriters()
 *   - stats.dial_usage       → handleDialUsage()
 *   - stats.dial_rejections  → handleDialRejections()
 *   - stats.analyst_findings → handleAnalystFindings()
 *   - stats.verdict_overturns → handleVerdictOverturns()
 *
 * All handlers are additive — Python runtime code is not modified.
 * Data sources per method:
 *   freshness_list    — DuckDB metric_event: SELECT metric, MAX(ts)
 *   weekly_velocity   — gh pr list subprocess (same as Python's backend/stats/weekly_velocity.py)
 *   sdk_vs_cc         — DuckDB agent_run + inline cost_pricing (RATE_CARD inline)
 *   stats_duckdb_writers — lsof subprocess (same as Python's backend/stats/duckdb_writers.py)
 *   dial_usage        — <STATE_DIR>/dial-registry.json + <STATE_DIR>/audit.jsonl
 *   dial_rejections   — <STATE_DIR>/audit.jsonl + <REPO>/.autonomous-team/hook-events/blocks-*.jsonl
 *   analyst_findings  — <REPO>/.autonomous-team/run-reports/*.json
 *   verdict_overturns — DuckDB metric_event: verdict_overturn + role_verdict metrics
 *
 * Skipped (left in PROXY_METHODS, complex Python deps):
 *   stats.sdk_lane       — depends on CreditTracker (sdk_credit.json) +
 *                          billing_regime + credentials.json; complex combo
 *   stats.cost_per_outcome — depends on CostTracker which reads SQLite blackboard
 *                            (state.db) via complex key patterns; not portworthy
 *
 * Design notes:
 *   - weekly_velocity uses execa/child_process for `gh pr list` (same as Python).
 *     No GH_TOKEN injection — relies on gh CLI credentials from the environment.
 *   - sdk_vs_cc inlines the cost_pricing RATE_CARD (exact same values as Python).
 *   - dial_usage reads dial-registry.json directly — mirrors Python's _load_registry().
 *   - dial_rejections path resolution mirrors Python exactly:
 *     _REPO_ROOT = __file__.parent.parent.parent → .autonomous-team/hook-events
 *   - analyst_findings reads the latest *.json from run-reports/ dir (newest filename).
 *   - verdict_overturns uses NOW() - INTERVAL 24 HOURS (same as Python's query).
 */

import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { openReadConn, closeConn, queryDicts } from "../duckdb-helpers.js";
import { stateDir as sharedStateDir } from "../config/state-paths.js";
import { resolveRepo } from "../config/repo.js";

// ---------------------------------------------------------------------------
// Shared path helpers
// ---------------------------------------------------------------------------

/** Resolve the runtime state directory (mirrors backend/state_paths.py / config/state-paths.ts). */
function stateDir(): string {
  return sharedStateDir();
}

/**
 * Resolve the DuckDB stats path (mirrors duckdb-helpers.ts dbPath()).
 * Kept here as a string resolver for subprocess-based code that needs the path.
 */
function dbPath(): string {
  const env = process.env.STATS_DB_PATH;
  if (env) return env;
  return join(stateDir(), "stats.duckdb");
}

/**
 * Resolve the repo root.
 * Priority: AF_REPO_ROOT → AUTONOMOUS_TEAM_DIR/.. → walk-up from import.meta.url.
 * This file lives at: ts-backend/src/rpc/stats-batch3.ts (4 levels below repo root).
 * In a git worktree the walk-up gives the worktree dir, not the main repo.
 * Set AF_REPO_ROOT or AUTONOMOUS_TEAM_DIR in production to avoid that.
 */
function repoRoot(): string {
  if (process.env.AF_REPO_ROOT) return process.env.AF_REPO_ROOT;
  // AUTONOMOUS_TEAM_DIR = <repo_root>/.autonomous-team → parent is repo root
  if (process.env.AUTONOMOUS_TEAM_DIR) return join(process.env.AUTONOMOUS_TEAM_DIR, "..");
  // Fallback: 4 levels up from this file — correct in main repo, may be wrong in worktrees
  const thisFile = new URL(import.meta.url).pathname;
  return join(thisFile, "..", "..", "..", "..", "..");
}

/**
 * Hook-events directory.  Resolution: AF_HOOK_EVENTS_DIR env → AUTONOMOUS_TEAM_DIR/hook-events
 * → repo-root/.autonomous-team/hook-events (via repoRoot()).
 * Mirrors Python: HOOK_EVENTS_DIR = _REPO_ROOT / ".autonomous-team" / "hook-events".
 */
function hookEventsDir(): string {
  if (process.env.AF_HOOK_EVENTS_DIR) return process.env.AF_HOOK_EVENTS_DIR;
  if (process.env.AUTONOMOUS_TEAM_DIR) return join(process.env.AUTONOMOUS_TEAM_DIR, "hook-events");
  return join(repoRoot(), ".autonomous-team", "hook-events");
}

// ---------------------------------------------------------------------------
// stats.freshness_list
// ---------------------------------------------------------------------------

/**
 * Return stats-metric freshness for all distinct metric_name rows in DuckDB.
 *
 * Response: {
 *   "rows": [{"metric_name": str, "last_ts": str, "age_seconds": int}, ...],
 *   "warn_age_seconds": int,  -- 7200 (2h)
 *   "bug_age_seconds": int    -- 86400 (24h)
 * }
 *
 * Mirrors: backend/stats_freshness_watchdog.check() +
 *          backend/rpc/stats_freshness.handle()
 *
 * Python constants (from stats_freshness_watchdog.py):
 *   WARN_AGE_SECONDS = 7200
 *   BUG_AGE_SECONDS  = 86400
 */
const WARN_AGE_SECONDS = 7200;
const BUG_AGE_SECONDS = 86400;

export async function handleFreshnessList(
  _params: Record<string, unknown>
): Promise<unknown> {
  const empty = { rows: [], warn_age_seconds: WARN_AGE_SECONDS, bug_age_seconds: BUG_AGE_SECONDS };

  let h;
  try {
    h = await openReadConn();
  } catch {
    return empty;
  }

  try {
    // Mirrors _query_freshness(): SELECT metric AS metric_name, MAX(ts) AS last_ts
    // FROM metric_event GROUP BY metric
    const rows = await queryDicts(
      h,
      `SELECT metric AS metric_name, MAX(ts) AS last_ts
       FROM metric_event
       GROUP BY metric`
    );

    const now = Date.now();
    const result: Array<{ metric_name: string; last_ts: string; age_seconds: number }> = [];

    for (const row of rows) {
      // last_ts comes back as ISO string from rowToDict/tsToIso
      const lastTsStr = row["last_ts"] as string | null;
      if (!lastTsStr) continue;

      // Parse as UTC (Python does the same: last_ts_aware = last_ts.astimezone(UTC))
      let lastTsMs: number;
      try {
        lastTsMs = new Date(lastTsStr).getTime();
        if (isNaN(lastTsMs)) continue;
      } catch {
        continue;
      }

      const ageSeconds = Math.floor((now - lastTsMs) / 1000);

      result.push({
        metric_name: (row["metric_name"] as string) ?? "",
        last_ts: lastTsStr,
        age_seconds: ageSeconds,
      });
    }

    return {
      rows: result,
      warn_age_seconds: WARN_AGE_SECONDS,
      bug_age_seconds: BUG_AGE_SECONDS,
    };
  } catch {
    return empty;
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats.weekly_velocity
// ---------------------------------------------------------------------------

/**
 * Return PRs merged in the last 7 days with per-day sparkline and trend.
 *
 * Params: {"project": str} (omit/null for AF default)
 *
 * Response: {
 *   "applicable": bool,    -- false when no PRs in 14d
 *   "total": int,
 *   "by_day": [{"date": "YYYY-MM-DD", "count": int}, ...7 entries],
 *   "window_start": "YYYY-MM-DDTHH:MM:SSZ",
 *   "window_end":   "YYYY-MM-DDTHH:MM:SSZ",
 *   "prev_total": int,
 *   "trend_pct":  int
 * }
 *
 * Mirrors: backend/stats/weekly_velocity.weekly_velocity() +
 *          backend/rpc/stats_weekly_velocity.handle()
 *
 * Notes:
 *   - "project" param is accepted but ignored (TS backend serves AF only,
 *     same as how batch 2 methods handle project-scoped params).
 *   - Calls `gh pr list` with the AF repo hardcoded (same default as Python
 *     when project/repo is None → REPO from backend._repo).
 *   - Timeout: 15s (matches Python subprocess timeout).
 */

const AF_REPO = resolveRepo();

/** Format a Date as "YYYY-MM-DDTHH:MM:SSZ". */
function fmtIsoZ(d: Date): string {
  return d.toISOString().replace(/\.\d+Z$/, "Z");
}

/** Build a list of 7 daily bucket dicts from windowStart. */
function build7DayBuckets(
  prs: Array<{ mergedAt: string }>,
  windowStart: Date
): Array<{ date: string; count: number }> {
  const buckets: Map<string, number> = new Map();
  for (let i = 0; i < 7; i++) {
    const day = new Date(windowStart.getTime() + i * 86400 * 1000);
    const key = day.toISOString().slice(0, 10);
    buckets.set(key, 0);
  }

  for (const pr of prs) {
    const mergedAt = pr.mergedAt;
    if (!mergedAt) continue;
    try {
      const d = new Date(mergedAt);
      const key = d.toISOString().slice(0, 10);
      if (buckets.has(key)) {
        buckets.set(key, (buckets.get(key) ?? 0) + 1);
      }
    } catch {
      continue;
    }
  }

  return Array.from(buckets.entries())
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([date, count]) => ({ date, count }));
}

export function handleWeeklyVelocity(
  _params: Record<string, unknown>
): unknown {
  const now = new Date();
  const windowEnd = now;
  // window: now-6d..today inclusive (7 days), matching Python: timedelta(days=6)
  const windowStart = new Date(now.getTime() - 6 * 86400 * 1000);
  const priorStart = new Date(now.getTime() - 13 * 86400 * 1000);
  const since14dStr = priorStart.toISOString().slice(0, 10);

  let allPrs: Array<{ mergedAt: string }> = [];
  try {
    const out = execFileSync(
      "gh",
      [
        "pr", "list",
        "--repo", AF_REPO,
        "--state", "merged",
        "--search", `merged:>=${since14dStr}`,
        "--json", "number,mergedAt",
        "--limit", "500",
      ],
      { encoding: "utf-8", timeout: 15000 }
    );
    const parsed = JSON.parse(out.trim()) as Array<{ mergedAt: string }>;
    allPrs = Array.isArray(parsed) ? parsed : [];
  } catch {
    // gh failure or timeout — return empty-window response
    return {
      applicable: false,
      total: 0,
      by_day: build7DayBuckets([], windowStart),
      window_start: fmtIsoZ(windowStart),
      window_end: fmtIsoZ(windowEnd),
      prev_total: 0,
      trend_pct: 0,
    };
  }

  const currentPrs: Array<{ mergedAt: string }> = [];
  const priorPrs: Array<{ mergedAt: string }> = [];

  for (const pr of allPrs) {
    const mergedAtStr = pr.mergedAt ?? "";
    if (!mergedAtStr) continue;
    try {
      const mergedAt = new Date(mergedAtStr);
      if (mergedAt >= windowStart) {
        currentPrs.push(pr);
      } else if (mergedAt >= priorStart) {
        priorPrs.push(pr);
      }
    } catch {
      continue;
    }
  }

  const byDay = build7DayBuckets(currentPrs, windowStart);
  const total = currentPrs.length;
  const prevTotal = priorPrs.length;
  // Python: round((total - prev_total) / max(prev_total, 1) * 100)
  const trendPct = Math.round(((total - prevTotal) / Math.max(prevTotal, 1)) * 100);
  const applicable = total > 0 || prevTotal > 0;

  return {
    applicable,
    total,
    by_day: byDay,
    window_start: fmtIsoZ(windowStart),
    window_end: fmtIsoZ(windowEnd),
    prev_total: prevTotal,
    trend_pct: trendPct,
  };
}

// ---------------------------------------------------------------------------
// stats.sdk_vs_cc
// ---------------------------------------------------------------------------

/**
 * Return per-role SDK vs CC comparison from agent_run.
 *
 * Response: {
 *   "rows": [{"role", "route", "run_count", "median_input_tok",
 *              "median_output_tok", "pass_rate", "cost_per_success_usd"}, ...],
 *   "has_routed_via": bool,
 *   "generated_at": "ISO",
 *   "error": str|null
 * }
 *
 * Mirrors: backend/stats/sdk_vs_cc.sdk_vs_cc_by_role()
 *          backend/rpc/stats_sdk_vs_cc.handle()
 *
 * Cost pricing (mirrors backend/cost_pricing.py RATE_CARD):
 *   claude-sonnet-4-6: input=$3/1M, output=$15/1M,
 *                      cache_write=$3.75/1M, cache_read=$0.30/1M
 *   _default: same rates (fallback for unknown/null model)
 */

// Mirrors Python RATE_CARD exactly
const RATE_CARD: Record<string, Record<string, number>> = {
  "claude-sonnet-4-6": { input: 3.00, output: 15.00, cache_write: 3.75, cache_read: 0.30 },
  "_default":          { input: 3.00, output: 15.00, cache_write: 3.75, cache_read: 0.30 },
};

function costUsd(
  inputTok: number,
  outputTok: number,
  cacheRead: number,
  cacheWrite: number,
  model: string | null
): number {
  const key = (model && RATE_CARD[model]) ? model : "_default";
  const rates = RATE_CARD[key];
  const _1m = 1_000_000.0;
  const cost =
    inputTok  * rates.input        / _1m +
    outputTok * rates.output       / _1m +
    cacheRead * rates.cache_read   / _1m +
    cacheWrite * rates.cache_write / _1m;
  // Python: round(cost, 8)
  return Math.round(cost * 1e8) / 1e8;
}

export async function handleSdkVsCc(
  _params: Record<string, unknown>
): Promise<unknown> {
  const generatedAt = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const emptyResult = {
    rows: [],
    has_routed_via: false,
    generated_at: generatedAt,
    error: null as string | null,
  };

  let h;
  try {
    h = await openReadConn();
  } catch (err) {
    return { ...emptyResult, error: `cannot open stats.duckdb: ${err}` };
  }

  try {
    // Check if routed_via column exists
    const colRows = await queryDicts(
      h,
      `SELECT column_name FROM information_schema.columns
       WHERE table_name='agent_run'`
    );
    const colNames = new Set(colRows.map(r => r["column_name"] as string));
    const hasRoutedVia = colNames.has("routed_via");

    if (!hasRoutedVia) {
      return emptyResult;
    }

    // Adaptive expressions for optional columns
    const cacheReadExpr  = colNames.has("cache_read")  ? "COALESCE(cache_read,  0)" : "0";
    const cacheWriteExpr = colNames.has("cache_write") ? "COALESCE(cache_write, 0)" : "0";
    const modelExpr      = colNames.has("model")       ? "FIRST(model)"              : "NULL";

    const rows = await queryDicts(
      h,
      `
      SELECT
          role,
          routed_via,
          COUNT(*)                                          AS run_count,
          MEDIAN(input_tok)                                 AS median_input_tok,
          MEDIAN(output_tok)                                AS median_output_tok,
          AVG(CASE WHEN verdict IN ('done', 'pass') THEN 1.0 ELSE 0.0 END)
                                                            AS pass_rate,
          SUM(CASE WHEN verdict IN ('done', 'pass')
                   THEN COALESCE(input_tok,  0) ELSE 0 END) AS pass_input_tok,
          SUM(CASE WHEN verdict IN ('done', 'pass')
                   THEN COALESCE(output_tok, 0) ELSE 0 END) AS pass_output_tok,
          SUM(CASE WHEN verdict IN ('done', 'pass')
                   THEN ${cacheReadExpr}          ELSE 0 END) AS pass_cache_read,
          SUM(CASE WHEN verdict IN ('done', 'pass')
                   THEN ${cacheWriteExpr}         ELSE 0 END) AS pass_cache_write,
          SUM(CASE WHEN verdict IN ('done', 'pass') THEN 1 ELSE 0 END)
                                                            AS pass_count,
          ${modelExpr}                                      AS model_sample
      FROM agent_run
      WHERE routed_via IS NOT NULL
        AND role IS NOT NULL
      GROUP BY role, routed_via
      ORDER BY role, routed_via
      `
    );

    const toInt = (v: unknown): number => {
      if (v === null || v === undefined) return 0;
      if (typeof v === "bigint") return Number(v);
      // Python's int() truncates toward zero (not rounds). For count columns this
      // makes no difference, but for MEDIAN values (e.g. 388.5) Python returns 388
      // while Math.round() would return 389. Use Math.trunc() to match Python exactly.
      return Math.trunc(Number(v));
    };
    const toFloat = (v: unknown): number | null => {
      if (v === null || v === undefined) return null;
      const n = typeof v === "number" ? v : Number(v);
      return isFinite(n) ? n : null;
    };

    const resultRows = rows.map(row => {
      const passCount  = toInt(row["pass_count"]);
      const runCount   = toInt(row["run_count"]);
      const passInTok  = toInt(row["pass_input_tok"]);
      const passOutTok = toInt(row["pass_output_tok"]);
      const passCacheR = toInt(row["pass_cache_read"]);
      const passCacheW = toInt(row["pass_cache_write"]);
      const modelSample = (row["model_sample"] as string | null) ?? null;

      let costPerSuccess: number | null = null;
      if (passCount > 0) {
        const totalCost = costUsd(passInTok, passOutTok, passCacheR, passCacheW, modelSample);
        // Python: round(total_pass_cost / pass_count, 8)
        costPerSuccess = Math.round((totalCost / passCount) * 1e8) / 1e8;
      }

      const passRate = toFloat(row["pass_rate"]);

      return {
        role: (row["role"] as string | null) ?? "unknown",
        route: (row["routed_via"] as string | null) ?? "unknown",
        run_count: runCount,
        median_input_tok: row["median_input_tok"] !== null
          ? toInt(row["median_input_tok"])
          : null,
        median_output_tok: row["median_output_tok"] !== null
          ? toInt(row["median_output_tok"])
          : null,
        // Python: round(float(pass_rate), 4) if pass_rate is not None else None
        pass_rate: passRate !== null ? Math.round(passRate * 1e4) / 1e4 : null,
        cost_per_success_usd: costPerSuccess,
      };
    });

    return {
      rows: resultRows,
      has_routed_via: true,
      generated_at: generatedAt,
      error: null,
    };
  } catch (err) {
    return {
      rows: [],
      has_routed_via: false,
      generated_at: generatedAt,
      error: String(err),
    };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// stats_duckdb_writers
// ---------------------------------------------------------------------------

/**
 * Return processes holding an open FD on stats.duckdb (via lsof).
 *
 * Response: {
 *   "writers": [{"pid": int, "cmd": str, "age_seconds": float|null, "fd_mode": str}],
 *   "checked_at": "ISO",
 *   "warning": str|null
 * }
 *
 * Mirrors: backend/stats/duckdb_writers.get_duckdb_writers() +
 *          backend/rpc/stats_duckdb_writers.handle()
 *
 * Notes:
 *   - lsof -F pcfan format is parsed identically to Python's _parse_lsof_output().
 *   - Process age uses /proc/<pid>/stat on Linux (same as Python's _process_age_seconds()).
 *   - lsof exits 1 when no processes hold the file — that's normal; stdout may be empty.
 */

/** Get process age in seconds via /proc/<pid>/stat (Linux only). */
function processAgeSeconds(pid: number): number | null {
  const procStat = `/proc/${pid}/stat`;
  if (!existsSync(procStat)) return null;
  try {
    const content = readFileSync(procStat, "ascii");
    const fields = content.split(" ");
    // Field 22 (index 21) is starttime in clock ticks since boot.
    const hz = 100; // SC_CLK_TCK default; Linux typically 100 or 250
    const uptimePath = "/proc/uptime";
    if (!existsSync(uptimePath)) return null;
    const uptime = parseFloat(readFileSync(uptimePath, "ascii").split(" ")[0]);
    const startTicks = parseInt(fields[21], 10);
    const startSecsSinceBoot = startTicks / hz;
    return Math.max(uptime - startSecsSinceBoot, 0);
  } catch {
    return null;
  }
}

interface LsofRow {
  pid: number;
  cmd: string;
  age_seconds: number | null;
  fd_mode: string;
}

/** Parse lsof -F pcfan output into writer rows (mirrors Python's _parse_lsof_output). */
function parseLsofOutput(output: string): LsofRow[] {
  const rows: LsofRow[] = [];
  let currentPid: number | null = null;
  let currentCmd = "";
  let currentFd = "";
  let currentAccess = "";

  for (const line of output.split("\n")) {
    if (!line) continue;
    const key = line[0];
    const val = line.slice(1);

    if (key === "p") {
      currentPid = /^\d+$/.test(val) ? parseInt(val, 10) : null;
      currentCmd = "";
      currentFd = "";
      currentAccess = "";
    } else if (key === "c") {
      currentCmd = val;
    } else if (key === "f") {
      currentFd = val;
      currentAccess = ""; // reset for new fd
    } else if (key === "a") {
      currentAccess = val.trim();
    } else if (key === "n" && currentPid !== null) {
      // Translate lsof access char to fd_mode (mirrors Python exactly)
      let fdMode: string;
      if (currentAccess === "u") {
        fdMode = "rw";
      } else if (currentAccess === "r") {
        fdMode = "r";
      } else if (currentAccess === "w") {
        fdMode = "w";
      } else {
        // Non-data fd (mem, txt, cwd, rtd) — preserve type name
        fdMode = /^\d+$/.test(currentFd) ? "" : currentFd;
      }

      rows.push({
        pid: currentPid,
        cmd: currentCmd,
        age_seconds: processAgeSeconds(currentPid),
        fd_mode: fdMode,
      });
    }
  }
  return rows;
}

export function handleDuckdbWriters(
  _params: Record<string, unknown>
): unknown {
  const checkedAt = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const db = dbPath();

  if (!existsSync(db)) {
    return { writers: [], checked_at: checkedAt, warning: null };
  }

  // Resolve absolute path (matches Python: str(db_path.resolve()))
  const absPath = db; // already absolute from dbPath()

  try {
    const out = execFileSync(
      "lsof",
      ["-F", "pcfan", "--", absPath],
      { encoding: "utf-8", timeout: 5000 }
    );
    const writers = parseLsofOutput(out);
    return { writers, checked_at: checkedAt, warning: null };
  } catch (err: unknown) {
    // lsof exits 1 when no processes have the file open — stdout may still have output
    if (err && typeof err === "object" && "stdout" in err) {
      // ENOENT → lsof not found
      if ("code" in err && (err as { code: unknown }).code === "ENOENT") {
        return { writers: [], checked_at: checkedAt, warning: "lsof not found on PATH" };
      }
      // Exit code 1 with output is normal (no holders)
      const stdout = (err as { stdout: string }).stdout ?? "";
      const writers = parseLsofOutput(stdout);
      return { writers, checked_at: checkedAt, warning: null };
    }
    // Timeout or other error
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("timed out") || msg.includes("ETIMEDOUT")) {
      return { writers: [], checked_at: checkedAt, warning: "lsof timed out" };
    }
    return { writers: [], checked_at: checkedAt, warning: msg };
  }
}

// ---------------------------------------------------------------------------
// stats.dial_usage
// ---------------------------------------------------------------------------

/**
 * Return current dial levels for all classes plus 24h activity counters.
 *
 * Params: {"project_name": str} (omit/null for AF default)
 *
 * Response shape mirrors backend/stats/dial_usage.read_dial_usage():
 * {
 *   "current_dials": [{name, level, verb_label, ceiling, active_directives, ttl_revert_at},...],
 *   "last_24h": {accepted, rejected_by_reason, ceiling_violations, last_ceiling_exceeded}
 * }
 *
 * Data sources:
 *   - dial-registry.json (same schema as Python's _load_registry())
 *   - audit.jsonl: kind="dial_change" | "dial_directive_rejected"
 *
 * Notes:
 *   - project_name param is accepted but the TS backend resolves its own stateDir().
 *   - _load_registry() is mirrored here by reading dial-registry.json directly.
 *   - Verb labels mirror Python's _VERB_LABELS dict exactly.
 */

const VERB_LABELS: Record<string, string> = {
  "docs.write":         "Write docs",
  "tests.add":          "Add tests",
  "deps.bump":          "Bump deps",
  "agent.spawn":        "Spawn agents",
  "merge.standard":     "Merge (standard)",
  "merge.fast-path":    "Merge (fast-path)",
  "intent.generate":    "Generate intent",
  "methodology.change": "Change methodology",
  "external.system":    "External system",
  "sandbox.modify":     "Modify sandbox",
  "cost.spend":         "Spend budget",
  "memory.write":       "Write memory",
  "archive.move":       "Archive files",
};

interface DialEntry {
  level: number;
  ceiling: number;
  directives: Array<{ ttl_until?: string }>;
}

/** Read dial-registry.json and return the parsed registry (mirrors Python's _load_registry). */
function loadDialRegistry(state: string): Map<string, DialEntry> {
  const registryPath = join(state, "dial-registry.json");
  if (!existsSync(registryPath)) return new Map();

  let data: unknown;
  try {
    data = JSON.parse(readFileSync(registryPath, "utf-8"));
  } catch {
    return new Map();
  }

  const registry = new Map<string, DialEntry>();

  if (Array.isArray(data)) {
    // Legacy list format
    for (const entry of data as Array<Record<string, unknown>>) {
      const cls = (entry["class"] as string) ?? (entry["class_name"] as string);
      if (cls) {
        registry.set(cls, {
          level: parseInt(String(entry["level"] ?? 1), 10) || 1,
          ceiling: parseInt(String(entry["ceiling"] ?? 4), 10) || 4,
          directives: (entry["directives"] as Array<{ ttl_until?: string }>) ?? [],
        });
      }
    }
  } else if (data && typeof data === "object") {
    for (const [cls, val] of Object.entries(data as Record<string, unknown>)) {
      const v = val as Record<string, unknown>;
      if (typeof v === "object" && v) {
        registry.set(cls, {
          level: parseInt(String(v["level"] ?? 1), 10) || 1,
          ceiling: parseInt(String(v["ceiling"] ?? 4), 10) || 4,
          directives: (v["directives"] as Array<{ ttl_until?: string }>) ?? [],
        });
      }
    }
  }

  return registry;
}

export function handleDialUsage(
  _params: Record<string, unknown>
): unknown {
  const state = stateDir();
  const auditLog = join(state, "audit.jsonl");

  // Build current_dials from registry
  const registry = loadDialRegistry(state);
  const currentDials: unknown[] = [];

  for (const [className, entry] of [...registry.entries()].sort()) {
    // Find earliest TTL among directives with ttl_until
    let ttlRevertAt: string | null = null;
    const ttlTimes: Date[] = [];

    for (const d of entry.directives ?? []) {
      if (d.ttl_until) {
        try {
          const t = new Date(d.ttl_until.replace("Z", "+00:00"));
          if (!isNaN(t.getTime())) ttlTimes.push(t);
        } catch { /* skip */ }
      }
    }
    if (ttlTimes.length > 0) {
      const earliest = ttlTimes.reduce((a, b) => (a < b ? a : b));
      // Python: min(ttl_times).isoformat(timespec="seconds")
      ttlRevertAt = earliest.toISOString().replace(/\.\d+Z$/, "").replace("Z", "+00:00").replace(/\+00:00$/, "");
      // Normalize to match Python isoformat(timespec="seconds"): "YYYY-MM-DDTHH:MM:SS+00:00"
      // But since the dashboard likely expects ISO, keep as-is without trailing +00:00
      // Python: 2026-05-23T14:00:00+00:00 — match exactly
      ttlRevertAt = earliest.toISOString().replace(/\.\d{3}Z$/, "+00:00");
    }

    currentDials.push({
      name:              className,
      level:             entry.level,
      verb_label:        VERB_LABELS[className] ?? className,
      ceiling:           entry.ceiling,
      active_directives: (entry.directives ?? []).length,
      ttl_revert_at:     ttlRevertAt,
    });
  }

  // Scan audit.jsonl for last-24h activity
  const cutoff = new Date(Date.now() - 24 * 3600 * 1000);
  let accepted = 0;
  const rejectedByReason: Record<string, number> = {
    ceiling_violation:      0,
    unauthenticated_source: 0,
    invalid_level:          0,
  };
  let ceilingViolations = 0;
  let lastCeilingExceeded: { class: string; timestamp: string } | null = null;

  if (existsSync(auditLog)) {
    try {
      const content = readFileSync(auditLog, "utf-8");
      for (const rawLine of content.split("\n")) {
        const line = rawLine.trim();
        if (!line) continue;

        let row: Record<string, unknown>;
        try {
          row = JSON.parse(line) as Record<string, unknown>;
        } catch {
          continue;
        }

        const kind = (row["kind"] as string) ?? "";
        if (kind !== "dial_change" && kind !== "dial_directive_rejected") continue;

        const tsStr = (row["timestamp"] as string) ?? "";
        if (!tsStr) continue;

        let ts: Date;
        try {
          ts = new Date(tsStr.replace("Z", "+00:00"));
          if (isNaN(ts.getTime()) || ts < cutoff) continue;
        } catch {
          continue;
        }

        if (kind === "dial_change") {
          accepted++;
        } else {
          // dial_directive_rejected
          const reason = (row["reason"] as string) ?? "";
          if (reason in rejectedByReason) {
            rejectedByReason[reason]++;
          }
          if (reason === "ceiling_violation") {
            ceilingViolations++;
            if (!lastCeilingExceeded || tsStr > lastCeilingExceeded.timestamp) {
              lastCeilingExceeded = {
                class: (row["class"] as string) ?? "",
                timestamp: tsStr,
              };
            }
          }
        }
      }
    } catch { /* file read error */ }
  }

  return {
    current_dials: currentDials,
    last_24h: {
      accepted,
      rejected_by_reason: rejectedByReason,
      ceiling_violations: ceilingViolations,
      last_ceiling_exceeded: lastCeilingExceeded,
    },
  };
}

// ---------------------------------------------------------------------------
// stats.dial_rejections
// ---------------------------------------------------------------------------

/**
 * Return 24h rejection counters across directive rejections + sandbox blocks.
 *
 * Params: {"project_name": str} (omit/null for AF default)
 *
 * Response shape mirrors backend/stats/dial_rejections.read_dial_rejections():
 * {
 *   "rejected_directives_24h": {"total", "by_reason", "last_at"},
 *   "sandbox_blocks_24h": {"total", "by_kind", "last_at"},
 *   "last_rejection": {"kind", "reason_or_class", "timestamp", "cwd"} | null
 * }
 *
 * Sandbox-block category mapping (mirrors Python exactly):
 *   sandbox_block_gh_api_mutation — reason starts with "sandbox_block_gh_api_mutation"
 *   sandbox_block_untrusted_cwd   — reason == "agent_spawn_in_untrusted_cwd"
 *   sandbox_block_agent_spawn     — reason starts with "agent_spawn_in" or "claude_spawn_forbidden:"
 *
 * Notes:
 *   - top-5 + "other" bucketing mirrors Python _top5_with_other().
 *   - HOOK_EVENTS_DIR is <repo_root>/.autonomous-team/hook-events (same as Python).
 */

/** Mirror Python's _classify_sandbox_block(). */
function classifySandboxBlock(reason: string): string | null {
  if (!reason) return null;
  if (reason.startsWith("sandbox_block_gh_api_mutation")) return "sandbox_block_gh_api_mutation";
  if (reason === "agent_spawn_in_untrusted_cwd") return "sandbox_block_untrusted_cwd";
  if (reason.startsWith("agent_spawn_in") || reason.startsWith("claude_spawn_forbidden:")) {
    return "sandbox_block_agent_spawn";
  }
  return null;
}

/** Mirror Python's _top5_with_other(). */
function top5WithOther(counts: Record<string, number>): Record<string, number> {
  const entries = Object.entries(counts).sort(([, a], [, b]) => b - a);
  if (entries.length <= 5) return Object.fromEntries(entries);
  const top5 = Object.fromEntries(entries.slice(0, 5));
  const otherTotal = entries.slice(5).reduce((acc, [, v]) => acc + v, 0);
  if (otherTotal > 0) top5["other"] = otherTotal;
  return top5;
}

export function handleDialRejections(
  _params: Record<string, unknown>
): unknown {
  const state = stateDir();
  const auditLog = join(state, "audit.jsonl");
  const eventsDir = hookEventsDir();

  const now = new Date();
  const cutoff = new Date(now.getTime() - 24 * 3600 * 1000);

  // Phase 1: scan audit.jsonl for dial_directive_rejected rows
  let dirTotal = 0;
  const dirReasons: Record<string, number> = {};
  let dirLastAt: string | null = null;
  let dirLastTs: Date | null = null;

  if (existsSync(auditLog)) {
    try {
      const content = readFileSync(auditLog, "utf-8");
      for (const rawLine of content.split("\n")) {
        const line = rawLine.trim();
        if (!line) continue;

        let row: Record<string, unknown>;
        try {
          row = JSON.parse(line) as Record<string, unknown>;
        } catch {
          continue;
        }

        if (row["kind"] !== "dial_directive_rejected") continue;

        // Python uses "ts" OR "timestamp" key
        const tsStr = (row["ts"] as string) ?? (row["timestamp"] as string) ?? "";
        if (!tsStr) continue;

        let ts: Date;
        try {
          ts = new Date(tsStr.replace("Z", "+00:00"));
          if (isNaN(ts.getTime()) || ts < cutoff) continue;
        } catch {
          continue;
        }

        dirTotal++;
        const reason = (row["reason"] as string) ?? "unknown";
        dirReasons[reason] = (dirReasons[reason] ?? 0) + 1;

        if (!dirLastTs || ts > dirLastTs) {
          dirLastTs = ts;
          dirLastAt = tsStr;
        }
      }
    } catch { /* file read error */ }
  }

  const dirByReason = top5WithOther(dirReasons);

  // Phase 2: scan blocks-*.jsonl for sandbox_block_* rows
  let blkTotal = 0;
  const blkByKind: Record<string, number> = { sandbox_block_agent_spawn: 0, sandbox_block_gh_api_mutation: 0, sandbox_block_untrusted_cwd: 0 };
  let blkLastAt: string | null = null;
  let blkLastTs: Date | null = null;

  const today = now.toISOString().slice(0, 10);
  const yesterday = new Date(now.getTime() - 86400 * 1000).toISOString().slice(0, 10);

  for (const dateStr of [yesterday, today]) {
    const blocksFile = join(eventsDir, `blocks-${dateStr}.jsonl`);
    if (!existsSync(blocksFile)) continue;

    try {
      const content = readFileSync(blocksFile, "utf-8");
      for (const rawLine of content.split("\n")) {
        const line = rawLine.trim();
        if (!line) continue;

        let row: Record<string, unknown>;
        try {
          row = JSON.parse(line) as Record<string, unknown>;
        } catch {
          continue;
        }

        if (row["decision"] !== "block") continue;

        const tsStr = (row["ts"] as string) ?? "";
        if (!tsStr) continue;

        let ts: Date;
        try {
          ts = new Date(tsStr.replace("Z", "+00:00"));
          if (isNaN(ts.getTime()) || ts < cutoff) continue;
        } catch {
          continue;
        }

        const reason = (row["reason"] as string) ?? "";
        const kind = classifySandboxBlock(reason);
        if (!kind) continue;

        blkTotal++;
        blkByKind[kind] = (blkByKind[kind] ?? 0) + 1;

        if (!blkLastTs || ts > blkLastTs) {
          blkLastTs = ts;
          blkLastAt = tsStr;
        }
      }
    } catch { /* file read error */ }
  }

  // Compute last_rejection across both sources
  type Candidate = { ts: Date; kind: string; reason_or_class: string; tsStr: string; cwd: string | null };
  const candidates: Candidate[] = [];

  // From directive rejections — re-scan for last row's full data
  if (dirLastTs !== null && existsSync(auditLog)) {
    try {
      const content = readFileSync(auditLog, "utf-8");
      for (const rawLine of content.split("\n")) {
        const line = rawLine.trim();
        if (!line) continue;

        let row: Record<string, unknown>;
        try {
          row = JSON.parse(line) as Record<string, unknown>;
        } catch {
          continue;
        }

        if (row["kind"] !== "dial_directive_rejected") continue;

        const tsStr = (row["ts"] as string) ?? (row["timestamp"] as string) ?? "";
        if (!tsStr) continue;

        let ts: Date;
        try {
          ts = new Date(tsStr.replace("Z", "+00:00"));
          if (isNaN(ts.getTime()) || ts < cutoff) continue;
        } catch {
          continue;
        }

        const reasonOrClass =
          (row["reason"] as string) ??
          (row["class"] as string) ??
          "unknown";

        candidates.push({ ts, kind: "dial_directive_rejected", reason_or_class: reasonOrClass, tsStr, cwd: null });
      }
    } catch { /* ignore */ }
  }

  // From sandbox blocks
  if (blkLastTs !== null) {
    for (const dateStr of [yesterday, today]) {
      const blocksFile = join(eventsDir, `blocks-${dateStr}.jsonl`);
      if (!existsSync(blocksFile)) continue;

      try {
        const content = readFileSync(blocksFile, "utf-8");
        for (const rawLine of content.split("\n")) {
          const line = rawLine.trim();
          if (!line) continue;

          let row: Record<string, unknown>;
          try {
            row = JSON.parse(line) as Record<string, unknown>;
          } catch {
            continue;
          }

          if (row["decision"] !== "block") continue;

          const tsStr = (row["ts"] as string) ?? "";
          if (!tsStr) continue;

          let ts: Date;
          try {
            ts = new Date(tsStr.replace("Z", "+00:00"));
            if (isNaN(ts.getTime()) || ts < cutoff) continue;
          } catch {
            continue;
          }

          const reason = (row["reason"] as string) ?? "";
          const kind = classifySandboxBlock(reason);
          if (!kind) continue;

          candidates.push({ ts, kind, reason_or_class: reason, tsStr, cwd: (row["cwd"] as string | null) ?? null });
        }
      } catch { /* ignore */ }
    }
  }

  let lastRejection: { kind: string; reason_or_class: string; timestamp: string; cwd: string | null } | null = null;
  if (candidates.length > 0) {
    const best = candidates.reduce((a, b) => (a.ts > b.ts ? a : b));
    lastRejection = {
      kind: best.kind,
      reason_or_class: best.reason_or_class,
      timestamp: best.tsStr,
      cwd: best.cwd,
    };
  }

  return {
    rejected_directives_24h: {
      total: dirTotal,
      by_reason: dirByReason,
      last_at: dirLastAt,
    },
    sandbox_blocks_24h: {
      total: blkTotal,
      by_kind: blkByKind,
      last_at: blkLastAt,
    },
    last_rejection: lastRejection,
  };
}

// ---------------------------------------------------------------------------
// stats.analyst_findings
// ---------------------------------------------------------------------------

/**
 * Return latest run-analyst findings grouped by severity.
 *
 * Response shape mirrors backend/stats/analyst_findings.load():
 * {
 *   "report_at": str|null,
 *   "window": {"since": str, "until": str}|null,
 *   "runs_analyzed": int,
 *   "by_severity": {"high": [...], "medium": [...], "low": [...]},
 *   "total": int,
 *   "generated_at": "ISO",
 *   "error": null
 * }
 *
 * Report directory: <REPO_ROOT>/.autonomous-team/run-reports/
 * Latest file: lexicographically largest *.json filename (same as Python sorted(..., reverse=True)[0]).
 *
 * Mirrors: backend/stats/analyst_findings.load()
 *          backend/rpc/stats_analyst_findings.handle()
 */

const SEVERITY_ORDER = ["high", "medium", "low"] as const;

export function handleAnalystFindings(
  _params: Record<string, unknown>
): unknown {
  const generatedAt = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const empty = {
    report_at: null as string | null,
    window: null as { since: string; until: string } | null,
    runs_analyzed: 0,
    by_severity: { high: [], medium: [], low: [] } as Record<string, unknown[]>,
    total: 0,
    generated_at: generatedAt,
    error: null as string | null,
  };

  const reportsDir = join(repoRoot(), ".autonomous-team", "run-reports");
  if (!existsSync(reportsDir)) return empty;

  // Find latest *.json file — Python: sorted(glob("*.json"), reverse=True)[0]
  let jsonFiles: string[];
  try {
    jsonFiles = readdirSync(reportsDir)
      .filter(f => f.endsWith(".json"))
      .sort()
      .reverse();
  } catch {
    return empty;
  }

  if (jsonFiles.length === 0) return empty;

  let report: Record<string, unknown>;
  try {
    const content = readFileSync(join(reportsDir, jsonFiles[0]), "utf-8");
    report = JSON.parse(content) as Record<string, unknown>;
  } catch {
    return empty;
  }

  const findings = (report["findings"] as Array<Record<string, unknown>>) ?? [];
  const bySeverity: Record<string, unknown[]> = { high: [], medium: [], low: [] };

  for (const finding of findings) {
    let sev = (finding["severity"] as string) ?? "low";
    if (!SEVERITY_ORDER.includes(sev as typeof SEVERITY_ORDER[number])) sev = "low";
    bySeverity[sev].push({
      category: finding["category"] ?? "",
      severity: sev,
      title: finding["title"] ?? "",
      evidence: finding["evidence"] ?? [],
      suggested_discussion_title: finding["suggested_discussion_title"] ?? "",
      suggested_tag: finding["suggested_tag"] ?? "",
    });
  }

  return {
    report_at: (report["report_at"] as string | null) ?? null,
    window: (report["window"] as { since: string; until: string } | null) ?? null,
    runs_analyzed: (report["runs_analyzed"] as number) ?? 0,
    by_severity: bySeverity,
    total: findings.length,
    generated_at: generatedAt,
    error: null,
  };
}

// ---------------------------------------------------------------------------
// stats.verdict_overturns
// ---------------------------------------------------------------------------

/**
 * Return per-role verdict overturn rates over the last 24 hours.
 *
 * Response: {
 *   "rows": [{"role", "overturns", "total_pass", "overturn_rate", "sample_size"}, ...]
 * }
 * overturn_rate is null when sample_size < 5 (UI shows "N/A").
 * Sorted: highest overturn_rate first, null rows last.
 *
 * Mirrors: backend/verdict_overturn.overturn_rate_by_role_24h()
 *          backend/rpc/stats_verdict_overturns.handle()
 *
 * NOTE: Python uses NOW() - INTERVAL 24 HOURS (no cutoff-string placeholder).
 * Faithful mirror: use the same DuckDB expression.
 */
export async function handleVerdictOverturns(
  _params: Record<string, unknown>
): Promise<unknown> {
  let h;
  try {
    h = await openReadConn();
  } catch {
    return { rows: [] };
  }

  try {
    // Count overturn events per prior_role in the last 24h
    const overturnRows = await queryDicts(
      h,
      `
      SELECT
          json_extract_string(tags, '$.role') AS role,
          COUNT(*)                            AS overturns
      FROM metric_event
      WHERE metric = 'verdict_overturn'
        AND ts >= NOW() - INTERVAL 24 HOURS
        AND json_extract_string(tags, '$.role') IS NOT NULL
      GROUP BY json_extract_string(tags, '$.role')
      `
    );

    // Count total pass/done verdicts per role in the last 24h (denominator)
    const passRows = await queryDicts(
      h,
      `
      SELECT
          json_extract_string(tags, '$.role')   AS role,
          COUNT(*)                              AS total_pass
      FROM metric_event
      WHERE metric = 'role_verdict'
        AND ts >= NOW() - INTERVAL 24 HOURS
        AND json_extract_string(tags, '$.verdict') IN ('pass', 'done')
        AND json_extract_string(tags, '$.role') IS NOT NULL
      GROUP BY json_extract_string(tags, '$.role')
      `
    );

    const toInt = (v: unknown): number => {
      if (typeof v === "bigint") return Number(v);
      return parseInt(String(v ?? 0), 10) || 0;
    };

    const overturnMap: Record<string, number> = {};
    for (const row of overturnRows) {
      const role = row["role"] as string | null;
      if (role) overturnMap[role] = toInt(row["overturns"]);
    }

    const passMap: Record<string, number> = {};
    for (const row of passRows) {
      const role = row["role"] as string | null;
      if (role) passMap[role] = toInt(row["total_pass"]);
    }

    // Union of all roles
    const allRoles = new Set([...Object.keys(overturnMap), ...Object.keys(passMap)]);
    const result: Array<{
      role: string;
      overturns: number;
      total_pass: number;
      overturn_rate: number | null;
      sample_size: number;
    }> = [];

    for (const role of allRoles) {
      const overturns = overturnMap[role] ?? 0;
      const totalPass = passMap[role] ?? 0;
      const sampleSize = totalPass;
      // Python: overturn_rate = (overturns / total_pass) if sample_size >= 5 else None
      const overturnRate = sampleSize >= 5 ? overturns / totalPass : null;
      result.push({ role, overturns, total_pass: totalPass, overturn_rate: overturnRate, sample_size: sampleSize });
    }

    // Sort: highest overturn_rate first, None rows last
    // Python: key=lambda r: (r["overturn_rate"] is None, -(r["overturn_rate"] if ... else 0))
    result.sort((a, b) => {
      const aNull = a.overturn_rate === null;
      const bNull = b.overturn_rate === null;
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      return b.overturn_rate! - a.overturn_rate!;
    });

    return { rows: result };
  } catch {
    return { rows: [] };
  } finally {
    closeConn(h);
  }
}
