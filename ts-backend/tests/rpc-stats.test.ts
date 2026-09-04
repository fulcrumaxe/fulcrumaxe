/**
 * Tests for stats.* RPC handlers — D#1437 P6a-native batch 2.
 *
 * Run: bun test tests/rpc-stats.test.ts --timeout 30000
 *
 * Coverage:
 *  1. stats.team_lead_tokens   — DB absent → null rates, sample_size=0
 *  2. stats.cost_spike_history — DB absent → {spikes:[], count:0, last_spike_iso:null}
 *  3. stats.role_success_rate  — DB absent → {rows:[]}
 *  4. stats.role_retry_rate    — DB absent → {rows:[]}
 *  5. stats.avg_fix_rounds_per_pr — DB absent → empty
 *  6. stats.pre_write_burn     — DB absent → {rows:[]}
 *  7. stats.cosmetic_blocks    — no events dir → {total_24h:0, hourly_7d:[]}
 *  8. stats.loop_idle_ratio    — no metrics file → {ratio:null, idle_count:0, sample_size:0}
 *  9. stats.parity_trend       — no history file → {runs:[], total_runs:0, history_path:...}
 * 10. Dispatch: all 9 methods reach native handlers (not proxy)
 * 11. stats.cosmetic_blocks: JSONL parsing produces correct hourly buckets
 * 12. stats.loop_idle_ratio: JSONL parsing respects cutoff, idle flag, and sample_size<5
 * 13. stats.parity_trend: limit param respected
 * 14. stats.team_lead_tokens: sample_size < 5 → null rates
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import {
  writeFileSync, mkdirSync, rmSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { rpcDispatchHandler } from "../src/routes/rpc.js";
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
} from "../src/rpc/stats.js";

// ---------------------------------------------------------------------------
// App factory — mirrors rpc-runs.test.ts
// ---------------------------------------------------------------------------

function makeApp(rpcToken: string): { app: Hono; tokenDir: string } {
  const tokenDir = join(tmpdir(), `rpc-stats-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(join(tokenDir, ".autonomous-team"), { recursive: true });
  writeFileSync(join(tokenDir, ".autonomous-team", "dashboard-token"), rpcToken + "\n");
  process.env.RPC_TOKEN_DIR_OVERRIDE = tokenDir;

  const app = new Hono();
  app.use("*", defaultDenyMiddleware);
  app.post("/rpc", rpcDispatchHandler);

  return { app, tokenDir };
}

function cleanup(tokenDir: string) {
  try { rmSync(tokenDir, { recursive: true, force: true }); } catch { /* ignore */ }
  delete process.env.RPC_TOKEN_DIR_OVERRIDE;
  delete process.env.STATS_DB_PATH;
  delete process.env.AF_HOOK_EVENTS_DIR;
  delete process.env.AF_LOOP_METRICS_PATH;
  delete process.env.PARITY_HISTORY_PATH;
}

async function rpc(
  app: Hono,
  method: string,
  params: Record<string, unknown> = {},
  token: string = "test-rpc-stats-token"
): Promise<{ status: number; body: Record<string, unknown> }> {
  const resp = await app.request("/rpc", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const body = await resp.json() as Record<string, unknown>;
  return { status: resp.status, body };
}

// ---------------------------------------------------------------------------
// §1 — Unit tests: handlers with DB absent
// ---------------------------------------------------------------------------

describe("stats.* handlers — DB absent graceful empty", () => {
  beforeEach(() => {
    process.env.STATS_DB_PATH = "/nonexistent/stats.duckdb";
  });
  afterEach(() => {
    delete process.env.STATS_DB_PATH;
    delete process.env.AF_HOOK_EVENTS_DIR;
    delete process.env.AF_LOOP_METRICS_PATH;
    delete process.env.PARITY_HISTORY_PATH;
  });

  // stats.team_lead_tokens
  it("handleTeamLeadTokens: DB absent → {avg:null, p50:null, p95:null, sample_size:0}", async () => {
    const result = await handleTeamLeadTokens({}) as Record<string, unknown>;
    expect(result).toEqual({ avg: null, p50: null, p95: null, sample_size: 0 });
  });

  it("handleTeamLeadTokens: custom since_hours, DB absent → empty", async () => {
    const result = await handleTeamLeadTokens({ since_hours: 48 }) as Record<string, unknown>;
    expect(result).toEqual({ avg: null, p50: null, p95: null, sample_size: 0 });
  });

  // stats.cost_spike_history
  it("handleCostSpikeHistory: DB absent → {spikes:[], count:0, last_spike_iso:null}", async () => {
    const result = await handleCostSpikeHistory({}) as Record<string, unknown>;
    expect(result).toEqual({ spikes: [], count: 0, last_spike_iso: null });
  });

  it("handleCostSpikeHistory: custom hours, DB absent → empty", async () => {
    const result = await handleCostSpikeHistory({ hours: 48 }) as Record<string, unknown>;
    expect(result).toEqual({ spikes: [], count: 0, last_spike_iso: null });
  });

  // stats.role_success_rate
  it("handleRoleSuccessRate: DB absent → {rows:[]}", async () => {
    const result = await handleRoleSuccessRate({}) as Record<string, unknown>;
    expect(result).toEqual({ rows: [] });
  });

  // stats.role_retry_rate
  it("handleRoleRetryRate: DB absent → {rows:[]}", async () => {
    const result = await handleRoleRetryRate({}) as Record<string, unknown>;
    expect(result).toEqual({ rows: [] });
  });

  // stats.avg_fix_rounds_per_pr
  it("handleAvgFixRoundsPerPr: DB absent → {avg_last_24h:null, sample_size:0, distribution:{}}", async () => {
    const result = await handleAvgFixRoundsPerPr({}) as Record<string, unknown>;
    expect(result).toEqual({ avg_last_24h: null, sample_size: 0, distribution: {} });
  });

  // stats.pre_write_burn
  it("handlePreWriteBurn: DB absent → {rows:[]}", async () => {
    const result = await handlePreWriteBurn({}) as Record<string, unknown>;
    expect(result).toEqual({ rows: [] });
  });

  it("handlePreWriteBurn: custom limit, DB absent → {rows:[]}", async () => {
    const result = await handlePreWriteBurn({ limit: 5 }) as Record<string, unknown>;
    expect(result).toEqual({ rows: [] });
  });
});

// ---------------------------------------------------------------------------
// §2 — File-based handlers: absent files → graceful empty
// ---------------------------------------------------------------------------

describe("stats.* file-based handlers — files absent", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = join(tmpdir(), `rpc-stats-files-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });
    // Point all file readers at the empty tmpDir
    process.env.AF_HOOK_EVENTS_DIR = tmpDir;
    process.env.AF_LOOP_METRICS_PATH = join(tmpDir, "loop-metrics.jsonl");
    process.env.PARITY_HISTORY_PATH = join(tmpDir, "parity-history.jsonl");
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AF_HOOK_EVENTS_DIR;
    delete process.env.AF_LOOP_METRICS_PATH;
    delete process.env.PARITY_HISTORY_PATH;
  });

  it("handleCosmeticBlocks: no events dir → {total_24h:0, hourly_7d:[]}", () => {
    const result = handleCosmeticBlocks({}) as Record<string, unknown>;
    expect(result["total_24h"]).toBe(0);
    expect(Array.isArray(result["hourly_7d"])).toBe(true);
    expect((result["hourly_7d"] as unknown[]).length).toBe(0);
  });

  it("handleLoopIdleRatio: no metrics file → {ratio:null, idle_count:0, sample_size:0}", () => {
    const result = handleLoopIdleRatio({}) as Record<string, unknown>;
    expect(result).toEqual({ ratio: null, idle_count: 0, sample_size: 0 });
  });

  it("handleParityTrend: no history file → {runs:[], total_runs:0, history_path:...}", () => {
    const result = handleParityTrend({}) as Record<string, unknown>;
    expect(result["runs"]).toEqual([]);
    expect(result["total_runs"]).toBe(0);
    expect(typeof result["history_path"]).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// §3 — File-based handlers: synthetic JSONL data
// ---------------------------------------------------------------------------

describe("stats.* file-based handlers — synthetic JSONL data", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = join(tmpdir(), `rpc-stats-jsonl-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AF_HOOK_EVENTS_DIR = tmpDir;
    process.env.AF_LOOP_METRICS_PATH = join(tmpDir, "loop-metrics.jsonl");
    process.env.PARITY_HISTORY_PATH = join(tmpDir, "parity-history.jsonl");
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AF_HOOK_EVENTS_DIR;
    delete process.env.AF_LOOP_METRICS_PATH;
    delete process.env.PARITY_HISTORY_PATH;
  });

  // cosmetic_blocks: write a JSONL file with known entries
  it("handleCosmeticBlocks: counts blocks in last 24h and buckets them", () => {
    const now = new Date();
    const recentTs = new Date(now.getTime() - 30 * 60 * 1000).toISOString(); // 30m ago
    const recentTs2 = new Date(now.getTime() - 60 * 60 * 1000).toISOString(); // 1h ago
    const oldTs = new Date(now.getTime() - 25 * 3600 * 1000).toISOString(); // 25h ago

    const dateStr = now.toISOString().slice(0, 10);
    const logFile = join(tmpDir, `cosmetic-blocks-${dateStr}.jsonl`);
    writeFileSync(logFile, [
      JSON.stringify({ ts: recentTs, reason: "retry_cosmetic" }),
      JSON.stringify({ ts: recentTs2, reason: "retry_cosmetic" }),
      JSON.stringify({ ts: oldTs, reason: "retry_cosmetic" }), // outside 24h
      "malformed-json",
    ].join("\n") + "\n");

    const result = handleCosmeticBlocks({}) as Record<string, unknown>;
    // total_24h should be 2 (recentTs + recentTs2), not 3 (oldTs is outside window)
    expect(result["total_24h"]).toBe(2);
    // hourly_7d should have entries
    expect(Array.isArray(result["hourly_7d"])).toBe(true);
    const hourly = result["hourly_7d"] as Array<{ hour_iso: string; count: number }>;
    // Each entry should have hour_iso ending in :00:00Z and a positive count
    for (const entry of hourly) {
      expect(entry["hour_iso"]).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$/);
      expect(entry["count"]).toBeGreaterThan(0);
    }
  });

  // loop_idle_ratio: write synthetic loop-metrics.jsonl
  it("handleLoopIdleRatio: sample_size < 5 → ratio:null with idle_count", () => {
    const now = new Date();
    const recentTs = (offset: number) => new Date(now.getTime() - offset * 60 * 1000).toISOString();

    // Write 3 iterations (< 5 required for ratio)
    const lines = [
      JSON.stringify({ timestamp: recentTs(5), agents_spawned: 0 }),    // idle
      JSON.stringify({ timestamp: recentTs(10), agents_spawned: 3 }),   // not idle
      JSON.stringify({ timestamp: recentTs(15), agents_spawned: 0 }),   // idle
    ].join("\n") + "\n";
    writeFileSync(process.env.AF_LOOP_METRICS_PATH!, lines);

    const result = handleLoopIdleRatio({}) as Record<string, unknown>;
    expect(result["ratio"]).toBeNull();
    expect(result["idle_count"]).toBe(2);
    expect(result["sample_size"]).toBe(3);
  });

  it("handleLoopIdleRatio: sample_size >= 5 → ratio computed", () => {
    const now = new Date();
    const recentTs = (offset: number) => new Date(now.getTime() - offset * 60 * 1000).toISOString();

    // 5 iterations: 2 idle, 3 active → ratio = 0.4
    const lines = [
      JSON.stringify({ timestamp: recentTs(5), agents_spawned: 0 }),
      JSON.stringify({ timestamp: recentTs(10), agents_spawned: 2 }),
      JSON.stringify({ timestamp: recentTs(15), agents_spawned: 0 }),
      JSON.stringify({ timestamp: recentTs(20), agents_spawned: 4 }),
      JSON.stringify({ timestamp: recentTs(25), agents_spawned: 1 }),
    ].join("\n") + "\n";
    writeFileSync(process.env.AF_LOOP_METRICS_PATH!, lines);

    const result = handleLoopIdleRatio({}) as Record<string, unknown>;
    expect(result["ratio"]).toBeCloseTo(0.4, 5);
    expect(result["idle_count"]).toBe(2);
    expect(result["sample_size"]).toBe(5);
  });

  it("handleLoopIdleRatio: old entries outside 24h window ignored", () => {
    const now = new Date();
    const recentTs = new Date(now.getTime() - 60 * 60 * 1000).toISOString(); // 1h ago
    const oldTs = new Date(now.getTime() - 25 * 3600 * 1000).toISOString(); // 25h ago

    const lines = [
      JSON.stringify({ timestamp: recentTs, agents_spawned: 0 }), // recent, idle
      JSON.stringify({ timestamp: oldTs, agents_spawned: 0 }),    // old, excluded
    ].join("\n") + "\n";
    writeFileSync(process.env.AF_LOOP_METRICS_PATH!, lines);

    const result = handleLoopIdleRatio({}) as Record<string, unknown>;
    // Only 1 entry in window → sample_size < 5 → ratio:null
    expect(result["sample_size"]).toBe(1);
    expect(result["ratio"]).toBeNull();
    expect(result["idle_count"]).toBe(1);
  });

  it("handleLoopIdleRatio: test-origin rows are skipped", () => {
    const now = new Date();
    const recentTs = (offset: number) => new Date(now.getTime() - offset * 60 * 1000).toISOString();

    const lines = [
      JSON.stringify({ timestamp: recentTs(5), agents_spawned: 0, origin: "test" }), // skipped
      JSON.stringify({ timestamp: recentTs(10), agents_spawned: 2 }),                // counted
    ].join("\n") + "\n";
    writeFileSync(process.env.AF_LOOP_METRICS_PATH!, lines);

    const result = handleLoopIdleRatio({}) as Record<string, unknown>;
    expect(result["sample_size"]).toBe(1); // test row excluded
  });

  it("handleLoopIdleRatio: idle:true flag counts as idle regardless of agents_spawned", () => {
    const now = new Date();
    const recentTs = (offset: number) => new Date(now.getTime() - offset * 60 * 1000).toISOString();

    const lines = [
      JSON.stringify({ timestamp: recentTs(5), agents_spawned: 3, idle: true }), // idle=true → idle
      JSON.stringify({ timestamp: recentTs(10), agents_spawned: 3 }),             // not idle
    ].join("\n") + "\n";
    writeFileSync(process.env.AF_LOOP_METRICS_PATH!, lines);

    const result = handleLoopIdleRatio({}) as Record<string, unknown>;
    expect(result["idle_count"]).toBe(1);
  });

  // parity_trend: write synthetic parity-history.jsonl
  it("handleParityTrend: returns all records when limit >= count", () => {
    const records = [
      { ts: "2026-05-20T10:00:00Z", overall: { pass: true }, per_role: [] },
      { ts: "2026-05-20T11:00:00Z", overall: { pass: false }, per_role: [] },
      { ts: "2026-05-20T12:00:00Z", overall: { pass: true }, per_role: [] },
    ];
    writeFileSync(
      process.env.PARITY_HISTORY_PATH!,
      records.map(r => JSON.stringify(r)).join("\n") + "\n"
    );

    const result = handleParityTrend({ limit: 20 }) as Record<string, unknown>;
    expect(result["total_runs"]).toBe(3);
    expect((result["runs"] as unknown[]).length).toBe(3);
  });

  it("handleParityTrend: limit param returns only the most recent N", () => {
    const records = Array.from({ length: 10 }, (_, i) => ({
      ts: `2026-05-20T${String(i).padStart(2, "0")}:00:00Z`,
      overall: {},
      per_role: [],
    }));
    writeFileSync(
      process.env.PARITY_HISTORY_PATH!,
      records.map(r => JSON.stringify(r)).join("\n") + "\n"
    );

    const result = handleParityTrend({ limit: 3 }) as Record<string, unknown>;
    expect(result["total_runs"]).toBe(10);
    const runs = result["runs"] as Array<Record<string, unknown>>;
    expect(runs.length).toBe(3);
    // Should be the last 3 records (most recent)
    expect(runs[2]["ts"]).toBe("2026-05-20T09:00:00Z");
  });

  it("handleParityTrend: malformed lines skipped, valid ones returned", () => {
    const content = [
      JSON.stringify({ ts: "2026-05-20T10:00:00Z", overall: {}, per_role: [] }),
      "this is not json",
      JSON.stringify({ ts: "2026-05-20T11:00:00Z", overall: {}, per_role: [] }),
    ].join("\n") + "\n";
    writeFileSync(process.env.PARITY_HISTORY_PATH!, content);

    const result = handleParityTrend({}) as Record<string, unknown>;
    expect(result["total_runs"]).toBe(2); // malformed skipped
    expect((result["runs"] as unknown[]).length).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// §4 — Dispatch: all 9 stats.* methods reach native handlers (not proxy path)
// ---------------------------------------------------------------------------

describe("POST /rpc — stats.* batch 2 dispatch (no Python backend needed)", () => {
  const TOKEN = "test-rpc-stats-dispatch-token";
  let tokenDir: string;
  let tmpDir: string;
  let app: Hono;

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;

    tmpDir = join(tmpdir(), `rpc-stats-dispatch-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });

    // Point all readers at nonexistent/empty paths so native handlers return graceful-empty
    process.env.STATS_DB_PATH = "/nonexistent/stats.duckdb";
    process.env.AF_HOOK_EVENTS_DIR = tmpDir;
    process.env.AF_LOOP_METRICS_PATH = join(tmpDir, "loop-metrics.jsonl");
    process.env.PARITY_HISTORY_PATH = join(tmpDir, "parity-history.jsonl");
    delete process.env.AF_API_AUTH_KEY;
  });

  afterEach(() => {
    cleanup(tokenDir);
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AF_HOOK_EVENTS_DIR;
    delete process.env.AF_LOOP_METRICS_PATH;
    delete process.env.PARITY_HISTORY_PATH;
  });

  it("stats.team_lead_tokens → HTTP 200, result envelope with avg/p50/p95/sample_size", async () => {
    const { status, body } = await rpc(app, "stats.team_lead_tokens", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["jsonrpc"]).toBe("2.0");
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect("avg" in result).toBe(true);
    expect("p50" in result).toBe(true);
    expect("p95" in result).toBe(true);
    expect("sample_size" in result).toBe(true);
    expect(result["sample_size"]).toBe(0);
  });

  it("stats.cost_spike_history → HTTP 200, result with spikes/count/last_spike_iso", async () => {
    const { status, body } = await rpc(app, "stats.cost_spike_history", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["spikes"])).toBe(true);
    expect(result["count"]).toBe(0);
    expect(result["last_spike_iso"]).toBeNull();
  });

  it("stats.role_success_rate → HTTP 200, result with rows array", async () => {
    const { status, body } = await rpc(app, "stats.role_success_rate", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["rows"])).toBe(true);
  });

  it("stats.role_retry_rate → HTTP 200, result with rows array", async () => {
    const { status, body } = await rpc(app, "stats.role_retry_rate", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["rows"])).toBe(true);
  });

  it("stats.avg_fix_rounds_per_pr → HTTP 200, result with avg_last_24h/sample_size/distribution", async () => {
    const { status, body } = await rpc(app, "stats.avg_fix_rounds_per_pr", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect("avg_last_24h" in result).toBe(true);
    expect("sample_size" in result).toBe(true);
    expect("distribution" in result).toBe(true);
    expect(result["sample_size"]).toBe(0);
  });

  it("stats.pre_write_burn → HTTP 200, result with rows array", async () => {
    const { status, body } = await rpc(app, "stats.pre_write_burn", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["rows"])).toBe(true);
  });

  it("stats.pre_write_burn with limit → HTTP 200, result", async () => {
    const { status, body } = await rpc(app, "stats.pre_write_burn", { limit: 5 }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
  });

  it("stats.cosmetic_blocks → HTTP 200, result with total_24h and hourly_7d", async () => {
    const { status, body } = await rpc(app, "stats.cosmetic_blocks", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(typeof result["total_24h"]).toBe("number");
    expect(Array.isArray(result["hourly_7d"])).toBe(true);
  });

  it("stats.loop_idle_ratio → HTTP 200, result with ratio/idle_count/sample_size", async () => {
    const { status, body } = await rpc(app, "stats.loop_idle_ratio", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect("ratio" in result).toBe(true);
    expect("idle_count" in result).toBe(true);
    expect("sample_size" in result).toBe(true);
    expect(result["ratio"]).toBeNull();
    expect(result["idle_count"]).toBe(0);
  });

  it("stats.parity_trend → HTTP 200, result with runs/total_runs/history_path", async () => {
    const { status, body } = await rpc(app, "stats.parity_trend", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["runs"])).toBe(true);
    expect(result["total_runs"]).toBe(0);
    expect(typeof result["history_path"]).toBe("string");
  });

  it("stats.parity_trend with limit param → HTTP 200", async () => {
    const { status, body } = await rpc(app, "stats.parity_trend", { limit: 5 }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// §5 — Live DuckDB integration (skipped if stats.duckdb absent)
// ---------------------------------------------------------------------------

describe("stats.* handlers — live DuckDB integration (skipped if DB absent)", () => {
  const tryDb = async () => {
    const { openReadConn, closeConn } = await import("../src/duckdb-helpers.js");
    try {
      const h = await openReadConn();
      closeConn(h);
      return true;
    } catch {
      return false;
    }
  };

  it("handleTeamLeadTokens: returns correct shape from live DB", async () => {
    if (!await tryDb()) return;
    const result = await handleTeamLeadTokens({ since_hours: 24 }) as Record<string, unknown>;
    expect("avg" in result).toBe(true);
    expect("p50" in result).toBe(true);
    expect("p95" in result).toBe(true);
    expect(typeof result["sample_size"]).toBe("number");
    // Each float field is null or a finite number
    for (const k of ["avg", "p50", "p95"]) {
      const v = result[k];
      if (v !== null) {
        expect(typeof v).toBe("number");
        expect(isFinite(v as number)).toBe(true);
      }
    }
  });

  it("handleRoleSuccessRate: rows have correct field shapes", async () => {
    if (!await tryDb()) return;
    const result = await handleRoleSuccessRate({}) as Record<string, unknown>;
    const rows = result["rows"] as Array<Record<string, unknown>>;
    for (const row of rows) {
      expect(typeof row["role"]).toBe("string");
      expect(typeof row["sample_size"]).toBe("number");
      if (row["success_rate"] !== null) {
        expect(typeof row["success_rate"]).toBe("number");
        expect(row["success_rate"] as number).toBeGreaterThanOrEqual(0);
        expect(row["success_rate"] as number).toBeLessThanOrEqual(1);
      }
    }
  });

  it("handleRoleRetryRate: rows have correct field shapes and sort order", async () => {
    if (!await tryDb()) return;
    const result = await handleRoleRetryRate({}) as Record<string, unknown>;
    const rows = result["rows"] as Array<Record<string, unknown>>;
    let prev: number | null = null;
    for (const row of rows) {
      expect(typeof row["role"]).toBe("string");
      if (row["retry_rate"] !== null) {
        const rate = row["retry_rate"] as number;
        if (prev !== null) expect(rate).toBeLessThanOrEqual(prev);
        prev = rate;
      }
    }
  });

  it("handleAvgFixRoundsPerPr: distribution keys are string integers", async () => {
    if (!await tryDb()) return;
    const result = await handleAvgFixRoundsPerPr({}) as Record<string, unknown>;
    const dist = result["distribution"] as Record<string, unknown>;
    for (const [k, v] of Object.entries(dist)) {
      expect(/^\d+$/.test(k)).toBe(true); // string integer key
      expect(typeof v).toBe("number");
    }
  });

  it("handlePreWriteBurn: rows have correct field shapes", async () => {
    if (!await tryDb()) return;
    const result = await handlePreWriteBurn({ limit: 5 }) as Record<string, unknown>;
    const rows = result["rows"] as Array<Record<string, unknown>>;
    for (const row of rows) {
      expect(row["role"]).toBe("executor");
      expect(typeof row["ratio_pct"]).toBe("number");
      expect(row["ratio_pct"] as number).toBeGreaterThan(10); // >10% threshold
    }
  });
});
