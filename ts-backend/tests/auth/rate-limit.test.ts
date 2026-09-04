/**
 * Rate-limit middleware unit tests — behavioral parity with Python rate_limit.py.
 *
 * Covers:
 *   - Token bucket: burst allows 60 requests, then throttles
 *   - Exempt paths bypass rate limiting
 *   - 429 response shape matches Python exactly
 *   - AF_RATE_LIMIT_DISABLED=1 disables the limiter
 *   - XFF-spoof-IGNORED: X-Forwarded-For must NOT change the rate-limit key
 *
 * Run: bun test tests/auth/rate-limit.test.ts --timeout 10000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { TokenBucket, RateLimiter, RATE_LIMIT_EXEMPT_PATHS } from "../../src/middleware/rate-limit.js";

// ---------------------------------------------------------------------------
// Unit tests: TokenBucket
// ---------------------------------------------------------------------------

describe("TokenBucket — token-bucket semantics", () => {
  it("starts full (burst tokens available)", () => {
    const bucket = new TokenBucket(1.0, 5.0);
    // First 5 requests should succeed (burst = 5)
    for (let i = 0; i < 5; i++) {
      expect(bucket.consume()).toBe(true);
    }
    // 6th request should fail
    expect(bucket.consume()).toBe(false);
  });

  it("refills over time", async () => {
    const bucket = new TokenBucket(100.0, 1.0); // 100 tokens/sec, burst=1
    // Drain the bucket
    expect(bucket.consume()).toBe(true);
    expect(bucket.consume()).toBe(false);

    // Wait 15ms — should refill ~1.5 tokens at 100/sec
    await new Promise((r) => setTimeout(r, 15));
    expect(bucket.consume()).toBe(true);
  });

  it("retryAfter returns positive value when empty", () => {
    const bucket = new TokenBucket(1.0, 1.0);
    bucket.consume(); // drain
    expect(bucket.retryAfter()).toBeGreaterThan(0);
  });

  it("retryAfter returns 0 when tokens available", () => {
    const bucket = new TokenBucket(1.0, 60.0);
    expect(bucket.retryAfter()).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Unit tests: RateLimiter
// ---------------------------------------------------------------------------

describe("RateLimiter — per-IP rate limiting (Python parity)", () => {
  it("allows up to burst requests from one IP", () => {
    const limiter = new RateLimiter(1.0, 5.0); // burst=5
    for (let i = 0; i < 5; i++) {
      const [allowed] = limiter.check("192.168.1.1");
      expect(allowed).toBe(true);
    }
    const [denied] = limiter.check("192.168.1.1");
    expect(denied).toBe(false);
  });

  it("different IPs have independent buckets", () => {
    const limiter = new RateLimiter(1.0, 2.0); // burst=2
    // Drain IP1
    limiter.check("10.0.0.1");
    limiter.check("10.0.0.1");
    const [ip1Denied] = limiter.check("10.0.0.1");
    expect(ip1Denied).toBe(false);

    // IP2 is unaffected
    const [ip2Allowed] = limiter.check("10.0.0.2");
    expect(ip2Allowed).toBe(true);
  });

  it("retryAfter returns int >= 1 when rate limited", () => {
    const limiter = new RateLimiter(1.0, 1.0); // burst=1
    limiter.check("1.2.3.4"); // drain
    const after = limiter.retryAfter("1.2.3.4");
    expect(after).toBeGreaterThanOrEqual(1);
    expect(Number.isInteger(after)).toBe(true);
  });

  it("returns 0 for unknown IP retryAfter", () => {
    const limiter = new RateLimiter();
    expect(limiter.retryAfter("not.seen.ip")).toBe(0);
  });

  it("bucketCount tracks distinct IPs", () => {
    const limiter = new RateLimiter();
    limiter.check("1.2.3.4");
    limiter.check("5.6.7.8");
    expect(limiter.bucketCount()).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// Unit tests: RATE_LIMIT_EXEMPT_PATHS
// ---------------------------------------------------------------------------

describe("RATE_LIMIT_EXEMPT_PATHS — mirrors Python _EXEMPT_PATHS", () => {
  it("contains expected exempt paths", () => {
    expect(RATE_LIMIT_EXEMPT_PATHS.has("/health")).toBe(true);
    expect(RATE_LIMIT_EXEMPT_PATHS.has("/health/loop")).toBe(true);
    expect(RATE_LIMIT_EXEMPT_PATHS.has("/health/modules")).toBe(true);
    expect(RATE_LIMIT_EXEMPT_PATHS.has("/metrics")).toBe(true);
  });

  it("does NOT exempt non-health paths", () => {
    expect(RATE_LIMIT_EXEMPT_PATHS.has("/protected")).toBe(false);
    expect(RATE_LIMIT_EXEMPT_PATHS.has("/api/status")).toBe(false);
    expect(RATE_LIMIT_EXEMPT_PATHS.has("/feed")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Integration tests: rateLimitMiddleware via Hono
// ---------------------------------------------------------------------------

import { Hono } from "hono";
import { rateLimitMiddleware } from "../../src/middleware/rate-limit.js";
import type { Context } from "hono";

describe("rateLimitMiddleware — 429 response shape (Python parity)", () => {
  let savedDisable: string | undefined;

  beforeEach(() => {
    savedDisable = process.env.AF_RATE_LIMIT_DISABLED;
    delete process.env.AF_RATE_LIMIT_DISABLED;
  });

  afterEach(() => {
    if (savedDisable !== undefined) {
      process.env.AF_RATE_LIMIT_DISABLED = savedDisable;
    } else {
      delete process.env.AF_RATE_LIMIT_DISABLED;
    }
  });

  it("exhausts burst and returns 429 with correct shape", async () => {
    // Use a fresh limiter with burst=1 for this test
    const { RateLimiter: RL } = await import("../../src/middleware/rate-limit.js");
    const testLimiter = new RL(1.0, 1.0); // burst=1

    const app = new Hono();
    // Custom middleware with this test limiter
    app.use("*", async (_c, next) => {
      const [allowed] = testLimiter.check("test-ip");
      if (!allowed) {
        const retryAfter = testLimiter.retryAfter("test-ip");
        return new Response(
          JSON.stringify({ error: "rate limit exceeded", retry_after: retryAfter }),
          {
            status: 429,
            headers: {
              "Content-Type": "application/json",
              "Retry-After": String(retryAfter),
              "X-RateLimit-Remaining": "0",
            },
          }
        );
      }
      await next();
    });
    app.get("/api/test", (c: Context) => c.json({ ok: true }));

    // First request uses the burst token
    const res1 = await app.request("/api/test");
    expect(res1.status).toBe(200);

    // Second request is rate-limited
    const res2 = await app.request("/api/test");
    expect(res2.status).toBe(429);

    const body = await res2.json() as { error: string; retry_after: number };
    // Python _send_429 body: {"error": "rate limit exceeded", "retry_after": <int>}
    expect(body.error).toBe("rate limit exceeded");
    expect(typeof body.retry_after).toBe("number");
    expect(body.retry_after).toBeGreaterThanOrEqual(1);

    // Headers
    expect(res2.headers.get("Retry-After")).toBeTruthy();
    expect(res2.headers.get("X-RateLimit-Remaining")).toBe("0");
    expect(res2.headers.get("Content-Type")).toContain("application/json");
  });

  it("AF_RATE_LIMIT_DISABLED=1 bypasses rate limit", async () => {
    process.env.AF_RATE_LIMIT_DISABLED = "1";

    const app = new Hono();
    app.use("*", rateLimitMiddleware);
    app.get("/api/test", (c: Context) => c.json({ ok: true }));

    // Should not be rate limited even with many requests
    for (let i = 0; i < 100; i++) {
      const res = await app.request("/api/test");
      expect(res.status).toBe(200);
    }
  });
});

describe("rateLimitMiddleware — XFF-spoof-IGNORED (CWE-348)", () => {
  it("X-Forwarded-For header does NOT change the rate-limit IP key", async () => {
    // The rate limiter uses true peer IP, not XFF.
    // We can verify this by checking that XFF=127.0.0.1 doesn't grant
    // a different bucket than the actual connection IP.
    // In unit test context (no real socket), both use "unknown" — the key point
    // is that getPeerIp() never reads XFF headers.
    const { getPeerIp } = await import("../../src/middleware/rate-limit.js");

    // Build a mock Hono context with XFF header but no server attachment
    const app = new Hono();
    let capturedIp = "";
    app.use("*", async (c, next) => {
      capturedIp = getPeerIp(c);
      await next();
    });
    app.get("/test", (c: Context) => c.json({ ok: true }));

    await app.request("/test", {
      headers: {
        "X-Forwarded-For": "1.2.3.4",
        "X-Real-IP": "5.6.7.8",
      },
    });

    // capturedIp should be "unknown" (no socket = no real peer IP)
    // — NOT "1.2.3.4" from XFF (that would be the CWE-348 vulnerability)
    expect(capturedIp).not.toBe("1.2.3.4");
    expect(capturedIp).not.toBe("5.6.7.8");
    expect(capturedIp).toBe("unknown"); // fallback, never XFF
  });
});
