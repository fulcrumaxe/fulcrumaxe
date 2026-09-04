/**
 * Loopback gate middleware for routes that must only be callable from localhost.
 *
 * Ports the loopback check from backend/routers/api_config.py to a reusable
 * Hono middleware. Mirrors the behavior of _is_loopback() and the Gate 1 check
 * in api_config.py exactly.
 *
 * Returns 403 with {"error": "forbidden: /api/config is localhost-only"}
 * when the connecting IP is not a loopback address.
 *
 * IP source: true peer IP from Bun socket (same extraction as rate-limit.ts).
 * We NEVER read X-Forwarded-For (CWE-348).
 */

import type { Context, MiddlewareHandler, Next } from "hono";
import { getPeerIp } from "./rate-limit.js";

// Exact loopback IP set from backend/routers/api_config.py
const LOOPBACK_IPS: ReadonlySet<string> = new Set([
  "127.0.0.1",
  "::1",
  "localhost",
]);

/**
 * Exact error messages from legacy api.py lines 2458 and 2466.
 */
export const MSG_NOT_LOCALHOST = "forbidden: /api/config is localhost-only";
export const MSG_CROSS_ORIGIN =
  "forbidden: cross-origin access to /api/config denied";

/**
 * Return true when the direct connecting IP is a loopback address.
 * Uses true peer IP — never XFF.
 */
export function isLoopback(c: Context): boolean {
  const ip = getPeerIp(c);
  return LOOPBACK_IPS.has(ip);
}

/**
 * Middleware factory — wraps a route handler with a loopback-only gate.
 *
 * Rejects non-loopback callers with 403.
 * Rejects cross-origin requests (Origin header pointing to non-localhost) with 403.
 *
 * Mirrors the two-gate check in backend/routers/api_config.py exactly.
 */
export const loopbackGateMiddleware: MiddlewareHandler = async (
  c: Context,
  next: Next
): Promise<void | Response> => {
  // Gate 1: loopback-only
  if (!isLoopback(c)) {
    return c.json({ error: MSG_NOT_LOCALHOST }, 403);
  }

  // Gate 2: refuse cross-origin requests (non-localhost Origin header)
  const origin = c.req.header("Origin") ?? "";
  if (
    origin &&
    !origin.startsWith("http://localhost") &&
    !origin.startsWith("http://127.0.0.1")
  ) {
    return c.json({ error: MSG_CROSS_ORIGIN }, 403);
  }

  await next();
};
