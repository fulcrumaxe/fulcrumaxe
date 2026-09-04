/**
 * Tests for runs.* RPC handlers — D#1437 P6a-native.
 *
 * Run: bun test tests/rpc-runs.test.ts --timeout 30000
 *
 * Coverage:
 *  1. runs.by_role — missing role → error, valid role → {runs:[]}
 *  2. runs.percentiles — returns {p50, p95, p99, sample_size}
 *  3. runs.stuck — returns {runs:[]}
 *  4. runs.roundtrip — missing pr → error, valid pr → {pr, latency_seconds}
 *  5. runs.active_over_time — returns {points:[{ts, count}]}
 *  6. runs.recent — returns {runs:[]}
 *  7. All methods: DB absent → graceful empty (not 500)
 *  8. Dispatch: runs.* methods reach native handlers (not proxy path)
 *
 * Gate 1 parity: all handlers are wired into NATIVE_HANDLERS and removed from
 * PROXY_METHODS in routes/rpc.ts.
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { rpcDispatchHandler } from "../src/routes/rpc.js";
import {
  handleByRole,
  handlePercentiles,
  handleStuck,
  handleRoundtrip,
  handleActiveOverTime,
  handleRecent,
} from "../src/rpc/runs.js";

// ---------------------------------------------------------------------------
// App factory — mirrors rpc.test.ts setup
// ---------------------------------------------------------------------------

function makeApp(rpcToken: string): { app: Hono; tokenDir: string } {
  const tokenDir = join(tmpdir(), `rpc-runs-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
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
}

async function rpc(
  app: Hono,
  method: string,
  params: Record<string, unknown> = {},
  token: string = "test-rpc-token"
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
// §1 — Unit tests: handlers with DB absent (STATS_DB_PATH=/nonexistent)
// These validate graceful-empty behavior without a real DuckDB.
// ---------------------------------------------------------------------------

describe("runs.* handlers — DB absent graceful empty", () => {
  beforeEach(() => {
    process.env.STATS_DB_PATH = "/nonexistent/stats.duckdb";
  });
  afterEach(() => {
    delete process.env.STATS_DB_PATH;
  });

  it("handleByRole: missing role throws", async () => {
    await expect(handleByRole({})).rejects.toThrow("'role' parameter is required");
  });

  it("handleByRole: DB absent → {runs: []}", async () => {
    const result = await handleByRole({ role: "executor" }) as Record<string, unknown>;
    expect(result).toEqual({ runs: [] });
  });

  it("handlePercentiles: DB absent → {p50:null, p95:null, p99:null, sample_size:0}", async () => {
    const result = await handlePercentiles({}) as Record<string, unknown>;
    expect(result).toEqual({ p50: null, p95: null, p99: null, sample_size: 0 });
  });

  it("handlePercentiles: with role, DB absent → empty", async () => {
    const result = await handlePercentiles({ role: "executor" }) as Record<string, unknown>;
    expect(result).toEqual({ p50: null, p95: null, p99: null, sample_size: 0 });
  });

  it("handleStuck: DB absent → {runs: []}", async () => {
    const result = await handleStuck({}) as Record<string, unknown>;
    expect(result).toEqual({ runs: [] });
  });

  it("handleStuck: custom threshold_seconds, DB absent → {runs: []}", async () => {
    const result = await handleStuck({ threshold_seconds: 3600 }) as Record<string, unknown>;
    expect(result).toEqual({ runs: [] });
  });

  it("handleRoundtrip: missing pr throws", async () => {
    await expect(handleRoundtrip({})).rejects.toThrow("'pr' parameter is required");
  });

  it("handleRoundtrip: DB absent → {pr: 42, latency_seconds: null}", async () => {
    const result = await handleRoundtrip({ pr: 42 }) as Record<string, unknown>;
    expect(result).toEqual({ pr: 42, latency_seconds: null });
  });

  it("handleActiveOverTime: DB absent → {points: []}", async () => {
    const result = await handleActiveOverTime({}) as Record<string, unknown>;
    expect(result).toHaveProperty("points");
    // When DB is absent, points is []
    expect(result["points"]).toEqual([]);
  });

  it("handleRecent: DB absent → {runs: []}", async () => {
    const result = await handleRecent({}) as Record<string, unknown>;
    expect(result).toEqual({ runs: [] });
  });
});

// ---------------------------------------------------------------------------
// §2 — Dispatch: runs.* methods reach native handlers (not proxy)
// ---------------------------------------------------------------------------

describe("POST /rpc — runs.* dispatch (no Python backend needed)", () => {
  const TOKEN = "test-rpc-dispatch-token";
  let tokenDir: string;
  let app: Hono;

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    // Point at nonexistent DB so native handlers return graceful-empty
    process.env.STATS_DB_PATH = "/nonexistent/stats.duckdb";
    delete process.env.AF_API_AUTH_KEY;
  });

  afterEach(() => cleanup(tokenDir));

  it("runs.by_role with valid role → HTTP 200, result envelope with runs:[]", async () => {
    const { status, body } = await rpc(app, "runs.by_role", { role: "executor" }, TOKEN);
    expect(status).toBe(200);
    expect(body["jsonrpc"]).toBe("2.0");
    expect(body["id"]).toBe(1);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["runs"])).toBe(true);
  });

  it("runs.by_role with missing role → HTTP 200, error envelope", async () => {
    const { status, body } = await rpc(app, "runs.by_role", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeDefined();
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
  });

  it("runs.percentiles → HTTP 200, result with p50/p95/p99/sample_size", async () => {
    const { status, body } = await rpc(app, "runs.percentiles", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect("p50" in result).toBe(true);
    expect("p95" in result).toBe(true);
    expect("p99" in result).toBe(true);
    expect("sample_size" in result).toBe(true);
    expect(result["sample_size"]).toBe(0);
  });

  it("runs.stuck → HTTP 200, result with runs:[]", async () => {
    const { status, body } = await rpc(app, "runs.stuck", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["runs"])).toBe(true);
  });

  it("runs.roundtrip with valid pr → HTTP 200, result {pr, latency_seconds}", async () => {
    const { status, body } = await rpc(app, "runs.roundtrip", { pr: 99 }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(result["pr"]).toBe(99);
    expect("latency_seconds" in result).toBe(true);
  });

  it("runs.roundtrip with missing pr → HTTP 200, error", async () => {
    const { status, body } = await rpc(app, "runs.roundtrip", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeDefined();
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
  });

  it("runs.active_over_time → HTTP 200, result with points array", async () => {
    const { status, body } = await rpc(app, "runs.active_over_time", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["points"])).toBe(true);
  });

  it("runs.active_over_time with params → HTTP 200, result", async () => {
    const since = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    const until = new Date().toISOString();
    const { status, body } = await rpc(app, "runs.active_over_time", {
      since_iso: since,
      until_iso: until,
      bucket_seconds: 300,
    }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["points"])).toBe(true);
  });

  it("runs.recent → HTTP 200, result with runs:[]", async () => {
    const { status, body } = await rpc(app, "runs.recent", {}, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["runs"])).toBe(true);
  });

  it("runs.recent with custom limit → HTTP 200, result", async () => {
    const { status, body } = await rpc(app, "runs.recent", { limit: 10 }, TOKEN);
    expect(status).toBe(200);
    expect(body["error"]).toBeUndefined();
    const result = body["result"] as Record<string, unknown>;
    expect(Array.isArray(result["runs"])).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// §3 — Integration: live DuckDB (if stats.duckdb is available)
// ---------------------------------------------------------------------------

describe("runs.* handlers — live DuckDB integration (skipped if DB absent)", () => {
  it("handleByRole: returns array of run rows with correct field shapes", async () => {
    // Try to open the real DB; if absent skip gracefully
    const { openReadConn, closeConn } = await import("../src/duckdb-helpers.js");
    let h;
    try {
      h = await openReadConn();
      closeConn(h);
    } catch {
      console.warn("stats.duckdb not found — skipping live integration tests");
      return;
    }

    const result = await handleByRole({
      role: "executor",
      since_iso: new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString(),
    }) as Record<string, unknown>;

    expect(Array.isArray(result["runs"])).toBe(true);
    // If there are rows, validate expected fields
    const runs = result["runs"] as Record<string, unknown>[];
    for (const run of runs.slice(0, 3)) {
      expect(typeof run["agent_id"]).toBe("string");
      expect(typeof run["role"]).toBe("string");
      // start_ts must be ISO-8601 string
      if (run["start_ts"] !== null) {
        expect(run["start_ts"]).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
      }
    }
  });

  it("handlePercentiles: returns float or null for all percentile fields", async () => {
    const { openReadConn, closeConn } = await import("../src/duckdb-helpers.js");
    let h;
    try {
      h = await openReadConn();
      closeConn(h);
    } catch {
      return;
    }

    const result = await handlePercentiles({}) as Record<string, unknown>;
    expect(typeof result["sample_size"]).toBe("number");
    // Each percentile is null or a finite number
    for (const k of ["p50", "p95", "p99"]) {
      const v = result[k];
      if (v !== null) {
        expect(typeof v).toBe("number");
        expect(isFinite(v as number)).toBe(true);
      }
    }
  });

  it("handleRecent: timestamps are ISO-8601 strings", async () => {
    const { openReadConn, closeConn } = await import("../src/duckdb-helpers.js");
    let h;
    try {
      h = await openReadConn();
      closeConn(h);
    } catch {
      return;
    }

    const result = await handleRecent({ limit: 5 }) as Record<string, unknown>;
    const runs = result["runs"] as Record<string, unknown>[];
    for (const run of runs) {
      if (run["start_ts"] !== null) {
        expect(run["start_ts"]).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
      }
      if (run["end_ts"] !== null) {
        expect(run["end_ts"]).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
      }
    }
  });

  it("handleActiveOverTime: bucket ts values are ISO-8601", async () => {
    const { openReadConn, closeConn } = await import("../src/duckdb-helpers.js");
    let h;
    try {
      h = await openReadConn();
      closeConn(h);
    } catch {
      return;
    }

    const since = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    const until = new Date().toISOString();
    const result = await handleActiveOverTime({
      since_iso: since,
      until_iso: until,
      bucket_seconds: 600,
    }) as Record<string, unknown>;

    const points = result["points"] as Record<string, unknown>[];
    expect(points.length).toBeGreaterThan(0);
    for (const pt of points) {
      expect(pt["ts"]).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
      expect(typeof pt["count"]).toBe("number");
    }
  });
});
