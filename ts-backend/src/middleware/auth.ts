/**
 * Default-deny auth middleware — P1 full implementation.
 *
 * Ports backend/deps/auth.py to TypeScript with exact behavioral parity.
 *
 * Security properties (all required by the security-expert):
 *   1. Default-deny: any non-public route without a valid token → 401/403
 *   2. PUBLIC_ROUTES: exact-match set (frozenset parity)
 *   3. PUBLIC_PREFIXES: startswith semantics — raw string prefix match.
 *      "/api/config" matches "/api/config", "/api/config/foo", and
 *      "/api/configEVIL" (Python str.startswith is a raw string prefix check,
 *      not a path-component boundary; TS path.startsWith behaves identically)
 *   4. AF_API_AUTH_KEY unset → auth disabled (always pass)
 *   5. crypto.timingSafeEqual for constant-time token comparison (CWE-208)
 *      NEVER string === (timing side-channel)
 *   6. Token missing → 401 Unauthorized
 *   7. Token present but wrong → 403 Forbidden
 *
 * Response bodies match Python DefaultDenyMiddleware exactly:
 *   {"detail": "unauthorized"}  (401)
 *   {"detail": "forbidden"}     (403)
 */

import { timingSafeEqual } from "node:crypto";
import type { Context, MiddlewareHandler, Next } from "hono";

// ---------------------------------------------------------------------------
// Public route allowlist — exact matches.
// Must stay in sync with backend/deps/auth.py PUBLIC_ROUTES.
// ---------------------------------------------------------------------------
export const PUBLIC_ROUTES: ReadonlySet<string> = new Set([
  "/health",
  "/health/loop",
  "/health/modules",
  "/docs",
  "/openapi.json",
  "/redoc",
  "/stub/stream",
  "/metrics",
  "/rpc",
  "/feed",
  "/events",
  "/dashboard",
  "/",
]);

// ---------------------------------------------------------------------------
// Public route prefixes — startswith semantics (Python backend/deps/auth.py).
// IMPORTANT: prefix "/api/config" matches "/api/config", "/api/config/foo",
//            and "/api/configEVIL" — path.startsWith(prefix) is a raw string
//            check (no path-component boundary), identical to Python's
//            str.startswith(). Both languages match the same set of paths.
// ---------------------------------------------------------------------------
export const PUBLIC_PREFIXES: readonly string[] = [
  "/api/config",
  "/api/projects",
  "/api/sessions",
  "/api/events",
  "/api/fleet/",
  "/api/ideas",
  "/api/spawn-blocks",
  "/api/loop/",
  "/api/innovate",
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Return true when path is a public route (no auth required).
 * Mirrors Python _is_public() in backend/deps/auth.py exactly.
 */
export function isPublic(path: string): boolean {
  if (PUBLIC_ROUTES.has(path)) return true;
  for (const prefix of PUBLIC_PREFIXES) {
    // Python: path == prefix or path.startswith(prefix)
    if (path === prefix || path.startsWith(prefix)) return true;
  }
  return false;
}

/**
 * Constant-time Bearer token comparison using crypto.timingSafeEqual.
 *
 * Converts both strings to UTF-8 buffers of equal length before comparing.
 * Returns false if lengths differ (avoids early exit that would leak timing).
 *
 * This mirrors Python's hmac.compare_digest() behavior (CWE-208 prevention).
 */
export function timingSafeTokenEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a, "utf8");
  const bBuf = Buffer.from(b, "utf8");
  if (aBuf.length !== bBuf.length) {
    // Lengths differ — create equal-length buffers for constant-time comparison,
    // then return false. This avoids leaking length information via timing.
    const maxLen = Math.max(aBuf.length, bBuf.length);
    const aPad = Buffer.alloc(maxLen, 0);
    const bPad = Buffer.alloc(maxLen, 0);
    aBuf.copy(aPad);
    bBuf.copy(bPad);
    timingSafeEqual(aPad, bPad); // run comparison for timing, discard result
    return false;
  }
  return timingSafeEqual(aBuf, bBuf);
}

/**
 * Extract the Bearer token from the Authorization header.
 * Returns null if the header is absent or not a Bearer token.
 * Returns an empty string ("") when the header has the Bearer scheme
 * but no credentials — this maps to Python's behavior where
 * "Bearer ".startswith("Bearer ") == True → token="" → compare_digest("", key) → 403.
 *
 * NOTE: Hono strips trailing whitespace from header values, so a raw
 * "Authorization: Bearer " header arrives here as "Bearer" (no space).
 * We therefore also treat the exact value "Bearer" as an empty-credential
 * Bearer token (i.e. return "") to match Python's 403 outcome.
 *
 * Mirrors Python _extract_bearer() in backend/deps/auth.py.
 */
export function extractBearer(authHeader: string): string | null {
  if (authHeader.startsWith("Bearer ")) {
    return authHeader.slice(7); // len("Bearer ") === 7
  }
  // Hono normalizes "Bearer " (trailing space) to "Bearer" — treat it as
  // an empty token so the status code matches Python's 403 (not 401).
  if (authHeader === "Bearer") {
    return "";
  }
  return null;
}

// ---------------------------------------------------------------------------
// Default-deny middleware
// ---------------------------------------------------------------------------

/**
 * Default-deny auth middleware — request #1 gate.
 *
 * Mirrors Python DefaultDenyMiddleware.dispatch() in backend/deps/auth.py.
 * Response bodies are byte-equivalent to the Python implementation.
 */
export const defaultDenyMiddleware: MiddlewareHandler = async (
  c: Context,
  next: Next
): Promise<void | Response> => {
  const authKey = process.env.AF_API_AUTH_KEY || null;

  // Auth disabled (AF_API_AUTH_KEY unset) — pass all requests.
  // Mirrors Python: if key is None: return await call_next(request)
  if (authKey === null) {
    await next();
    return;
  }

  const path = new URL(c.req.url).pathname;

  // Public routes pass through without a token.
  if (isPublic(path)) {
    await next();
    return;
  }

  // Extract bearer token
  const authHeader = c.req.header("Authorization") ?? "";
  const token = extractBearer(authHeader);

  if (token === null) {
    // Token missing → 401 Unauthorized
    // Mirrors Python: return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return c.json({ detail: "unauthorized" }, 401);
  }

  // Constant-time token comparison (CWE-208 prevention)
  // Mirrors Python: if not hmac.compare_digest(token, key): return 403
  if (!timingSafeTokenEqual(token, authKey)) {
    return c.json({ detail: "forbidden" }, 403);
  }

  await next();
};
