/**
 * Per-route RBAC check helper for auth-gated routes.
 *
 * Ports backend/deps/rbac.py make_require_rbac() semantics to TypeScript.
 *
 * Called at the top of each route handler that requires RBAC gating:
 *
 *   const deny = checkRbac(c, "GET", "/sessions");
 *   if (deny !== null) return deny;
 *
 * Semantics (exact port of Python deps/rbac.py make_require_rbac):
 *   - No token (auth disabled or no Authorization header) → pass (null)
 *   - RBAC not configured → pass (null)
 *   - Token not in RBAC key table → pass (null) [legacy single-key model]
 *   - Token has an explicit role that does not allow method+path → 403
 *
 * Config path: .autonomous-team/config.json (same as Python)
 */

import type { Context } from "hono";
import { join } from "node:path";
import { RBACManager } from "../rbac/rbac.js";
import { extractBearer } from "./auth.js";

// ---------------------------------------------------------------------------
// Shared RBAC manager — loaded once at module load time.
// Same config file as Python deps/rbac.py _CONFIG_FILE.
// ---------------------------------------------------------------------------

// ts-backend/src/middleware/ -> ts-backend/src/ -> ts-backend/ -> repo root
const REPO_ROOT = join(import.meta.dir, "..", "..", "..");
const CONFIG_FILE = join(REPO_ROOT, ".autonomous-team", "config.json");

let _rbacManager: RBACManager | null = null;

function getRbacManager(): RBACManager {
  if (_rbacManager === null) {
    _rbacManager = new RBACManager(CONFIG_FILE);
  }
  return _rbacManager;
}

/**
 * Check RBAC for a route. Returns a 403 Response if denied, null if allowed.
 *
 * Exact port of Python make_require_rbac() closure:
 *   - token is None → return (pass; auth layer handles 401)
 *   - rbac not enabled → return (pass)
 *   - token not in key table → return (pass; legacy single-key model)
 *   - role denies method+path → raise HTTPException(403)
 */
export function checkRbac(
  c: Context,
  method: string,
  path: string
): Response | null {
  const authHeader = c.req.header("Authorization") ?? "";
  const token = extractBearer(authHeader);

  // No token — auth layer handles 401; RBAC is post-auth gate
  if (token === null) return null;

  const rbac = getRbacManager();

  // RBAC not configured → allow-all
  if (!rbac.enabled) return null;

  // Token not in RBAC key table → legacy model, allow through
  if (rbac.getRoleForToken(token) === null) return null;

  // Token has an explicit role — enforce it
  if (!rbac.check(token, method, path)) {
    return c.json({ detail: "forbidden" }, 403);
  }

  return null;
}

/** Reset the cached RBAC manager (for tests that modify config). */
export function resetRbacManager(): void {
  _rbacManager = null;
}
