/**
 * P1 auth/RBAC negative-test parity suite.
 *
 * Tests the SAME outcomes as Python for all security-critical scenarios:
 *   - 401 no-token (protected route, auth enabled)
 *   - 403 wrong-token (protected route, auth enabled)
 *   - PUBLIC_ROUTES exact match (pass through)
 *   - PUBLIC_PREFIXES startswith (pass through, no trailing-slash widening)
 *   - Auth disabled (AF_API_AUTH_KEY unset — pass everything)
 *   - Constant-time compare (timingSafeTokenEqual behavior)
 *   - XFF-spoof-IGNORED (X-Forwarded-For must NOT affect auth decision)
 *
 * All assertions reference documented Python behavior in backend/deps/auth.py.
 *
 * Run: bun test tests/auth/auth.test.ts --timeout 10000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import {
  isPublic,
  PUBLIC_ROUTES,
  PUBLIC_PREFIXES,
  timingSafeTokenEqual,
  extractBearer,
  defaultDenyMiddleware,
} from "../../src/middleware/auth.js";
import { Hono } from "hono";
import type { Context } from "hono";

// ---------------------------------------------------------------------------
// Unit tests: isPublic() — mirrors Python _is_public()
// ---------------------------------------------------------------------------

describe("isPublic() — PUBLIC_ROUTES exact match", () => {
  it("returns true for every route in PUBLIC_ROUTES", () => {
    for (const route of PUBLIC_ROUTES) {
      expect(isPublic(route)).toBe(true);
    }
  });

  it("returns false for a path not in PUBLIC_ROUTES (no auth bypass)", () => {
    expect(isPublic("/protected")).toBe(false);
    expect(isPublic("/api/secret")).toBe(false);
    expect(isPublic("/budget/status")).toBe(false);
  });

  it("is case-sensitive — /Health is NOT /health", () => {
    expect(isPublic("/Health")).toBe(false);
    expect(isPublic("/HEALTH")).toBe(false);
  });
});

describe("isPublic() — PUBLIC_PREFIXES startswith semantics (Python parity)", () => {
  it("matches exact prefix path", () => {
    for (const prefix of PUBLIC_PREFIXES) {
      expect(isPublic(prefix)).toBe(true);
    }
  });

  it("matches path starting with a prefix", () => {
    expect(isPublic("/api/config/something")).toBe(true);
    expect(isPublic("/api/projects/abc")).toBe(true);
    expect(isPublic("/api/sessions/xyz")).toBe(true);
    expect(isPublic("/api/fleet/nodes")).toBe(true);
  });

  it("matches Python startswith semantics exactly (security parity)", () => {
    // Python's str.startswith() is a raw string prefix — "/api/configEVIL"
    // DOES startswith "/api/config" in Python, so it IS public.
    // The security-expert's concern was that TS must be IDENTICAL to Python,
    // not accidentally narrower or wider. Our TS uses the same raw startsWith.
    //
    // Verified: python3 -c "print('/api/configEVIL'.startswith('/api/config'))"
    //           → True
    expect(isPublic("/api/configEVIL")).toBe(true);   // Python: True
    expect(isPublic("/api/configExtra")).toBe(true);  // Python: True
    expect(isPublic("/api/projectsEVIL")).toBe(true); // Python: True
    expect(isPublic("/api/sessionsEVIL")).toBe(true); // Python: True
    expect(isPublic("/api/ideasEVIL")).toBe(true);    // Python: True
    expect(isPublic("/api/innovateEVIL")).toBe(true); // Python: True

    // Paths that genuinely do NOT start with any prefix → not public
    expect(isPublic("/evil/config")).toBe(false);
    expect(isPublic("/xapi/config")).toBe(false);
  });

  it("handles trailing-slash prefix correctly — /api/fleet/ only matches /api/fleet/ + sub-paths", () => {
    // "/api/fleet/" with trailing slash in PUBLIC_PREFIXES
    expect(isPublic("/api/fleet/")).toBe(true);
    expect(isPublic("/api/fleet/node1")).toBe(true);
    // "/api/fleet" (no trailing slash) is NOT in PUBLIC_PREFIXES but is
    // a prefix startsWith match — "/api/fleet" does NOT startsWith "/api/fleet/"
    // So "/api/fleet" (exact, no slash) should NOT match the prefix "/api/fleet/"
    expect(isPublic("/api/fleet")).toBe(false);
  });

  it("matches /api/loop/ prefix sub-paths", () => {
    expect(isPublic("/api/loop/")).toBe(true);
    expect(isPublic("/api/loop/run")).toBe(true);
    expect(isPublic("/api/loop/status")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Unit tests: extractBearer()
// ---------------------------------------------------------------------------

describe("extractBearer()", () => {
  it("extracts token from valid Bearer header", () => {
    expect(extractBearer("Bearer mytoken123")).toBe("mytoken123");
    expect(extractBearer("Bearer ")).toBe(""); // empty token (trailing space stripped by Hono, but test raw function)
    expect(extractBearer("Bearer abc def")).toBe("abc def"); // spaces in token
  });

  it('returns "" for "Bearer" (exact) — Hono-normalized empty-credential form', () => {
    // Hono strips trailing spaces from header values, so "Authorization: Bearer " arrives as "Bearer".
    // extractBearer("Bearer") returns "" (empty string) so the middleware can proceed to
    // timingSafeTokenEqual and return 403, matching Python's behavior for Bearer + empty credentials.
    expect(extractBearer("Bearer")).toBe("");
  });

  it("returns null for non-Bearer headers", () => {
    expect(extractBearer("")).toBe(null);
    expect(extractBearer("Basic dXNlcjpwYXNz")).toBe(null);
    expect(extractBearer("bearer token")).toBe(null); // case-sensitive
    expect(extractBearer("BEARER token")).toBe(null);
    expect(extractBearer("Token abc")).toBe(null);
  });
});

// ---------------------------------------------------------------------------
// Unit tests: timingSafeTokenEqual()
// ---------------------------------------------------------------------------

describe("timingSafeTokenEqual() — constant-time comparison (CWE-208)", () => {
  it("returns true for identical strings", () => {
    expect(timingSafeTokenEqual("abc", "abc")).toBe(true);
    expect(timingSafeTokenEqual("", "")).toBe(true);
    expect(timingSafeTokenEqual("supersecretkey", "supersecretkey")).toBe(true);
  });

  it("returns false for different strings", () => {
    expect(timingSafeTokenEqual("abc", "xyz")).toBe(false);
    expect(timingSafeTokenEqual("abc", "abcd")).toBe(false);
    expect(timingSafeTokenEqual("abc", "ab")).toBe(false);
    expect(timingSafeTokenEqual("abc", "ABC")).toBe(false);
  });

  it("returns false for empty vs non-empty", () => {
    expect(timingSafeTokenEqual("", "a")).toBe(false);
    expect(timingSafeTokenEqual("a", "")).toBe(false);
  });

  it("handles unicode strings", () => {
    expect(timingSafeTokenEqual("café", "café")).toBe(true);
    expect(timingSafeTokenEqual("café", "cafe")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Integration tests: defaultDenyMiddleware via Hono app
// ---------------------------------------------------------------------------

describe("defaultDenyMiddleware — auth disabled (AF_API_AUTH_KEY unset)", () => {
  let savedKey: string | undefined;

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

  it("passes all requests when auth is disabled", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ secret: "data" }));

    const res = await app.request("/protected");
    expect(res.status).toBe(200);
  });

  it("passes even without any Authorization header", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/any-route", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/any-route");
    expect(res.status).toBe(200);
  });
});

describe("defaultDenyMiddleware — 401 no-token (auth enabled)", () => {
  let savedKey: string | undefined;

  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    process.env.AF_API_AUTH_KEY = "test-secret-key";
  });

  afterEach(() => {
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
  });

  it("returns 401 for protected route with no token", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ secret: "data" }));

    const res = await app.request("/protected");
    expect(res.status).toBe(401);
    const body = await res.json();
    // Python: {"detail": "unauthorized"}
    expect(body).toEqual({ detail: "unauthorized" });
  });

  it("returns 401 with empty Authorization header", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected", {
      headers: { Authorization: "" },
    });
    expect(res.status).toBe(401);
  });

  it("returns 401 with non-Bearer Authorization scheme", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected", {
      headers: { Authorization: "Basic dXNlcjpwYXNz" },
    });
    expect(res.status).toBe(401);
  });
});

describe("defaultDenyMiddleware — 403 wrong-token (auth enabled)", () => {
  let savedKey: string | undefined;

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

  it("returns 403 for protected route with wrong token", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ secret: "data" }));

    const res = await app.request("/protected", {
      headers: { Authorization: "Bearer wrong-key" },
    });
    expect(res.status).toBe(403);
    const body = await res.json();
    // Python: {"detail": "forbidden"}
    expect(body).toEqual({ detail: "forbidden" });
  });

  it("returns 403 for nearly-correct token (not constant-time bypass)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected", {
      headers: { Authorization: "Bearer correct-ke" }, // one char short
    });
    expect(res.status).toBe(403);
  });

  it("returns 200 with correct token", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ secret: "data" }));

    const res = await app.request("/protected", {
      headers: { Authorization: "Bearer correct-key" },
    });
    expect(res.status).toBe(200);
  });
});

describe("defaultDenyMiddleware — PUBLIC_ROUTES pass through (auth enabled)", () => {
  let savedKey: string | undefined;

  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    process.env.AF_API_AUTH_KEY = "some-key";
  });

  afterEach(() => {
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
  });

  it("passes /health without token (PUBLIC_ROUTE)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/health", (c: Context) => c.json({ status: "ok" }));

    const res = await app.request("/health");
    expect(res.status).toBe(200);
  });

  it("passes /metrics without token (PUBLIC_ROUTE)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/metrics", (c: Context) => c.text("# metrics\n"));

    const res = await app.request("/metrics");
    expect(res.status).toBe(200);
  });

  it("passes /feed without token (PUBLIC_ROUTE — SSE, auth via query param)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/feed", (c: Context) => c.json({ events: [] }));

    const res = await app.request("/feed");
    expect(res.status).toBe(200);
  });
});

describe("defaultDenyMiddleware — PUBLIC_PREFIXES pass through (auth enabled)", () => {
  let savedKey: string | undefined;

  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    process.env.AF_API_AUTH_KEY = "some-key";
  });

  afterEach(() => {
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
  });

  it("passes /api/config (exact prefix match)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/api/config", (c: Context) => c.json({ config: "value" }));

    const res = await app.request("/api/config");
    expect(res.status).toBe(200);
  });

  it("passes /api/config/something (sub-path of prefix)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/api/config/something", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/api/config/something");
    expect(res.status).toBe(200);
  });

  it("passes /api/configEVIL (Python startswith semantics — same as Python)", async () => {
    // Python str.startswith("/api/config") matches "/api/configEVIL".
    // Both Python and TS treat this as public — parity is correct.
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/api/configEVIL", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/api/configEVIL");
    // Python: isPublic → True → pass through; TS must match
    expect(res.status).toBe(200);
  });

  it("blocks a path with no prefix match at all", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/evil/config", (c: Context) => c.json({ evil: true }));

    const res = await app.request("/evil/config");
    expect(res.status).toBe(401); // no token, not public
  });
});

// ---------------------------------------------------------------------------
// Status-code parity tests: empty-Bearer and absent-header boundary
// Verifies TS matches Python's exact 401/403 boundary:
//   no header              → None   → 401 (Python: auth_header="" → not startswith "Bearer " → None)
//   "Bearer" (no space)    → None   → 401 (Python: "Bearer".startswith("Bearer ") == False → None)
//   "Bearer " (empty creds)→ ""     → 403 (Python: "Bearer ".startswith("Bearer ") == True → "" → compare_digest("", key) == False)
//   "Bearer wrongtoken"    → token  → 403 (Python: compare_digest fails)
// ---------------------------------------------------------------------------

describe("defaultDenyMiddleware — 401/403 boundary: empty-Bearer and absent-header (Python status parity)", () => {
  let savedKey: string | undefined;

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

  it("no Authorization header → 401 (Python: _extract_bearer returns None → 401)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected");
    expect(res.status).toBe(401);
    const body = await res.json();
    expect(body).toEqual({ detail: "unauthorized" });
  });

  it('"Bearer" (no trailing space, Hono-normalized) → 403 (Hono strips trailing whitespace so "Bearer " and "Bearer" are indistinguishable; both yield 403)', async () => {
    // NOTE: Hono normalizes header values — "Bearer " (trailing space) and "Bearer" (no space)
    // both arrive as "Bearer" in c.req.header(). Since we cannot distinguish them at the
    // framework level, both are treated as empty-credential Bearer tokens → 403.
    // Python would return 401 for raw "Bearer" (no space) but 403 for "Bearer " (with space);
    // this boundary is unrepresentable in Hono, so we choose the safer 403 path
    // (bearer scheme was present, credentials were absent/empty).
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected", {
      headers: { Authorization: "Bearer" },
    });
    expect(res.status).toBe(403);
  });

  it('"Bearer " (space, empty credentials) → 403 (Python: startswith "Bearer " → "" → compare_digest("", key) == False → 403)', async () => {
    // Python: "Bearer ".startswith("Bearer ") == True → token = "" → hmac.compare_digest("", key) == False → 403
    // TS must match: extractBearer("Bearer ") returns "" (not null) → timingSafeTokenEqual → false → 403
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected", {
      headers: { Authorization: "Bearer " },
    });
    expect(res.status).toBe(403);
    const body = await res.json();
    expect(body).toEqual({ detail: "forbidden" });
  });

  it('"Bearer wrongtoken" → 403 (Python: token present but wrong → compare_digest fails → 403)', async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected", {
      headers: { Authorization: "Bearer wrongtoken" },
    });
    expect(res.status).toBe(403);
    const body = await res.json();
    expect(body).toEqual({ detail: "forbidden" });
  });
});

describe("defaultDenyMiddleware — XFF-spoof-IGNORED", () => {
  let savedKey: string | undefined;

  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    process.env.AF_API_AUTH_KEY = "real-key";
  });

  afterEach(() => {
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
  });

  it("X-Forwarded-For header does NOT affect auth decision", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ secret: "data" }));

    // Spoofed XFF should NOT bypass auth — still requires Bearer token
    const res = await app.request("/protected", {
      headers: {
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
      },
    });
    // Must be 401 — XFF does not grant auth
    expect(res.status).toBe(401);
  });

  it("XFF does not grant access to protected routes (wrong token still 403)", async () => {
    const app = new Hono();
    app.use("*", defaultDenyMiddleware);
    app.get("/protected", (c: Context) => c.json({ ok: true }));

    const res = await app.request("/protected", {
      headers: {
        Authorization: "Bearer wrong",
        "X-Forwarded-For": "127.0.0.1",
      },
    });
    expect(res.status).toBe(403);
  });
});
