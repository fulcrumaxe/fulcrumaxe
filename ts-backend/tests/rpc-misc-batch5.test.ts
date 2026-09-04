/**
 * Tests for misc-batch5 RPC handlers — D#1437 P6a-native batch 5.
 *
 * Coverage: 1.dial.list 2.auth_retry.summary 3.circuit_breaker.summary
 *  4.circuitBreaker.history 5.kpi.history 6.kpi.cycle_time
 *  7.cost.per_discussion 8.cost.by_discussion 9.Dispatch all 8
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Database } from "bun:sqlite";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { rpcDispatchHandler } from "../src/routes/rpc.js";
import {
  handleDialList,
  handleAuthRetrySummary,
  handleCircuitBreakerSummary,
  handleCircuitBreakerHistory,
  handleKpiHistory,
  handleKpiCycleTime,
  handleCostPerDiscussion,
  handleCostByDiscussion,
} from "../src/rpc/misc-batch5.js";

// App factory
function makeApp(rpcToken: string): { app: Hono; tokenDir: string } {
  const r = Math.random().toString(36).slice(2);
  const tokenDir = join(tmpdir(), 'rpc-misc-b5-' + Date.now() + '-' + r);
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
  delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
  delete process.env.AF_REPO_ROOT;
  delete process.env.AUTONOMOUS_TEAM_DIR;
}

async function rpc(
  app: Hono,
  method: string,
  params: Record<string, unknown> = {},
  token = "test-rpc-b5-token"
): Promise<{ status: number; body: Record<string, unknown> }> {
  const resp = await app.request("/rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const body = await resp.json() as Record<string, unknown>;
  return { status: resp.status, body };
}

// --- §1 dial.list ---

describe("handleDialList", () => {
  let tmpDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-dial-' + Date.now() + '-' + r);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_STATE_DIR = tmpDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
  });

  it("missing registry file -> {dials:[]}", () => {
    const result = handleDialList({}) as Record<string, unknown>;
    expect(Array.isArray(result["dials"])).toBe(true);
    expect((result["dials"] as unknown[]).length).toBe(0);
  });

  it("dict-format registry -> sorted dials with correct fields", () => {
    const registry = {
      "merge.standard": { level: 2, ceiling: 4, directives: [] },
      "agent.spawn": { level: 3, ceiling: 5, directives: [] },
    };
    writeFileSync(join(tmpDir, "dial-registry.json"), JSON.stringify(registry));
    const result = handleDialList({}) as Record<string, unknown>;
    const dials = result["dials"] as Array<Record<string, unknown>>;
    expect(dials.length).toBe(2);
    expect(dials[0]["name"]).toBe("agent.spawn");
    expect(dials[0]["level"]).toBe(3);
    expect(dials[0]["ceiling"]).toBe(5);
    expect(dials[0]["active_directives"]).toBe(0);
    expect(dials[0]["ttl_revert_at"]).toBeNull();
    expect(dials[1]["name"]).toBe("merge.standard");
    expect(dials[1]["level"]).toBe(2);
  });

  it("list-format registry -> dials read correctly", () => {
    const registry = [{ class: "review.gate", level: 1, ceiling: 3, directives: [] }];
    writeFileSync(join(tmpDir, "dial-registry.json"), JSON.stringify(registry));
    const result = handleDialList({}) as Record<string, unknown>;
    const dials = result["dials"] as Array<Record<string, unknown>>;
    expect(dials.length).toBe(1);
    expect(dials[0]["name"]).toBe("review.gate");
    expect(dials[0]["level"]).toBe(1);
    expect(dials[0]["ceiling"]).toBe(3);
  });

  it("directive with ttl_until -> ttl_revert_at is an ISO string", () => {
    const registry = {
      "agent.spawn": { level: 4, ceiling: 5, directives: [{ ttl_until: "2099-12-31T23:59:59Z", reason: "test" }] },
    };
    writeFileSync(join(tmpDir, "dial-registry.json"), JSON.stringify(registry));
    const result = handleDialList({}) as Record<string, unknown>;
    const dials = result["dials"] as Array<Record<string, unknown>>;
    expect(dials.length).toBe(1);
    expect(dials[0]["active_directives"]).toBe(1);
    expect(typeof dials[0]["ttl_revert_at"]).toBe("string");
    expect(dials[0]["ttl_revert_at"]).not.toBeNull();
  });

  it("malformed registry JSON -> {dials:[]}", () => {
    writeFileSync(join(tmpDir, "dial-registry.json"), "not valid json {{");
    const result = handleDialList({}) as Record<string, unknown>;
    expect((result["dials"] as unknown[]).length).toBe(0);
  });
});

// --- §2 auth_retry.summary ---

describe("handleAuthRetrySummary", () => {
  let tmpDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-auth-' + Date.now() + '-' + r);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_STATE_DIR = tmpDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
  });

  it("no state.db -> {count_24h:0, count_total:0, last_seen:null}", () => {
    const result = handleAuthRetrySummary({}) as Record<string, unknown>;
    expect(result["count_24h"]).toBe(0);
    expect(result["count_total"]).toBe(0);
    expect(result["last_seen"]).toBeNull();
  });

  it("happy path: reads count and timestamps from SQLite blackboard", () => {
    const dbPath = join(tmpDir, "state.db");
    const db = new Database(dbPath);
    db.exec("CREATE TABLE IF NOT EXISTS blackboard (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
    const recent1 = new Date(Date.now() - 60 * 1000).toISOString();
    const recent2 = new Date(Date.now() - 3600 * 1000).toISOString();
    const old1 = new Date(Date.now() - 30 * 3600 * 1000).toISOString();
    const ins = db.query<void, [string, string]>("INSERT INTO blackboard (key, value) VALUES (?, ?)");
    ins.run("auth_retry_count", JSON.stringify({ value: 7, version: 1 }));
    ins.run("auth_retry_timestamps", JSON.stringify({ value: [old1, recent2, recent1], version: 1 }));
    db.close();
    const result = handleAuthRetrySummary({}) as Record<string, unknown>;
    expect(result["count_total"]).toBe(7);
    expect(result["count_24h"]).toBe(2);
    expect(result["last_seen"]).toBe(recent1);
  });

  it("empty timestamps array -> count_24h:0, last_seen:null", () => {
    const dbPath = join(tmpDir, "state.db");
    const db = new Database(dbPath);
    db.exec("CREATE TABLE IF NOT EXISTS blackboard (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
    const ins = db.query<void, [string, string]>("INSERT INTO blackboard (key, value) VALUES (?, ?)");
    ins.run("auth_retry_count", JSON.stringify({ value: 3, version: 1 }));
    ins.run("auth_retry_timestamps", JSON.stringify({ value: [], version: 1 }));
    db.close();
    const result = handleAuthRetrySummary({}) as Record<string, unknown>;
    expect(result["count_total"]).toBe(3);
    expect(result["count_24h"]).toBe(0);
    expect(result["last_seen"]).toBeNull();
  });
});


// --- §3 circuit_breaker.summary ---

describe("handleCircuitBreakerSummary", () => {
  let tmpDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-cb-' + Date.now() + '-' + r);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_STATE_DIR = tmpDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
  });

  it("no blackboard dir -> {tripped:[], warnings:[], threshold:3}", () => {
    const result = handleCircuitBreakerSummary({}) as Record<string, unknown>;
    expect(Array.isArray(result["tripped"])).toBe(true);
    expect((result["tripped"] as unknown[]).length).toBe(0);
    expect(Array.isArray(result["warnings"])).toBe(true);
    expect((result["warnings"] as unknown[]).length).toBe(0);
    expect(result["threshold"]).toBe(3);
  });

  it("happy path: tripped and warnings classified correctly", () => {
    const bbRoot = join(tmpDir, "blackboard");
    mkdirSync(join(bbRoot, "failures"), { recursive: true });
    mkdirSync(join(bbRoot, "failures_meta"), { recursive: true });
    // disc 10: count=4 -> tripped
    writeFileSync(join(bbRoot, "failures", "10.json"), JSON.stringify({ value: 4, version: 1 }));
    writeFileSync(join(bbRoot, "failures_meta", "10.json"), JSON.stringify({ value: { agent: "executor", reason: "timeout", updated_at: "2026-05-01T12:00:00Z" }, version: 1 }));
    // disc 20: count=2 -> warning
    writeFileSync(join(bbRoot, "failures", "20.json"), JSON.stringify({ value: 2, version: 1 }));
    writeFileSync(join(bbRoot, "failures_meta", "20.json"), JSON.stringify({ value: { agent: "code-reviewer", reason: "lint_fail", updated_at: "2026-05-02T10:00:00Z" }, version: 1 }));
    const result = handleCircuitBreakerSummary({}) as Record<string, unknown>;
    const tripped = result["tripped"] as Array<Record<string, unknown>>;
    const warnings = result["warnings"] as Array<Record<string, unknown>>;
    expect(tripped.length).toBe(1);
    expect(tripped[0]["discussion"]).toBe(10);
    expect(tripped[0]["count"]).toBe(4);
    expect(tripped[0]["agent"]).toBe("executor");
    expect(tripped[0]["reason"]).toBe("timeout");
    expect(warnings.length).toBe(1);
    expect(warnings[0]["discussion"]).toBe(20);
    expect(warnings[0]["count"]).toBe(2);
    expect(warnings[0]["agent"]).toBe("code-reviewer");
    expect(result["threshold"]).toBe(3);
  });

  it("count=0 entries excluded from warnings", () => {
    const bbRoot = join(tmpDir, "blackboard");
    mkdirSync(join(bbRoot, "failures"), { recursive: true });
    writeFileSync(join(bbRoot, "failures", "99.json"), JSON.stringify({ value: 0, version: 1 }));
    const result = handleCircuitBreakerSummary({}) as Record<string, unknown>;
    expect((result["tripped"] as unknown[]).length).toBe(0);
    expect((result["warnings"] as unknown[]).length).toBe(0);
  });
});

// --- §4 circuitBreaker.history ---

describe("handleCircuitBreakerHistory", () => {
  let tmpDir: string;
  let atDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-cbh-' + Date.now() + '-' + r);
    atDir = join(tmpDir, ".autonomous-team");
    mkdirSync(atDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_STATE_DIR = tmpDir;
    process.env.AF_REPO_ROOT = tmpDir;
    process.env.AUTONOMOUS_TEAM_DIR = atDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    delete process.env.AF_REPO_ROOT;
    delete process.env.AUTONOMOUS_TEAM_DIR;
  });

  it("role param required -> throws -32602", () => {
    expect(() => handleCircuitBreakerHistory({})).toThrow();
    try { handleCircuitBreakerHistory({}); } catch (e) {
      expect((e as Error & { rpc_code?: number }).rpc_code).toBe(-32602);
    }
  });

  it("no history file -> []", () => {
    const result = handleCircuitBreakerHistory({ role: "executor" });
    expect(result).toEqual([]);
  });

  it("happy path: filters by role, returns last N entries", () => {
    const data = [
      JSON.stringify({ role: "executor", discussion: 10, ts: "2026-05-01T10:00:00Z" }),
      JSON.stringify({ role: "code-reviewer", discussion: 11, ts: "2026-05-01T11:00:00Z" }),
      JSON.stringify({ role: "executor", discussion: 12, ts: "2026-05-01T12:00:00Z" }),
      JSON.stringify({ role: "executor", discussion: 13, ts: "2026-05-01T13:00:00Z" }),
    ];
    writeFileSync(join(tmpDir, "circuit-breaker-history.jsonl"), data.join("\n") + "\n");
    const result = handleCircuitBreakerHistory({ role: "executor", limit: 2 }) as Array<Record<string, unknown>>;
    expect(result.length).toBe(2);
    expect(result[0]["discussion"]).toBe(12);
    expect(result[1]["discussion"]).toBe(13);
  });

  it("default limit=20 when not specified", () => {
    const entries = Array.from({ length: 25 }, (_, i) =>
      JSON.stringify({ role: "executor", discussion: i, ts: "2026-05-01T00:00:00Z" })
    );
    writeFileSync(join(tmpDir, "circuit-breaker-history.jsonl"), entries.join("\n") + "\n");
    const result = handleCircuitBreakerHistory({ role: "executor" }) as unknown[];
    expect(result.length).toBe(20);
  });
});

// --- §5 kpi.history ---

describe("handleKpiHistory", () => {
  let tmpDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-kpi-' + Date.now() + '-' + r);
    mkdirSync(tmpDir, { recursive: true });
    process.env.AF_REPO_ROOT = tmpDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AF_REPO_ROOT;
  });

  it("days=0 -> throws -32602", () => {
    expect(() => handleKpiHistory({ days: 0 })).toThrow();
    try { handleKpiHistory({ days: 0 }); } catch (e) {
      expect((e as Error & { rpc_code?: number }).rpc_code).toBe(-32602);
    }
  });

  it("days=NaN string -> throws -32602", () => {
    expect(() => handleKpiHistory({ days: "abc" })).toThrow();
    try { handleKpiHistory({ days: "abc" }); } catch (e) {
      expect((e as Error & { rpc_code?: number }).rpc_code).toBe(-32602);
    }
  });

  it("squash-merge regex: matches PR subjects correctly", () => {
    // Test the regex pattern used internally by kpi.history parsing
    const PR_RE = /^#\d+:|\(#\d+\)\s*$/;
    const subjects = [
      "#1432: migrate sessions",
      "add URL detection (#1431)",
      "add URL detection",
      "#1430: fix something (extra)",
      "random commit #1430",
    ];
    const matched = subjects.filter(s => PR_RE.test(s));
    expect(matched.length).toBe(3);
    expect(matched[0]).toBe("#1432: migrate sessions");
    expect(matched[1]).toBe("add URL detection (#1431)");
    expect(matched[2]).toBe("#1430: fix something (extra)");
  });

  it("no git repo at AF_REPO_ROOT -> [] (graceful)", () => {
    // tmpDir has no .git -> git log fails -> handler returns []
    const result = handleKpiHistory({ days: 7 });
    expect(Array.isArray(result)).toBe(true);
  });

  it("default days=30 when unspecified -> no throw", () => {
    const result = handleKpiHistory({});
    expect(Array.isArray(result)).toBe(true);
  });
});

// --- §6 kpi.cycle_time ---

describe("handleKpiCycleTime", () => {
  let tmpDir: string;
  let atDir: string;
  let stateDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-kpict-' + Date.now() + '-' + r);
    atDir = join(tmpDir, ".autonomous-team");
    stateDir = join(tmpDir, "state");
    mkdirSync(atDir, { recursive: true });
    mkdirSync(stateDir, { recursive: true });
    process.env.AF_REPO_ROOT = tmpDir;
    process.env.AUTONOMOUS_TEAM_DIR = atDir;
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AF_REPO_ROOT;
    delete process.env.AUTONOMOUS_TEAM_DIR;
    delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
  });

  it("no registry.json -> zero buckets", () => {
    const result = handleKpiCycleTime({}) as Array<Record<string, unknown>>;
    expect(result.length).toBe(4);
    for (const row of result) {
      expect(typeof row["bucket"]).toBe("string");
      expect(row["count"]).toBe(0);
    }
    const buckets = result.map(r => r["bucket"]);
    expect(buckets).toContain("0-2h");
    expect(buckets).toContain("2-6h");
    expect(buckets).toContain("6-24h");
    expect(buckets).toContain("24h+");
  });

  it("days=0 -> throws -32602", () => {
    expect(() => handleKpiCycleTime({ days: 0 })).toThrow();
    try { handleKpiCycleTime({ days: 0 }); } catch (e) {
      expect((e as Error & { rpc_code?: number }).rpc_code).toBe(-32602);
    }
  });

  it("happy path: buckets computed from registry discussions", () => {
    const now = new Date();
    const makeDisc = (num: number, hoursAgo: number, cycleDurationHours: number) => {
      const closedAt = new Date(now.getTime() - hoursAgo * 3600 * 1000);
      const createdAt = new Date(closedAt.getTime() - cycleDurationHours * 3600 * 1000);
      return { number: num, status: "DONE", closed_at: closedAt.toISOString(), created_at: createdAt.toISOString() };
    };
    const registry = {
      discussions: [
        makeDisc(1, 1, 1),   // 1h cycle -> 0-2h
        makeDisc(2, 2, 4),   // 4h cycle -> 2-6h
        makeDisc(3, 3, 12),  // 12h cycle -> 6-24h
        makeDisc(4, 4, 48),  // 48h cycle -> 24h+
        { number: 5, status: "DISCUSSING", closed_at: now.toISOString(), created_at: new Date(now.getTime() - 3600000).toISOString() },
      ],
    };
    writeFileSync(join(atDir, "registry.json"), JSON.stringify(registry));
    const result = handleKpiCycleTime({ days: 7 }) as Array<Record<string, unknown>>;
    expect(result.length).toBe(4);
    const byBucket: Record<string, number> = {};
    for (const row of result) byBucket[row["bucket"] as string] = row["count"] as number;
    expect(byBucket["0-2h"]).toBe(1);
    expect(byBucket["2-6h"]).toBe(1);
    expect(byBucket["6-24h"]).toBe(1);
    expect(byBucket["24h+"]).toBe(1);
  });
});

// --- §7 cost.per_discussion ---

describe("handleCostPerDiscussion", () => {
  let tmpDir: string;
  let stateDir: string;
  let atDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-cpd-' + Date.now() + '-' + r);
    stateDir = join(tmpDir, "state");
    atDir = join(tmpDir, ".autonomous-team");
    mkdirSync(stateDir, { recursive: true });
    mkdirSync(atDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
    process.env.AUTONOMOUS_TEAM_DIR = atDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    delete process.env.AUTONOMOUS_TEAM_DIR;
  });

  it("missing discussion param -> throws -32602", () => {
    expect(() => handleCostPerDiscussion({})).toThrow();
    try { handleCostPerDiscussion({}); } catch (e) {
      expect((e as Error & { rpc_code?: number }).rpc_code).toBe(-32602);
    }
  });

  it("no agent records -> null (discussion not found)", () => {
    const result = handleCostPerDiscussion({ discussion: 42 });
    expect(result).toBeNull();
  });

  it("happy path: aggregates agent records for a discussion", () => {
    const agentsDir = join(stateDir, "blackboard", "budget", "agents");
    mkdirSync(agentsDir, { recursive: true });
    const mkRec = (id: string, role: string, disc: number, pr: number | null, inp: number, out: number) => ({
      value: { agent: role, agent_id: id, input: inp, output: out, cache_read_tokens: 0, cache_write_tokens: 0,
               model: "claude-sonnet-4-6", finished: new Date().toISOString(), discussion: disc, pr },
      version: 1,
    });
    writeFileSync(join(agentsDir, "exec-001.json"), JSON.stringify(mkRec("exec-001", "executor", 42, 100, 10000, 2000)));
    writeFileSync(join(agentsDir, "cr-001.json"), JSON.stringify(mkRec("cr-001", "code-reviewer", 42, 100, 5000, 1000)));
    writeFileSync(join(agentsDir, "exec-002.json"), JSON.stringify(mkRec("exec-002", "executor", 99, null, 8000, 3000)));
    const result = handleCostPerDiscussion({ discussion: 42 }) as Record<string, unknown>;
    expect(result).not.toBeNull();
    expect(result["discussion"]).toBe(42);
    expect(result["agent_count"]).toBe(2);
    expect(typeof result["cost_usd"]).toBe("number");
    expect((result["cost_usd"] as number)).toBeGreaterThan(0);
    const ab = result["agent_breakdown"] as Record<string, number>;
    expect(ab["executor"]).toBeGreaterThan(0);
    expect(ab["code-reviewer"]).toBeGreaterThan(0);
    const pb = result["pr_breakdown"] as Record<string, number>;
    expect(pb["100"]).toBeGreaterThan(0);
  });
});

// --- §8 cost.by_discussion ---

describe("handleCostByDiscussion", () => {
  let tmpDir: string;
  let stateDir: string;
  let atDir: string;
  beforeEach(() => {
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-cbd-' + Date.now() + '-' + r);
    stateDir = join(tmpDir, "state");
    atDir = join(tmpDir, ".autonomous-team");
    mkdirSync(stateDir, { recursive: true });
    mkdirSync(atDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
    process.env.AUTONOMOUS_TEAM_DIR = atDir;
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.AUTONOMOUS_TEAM_STATE_DIR;
    delete process.env.AUTONOMOUS_TEAM_DIR;
  });

  it("top=0 -> throws -32602", () => {
    expect(() => handleCostByDiscussion({ top: 0 })).toThrow();
    try { handleCostByDiscussion({ top: 0 }); } catch (e) {
      expect((e as Error & { rpc_code?: number }).rpc_code).toBe(-32602);
    }
  });

  it("days=0 -> throws -32602", () => {
    expect(() => handleCostByDiscussion({ days: 0 })).toThrow();
    try { handleCostByDiscussion({ days: 0 }); } catch (e) {
      expect((e as Error & { rpc_code?: number }).rpc_code).toBe(-32602);
    }
  });

  it("no agent records -> []", () => {
    const result = handleCostByDiscussion({});
    expect(result).toEqual([]);
  });

  it("happy path: aggregates, sorts by usd desc, respects top param", () => {
    const agentsDir = join(stateDir, "blackboard", "budget", "agents");
    mkdirSync(agentsDir, { recursive: true });
    const recent = new Date().toISOString();
    const mkRec = (id: string, disc: number, inp: number, out: number) => ({
      value: { agent: "executor", agent_id: id, input: inp, output: out, cache_read_tokens: 0, cache_write_tokens: 0,
               model: "claude-sonnet-4-6", finished: recent, discussion: disc, pr: null },
      version: 1,
    });
    writeFileSync(join(agentsDir, "a1.json"), JSON.stringify(mkRec("a1", 10, 1000, 200)));
    writeFileSync(join(agentsDir, "a2.json"), JSON.stringify(mkRec("a2", 20, 100000, 20000)));
    writeFileSync(join(agentsDir, "a3.json"), JSON.stringify(mkRec("a3", 30, 10000, 2000)));
    const result = handleCostByDiscussion({ top: 2 }) as Array<Record<string, unknown>>;
    expect(result.length).toBe(2);
    expect(result[0]["discussion"]).toBe(20);
    expect(result[1]["discussion"]).toBe(30);
    expect((result[0]["usd"] as number)).toBeGreaterThan(result[1]["usd"] as number);
    expect(typeof result[0]["tokens"]).toBe("number");
    expect(typeof result[0]["usd"]).toBe("number");
  });

  it("days filter excludes old entries", () => {
    const agentsDir = join(stateDir, "blackboard", "budget", "agents");
    mkdirSync(agentsDir, { recursive: true });
    const recent = new Date().toISOString();
    const old = new Date(Date.now() - 100 * 24 * 3600 * 1000).toISOString();
    writeFileSync(join(agentsDir, "b1.json"), JSON.stringify({
      value: { agent: "executor", agent_id: "b1", input: 5000, output: 1000, cache_read_tokens: 0, cache_write_tokens: 0,
               model: "claude-sonnet-4-6", finished: recent, discussion: 10, pr: null },
      version: 1,
    }));
    writeFileSync(join(agentsDir, "b2.json"), JSON.stringify({
      value: { agent: "executor", agent_id: "b2", input: 50000, output: 10000, cache_read_tokens: 0, cache_write_tokens: 0,
               model: "claude-sonnet-4-6", finished: old, discussion: 20, pr: null },
      version: 1,
    }));
    const result = handleCostByDiscussion({ days: 30 }) as Array<Record<string, unknown>>;
    expect(result.length).toBe(1);
    expect(result[0]["discussion"]).toBe(10);
  });
});

// --- §9 Dispatch: all 8 handlers reach native handlers ---

describe("POST /rpc — misc-batch5 dispatch (no Python backend)", () => {
  const TOKEN = "test-rpc-b5-dispatch-token";
  let tokenDir: string;
  let tmpDir: string;
  let stateDir: string;
  let atDir: string;
  let app: Hono;

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    const r = Math.random().toString(36).slice(2);
    tmpDir = join(tmpdir(), 'rpc-b5-dispatch-' + Date.now() + '-' + r);
    stateDir = join(tmpDir, "state");
    atDir = join(tmpDir, ".autonomous-team");
    mkdirSync(stateDir, { recursive: true });
    mkdirSync(atDir, { recursive: true });
    process.env.AUTONOMOUS_TEAM_STATE_DIR = stateDir;
    process.env.AF_REPO_ROOT = tmpDir;
    process.env.AUTONOMOUS_TEAM_DIR = atDir;
    delete process.env.AF_API_AUTH_KEY;
  });

  afterEach(() => {
    cleanup(tokenDir);
    try { rmSync(tmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
    delete process.env.AUTONOMOUS_TEAM_DIR;
  });

  it("dial.list -> HTTP 200, result has dials array", async () => {
    const { status, body } = await rpc(app, "dial.list", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["dials"])).toBe(true);
  });

  it("auth_retry.summary -> HTTP 200, result has count_24h/count_total/last_seen", async () => {
    const { status, body } = await rpc(app, "auth_retry.summary", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(typeof result["count_24h"]).toBe("number");
    expect(typeof result["count_total"]).toBe("number");
    expect("last_seen" in result).toBe(true);
  });

  it("circuit_breaker.summary -> HTTP 200, result has tripped/warnings/threshold", async () => {
    const { status, body } = await rpc(app, "circuit_breaker.summary", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["tripped"])).toBe(true);
    expect(Array.isArray(result["warnings"])).toBe(true);
    expect(result["threshold"]).toBe(3);
  });

  it("circuitBreaker.history with missing role -> -32602 (not -32601 method-not-found)", async () => {
    const { status, body } = await rpc(app, "circuitBreaker.history", {}, TOKEN);
    expect(status).toBe(200);
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32602);
    expect(err["code"]).not.toBe(-32601);
  });

  it("kpi.history -> HTTP 200, result is array", async () => {
    const { status, body } = await rpc(app, "kpi.history", { days: 7 }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    expect(Array.isArray(body["result"])).toBe(true);
  });

  it("kpi.cycle_time -> HTTP 200, result has 4 buckets", async () => {
    const { status, body } = await rpc(app, "kpi.cycle_time", { days: 30 }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Array<Record<string, unknown>>;
    expect(Array.isArray(result)).toBe(true);
    expect(result.length).toBe(4);
    const buckets = result.map(r => r["bucket"]);
    expect(buckets).toContain("0-2h");
    expect(buckets).toContain("2-6h");
    expect(buckets).toContain("6-24h");
    expect(buckets).toContain("24h+");
  });

  it("cost.per_discussion with missing param -> -32602 (not -32601)", async () => {
    const { status, body } = await rpc(app, "cost.per_discussion", {}, TOKEN);
    expect(status).toBe(200);
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32602);
    expect(err["code"]).not.toBe(-32601);
  });

  it("cost.by_discussion -> HTTP 200, result is array", async () => {
    const { status, body } = await rpc(app, "cost.by_discussion", { top: 5, days: 30 }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    expect(Array.isArray(body["result"])).toBe(true);
  });
});
