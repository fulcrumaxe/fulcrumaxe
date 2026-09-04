/**
 * Spawn-origin guard middleware for the Bun+Hono server.
 *
 * Ports backend/deps/origin_guard.py to TypeScript with byte-equivalent behavior.
 *
 * Blocks requests whose User-Agent matches HeadlessChrome, Puppeteer, or
 * Playwright (case-insensitive) and returns 403 with the exact same JSON body:
 *   {"error": "spawn_blocked_test_origin"}
 *
 * Apply this middleware ONLY to the three spawn-trigger routes:
 *   POST /api/loop/run
 *   POST /api/projects/{pid}/loop/run
 *   POST /api/innovate/tick
 *
 * Env-var bypasses (both preserve auth gate — only the UA check is skipped):
 *   AF_ALLOW_TEST_ORIGIN_SPAWNS=1  — legacy bypass for local human-driven dev
 *   AF_MCP_TEST_ORIGIN=1           — MCP Chrome DevTools scenario runs
 */

import type { Context, MiddlewareHandler, Next } from "hono";

// Exact same regex as backend/deps/origin_guard.py (and api.py:1767)
const TEST_UA_RE = /HeadlessChrome|Puppeteer|playwright/i;

/**
 * Middleware that blocks requests from test-runner User-Agents.
 *
 * Mirrors require_not_test_origin in backend/deps/origin_guard.py exactly:
 * - Blocks by User-Agent matching TEST_UA_RE (case-insensitive)
 * - Does NOT block by Origin alone
 * - Bypasses when AF_ALLOW_TEST_ORIGIN_SPAWNS=1 or AF_MCP_TEST_ORIGIN=1
 */
export const originGuardMiddleware: MiddlewareHandler = async (
  c: Context,
  next: Next
): Promise<void | Response> => {
  // Env-var bypasses (must check before UA inspection)
  if (process.env.AF_ALLOW_TEST_ORIGIN_SPAWNS?.trim() === "1") {
    await next();
    return;
  }
  if (process.env.AF_MCP_TEST_ORIGIN?.trim() === "1") {
    await next();
    return;
  }

  const ua = c.req.header("user-agent") ?? "";

  if (TEST_UA_RE.test(ua)) {
    return c.json({ error: "spawn_blocked_test_origin" }, 403);
  }

  await next();
};
