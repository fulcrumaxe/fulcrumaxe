/**
 * POST /rpc — JSON-RPC 2.0 dispatch (P6a: read-only methods + dispatch layer).
 *
 * Parity contract (mirrors backend/routers/rpc.py exactly):
 *   - Invalid JSON body  → HTTP 400, jsonrpc error envelope (code -32000)
 *   - Auth failure       → HTTP 401, jsonrpc error envelope (code -32000)
 *   - Spawn-guard hit    → HTTP 200, jsonrpc error {code:-32000}
 *   - Unknown method     → HTTP 200, jsonrpc error {code:-32601}
 *   - Handler exception  → HTTP 200, jsonrpc error {code: exc.rpc_code or -32000}
 *   - Success            → HTTP 200, jsonrpc result envelope
 *
 * Auth model:
 *   Reads RPC token from .autonomous-team/dashboard-token (same file Python uses).
 *   This is SEPARATE from AF_API_AUTH_KEY (the REST bearer key).
 *   /rpc is listed in PUBLIC_ROUTES so default-deny lets the request through;
 *   this handler then self-authenticates against the RPC token.
 *   Accepts: Authorization: Bearer <token>  OR  ?token=<token> query param.
 *   Fail-closed: missing/empty token file → 401 ALL requests.
 *
 * P6a scope (read-only methods only):
 *   Two categories of read-only handler:
 *     A) Natively implemented in TS — stats.summary, stats.series (reuse DuckDB reader)
 *     B) Proxied to Python FastAPI /rpc — all other read-only methods.
 *        The proxy preserves the exact JSON-RPC envelope from Python, so response
 *        shape parity is guaranteed by construction for those methods.
 *
 * P6b (mutating — native in TS, temp-copy parity tested):
 *   dial.set, auth_retry.record, fleet.discovery_ack
 *
 * Still deferred (spawn/kill real loops — return method-not-found):
 *   loop.start, loop.stop
 *
 * The proxy approach for non-stats read-only methods is the simplest faithful
 * mirror: Python is the reference implementation; forwarding to it guarantees
 * byte-identical responses. The alternative (re-implementing CostTracker,
 * kpi_engine, circuit_breaker, gh-CLI calls, etc.) would be P7-scope work.
 * This is documented as the intended approach in the PR description.
 */

import type { Context } from "hono";
import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { openReadConn, closeConn, queryDicts } from "../duckdb-helpers.js";
import { bigIntToExact } from "../normalizer.js";
import {
  handleByRole,
  handlePercentiles,
  handleStuck,
  handleRoundtrip,
  handleActiveOverTime,
  handleRecent,
} from "../rpc/runs.js";
import {
  handleTeamLeadTokens,
  handleCostSpikeHistory,
  handleRoleSuccessRate,
  handleRoleRetryRate,
  handleAvgFixRoundsPerPr,
  handlePreWriteBurn,
  handleCosmeticBlocks,
  handleLoopIdleRatio,
  handleParityTrend,
} from "../rpc/stats.js";
import {
  handleFreshnessList,
  handleWeeklyVelocity,
  handleSdkVsCc,
  handleDuckdbWriters,
  handleDialUsage,
  handleDialRejections,
  handleAnalystFindings,
  handleVerdictOverturns,
} from "../rpc/stats-batch3.js";
import {
  handleLoopList,
  handleLoopEvents,
  handleAgentsTail,
  handleLoopTimeline,
  handleLoopIterationDetail,
  handleDashboardGatesSnapshot,
} from "../rpc/loop.js";
import {
  handleDialList,
  handleAuthRetrySummary,
  handleCircuitBreakerSummary,
  handleCircuitBreakerHistory,
  handleKpiHistory,
  handleKpiCycleTime,
  handleCostPerDiscussion,
  handleCostByDiscussion,
} from "../rpc/misc-batch5.js";
import {
  handleDialSet,
  handleAuthRetryRecord,
  handleFleetDiscoveryAck,
} from "../rpc/mutating-p6b.js";
import { handleDora } from "../rpc/stats-dora.js";

// ---------------------------------------------------------------------------
// Paths — mirrors Python backend/routers/rpc.py _REPO_ROOT / _TOKEN_PATH
// ---------------------------------------------------------------------------

function repoRoot(): string {
  // AF_REPO_ROOT env var: set this to the repo root in production / when running
  // the TS backend from a git worktree. Falls back to walking up from __dirname.
  if (process.env.AF_REPO_ROOT) {
    return process.env.AF_REPO_ROOT;
  }

  // Walk up from the current file looking for the .autonomous-team directory.
  // This works in both the main repo and in a git worktree, because either:
  //   - main repo: .autonomous-team is at the repo root (same as ts-backend/../)
  //   - worktree: .autonomous-team doesn't exist in the worktree subtree,
  //     so we fall back to the Python backend's AUTONOMOUS_TEAM_DIR env.
  const autonomousTeamDir = process.env.AUTONOMOUS_TEAM_DIR;
  if (autonomousTeamDir) {
    // AUTONOMOUS_TEAM_DIR = <repo_root>/.autonomous-team → parent is repo root
    return join(autonomousTeamDir, "..");
  }

  // Fallback: go up 4 directories from ts-backend/src/routes/rpc.ts.
  // This is correct in the main repo layout. In a worktree, the caller should
  // set AF_REPO_ROOT or AUTONOMOUS_TEAM_DIR to avoid misresolution.
  const thisFile =
    typeof __filename !== "undefined"
      ? __filename
      : fileURLToPath(import.meta.url);
  return join(dirname(thisFile), "..", "..", "..", "..");
}

function tokenPath(): string {
  // RPC_TOKEN_DIR_OVERRIDE: test-only env var so tests can point to a temp dir
  // without modifying the real .autonomous-team/dashboard-token file.
  const override = process.env.RPC_TOKEN_DIR_OVERRIDE;
  if (override) {
    return join(override, ".autonomous-team", "dashboard-token");
  }
  return join(repoRoot(), ".autonomous-team", "dashboard-token");
}

// ---------------------------------------------------------------------------
// RPC token loader — mirrors Python _load_rpc_token() exactly.
// Fail-closed: missing/empty file → "" → 401 all requests.
// ---------------------------------------------------------------------------

function loadRpcToken(): string {
  try {
    return readFileSync(tokenPath(), "utf-8").trim();
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Spawn-guard constants — mirrors backend/routers/rpc.py verbatim
// ---------------------------------------------------------------------------

const SPAWN_METHODS: ReadonlySet<string> = new Set(["loop.start"]);
const TEST_UA_RE = /HeadlessChrome|Puppeteer|playwright/i;
const TEST_ORIGINS: ReadonlySet<string> = new Set([
  "http://localhost:5173",
  "http://127.0.0.1:5173",
]);

// ---------------------------------------------------------------------------
// Mutating methods still deferred (spawn/kill real loops — dangerous to test).
// These return method-not-found. The 3 safe mutating methods (dial.set,
// auth_retry.record, fleet.discovery_ack) are now native in NATIVE_HANDLERS.
// ---------------------------------------------------------------------------
const DEFERRED_METHODS: ReadonlySet<string> = new Set([
  "loop.start",
  "loop.stop",
]);

// ---------------------------------------------------------------------------
// JSON-RPC 2.0 envelope helpers — mirror Python _rpc_ok / _rpc_err exactly
// ---------------------------------------------------------------------------

function rpcOk(id: unknown, result: unknown): object {
  return { jsonrpc: "2.0", id, result };
}

function rpcErr(id: unknown, code: number, message: string): object {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

// ---------------------------------------------------------------------------
// Python backend proxy URL — where FastAPI /rpc lives
// ---------------------------------------------------------------------------

function pythonRpcUrl(): string {
  const port = process.env.PYTHON_API_PORT ?? "18099";
  return `http://127.0.0.1:${port}/rpc`;
}

// ---------------------------------------------------------------------------
// Unit corrections — mirrors stats_reader._UNIT_CORRECTIONS (same as stats-metrics.ts)
// ---------------------------------------------------------------------------
const UNIT_CORRECTIONS: Record<string, Record<string, string>> = {
  orphan_worktree_rate: { ratio: "count" },
};

function correctUnit(metric: string, unit: string): string {
  return UNIT_CORRECTIONS[metric]?.[unit] ?? unit;
}

// ---------------------------------------------------------------------------
// Native TS handler: stats.summary
// Mirrors Python _rpc_stats_summary via stats_reader.summary()
// Response: {"metrics": [{name, value, unit, updated_at_iso}, ...]}
// ---------------------------------------------------------------------------

async function handleStatsSummary(params: Record<string, unknown>): Promise<unknown> {
  // project scoping: when params.project is set, Python swaps STATS_DB_PATH.
  // The TS backend uses the default path from the env (same as stats-metrics.ts GET route).
  // Per-project DB path swap is a P6b enhancement.
  const _project = params["project"];
  void _project; // acknowledged but not yet used

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { metrics: [] };
  }
  try {
    const rows = await queryDicts(h, `
      SELECT metric, value, unit, ts
      FROM (
        SELECT metric, value, unit, ts,
               ROW_NUMBER() OVER (PARTITION BY metric ORDER BY ts DESC) AS rn
        FROM metric_event
      ) t
      WHERE rn = 1
      ORDER BY metric
    `);

    const metrics = rows.map(row => {
      const metric = row["metric"] as string;
      const unit = correctUnit(metric, (row["unit"] as string | null) ?? "");
      const ts = row["ts"] as string | null;
      const rawVal = row["value"];
      const value = typeof rawVal === "bigint" ? bigIntToExact(rawVal) : (rawVal ?? null);
      return { name: metric, value, unit, updated_at_iso: ts ?? null };
    });

    return { metrics };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// Native TS handler: stats.series
// Mirrors Python _rpc_stats_series via stats_reader.series(name, since_hours)
// Response: {"name": str, "points": [{ts_iso, value}, ...]}
// ---------------------------------------------------------------------------

async function handleStatsSeries(params: Record<string, unknown>): Promise<unknown> {
  const name = (params["name"] as string | undefined) ?? "";
  if (!name) {
    // Python raises ValueError (no rpc_code attr) → dispatched as -32000.
    // Faithful mirror: use -32000 (default), NOT -32602, to match Python behavior.
    throw new Error("'name' parameter is required");
  }
  const sinceHours = Math.max(
    1,
    Math.min(8760, parseInt(String(params["since_hours"] ?? "168"), 10) || 168)
  );

  const cutoff = new Date(Date.now() - sinceHours * 3600 * 1000);
  const cutoffStr = cutoff.toISOString().replace("T", " ").slice(0, 19);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return { name, points: [] };
  }
  try {
    const rows = await queryDicts(
      h,
      `
      SELECT ts, value
      FROM metric_event
      WHERE metric = ?
        AND ts >= CAST(? AS TIMESTAMP)
      ORDER BY ts
      `,
      [name, cutoffStr]
    );

    const points = rows.map(row => {
      const ts = row["ts"] as string | null;
      const rawVal = row["value"];
      const value = typeof rawVal === "bigint" ? bigIntToExact(rawVal) : (rawVal ?? null);
      return { ts_iso: ts ?? "", value };
    });

    return { name, points };
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// Proxy handler — forward to Python FastAPI /rpc (read-only methods)
//
// Forwards the original JSON-RPC envelope to Python's /rpc using the same
// RPC token. Python's response is returned verbatim — this guarantees
// byte-identical response bodies for all proxied methods without re-implementing
// Python's business logic (CostTracker, kpi_engine, gh CLI calls, etc.).
// ---------------------------------------------------------------------------

async function proxyToPython(
  rpcToken: string,
  reqId: unknown,
  method: string,
  params: Record<string, unknown>
): Promise<unknown> {
  const envelope = { jsonrpc: "2.0", id: reqId, method, params };
  const url = pythonRpcUrl();

  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${rpcToken}`,
      },
      body: JSON.stringify(envelope),
      // 30s timeout — same as Python _gh_graphql timeout
      signal: AbortSignal.timeout(30_000),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`proxy_error: ${msg}`);
  }

  try {
    return await resp.json();
  } catch {
    return rpcErr(reqId, -32000, "proxy_parse_error");
  }
}

// ---------------------------------------------------------------------------
// Read-only method registry
// ---------------------------------------------------------------------------

// Native: implemented in TS above or in rpc/ modules.
// runs.* methods moved from PROXY to native in P6a-native batch 1 (D#1437 #1448).
// stats.* batch 2 methods moved from PROXY to native (D#1437 batch 2).
// loop.* + agents.tail + dashboard.gates_snapshot moved from PROXY to native (D#1437 batch 4).
const NATIVE_HANDLERS: Record<string, (params: Record<string, unknown>) => Promise<unknown> | unknown> = {
  "stats.summary": handleStatsSummary,
  "stats.series": handleStatsSeries,
  // runs.* cluster — native DuckDB reads (P6a-native batch 1)
  "runs.by_role": handleByRole,
  "runs.percentiles": handlePercentiles,
  "runs.stuck": handleStuck,
  "runs.roundtrip": handleRoundtrip,
  "runs.active_over_time": handleActiveOverTime,
  "runs.recent": handleRecent,
  // stats.* cluster — native DuckDB + file reads (P6a-native batch 2)
  "stats.team_lead_tokens": handleTeamLeadTokens,
  "stats.cost_spike_history": handleCostSpikeHistory,
  "stats.role_success_rate": handleRoleSuccessRate,
  "stats.role_retry_rate": handleRoleRetryRate,
  "stats.avg_fix_rounds_per_pr": handleAvgFixRoundsPerPr,
  "stats.pre_write_burn": handlePreWriteBurn,
  "stats.cosmetic_blocks": handleCosmeticBlocks,
  "stats.loop_idle_ratio": handleLoopIdleRatio,
  "stats.parity_trend": handleParityTrend,
  // stats.* cluster — native TS (P6a-native batch 3)
  "stats.freshness_list": handleFreshnessList,
  "stats.weekly_velocity": handleWeeklyVelocity,
  "stats.sdk_vs_cc": handleSdkVsCc,
  "stats_duckdb_writers": handleDuckdbWriters,
  "stats.dial_usage": handleDialUsage,
  "stats.dial_rejections": handleDialRejections,
  "stats.analyst_findings": handleAnalystFindings,
  "stats.verdict_overturns": handleVerdictOverturns,
  // loop.* + agents.tail + dashboard.gates_snapshot cluster — file reads (P6a-native batch 4)
  "loop.list": handleLoopList,
  "loop.events": handleLoopEvents,
  "loop.timeline": handleLoopTimeline,
  "loop.iteration_detail": handleLoopIterationDetail,
  "agents.tail": handleAgentsTail,
  "dashboard.gates_snapshot": handleDashboardGatesSnapshot,
  // misc cluster — dial, auth_retry, circuit_breaker, kpi, cost (P6a-native batch 5)
  "dial.list": handleDialList,
  "auth_retry.summary": handleAuthRetrySummary,
  "circuit_breaker.summary": handleCircuitBreakerSummary,
  "circuitBreaker.history": handleCircuitBreakerHistory,
  "kpi.history": handleKpiHistory,
  "kpi.cycle_time": handleKpiCycleTime,
  "cost.per_discussion": handleCostPerDiscussion,
  "cost.by_discussion": handleCostByDiscussion,
  // DORA + KPI snapshot — native TS (D#1471)
  "stats.dora": handleDora,
  // mutating methods — P6b: safe bounded writes (temp-copy parity tested)
  "dial.set": handleDialSet,
  "auth_retry.record": handleAuthRetryRecord,
  "fleet.discovery_ack": handleFleetDiscoveryAck,
};

// Proxy: forward to Python /rpc. Explicitly listed so unknown methods still
// return -32601 (not silently proxied to Python).
// Note: runs.* methods removed (now native, batch 1).
// Note: stats.* batch 2 methods removed (now native, batch 2).
// Note: stats.* batch 3 methods removed (now native, batch 3):
//   freshness_list, weekly_velocity, sdk_vs_cc, stats_duckdb_writers,
//   dial_usage, dial_rejections, analyst_findings, verdict_overturns.
// Note: stats.sdk_lane and stats.cost_per_outcome remain proxied:
//   sdk_lane   — depends on CreditTracker (sdk_credit.json) + billing_regime + env combo
//   cost_per_outcome — depends on CostTracker + DuckDB project scoping
// Note: loop.* + agents.tail + dashboard.gates_snapshot removed (now native, batch 4).
// Note: dial.list, auth_retry.summary, circuit_breaker.summary, circuitBreaker.history,
//   kpi.history, kpi.cycle_time, cost.per_discussion, cost.by_discussion removed
//   (now native, batch 5 — misc-batch5.ts).
const PROXY_METHODS: ReadonlySet<string> = new Set([
  "dashboard.pr_detail",
  "dashboard.pr_list",
  "team_status.snapshot",
  "claude_spawn_tracker.summary",
  "discussions.list",
  "discussions.get",
  // stats.* deferred — complex Python deps, not portworthy for this batch
  "stats.sdk_lane",
  "stats.cost_per_outcome",
  "a2a.list_active",
  "a2a.tail",
  // fleet.* deferred — backend.fleet.discovery (Python-specific state scanning)
  "fleet.projects",
  "fleet.cost",
  "fleet.concurrency",
]);

// ---------------------------------------------------------------------------
// Main handler — POST /rpc
// ---------------------------------------------------------------------------

export async function rpcDispatchHandler(c: Context): Promise<Response> {
  // ------------------------------------------------------------------
  // 1. Parse body — before auth so we can attach the correct req id.
  //    mirrors Python: parse first, then auth.
  //    Invalid JSON → 400, code -32000 (matches Python exactly)
  // ------------------------------------------------------------------
  let raw: string;
  try {
    raw = await c.req.text();
  } catch {
    raw = "";
  }

  let req: Record<string, unknown> = {};
  try {
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("body must be a JSON object");
      }
      req = parsed as Record<string, unknown>;
    }
  } catch {
    return c.json(rpcErr(null, -32000, "invalid JSON"), 400);
  }

  const reqId: unknown = "id" in req ? req["id"] : null;
  const method: string = typeof req["method"] === "string" ? req["method"] : "";
  const rawParams = req["params"];
  const params: Record<string, unknown> =
    rawParams !== null &&
    rawParams !== undefined &&
    typeof rawParams === "object" &&
    !Array.isArray(rawParams)
      ? (rawParams as Record<string, unknown>)
      : {};

  // ------------------------------------------------------------------
  // 2. Auth — RPC token (SEPARATE from AF_API_AUTH_KEY REST key).
  //    Accept: Authorization: Bearer <token>  OR  ?token=<token>
  //    Fail-closed: empty token file → 401 all requests.
  //    mirrors Python hmac.compare_digest via timingSafeEqual (CWE-208).
  // ------------------------------------------------------------------
  const rpcToken = loadRpcToken();
  let bearer: string | null = null;

  const authHeader = c.req.header("Authorization") ?? "";
  if (authHeader.startsWith("Bearer ")) {
    bearer = authHeader.slice("Bearer ".length).trim();
  }
  if (bearer === null) {
    bearer = c.req.query("token") ?? null;
  }

  let authOk = false;
  if (rpcToken && bearer !== null) {
    try {
      const a = Buffer.from(bearer, "utf-8");
      const b = Buffer.from(rpcToken, "utf-8");
      authOk = a.length === b.length && timingSafeEqual(a, b);
    } catch {
      authOk = false;
    }
  }

  if (!authOk) {
    return c.json(rpcErr(reqId, -32000, "unauthorized"), 401);
  }

  // ------------------------------------------------------------------
  // 3. Spawn-guard for loop.start — mirrors Python rpc.py exactly.
  //    Applied before DEFERRED_METHODS check (Python order).
  // ------------------------------------------------------------------
  if (SPAWN_METHODS.has(method)) {
    const allowEnv = (process.env.AF_ALLOW_TEST_ORIGIN_SPAWNS ?? "").trim() === "1";
    if (!allowEnv) {
      const ua = c.req.header("user-agent") ?? "";
      const origin = c.req.header("origin") ?? "";
      if (TEST_UA_RE.test(ua) || TEST_ORIGINS.has(origin)) {
        return c.json(rpcErr(reqId, -32000, "spawn_blocked_test_origin"), 200);
      }
    }
  }

  // ------------------------------------------------------------------
  // 4. Deferred P6b methods — return method-not-found (same as Python
  //    would for any method absent from _RPC_METHODS registry).
  // ------------------------------------------------------------------
  if (DEFERRED_METHODS.has(method)) {
    return c.json(rpcErr(reqId, -32601, `method not found: ${method}`), 200);
  }

  // ------------------------------------------------------------------
  // 5. Dispatch — native TS handlers → proxy → not-found
  // ------------------------------------------------------------------

  // 5a. Native TS handler (stats.summary, stats.series)
  const nativeHandler = NATIVE_HANDLERS[method];
  if (nativeHandler) {
    try {
      const result = await nativeHandler(params);
      return c.json(rpcOk(reqId, result), 200);
    } catch (exc) {
      const code = (exc as { rpc_code?: number }).rpc_code ?? -32000;
      const msg = exc instanceof Error ? exc.message : String(exc);
      return c.json(rpcErr(reqId, code, msg), 200);
    }
  }

  // 5b. Proxy to Python /rpc (all other read-only methods)
  if (PROXY_METHODS.has(method)) {
    try {
      const body = await proxyToPython(rpcToken, reqId, method, params);
      // body is already a valid JSON-RPC envelope from Python — return verbatim
      return c.json(body, 200);
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc);
      return c.json(rpcErr(reqId, -32000, msg), 200);
    }
  }

  // 5c. Unknown method — same error Python returns for unregistered methods
  return c.json(rpcErr(reqId, -32601, `method not found: ${method}`), 200);
}
