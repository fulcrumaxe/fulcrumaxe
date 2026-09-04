/**
 * Tests for /spawn-queue* and /spawn-blocks GET routes (D#1437 P2).
 *
 * Run: bun test tests/spawn-queue.test.ts --timeout 15000
 *
 * Covers:
 *  - Response shape for /spawn-queue (status), /spawn-queue/pending,
 *    /spawn-queue/active, /spawn-blocks
 *  - ?limit query param on /spawn-blocks
 *  - Negative-auth parity: 401 no-token, 403 wrong-token
 *  - Auth disabled passes all routes
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import {
  spawnQueueStatusHandler,
  spawnQueuePendingHandler,
  spawnQueueActiveHandler,
  spawnBlocksHandler,
  spawnBlocksSubHandler,
} from "../src/routes/spawn-queue.js";

let savedKey: string | undefined;

function makeApp(authKey?: string): Hono {
  if (authKey !== undefined) {
    process.env.AF_API_AUTH_KEY = authKey;
  } else {
    delete process.env.AF_API_AUTH_KEY;
  }

  const app = new Hono();
  app.use("*", defaultDenyMiddleware);

  app.get("/spawn-queue", spawnQueueStatusHandler);
  app.get("/spawn-queue/pending", spawnQueuePendingHandler);
  app.get("/spawn-queue/active", spawnQueueActiveHandler);
  app.get("/spawn-blocks", spawnBlocksHandler);
  app.get("/spawn-blocks/*", spawnBlocksSubHandler);

  return app;
}

// ---------------------------------------------------------------------------
// Auth disabled — all routes return 200
// ---------------------------------------------------------------------------

describe("/spawn-queue — auth disabled", () => {
  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    delete process.env.AF_API_AUTH_KEY;
  });

  afterEach(() => {
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
  });

  it("GET /spawn-queue returns 200 with status shape", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-queue");
    expect(res.status).toBe(200);
    const body = await res.json() as Record<string, unknown>;
    expect(typeof body["pending"]).toBe("number");
    expect(typeof body["active_total"]).toBe("number");
    expect(typeof body["total_limit"]).toBe("number");
    expect(typeof body["utilization_pct"]).toBe("number");
    expect(typeof body["by_role"]).toBe("object");
    expect(typeof body["completed"]).toBe("number");
    expect(typeof body["failed"]).toBe("number");
  });

  it("GET /spawn-queue/pending returns 200 with pending array", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-queue/pending");
    expect(res.status).toBe(200);
    const body = await res.json() as Record<string, unknown>;
    expect(Array.isArray(body["pending"])).toBe(true);
  });

  it("GET /spawn-queue/active returns 200 with active array", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-queue/active");
    expect(res.status).toBe(200);
    const body = await res.json() as Record<string, unknown>;
    expect(Array.isArray(body["active"])).toBe(true);
  });

  it("GET /spawn-blocks returns 200 with array", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-blocks");
    expect(res.status).toBe(200);
    const body = await res.json() as unknown;
    expect(Array.isArray(body)).toBe(true);
  });

  it("GET /spawn-blocks?limit=5 respects limit", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-blocks?limit=5");
    expect(res.status).toBe(200);
    const body = await res.json() as unknown[];
    // Cannot exceed the limit
    expect(body.length).toBeLessThanOrEqual(5);
  });

  it("GET /spawn-blocks/anything returns same as /spawn-blocks (sub-path)", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-blocks/sub-path");
    expect(res.status).toBe(200);
    const body = await res.json() as unknown;
    expect(Array.isArray(body)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Response shape: spawn-queue status fields
// ---------------------------------------------------------------------------

describe("/spawn-queue status — field shapes", () => {
  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    delete process.env.AF_API_AUTH_KEY;
  });

  afterEach(() => {
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
  });

  it("utilization_pct is between 0 and 100", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-queue");
    const body = await res.json() as Record<string, unknown>;
    const pct = body["utilization_pct"] as number;
    expect(pct).toBeGreaterThanOrEqual(0);
    expect(pct).toBeLessThanOrEqual(100);
  });

  it("by_role contains expected roles from DEFAULT_LIMITS", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-queue");
    const body = await res.json() as Record<string, unknown>;
    const byRole = body["by_role"] as Record<string, unknown>;
    // Should have the standard roles from DEFAULT_LIMITS (minus _total)
    expect("executor" in byRole).toBe(true);
    expect("code-reviewer" in byRole).toBe(true);
    // Each role entry has active + limit
    const executor = byRole["executor"] as Record<string, unknown>;
    expect(typeof executor["active"]).toBe("number");
    expect(typeof executor["limit"]).toBe("number");
  });

  it("/spawn-blocks response items have expected shape (if any)", async () => {
    const app = makeApp();
    const res = await app.request("/spawn-blocks");
    const body = await res.json() as Record<string, unknown>[];
    for (const item of body) {
      expect(typeof item["role"]).toBe("string");
      expect(typeof item["reason"]).toBe("string");
      expect(typeof item["ts"]).toBe("string");
      // discussion may be null or a number/string
      expect("discussion" in item).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// Negative-auth parity
// ---------------------------------------------------------------------------

describe("/spawn-queue — negative-auth parity (auth enabled)", () => {
  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    process.env.AF_API_AUTH_KEY = "correct-key";
  });

  afterEach(() => {
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
  });

  it("GET /spawn-queue returns 401 with no token", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/spawn-queue");
    expect(res.status).toBe(401);
    const body = await res.json() as Record<string, unknown>;
    expect(body).toEqual({ detail: "unauthorized" });
  });

  it("GET /spawn-queue returns 403 with wrong token", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/spawn-queue", {
      headers: { Authorization: "Bearer wrong" },
    });
    expect(res.status).toBe(403);
    const body = await res.json() as Record<string, unknown>;
    expect(body).toEqual({ detail: "forbidden" });
  });

  it("GET /spawn-queue returns 200 with correct token", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/spawn-queue", {
      headers: { Authorization: "Bearer correct-key" },
    });
    expect(res.status).toBe(200);
  });

  it("GET /spawn-queue/pending returns 401 with no token", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/spawn-queue/pending");
    expect(res.status).toBe(401);
  });

  it("GET /spawn-queue/active returns 401 with no token", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/spawn-queue/active");
    expect(res.status).toBe(401);
  });

  it("GET /spawn-blocks returns 401 with no token", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/spawn-blocks");
    expect(res.status).toBe(401);
  });

  it("XFF does not bypass auth on /spawn-queue", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/spawn-queue", {
      headers: {
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
      },
    });
    expect(res.status).toBe(401);
  });
});
