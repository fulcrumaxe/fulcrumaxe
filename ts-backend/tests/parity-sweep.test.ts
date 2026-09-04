/**
 * Unit tests for parity-sweep.ts — report shaping and divergence detection.
 *
 * Run: bun test tests/parity-sweep.test.ts --timeout 15000
 *
 * These tests exercise the pure logic of the sweep harness using stub inputs
 * only — no live TS backend, no live Python backend, no network calls.
 *
 * What is tested:
 *  - buildReport() shapes the result envelope correctly
 *  - assertAgainstFixture() detects status mismatches and body divergences
 *  - assertAgainstFixture() handles missing fixtures gracefully
 *  - assertAgainstFixture() handles fixture read errors gracefully
 *  - SWEEP_ROUTES inventory contains only GET read-only routes
 *  - printSummary() produces human-readable output (smoke test)
 */

import { describe, it, expect } from "bun:test";
import { join } from "node:path";
import {
  buildReport,
  assertAgainstFixture,
  printSummary,
  SWEEP_ROUTES,
} from "../src/parity-sweep.js";
import type { RouteResult, SweepRoute } from "../src/parity-sweep.js";

// ---------------------------------------------------------------------------
// Fixture directory — the real fixtures/ folder already has captured golden data
// ---------------------------------------------------------------------------
const FIXTURES_DIR = join(import.meta.dir, "..", "fixtures");

// ---------------------------------------------------------------------------
// buildReport — pure report shaping
// ---------------------------------------------------------------------------

describe("buildReport", () => {
  it("counts totals correctly when all routes pass", () => {
    const results: RouteResult[] = [
      {
        route: "/health",
        probe_path: "/health",
        mode: "golden",
        status_match: true,
        body_match: true,
        diverged: false,
        ts_status: 200,
        ref_status: 200,
        divergence_detail: null,
      },
      {
        route: "/sessions",
        probe_path: "/sessions",
        mode: "golden",
        status_match: true,
        body_match: true,
        diverged: false,
        ts_status: 200,
        ref_status: 200,
        divergence_detail: null,
      },
    ];

    const report = buildReport(results, "golden");
    expect(report.total).toBe(2);
    expect(report.at_parity).toBe(2);
    expect(report.diverged).toBe(0);
    expect(report.mode).toBe("golden");
    expect(report.results).toHaveLength(2);
    // generated_at is an ISO-8601 string
    expect(report.generated_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it("counts divergences correctly when some routes fail", () => {
    const results: RouteResult[] = [
      {
        route: "/health",
        probe_path: "/health",
        mode: "golden",
        status_match: true,
        body_match: true,
        diverged: false,
        ts_status: 200,
        ref_status: 200,
        divergence_detail: null,
      },
      {
        route: "/sessions",
        probe_path: "/sessions",
        mode: "golden",
        status_match: false,
        body_match: false,
        diverged: true,
        ts_status: 500,
        ref_status: 200,
        divergence_detail: "Status mismatch: fixture=200 ts=500",
      },
      {
        route: "/spawn-queue",
        probe_path: "/spawn-queue",
        mode: "shadow",
        status_match: true,
        body_match: false,
        diverged: true,
        ts_status: 200,
        ref_status: 200,
        divergence_detail: "Body mismatch:\n  python: {}\n  ts: {extra:1}",
      },
    ];

    const report = buildReport(results, "live-shadow");
    expect(report.total).toBe(3);
    expect(report.at_parity).toBe(1);
    expect(report.diverged).toBe(2);
    expect(report.mode).toBe("live-shadow");
  });

  it("returns empty report for empty results", () => {
    const report = buildReport([], "golden");
    expect(report.total).toBe(0);
    expect(report.at_parity).toBe(0);
    expect(report.diverged).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// assertAgainstFixture — golden mode comparison logic
// ---------------------------------------------------------------------------

describe("assertAgainstFixture — no fixture", () => {
  const routeNoFixture: SweepRoute = {
    route: "/sessions/current",
    authRequired: true,
    fixture: null,
  };

  it("passes when TS returns a non-5xx status", () => {
    const result = assertAgainstFixture(routeNoFixture, 401, '{"error":"unauthorized"}', FIXTURES_DIR);
    expect(result.diverged).toBe(false);
    expect(result.mode).toBe("no-fixture");
    expect(result.note).toContain("no fixture");
  });

  it("fails when TS returns a 5xx status", () => {
    const result = assertAgainstFixture(routeNoFixture, 500, '{"error":"internal"}', FIXTURES_DIR);
    expect(result.diverged).toBe(true);
    expect(result.divergence_detail).toContain("500");
  });

  it("passes on 404 (expected for missing param routes)", () => {
    const paramRoute: SweepRoute = {
      route: "/sessions/:session_id",
      authRequired: true,
      fixture: null,
      examplePath: "/sessions/parity-probe-no-such-session-000",
    };
    const result = assertAgainstFixture(paramRoute, 404, '{"error":"not found"}', FIXTURES_DIR);
    expect(result.diverged).toBe(false);
    expect(result.probe_path).toBe("/sessions/parity-probe-no-such-session-000");
  });
});

describe("assertAgainstFixture — with real health.json fixture", () => {
  const healthRoute: SweepRoute = {
    route: "/health",
    authRequired: false,
    fixture: "health.json",
  };

  it("passes when TS body matches fixture structure after normalization", () => {
    // This is the exact structure the fixture expects (with masked fields already <masked>)
    const tsBody = JSON.stringify({
      _api_version: 1,
      ok: true,
      loop_last_run: "2026-05-23T17:39:14Z",
      loop_duration_s: 300,
      loop_idle_rate: 0.0,
      malformed_lines: 0,
    });
    const result = assertAgainstFixture(healthRoute, 200, tsBody, FIXTURES_DIR);
    expect(result.status_match).toBe(true);
    expect(result.body_match).toBe(true);
    expect(result.diverged).toBe(false);
    expect(result.mode).toBe("golden");
    expect(result.ref_status).toBe(200);
  });

  it("detects status mismatch", () => {
    const tsBody = JSON.stringify({ _api_version: 1, ok: true, loop_last_run: "2026-05-23T17:39:14Z", loop_duration_s: 300, loop_idle_rate: 0.0, malformed_lines: 0 });
    const result = assertAgainstFixture(healthRoute, 500, tsBody, FIXTURES_DIR);
    expect(result.status_match).toBe(false);
    expect(result.diverged).toBe(true);
    expect(result.divergence_detail).toContain("Status mismatch");
    expect(result.divergence_detail).toContain("500");
  });

  it("detects body divergence — extra field", () => {
    // Adding an unexpected field changes the normalized form
    const tsBody = JSON.stringify({
      _api_version: 1,
      ok: true,
      loop_last_run: "2026-05-23T17:39:14Z",
      loop_duration_s: 300,
      loop_idle_rate: 0.0,
      malformed_lines: 0,
      unexpected_field: "should-not-be-here",
    });
    const result = assertAgainstFixture(healthRoute, 200, tsBody, FIXTURES_DIR);
    expect(result.body_match).toBe(false);
    expect(result.diverged).toBe(true);
    expect(result.divergence_detail).toContain("Body mismatch");
  });

  it("detects body divergence — wrong ok value", () => {
    const tsBody = JSON.stringify({
      _api_version: 1,
      ok: false,  // should be true
      loop_last_run: "2026-05-23T17:39:14Z",
      loop_duration_s: 300,
      loop_idle_rate: 0.0,
      malformed_lines: 0,
    });
    const result = assertAgainstFixture(healthRoute, 200, tsBody, FIXTURES_DIR);
    expect(result.body_match).toBe(false);
    expect(result.diverged).toBe(true);
  });
});

describe("assertAgainstFixture — fixture read error", () => {
  it("returns diverged=true when fixture file does not exist", () => {
    const badRoute: SweepRoute = {
      route: "/health",
      authRequired: false,
      fixture: "nonexistent-fixture.json",
    };
    const result = assertAgainstFixture(badRoute, 200, '{"ok":true}', FIXTURES_DIR);
    expect(result.diverged).toBe(true);
    expect(result.divergence_detail).toContain("Cannot read fixture file");
  });

  it("returns diverged=true when TS body is not valid JSON", () => {
    const healthRoute: SweepRoute = {
      route: "/health",
      authRequired: false,
      fixture: "health.json",
    };
    const result = assertAgainstFixture(healthRoute, 200, "not-json{{", FIXTURES_DIR);
    expect(result.diverged).toBe(true);
    expect(result.divergence_detail).toContain("parse error");
  });
});

describe("assertAgainstFixture — structure-only (spawn-queue fixture)", () => {
  const spawnQueueRoute: SweepRoute = {
    route: "/spawn-queue",
    authRequired: true,
    fixture: "spawn-queue.json",
    structureOnly: true,
  };

  it("passes when live counts differ from fixture but structure matches", () => {
    // The fixture has by_role with specific structure; live counts will differ.
    // _api_version is injected by TS middleware but absent from old fixtures —
    // it is in the stripped set so it gets removed from both sides before comparison.
    const tsBody = JSON.stringify({
      _api_version: 1,           // added by middleware — not in fixture
      active_total: 3,           // differs from fixture (0)
      completed: 42,             // differs
      failed: 1,                 // differs
      pending: 2,                // differs
      utilization_pct: 50,       // differs
      total_limit: 6,            // matches fixture
      by_role: {
        "code-reviewer": { active: 1, limit: 2 },
        "executor": { active: 2, limit: 2 },
        "mission-analyst": { active: 0, limit: 1 },
        "project-manager": { active: 0, limit: 1 },
        "security-reviewer": { active: 0, limit: 1 },
      },
    });
    const result = assertAgainstFixture(spawnQueueRoute, 200, tsBody, FIXTURES_DIR);
    // Structure-only stripping removes live-varying and middleware keys from both sides
    expect(result.status_match).toBe(true);
    expect(result.body_match).toBe(true);
    expect(result.diverged).toBe(false);
  });

  it("detects structural divergence — missing by_role key", () => {
    const tsBody = JSON.stringify({
      _api_version: 1,
      active_total: 0,
      completed: 0,
      failed: 0,
      pending: 0,
      utilization_pct: 0,
      total_limit: 6,
      // by_role is missing entirely
    });
    const result = assertAgainstFixture(spawnQueueRoute, 200, tsBody, FIXTURES_DIR);
    // The fixture body has by_role; the TS body does not — divergence after stripping
    expect(result.body_match).toBe(false);
    expect(result.diverged).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// stripKeys — recursive key-stripping helper
// ---------------------------------------------------------------------------

import { stripKeys } from "../src/parity-sweep.js";
import type { JsonValue } from "../src/normalizer.js";

describe("stripKeys", () => {
  it("removes top-level keys that are in the stripped set", () => {
    const input: JsonValue = { _api_version: 1, ok: true, status: "good" };
    const result = stripKeys(input, new Set(["_api_version"]));
    expect(JSON.stringify(result)).toBe('{"ok":true,"status":"good"}');
  });

  it("removes nested keys recursively", () => {
    const input: JsonValue = { data: { _api_version: 1, count: 5 }, name: "test" };
    const result = stripKeys(input, new Set(["_api_version"]));
    expect(JSON.stringify(result)).toBe('{"data":{"count":5},"name":"test"}');
  });

  it("strips keys inside array objects", () => {
    const input: JsonValue = [{ _api_version: 1, name: "a" }, { _api_version: 1, name: "b" }];
    const result = stripKeys(input, new Set(["_api_version"]));
    expect(JSON.stringify(result)).toBe('[{"name":"a"},{"name":"b"}]');
  });

  it("returns null and primitives unchanged", () => {
    expect(stripKeys(null, new Set(["x"]))).toBeNull();
    expect(stripKeys(42 as JsonValue, new Set(["x"]))).toBe(42);
    expect(stripKeys("hello" as JsonValue, new Set(["x"]))).toBe("hello");
  });

  it("handles empty stripped set", () => {
    const input: JsonValue = { a: 1, b: 2 };
    const result = stripKeys(input, new Set());
    expect(JSON.stringify(result)).toBe('{"a":1,"b":2}');
  });
});

// ---------------------------------------------------------------------------
// SWEEP_ROUTES inventory — sanity checks
// ---------------------------------------------------------------------------

describe("SWEEP_ROUTES inventory", () => {
  it("contains at least one public route and one auth route", () => {
    const publicRoutes = SWEEP_ROUTES.filter((r) => !r.authRequired);
    const authRoutes = SWEEP_ROUTES.filter((r) => r.authRequired);
    expect(publicRoutes.length).toBeGreaterThanOrEqual(1);
    expect(authRoutes.length).toBeGreaterThanOrEqual(1);
  });

  it("does not include any POST/SSE/mutation routes", () => {
    // All sweep routes must be GET safe — check naming conventions
    const forbidden = ["/rpc", "/graphql", "/budget/init", "/feed", "/events"];
    for (const r of SWEEP_ROUTES) {
      const path = r.route;
      expect(forbidden).not.toContain(path);
    }
  });

  it("includes /health", () => {
    const health = SWEEP_ROUTES.find((r) => r.route === "/health");
    expect(health).toBeDefined();
    expect(health?.authRequired).toBe(false);
    expect(health?.fixture).toBe("health.json");
  });

  it("all parameterised routes have an examplePath", () => {
    for (const r of SWEEP_ROUTES) {
      if (r.route.includes(":")) {
        expect(r.examplePath).toBeDefined();
        expect(r.examplePath?.startsWith("/")).toBe(true);
      }
    }
  });

  it("all fixture filenames end with .json", () => {
    for (const r of SWEEP_ROUTES) {
      if (r.fixture) {
        expect(r.fixture.endsWith(".json")).toBe(true);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// printSummary — smoke test (should not throw)
// ---------------------------------------------------------------------------

describe("printSummary", () => {
  it("runs without throwing on a mixed report", () => {
    const results: RouteResult[] = [
      {
        route: "/health",
        probe_path: "/health",
        mode: "golden",
        status_match: true,
        body_match: true,
        diverged: false,
        ts_status: 200,
        ref_status: 200,
        divergence_detail: null,
      },
      {
        route: "/sessions",
        probe_path: "/sessions",
        mode: "shadow",
        status_match: false,
        body_match: false,
        diverged: true,
        ts_status: 500,
        ref_status: 200,
        divergence_detail: "Status mismatch: python=200 ts=500",
      },
    ];
    const report = buildReport(results, "live-shadow");
    // Should not throw
    expect(() => printSummary(report)).not.toThrow();
  });
});
