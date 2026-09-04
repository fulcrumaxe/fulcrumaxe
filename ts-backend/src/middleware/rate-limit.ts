/**
 * Per-IP token-bucket rate limiter middleware for the Bun+Hono server.
 *
 * Ports backend/middleware/rate_limit.py + backend/rate_limiter.py to TypeScript
 * with exact behavioral parity.
 *
 * Parameters (matching Python backend exactly):
 *   rate=1.0 tokens/second, burst=60.0 tokens
 *   cleanup_interval=60.0s, stale_after=600.0s
 *
 * IP source:
 *   Derived from the raw socket / Bun server's remoteAddr — the TRUE peer IP.
 *   We intentionally do NOT read X-Forwarded-For; it is spoofable (CWE-348).
 *   In Hono on Bun, the peer IP is obtained via c.env?.server (Bun.Server)
 *   requestIP() helper. Falls back to "unknown" — never XFF.
 *
 * Rate-limit exemptions (matching legacy api.py exemptions exactly):
 *   /health, /health/loop, /health/modules — exempt
 *   /metrics — exempt
 *
 * 429 response body matches Python _send_429 exactly:
 *   {"error": "rate limit exceeded", "retry_after": <int>}
 *   Headers: Content-Type: application/json, Retry-After: <int>,
 *            X-RateLimit-Remaining: 0
 *
 * Disable: set AF_RATE_LIMIT_DISABLED=1
 */

import type { Context, MiddlewareHandler, Next } from "hono";

// ---------------------------------------------------------------------------
// Paths exempt from rate limiting — must match Python backend exactly.
// ---------------------------------------------------------------------------
export const RATE_LIMIT_EXEMPT_PATHS: ReadonlySet<string> = new Set([
  "/health",
  "/health/loop",
  "/health/modules",
  "/metrics",
]);

// ---------------------------------------------------------------------------
// Token bucket — mirrors Python TokenBucket in backend/rate_limiter.py
// ---------------------------------------------------------------------------
export class TokenBucket {
  private readonly rate: number;
  private readonly burst: number;
  private tokens: number;
  private lastRefill: number; // performance.now() ms

  constructor(rate: number, burst: number) {
    this.rate = rate;
    this.burst = burst;
    this.tokens = burst; // new bucket starts full
    this.lastRefill = performance.now();
  }

  private refill(): void {
    const now = performance.now();
    const elapsed = (now - this.lastRefill) / 1000; // convert ms → seconds
    this.tokens = Math.min(this.burst, this.tokens + elapsed * this.rate);
    this.lastRefill = now;
  }

  consume(): boolean {
    this.refill();
    if (this.tokens >= 1.0) {
      this.tokens -= 1.0;
      return true;
    }
    return false;
  }

  tokensRemaining(): number {
    this.refill();
    return this.tokens;
  }

  retryAfter(): number {
    this.refill();
    if (this.tokens >= 1.0) return 0;
    const deficit = 1.0 - this.tokens;
    return deficit / this.rate;
  }

  get lastSeen(): number {
    return this.lastRefill;
  }
}

// ---------------------------------------------------------------------------
// Rate limiter — mirrors Python RateLimiter in backend/rate_limiter.py
// ---------------------------------------------------------------------------
export class RateLimiter {
  private readonly rate: number;
  private readonly burst: number;
  private readonly staleAfterMs: number;
  private readonly cleanupIntervalMs: number;
  private readonly buckets: Map<string, TokenBucket> = new Map();
  private lastCleanup: number; // performance.now() ms

  constructor(
    rate = 1.0,
    burst = 60.0,
    cleanupInterval = 60.0, // seconds
    staleAfter = 600.0 // seconds
  ) {
    this.rate = rate;
    this.burst = burst;
    this.staleAfterMs = staleAfter * 1000;
    this.cleanupIntervalMs = cleanupInterval * 1000;
    this.lastCleanup = performance.now();
  }

  private getOrCreate(ip: string): TokenBucket {
    let bucket = this.buckets.get(ip);
    if (!bucket) {
      bucket = new TokenBucket(this.rate, this.burst);
      this.buckets.set(ip, bucket);
    }
    return bucket;
  }

  private maybeCleanup(): void {
    const now = performance.now();
    if (now - this.lastCleanup < this.cleanupIntervalMs) return;
    const cutoff = now - this.staleAfterMs;
    for (const [ip, bucket] of this.buckets) {
      if (bucket.lastSeen < cutoff) this.buckets.delete(ip);
    }
    this.lastCleanup = now;
  }

  check(ip: string): [boolean, number] {
    this.maybeCleanup();
    const bucket = this.getOrCreate(ip);
    const allowed = bucket.consume();
    const remaining = bucket.tokensRemaining();
    return [allowed, remaining];
  }

  retryAfter(ip: string): number {
    const bucket = this.buckets.get(ip);
    if (!bucket) return 0;
    const secs = bucket.retryAfter();
    return Math.max(1, Math.floor(secs) + 1);
  }

  bucketCount(): number {
    return this.buckets.size;
  }
}

// ---------------------------------------------------------------------------
// Shared limiter instance — same parameters as Python api.py:2017-2019
// ---------------------------------------------------------------------------
export const sharedLimiter = new RateLimiter(1.0, 60.0, 60.0, 600.0);

// ---------------------------------------------------------------------------
// Peer IP extraction
// ---------------------------------------------------------------------------

/**
 * Extract the true peer IP from a Hono/Bun context.
 *
 * Bun exposes the server via c.env?.server (BunServer) which has a
 * requestIP(request) method returning { address, port, family }.
 * We NEVER read X-Forwarded-For — it is spoofable (CWE-348).
 */
export function getPeerIp(c: Context): string {
  try {
    const server = (
      c.env as
        | {
            server?: {
              requestIP?: (
                req: Request
              ) => { address: string } | null;
            };
          }
        | undefined
    )?.server;
    if (server?.requestIP) {
      const addr = server.requestIP(c.req.raw);
      if (addr?.address) return addr.address;
    }
  } catch {
    // Fall through to unknown
  }
  return "unknown";
}

// ---------------------------------------------------------------------------
// Hono middleware
// ---------------------------------------------------------------------------

export const rateLimitMiddleware: MiddlewareHandler = async (
  c: Context,
  next: Next
): Promise<void | Response> => {
  // Check disable flag
  if (process.env.AF_RATE_LIMIT_DISABLED === "1") {
    await next();
    return;
  }

  const path = new URL(c.req.url).pathname;

  // Exempt paths pass through unconditionally
  if (RATE_LIMIT_EXEMPT_PATHS.has(path)) {
    await next();
    return;
  }

  const ip = getPeerIp(c);
  const [allowed] = sharedLimiter.check(ip);

  if (!allowed) {
    const retryAfter = sharedLimiter.retryAfter(ip);
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
};
