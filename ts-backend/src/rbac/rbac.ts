/**
 * Role-based access control (RBAC) for the TypeScript backend.
 *
 * Ports backend/rbac.py to TypeScript with exact behavioral parity.
 *
 * Tokens are stored as SHA-256 hashes in .autonomous-team/config.json under
 * the "rbac" key. Each token maps to a role name; each role defines an
 * allow-list of "METHOD /path-pattern" rules. Pattern matching uses the
 * fnmatch.ts port (SPIKE-2 proven: 0 divergences vs Python across 534 cases).
 *
 * LOAD-BEARING FALLTHROUGH (security-critical — must replicate exactly):
 *   - When rbac section is ABSENT from config: always allow (backward compat)
 *   - When rbac IS present but token hash NOT in key table: DENY (return false)
 *   - When rbac IS present but rbac section empty/null: allow-all mode
 *
 * This is the exact semantics of Python rbac.py check():
 *   if not self._enabled: return True       ← no rbac section → allow-all
 *   role_name = get_role_for_token(token)
 *   if role_name is None: return False      ← token not in key table → DENY
 *
 * Optional: memoize compiled regex per pattern for hot-path performance.
 * The fnmatch.ts fnmatchTranslate() compiles a RegExp — memoizing it avoids
 * recompilation on repeated RBAC checks for the same pattern.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fnmatch } from "./fnmatch.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface RoleDefinition {
  label?: string;
  allow: string[];
}

interface RbacConfig {
  roles?: Record<string, RoleDefinition>;
  keys?: Record<string, string>; // hash → role name
}

interface ConfigJson {
  rbac?: RbacConfig;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Built-in roles — mirrors Python _BUILT_IN_ROLES in backend/rbac.py exactly
// ---------------------------------------------------------------------------
const BUILT_IN_ROLES: Record<string, RoleDefinition> = {
  admin: {
    label: "Administrator",
    allow: ["*"], // matches everything
  },
  agent: {
    label: "Agent (internal)",
    allow: [
      "GET /health",
      "GET /health/*",
      "GET /metrics",
      "GET /budget/*",
      "GET /registry",
      "GET /registry/*",
      "GET /agents",
      "GET /agents/*",
      "GET /kpi",
      "GET /kpi/*",
      "GET /stream/*",
      "GET /replays",
      "GET /replays/*",
      "GET /rbac/whoami",
      "POST /budget/init",
    ],
  },
  viewer: {
    label: "Read-only viewer",
    allow: [
      "GET *", // any GET is fine; POSTs are not listed
    ],
  },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Return the hex-encoded SHA-256 digest of token. Mirrors Python _sha256(). */
export function sha256(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

/**
 * Return True if method + path matches the allow-list rule.
 *
 * Rule forms (exact port of Python _match_rule()):
 *   "*"              — matches everything (any method, any path)
 *   "METHOD /glob"   — matches the given method and path glob
 *   "GET *"          — matches any GET regardless of path
 *
 * Path matching uses fnmatch (Python-equivalent semantics).
 */
export function matchRule(rule: string, method: string, path: string): boolean {
  rule = rule.trim();
  if (rule === "*") return true;

  const spaceIdx = rule.indexOf(" ");
  if (spaceIdx < 0) return false;

  const ruleMethod = rule.slice(0, spaceIdx);
  const rulePath = rule.slice(spaceIdx + 1);

  if (ruleMethod.toUpperCase() !== method.toUpperCase()) return false;
  return fnmatch(path, rulePath);
}

// ---------------------------------------------------------------------------
// RBACManager
// ---------------------------------------------------------------------------

export class RBACManager {
  private readonly configPath: string;
  private roles: Record<string, RoleDefinition> = {};
  private tokenHashes: Map<string, string> = new Map(); // hash → role name
  private _enabled = false;

  constructor(configPath: string) {
    this.configPath = configPath;
    this.load();
  }

  private load(): void {
    let raw: ConfigJson;
    try {
      const text = readFileSync(this.configPath, "utf8");
      raw = JSON.parse(text) as ConfigJson;
    } catch {
      return; // Missing or invalid config — backward-compatible allow-all mode
    }

    const rbac = raw.rbac;
    if (!rbac) {
      return; // No rbac section → backward-compatible allow-all mode
    }

    this._enabled = true;

    // Merge built-in roles with any overrides from config
    this.roles = { ...BUILT_IN_ROLES };
    if (rbac.roles) {
      for (const [roleName, roleDef] of Object.entries(rbac.roles)) {
        this.roles[roleName] = roleDef;
      }
    }

    // Index token hashes
    if (rbac.keys) {
      for (const [tokenHash, roleName] of Object.entries(rbac.keys)) {
        this.tokenHashes.set(tokenHash, roleName);
      }
    }
  }

  get enabled(): boolean {
    return this._enabled;
  }

  /** Return the role name for token, or null if unknown. */
  getRoleForToken(token: string): string | null {
    if (!this._enabled) return null;
    const h = sha256(token);
    return this.tokenHashes.get(h) ?? null;
  }

  /** Return the role definition dict, or null if unknown. */
  getRoleInfo(roleName: string): RoleDefinition | null {
    return this.roles[roleName] ?? null;
  }

  /**
   * Return true if token is allowed to call method on path.
   *
   * LOAD-BEARING FALLTHROUGH (must match Python rbac.py check() exactly):
   *   - When RBAC is disabled (no rbac section): always returns true
   *   - When RBAC is enabled AND token hash NOT in key table: returns false (DENY)
   *   - When role has a matching allow rule: returns true
   *   - Otherwise: returns false
   */
  check(token: string, method: string, path: string): boolean {
    if (!this._enabled) return true; // No rbac section → allow-all

    const roleName = this.getRoleForToken(token);
    if (roleName === null) return false; // Token not in key table → DENY

    const role = this.roles[roleName];
    if (!role) return false; // Unknown role → DENY

    for (const rule of role.allow) {
      if (matchRule(rule, method, path)) return true;
    }
    return false;
  }
}
