/**
 * Tests for POST /rpc — JSON-RPC 2.0 dispatch parity (D#1437 P6a).
 *
 * Run: bun test tests/rpc.test.ts --timeout 30000
 *
 * Coverage:
 *  1. Dispatch layer: invalid-JSON→400, auth-fail→401, method-not-found→200/-32601
 *  2. Auth: missing token → 401, wrong token → 401, correct token → 200
 *  3. Spawn-guard: loop.start from test UA → 200/-32000 spawn_blocked_test_origin
 *  4. Deferred P6b methods: return method-not-found like unregistered methods
 *  5. Native stats.summary handler: returns {metrics:[]} when DB absent
 *  6. Native stats.series handler: 'name' required → 200/-32602
 *  7. Envelope parity: jsonrpc:"2.0", id echoed, result/error structure
 *
 * Gate 2 parity proof (no live Python backend needed for unit tests):
 *   - invalid-JSON body → HTTP 400, {jsonrpc:"2.0", id:null, error:{code:-32000}}
 *   - bad/missing auth  → HTTP 401, {jsonrpc:"2.0", id:<from_body>, error:{code:-32000}}
 *   - unknown method    → HTTP 200, {jsonrpc:"2.0", id:<from_body>, error:{code:-32601}}
 *   - spawn-guard       → HTTP 200, {jsonrpc:"2.0", id:<from_body>, error:{code:-32000, message:"spawn_blocked_test_origin"}}
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { rpcDispatchHandler } from "../src/routes/rpc.js";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// Create a fresh Hono app with the RPC handler and auth middleware.
// Set RPC_TOKEN_DIR to a temp dir containing a known token.
function makeApp(rpcToken?: string): { app: Hono; tokenDir: string } {
  const tokenDir = join(tmpdir(), `rpc-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(join(tokenDir, ".autonomous-team"), { recursive: true });

  if (rpcToken !== undefined) {
    writeFileSync(join(tokenDir, ".autonomous-team", "dashboard-token"), rpcToken + "\n");
  }
  // Point RPC_TOKEN_OVERRIDE env so the handler can find the token.
  // We set this before creating the app so the handler reads it at request time.
  process.env.RPC_TOKEN_DIR_OVERRIDE = tokenDir;

  // /rpc is in PUBLIC_ROUTES so default-deny lets it through; the handler
  // self-authenticates using the RPC token.
  const app = new Hono();
  app.use("*", defaultDenyMiddleware);
  app.post("/rpc", rpcDispatchHandler);

  return { app, tokenDir };
}

function cleanup(tokenDir: string) {
  try {
    rmSync(tokenDir, { recursive: true, force: true });
  } catch { /* ignore */ }
  delete process.env.RPC_TOKEN_DIR_OVERRIDE;
}

// Send a JSON-RPC request to the app.
async function rpc(
  app: Hono,
  method: string,
  params: Record<string, unknown> = {},
  id: unknown = 1,
  token?: string
): Promise<{ status: number; body: unknown }> {
  const envelope = { jsonrpc: "2.0", id, method, params };
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token !== undefined) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const resp = await app.request("/rpc", {
    method: "POST",
    headers,
    body: JSON.stringify(envelope),
  });
  const body = await resp.json();
  return { status: resp.status, body };
}

// ---------------------------------------------------------------------------
// §1 — Dispatch layer: JSON parsing + envelope shape
// ---------------------------------------------------------------------------

describe("POST /rpc — dispatch layer parity", () => {
  let tokenDir: string;
  let app: Hono;
  const TOKEN = "test-rpc-token-abc123";

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    delete process.env.AF_API_AUTH_KEY; // no REST auth for RPC tests
  });

  afterEach(() => cleanup(tokenDir));

  it("invalid JSON body → HTTP 400, error envelope with code -32000, id null", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "not-json{{{",
    });
    expect(resp.status).toBe(400);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["jsonrpc"]).toBe("2.0");
    expect(body["id"]).toBeNull();
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
    expect(typeof err["message"]).toBe("string");
    expect(body["result"]).toBeUndefined();
  });

  it("empty body with auth → HTTP 200 method-not-found (empty body treated as empty dict)", async () => {
    // Python: `req = json.loads(raw) if raw else {}` — empty body → {} → valid dispatch
    // method="" → not in _RPC_METHODS → 200 -32601.
    // TS mirrors this: empty body → req={} → auth check → method="" → not-found.
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${TOKEN}`,
      },
      body: "",
    });
    expect(resp.status).toBe(200);
    const body = await resp.json() as Record<string, unknown>;
    // method="" → not found → -32601
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32601);
  });

  it("JSON array body → HTTP 400 (body must be a JSON object)", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${TOKEN}`,
      },
      body: "[1, 2, 3]",
    });
    expect(resp.status).toBe(400);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["jsonrpc"]).toBe("2.0");
    expect(body["id"]).toBeNull();
  });

  it("request id is echoed in all responses", async () => {
    const { status, body } = await rpc(app, "nonexistent.method", {}, 42, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["id"]).toBe(42);
    expect(b["jsonrpc"]).toBe("2.0");
  });

  it("string id is echoed", async () => {
    const { body } = await rpc(app, "nonexistent.method", {}, "my-req-id", TOKEN);
    const b = body as Record<string, unknown>;
    expect(b["id"]).toBe("my-req-id");
  });

  it("null id is echoed", async () => {
    const { body } = await rpc(app, "nonexistent.method", {}, null, TOKEN);
    const b = body as Record<string, unknown>;
    expect(b["id"]).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// §2 — Auth parity
// ---------------------------------------------------------------------------

describe("POST /rpc — auth parity", () => {
  let tokenDir: string;
  let app: Hono;
  const TOKEN = "correct-token-xyz";

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    delete process.env.AF_API_AUTH_KEY;
  });

  afterEach(() => cleanup(tokenDir));

  it("no token → HTTP 401, code -32000, message 'unauthorized'", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "stats.summary", params: {} }),
    });
    expect(resp.status).toBe(401);
    const body = await resp.json() as Record<string, unknown>;
    expect(body["jsonrpc"]).toBe("2.0");
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
    expect(err["message"]).toBe("unauthorized");
  });

  it("wrong token → HTTP 401", async () => {
    const { status, body } = await rpc(app, "stats.summary", {}, 1, "wrong-token");
    expect(status).toBe(401);
    const b = body as Record<string, unknown>;
    const err = b["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
  });

  it("correct token via Bearer header → HTTP 200 (dispatches)", async () => {
    const { status } = await rpc(app, "stats.summary", {}, 1, TOKEN);
    // stats.summary is a native handler; if DuckDB absent returns {metrics:[]}
    expect(status).toBe(200);
  });

  it("correct token via ?token= query param → HTTP 200", async () => {
    const resp = await app.request(`/rpc?token=${TOKEN}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "stats.summary", params: {} }),
    });
    expect(resp.status).toBe(200);
  });

  it("missing token file → HTTP 401 (fail-closed)", async () => {
    // Remove the token file to simulate a missing config
    rmSync(join(tokenDir, ".autonomous-team", "dashboard-token"));
    const { status, body } = await rpc(app, "stats.summary", {}, 1, TOKEN);
    expect(status).toBe(401);
    const b = body as Record<string, unknown>;
    const err = b["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
    expect(err["message"]).toBe("unauthorized");
  });

  it("id from body is preserved in auth-fail 401 response", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // No Authorization header
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 99, method: "stats.summary", params: {} }),
    });
    expect(resp.status).toBe(401);
    const body = await resp.json() as Record<string, unknown>;
    // Python preserves the id in auth-fail response; we should too
    expect(body["id"]).toBe(99);
  });
});

// ---------------------------------------------------------------------------
// §3 — Method routing parity
// ---------------------------------------------------------------------------

describe("POST /rpc — method routing parity", () => {
  let tokenDir: string;
  let app: Hono;
  const TOKEN = "route-test-token";

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    delete process.env.AF_API_AUTH_KEY;
  });

  afterEach(() => cleanup(tokenDir));

  it("unknown method → HTTP 200, code -32601, message contains method name", async () => {
    const { status, body } = await rpc(app, "does.not.exist", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["jsonrpc"]).toBe("2.0");
    const err = b["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32601);
    expect((err["message"] as string).includes("does.not.exist")).toBe(true);
    expect(b["result"]).toBeUndefined();
  });

  it("deferred P6b method loop.start → method-not-found (not a spawn)", async () => {
    const { status, body } = await rpc(app, "loop.start", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const err = b["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32601);
    expect((err["message"] as string)).toContain("loop.start");
  });

  it("deferred P6b method loop.stop → method-not-found", async () => {
    const { status, body } = await rpc(app, "loop.stop", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const err = b["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32601);
  });

  // P6b: dial.set, auth_retry.record, fleet.discovery_ack are now native (no longer deferred).
  // They return -32000 (validation/handler error) not -32601 (method-not-found).

  it("P6b dial.set — now native, invalid params → -32000 (not -32601)", async () => {
    // No name param → validation error -32000 (handler throws, not method-not-found)
    const { status, body } = await rpc(app, "dial.set", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const err = b["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000); // -32000, NOT -32601
  });

  it("P6b auth_retry.record — now native, returns recorded:true or result (not method-not-found)", async () => {
    const { status, body } = await rpc(app, "auth_retry.record", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    // Returns a result (not a -32601 method-not-found error)
    expect("result" in b || "error" in b).toBe(true);
    if ("error" in b) {
      // If error, it must NOT be method-not-found (-32601)
      const err = b["error"] as Record<string, unknown>;
      expect(err["code"]).not.toBe(-32601);
    }
  });

  it("P6b fleet.discovery_ack — now native, missing project_name → ok:false result (not method-not-found)", async () => {
    const { status, body } = await rpc(app, "fleet.discovery_ack", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    // Returns ok:false result (not a -32601 method-not-found error)
    if ("result" in b) {
      const result = b["result"] as Record<string, unknown>;
      expect(result["ok"]).toBe(false);
    } else if ("error" in b) {
      const err = b["error"] as Record<string, unknown>;
      expect(err["code"]).not.toBe(-32601);
    }
  });
});

// ---------------------------------------------------------------------------
// §4 — Spawn-guard parity
// ---------------------------------------------------------------------------

describe("POST /rpc — spawn-guard parity", () => {
  let tokenDir: string;
  let app: Hono;
  const TOKEN = "spawn-guard-token";

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    delete process.env.AF_API_AUTH_KEY;
    delete process.env.AF_ALLOW_TEST_ORIGIN_SPAWNS;
  });

  afterEach(() => {
    cleanup(tokenDir);
    delete process.env.AF_ALLOW_TEST_ORIGIN_SPAWNS;
  });

  it("loop.start from HeadlessChrome UA → 200, code -32000, spawn_blocked_test_origin", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${TOKEN}`,
        "User-Agent": "Mozilla/5.0 HeadlessChrome/120.0.0.0",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "loop.start", params: {} }),
    });
    expect(resp.status).toBe(200);
    const body = await resp.json() as Record<string, unknown>;
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
    expect(err["message"]).toBe("spawn_blocked_test_origin");
  });

  it("loop.start from Puppeteer UA → spawn_blocked (case-insensitive)", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${TOKEN}`,
        "User-Agent": "puppeteer/21.0.0",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 2, method: "loop.start", params: {} }),
    });
    expect(resp.status).toBe(200);
    const body = await resp.json() as Record<string, unknown>;
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
    expect(err["message"]).toBe("spawn_blocked_test_origin");
  });

  it("loop.start from Vite origin → spawn_blocked", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${TOKEN}`,
        Origin: "http://localhost:5173",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 3, method: "loop.start", params: {} }),
    });
    expect(resp.status).toBe(200);
    const body = await resp.json() as Record<string, unknown>;
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
  });

  it("loop.start from playwright UA → spawn_blocked", async () => {
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${TOKEN}`,
        "User-Agent": "Playwright/1.40",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 4, method: "loop.start", params: {} }),
    });
    expect(resp.status).toBe(200);
    const body = await resp.json() as Record<string, unknown>;
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
    expect(err["message"]).toBe("spawn_blocked_test_origin");
  });

  it("loop.start with AF_ALLOW_TEST_ORIGIN_SPAWNS=1 bypasses spawn-guard → deferred P6b returns -32601", async () => {
    process.env.AF_ALLOW_TEST_ORIGIN_SPAWNS = "1";
    const resp = await app.request("/rpc", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${TOKEN}`,
        "User-Agent": "HeadlessChrome",
      },
      body: JSON.stringify({ jsonrpc: "2.0", id: 5, method: "loop.start", params: {} }),
    });
    expect(resp.status).toBe(200);
    const body = await resp.json() as Record<string, unknown>;
    // With bypass env, spawn-guard passes, but loop.start is DEFERRED → -32601
    const err = body["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32601);
    expect((err["message"] as string)).toContain("loop.start");
  });
});

// ---------------------------------------------------------------------------
// §5 — Native handler: stats.summary
// ---------------------------------------------------------------------------

describe("POST /rpc — stats.summary native handler", () => {
  let tokenDir: string;
  let app: Hono;
  const TOKEN = "stats-test-token";

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    delete process.env.AF_API_AUTH_KEY;
    // Point to a non-existent DB to trigger graceful fallback
    process.env.STATS_DB_PATH = "/tmp/nonexistent-stats-12345.duckdb";
  });

  afterEach(() => {
    cleanup(tokenDir);
    delete process.env.STATS_DB_PATH;
  });

  it("stats.summary with absent DB → HTTP 200, {metrics:[]}", async () => {
    const { status, body } = await rpc(app, "stats.summary", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["jsonrpc"]).toBe("2.0");
    expect(b["id"]).toBe(1);
    expect(b["error"]).toBeUndefined();
    const result = b["result"] as Record<string, unknown>;
    expect(Array.isArray(result["metrics"])).toBe(true);
  });

  it("stats.summary response wraps result in jsonrpc envelope", async () => {
    const { body } = await rpc(app, "stats.summary", {}, "req-42", TOKEN);
    const b = body as Record<string, unknown>;
    expect(b["jsonrpc"]).toBe("2.0");
    expect(b["id"]).toBe("req-42");
    expect("result" in b).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// §6 — Native handler: stats.series
// ---------------------------------------------------------------------------

describe("POST /rpc — stats.series native handler", () => {
  let tokenDir: string;
  let app: Hono;
  const TOKEN = "series-test-token";

  beforeEach(() => {
    const result = makeApp(TOKEN);
    app = result.app;
    tokenDir = result.tokenDir;
    delete process.env.AF_API_AUTH_KEY;
    process.env.STATS_DB_PATH = "/tmp/nonexistent-stats-series-12345.duckdb";
  });

  afterEach(() => {
    cleanup(tokenDir);
    delete process.env.STATS_DB_PATH;
  });

  it("stats.series with absent DB → HTTP 200, {name, points:[]}", async () => {
    const { status, body } = await rpc(app, "stats.series", { name: "loop_count" }, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    expect(b["error"]).toBeUndefined();
    const result = b["result"] as Record<string, unknown>;
    expect(result["name"]).toBe("loop_count");
    expect(Array.isArray(result["points"])).toBe(true);
  });

  it("stats.series with missing name → HTTP 200, code -32000 (mirrors Python ValueError → -32000)", async () => {
    // Python raises ValueError (no rpc_code attr) → getattr(exc, 'rpc_code', -32000) = -32000
    // Faithful mirror: -32000 not -32602.
    const { status, body } = await rpc(app, "stats.series", {}, 1, TOKEN);
    expect(status).toBe(200);
    const b = body as Record<string, unknown>;
    const err = b["error"] as Record<string, unknown>;
    expect(err["code"]).toBe(-32000);
    expect((err["message"] as string).includes("name")).toBe(true);
  });
});
