/**
 * RBAC module unit tests — behavioral parity with Python backend/rbac.py.
 *
 * Covers:
 *   - SHA-256 token hashing
 *   - Role allow-lists and fnmatch glob matching
 *   - The LOAD-BEARING fallthrough: unknown token → DENY (when RBAC enabled)
 *   - RBAC disabled (no rbac section) → allow-all
 *   - Built-in roles: admin, agent, viewer
 *   - Custom roles from config
 *
 * Run: bun test tests/auth/rbac.test.ts --timeout 10000
 */

import { describe, it, expect } from "bun:test";
import { sha256, matchRule, RBACManager } from "../../src/rbac/rbac.js";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

// ---------------------------------------------------------------------------
// Unit tests: sha256()
// ---------------------------------------------------------------------------

describe("sha256() — mirrors Python _sha256()", () => {
  it("produces known SHA-256 hex digests", () => {
    // These expected values are computed from Python:
    // import hashlib; hashlib.sha256("test".encode("utf-8")).hexdigest()
    expect(sha256("test")).toBe(
      "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    );
    expect(sha256("")).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
  });

  it("is deterministic", () => {
    expect(sha256("mytoken")).toBe(sha256("mytoken"));
  });

  it("is sensitive to case", () => {
    expect(sha256("abc")).not.toBe(sha256("ABC"));
  });
});

// ---------------------------------------------------------------------------
// Unit tests: matchRule()
// ---------------------------------------------------------------------------

describe("matchRule() — mirrors Python _match_rule()", () => {
  it("'*' matches everything", () => {
    expect(matchRule("*", "GET", "/any/path")).toBe(true);
    expect(matchRule("*", "POST", "/other")).toBe(true);
    expect(matchRule("*", "DELETE", "/admin")).toBe(true);
  });

  it("'METHOD /exact' matches exactly", () => {
    expect(matchRule("GET /health", "GET", "/health")).toBe(true);
    expect(matchRule("GET /health", "POST", "/health")).toBe(false);
    expect(matchRule("GET /health", "GET", "/health/loop")).toBe(false);
  });

  it("'GET *' matches any GET path", () => {
    expect(matchRule("GET *", "GET", "/anything")).toBe(true);
    expect(matchRule("GET *", "GET", "/a/b/c")).toBe(true);
    expect(matchRule("GET *", "POST", "/anything")).toBe(false);
  });

  it("fnmatch glob — '*' crosses path separators (Python semantics)", () => {
    // Python fnmatch: * matches any sequence including /
    expect(matchRule("GET /health/*", "GET", "/health/loop")).toBe(true);
    expect(matchRule("GET /health/*", "GET", "/health/modules")).toBe(true);
    expect(matchRule("GET /budget/*", "GET", "/budget/status")).toBe(true);
    expect(matchRule("GET /agents/*", "GET", "/agents/123/detail")).toBe(true);
  });

  it("method matching is case-insensitive", () => {
    expect(matchRule("GET /health", "get", "/health")).toBe(true);
    expect(matchRule("get /health", "GET", "/health")).toBe(true);
  });

  it("returns false for malformed rules", () => {
    expect(matchRule("MALFORMED", "GET", "/path")).toBe(false);
    expect(matchRule("", "GET", "/path")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Helper: create a temp config.json for testing
// ---------------------------------------------------------------------------

function makeTempConfig(content: object): string {
  const dir = join(tmpdir(), `rbac-test-${Date.now()}-${Math.random()}`);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, "config.json");
  writeFileSync(path, JSON.stringify(content), "utf8");
  return path;
}

function cleanupTempConfig(configPath: string): void {
  try {
    rmSync(configPath);
    rmSync(configPath.replace("/config.json", ""), { recursive: true });
  } catch {
    // ignore cleanup errors
  }
}

// ---------------------------------------------------------------------------
// Integration tests: RBACManager
// ---------------------------------------------------------------------------

describe("RBACManager — RBAC disabled (no rbac section)", () => {
  it("allows all requests when config has no rbac key", () => {
    const path = makeTempConfig({ someOtherKey: "value" });
    try {
      const mgr = new RBACManager(path);
      expect(mgr.enabled).toBe(false);
      // ALLOW-ALL when no rbac section (backward compat — Python parity)
      expect(mgr.check("any-token", "GET", "/any-path")).toBe(true);
      expect(mgr.check("", "DELETE", "/admin")).toBe(true);
    } finally {
      cleanupTempConfig(path);
    }
  });

  it("allows all requests when config file is missing", () => {
    const mgr = new RBACManager("/nonexistent/config.json");
    expect(mgr.enabled).toBe(false);
    expect(mgr.check("any-token", "GET", "/anything")).toBe(true);
  });
});

describe("RBACManager — LOAD-BEARING: unknown token → DENY", () => {
  it("DENIES when token hash is NOT in key table (RBAC enabled)", () => {
    const knownToken = "known-token";
    const knownHash = sha256(knownToken);
    const path = makeTempConfig({
      rbac: {
        keys: { [knownHash]: "viewer" },
        roles: {},
      },
    });
    try {
      const mgr = new RBACManager(path);
      expect(mgr.enabled).toBe(true);

      // Known token → passes role check (viewer allows GET *)
      expect(mgr.check(knownToken, "GET", "/health")).toBe(true);

      // Unknown token → DENY (this is the load-bearing fallthrough)
      expect(mgr.check("unknown-token", "GET", "/health")).toBe(false);
      expect(mgr.check("", "GET", "/health")).toBe(false);
      expect(mgr.check("almost-known-token", "GET", "/health")).toBe(false);
    } finally {
      cleanupTempConfig(path);
    }
  });
});

describe("RBACManager — built-in roles", () => {
  let configPath: string;

  const adminToken = "admin-token";
  const agentToken = "agent-token";
  const viewerToken = "viewer-token";

  beforeAll(() => {
    configPath = makeTempConfig({
      rbac: {
        keys: {
          [sha256(adminToken)]: "admin",
          [sha256(agentToken)]: "agent",
          [sha256(viewerToken)]: "viewer",
        },
        roles: {},
      },
    });
  });

  afterAll(() => {
    cleanupTempConfig(configPath);
  });

  it("admin role: allows everything ('*' rule)", () => {
    const mgr = new RBACManager(configPath);
    expect(mgr.check(adminToken, "GET", "/health")).toBe(true);
    expect(mgr.check(adminToken, "POST", "/admin/secret")).toBe(true);
    expect(mgr.check(adminToken, "DELETE", "/anything")).toBe(true);
  });

  it("agent role: allows specific routes", () => {
    const mgr = new RBACManager(configPath);
    expect(mgr.check(agentToken, "GET", "/health")).toBe(true);
    expect(mgr.check(agentToken, "GET", "/health/loop")).toBe(true);
    expect(mgr.check(agentToken, "GET", "/metrics")).toBe(true);
    expect(mgr.check(agentToken, "GET", "/budget/status")).toBe(true);
    expect(mgr.check(agentToken, "GET", "/registry")).toBe(true);
    expect(mgr.check(agentToken, "GET", "/rbac/whoami")).toBe(true);
    expect(mgr.check(agentToken, "POST", "/budget/init")).toBe(true);
  });

  it("agent role: DENIES routes not in allow list", () => {
    const mgr = new RBACManager(configPath);
    expect(mgr.check(agentToken, "POST", "/admin/action")).toBe(false);
    expect(mgr.check(agentToken, "DELETE", "/resource")).toBe(false);
    expect(mgr.check(agentToken, "POST", "/loop/run")).toBe(false);
  });

  it("viewer role: allows any GET, denies non-GET", () => {
    const mgr = new RBACManager(configPath);
    expect(mgr.check(viewerToken, "GET", "/anything")).toBe(true);
    expect(mgr.check(viewerToken, "GET", "/admin/status")).toBe(true);
    expect(mgr.check(viewerToken, "GET", "/budget/status")).toBe(true);
    // POSTs not allowed for viewer
    expect(mgr.check(viewerToken, "POST", "/any")).toBe(false);
    expect(mgr.check(viewerToken, "DELETE", "/any")).toBe(false);
  });
});

describe("RBACManager — custom roles from config", () => {
  it("merges custom role with built-in roles", () => {
    const customToken = "custom-token";
    const configPath = makeTempConfig({
      rbac: {
        keys: { [sha256(customToken)]: "readonly-api" },
        roles: {
          "readonly-api": {
            label: "Read-only API user",
            allow: ["GET /api/*"],
          },
        },
      },
    });

    try {
      const mgr = new RBACManager(configPath);
      expect(mgr.check(customToken, "GET", "/api/status")).toBe(true);
      expect(mgr.check(customToken, "GET", "/api/feed")).toBe(true);
      expect(mgr.check(customToken, "POST", "/api/action")).toBe(false);
      expect(mgr.check(customToken, "GET", "/health")).toBe(false); // not in allow list
    } finally {
      cleanupTempConfig(configPath);
    }
  });
});

// TypeScript requires this import for beforeAll/afterAll
import { beforeAll, afterAll } from "bun:test";
