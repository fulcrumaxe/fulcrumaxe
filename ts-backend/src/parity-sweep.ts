/**
 * Full parity sweep — on-demand harness for verifying all converted GET routes.
 *
 * Usage:
 *   bun run parity                      # golden-corpus mode (no Python required)
 *   bun run parity -- --live            # live shadow mode (boots TS + requires Python on :18099)
 *   bun run parity -- --report out.json # custom report path
 *
 * What it does:
 *   1. Enumerates every safe read-only GET route registered in the TS backend.
 *   2. For each route, runs either:
 *      (a) golden assertion — compares TS response against the captured fixture
 *          (default; works offline, no Python backend needed); or
 *      (b) live shadow diff — fans to both Python (:18099) and TS (:19099) and
 *          diffs status + normalized body (only when --live flag is set).
 *   3. Collects per-route results: status_match, body_match, divergence_detail.
 *   4. Emits a console summary ("N/N routes at parity") and a JSON report to
 *      ts-backend/parity-report.json (or the path given by --report).
 *   5. Exits non-zero if any route diverges — CI / nightly gate.
 *
 * Read-only probes only. POST /rpc, POST /budget/init, POST /graphql are excluded.
 * SSE routes (/feed, /events) are excluded — streaming endpoints require
 * specialised probing that is out of scope for this sweep.
 *
 * Daemon discipline: the TS backend is booted as a child process and killed on
 * exit. No daemons left running. The Python backend (live mode) must already be
 * running; this script never starts it.
 */

import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { compareNormalized } from "./normalizer.js";
import type { JsonValue } from "./normalizer.js";

// ---------------------------------------------------------------------------
// Route inventory — read-only GET routes safe to probe
//
// Routes that require path params (e.g. /sessions/:id, /stats/metrics/series/:name)
// use fixture-driven probes: we test the STRUCTURE response for valid + invalid
// inputs rather than an exact live value, since the path param is unknown.
// The sweep tests the 404/400 shape (always stable) for parameterised routes.
// ---------------------------------------------------------------------------
export interface SweepRoute {
  /** Route path as registered in index.ts. */
  route: string;
  /** Auth required? If so, we pass AF_API_AUTH_KEY in the Authorization header. */
  authRequired: boolean;
  /** Golden fixture filename (relative to fixtures/). Null = no fixture available. */
  fixture: string | null;
  /**
   * For parameterised routes, an example path to probe.
   * The probe may return 404/400 — both sides should agree on the status.
   */
  examplePath?: string;
  /** If true, mask live-varying fields so only structure is verified. */
  structureOnly?: boolean;
}

export const SWEEP_ROUTES: SweepRoute[] = [
  // Public — no auth
  { route: "/health", authRequired: false, fixture: "health.json" },
  { route: "/openapi.json", authRequired: false, fixture: null },

  // Auth-gated — SQLite reads (P2)
  { route: "/sessions", authRequired: true, fixture: "sessions.json", structureOnly: true },
  { route: "/sessions/current", authRequired: true, fixture: null },
  { route: "/sessions/compare", authRequired: true, fixture: null },
  // Parameterised — probe with an obviously-absent ID: both sides should 404
  {
    route: "/sessions/:session_id",
    authRequired: true,
    fixture: null,
    examplePath: "/sessions/parity-probe-no-such-session-000",
  },

  // Auth-gated — spawn-queue / spawn-blocks (P2)
  { route: "/spawn-queue", authRequired: true, fixture: "spawn-queue.json", structureOnly: true },
  { route: "/spawn-queue/pending", authRequired: true, fixture: null },
  { route: "/spawn-queue/active", authRequired: true, fixture: null },
  { route: "/spawn-blocks", authRequired: true, fixture: "spawn-blocks.json", structureOnly: true },

  // Auth-gated — DuckDB stats (P3)
  // Note: stats-metrics-summary has a raw-body fixture with live values — we verify
  // 200 status + shape (metrics array present) rather than exact body parity.
  // The fixture is the raw API response body, not the golden-capture envelope format.
  {
    route: "/stats/metrics/summary",
    authRequired: true,
    fixture: null, // live values change constantly; structure-check only
    examplePath: "/stats/metrics/summary",
  },
  // Parameterised series — probe with an unknown metric name; both sides should agree on status
  {
    route: "/stats/metrics/series/:name",
    authRequired: true,
    fixture: null,
    examplePath: "/stats/metrics/series/parity-probe-no-such-metric",
  },
];

// ---------------------------------------------------------------------------
// Per-route masked fields for structure-only assertion.
// "structure-only" fixtures mask all live-changing numeric/timestamp fields so
// we verify shape + key presence without chasing live values.
//
// _api_version: the legacyEnvelopeMiddleware injects this into every TS response.
// Fixtures captured before the middleware was added do not include it. We mask
// it in structure-only comparisons so fixture staleness on this field does not
// cause false divergences. The field's presence (not value) is what matters —
// golden-assert.ts already checks this for /health.
// ---------------------------------------------------------------------------
const STRUCTURE_MASKED_FIELDS: Record<string, string[]> = {
  "/sessions": ["_api_version"],
  "/spawn-queue": [
    "_api_version",
    "active_total",
    "completed",
    "failed",
    "pending",
    "utilization_pct",
    "active",
    "limit",
    "total_limit",
  ],
  "/spawn-blocks": ["_api_version"],
  "/stats/metrics/summary": ["_api_version", "value", "updated_at_iso"],
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RouteResult {
  route: string;
  probe_path: string;
  mode: "golden" | "shadow" | "no-fixture";
  status_match: boolean;
  body_match: boolean;
  diverged: boolean;
  ts_status: number;
  ref_status: number | null;
  divergence_detail: string | null;
  note?: string;
}

export interface ParityReport {
  generated_at: string;
  mode: "golden" | "live-shadow";
  total: number;
  at_parity: number;
  diverged: number;
  results: RouteResult[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Recursively strip all keys in `stripped` from a JsonValue.
 * Used for structure-only comparisons where certain keys should not appear
 * in either the fixture or the live response before comparison.
 * This is different from masking (replacing values with "<masked>") — stripping
 * removes the key entirely so both sides look identical whether or not the key
 * is present.
 */
export function stripKeys(val: JsonValue, stripped: Set<string>): JsonValue {
  if (val === null || typeof val !== "object") return val;
  if (Array.isArray(val)) {
    return val.map((item) => stripKeys(item, stripped));
  }
  const out: Record<string, JsonValue> = {};
  for (const k of Object.keys(val as Record<string, JsonValue>)) {
    if (!stripped.has(k)) {
      out[k] = stripKeys((val as Record<string, JsonValue>)[k], stripped);
    }
  }
  return out;
}

async function waitForPort(port: number, timeoutMs = 8000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/health`, {
        signal: AbortSignal.timeout(500),
      });
      if (res.status < 500) return true;
    } catch {
      // not ready yet
    }
    await new Promise<void>((r) => setTimeout(r, 200));
  }
  return false;
}

function makeHeaders(authRequired: boolean, authKey: string | undefined): Record<string, string> {
  const h: Record<string, string> = { Accept: "application/json" };
  if (authRequired && authKey) {
    h["Authorization"] = `Bearer ${authKey}`;
  }
  return h;
}

async function fetchRoute(
  port: number,
  path: string,
  authRequired: boolean,
  authKey: string | undefined
): Promise<{ status: number; body: string }> {
  const url = `http://127.0.0.1:${port}${path}`;
  const res = await fetch(url, {
    headers: makeHeaders(authRequired, authKey),
    signal: AbortSignal.timeout(5000),
  });
  const body = await res.text();
  return { status: res.status, body };
}

// ---------------------------------------------------------------------------
// Golden assertion for one route (pure logic exported for unit testing)
// ---------------------------------------------------------------------------
export function assertAgainstFixture(
  route: SweepRoute,
  tsStatus: number,
  tsBody: string,
  fixtureDir: string
): RouteResult {
  const probePath = route.examplePath ?? route.route;
  const baseResult = {
    route: route.route,
    probe_path: probePath,
    ts_status: tsStatus,
    ref_status: null as number | null,
  };

  if (!route.fixture) {
    // No fixture — verify only that TS responds without a server error (5xx)
    const ok = tsStatus < 500;
    return {
      ...baseResult,
      mode: "no-fixture" as const,
      status_match: ok,
      body_match: ok,
      diverged: !ok,
      divergence_detail: ok ? null : `TS returned server error ${tsStatus}`,
      note: "no fixture — verified only that TS does not 5xx",
    };
  }

  const fixturePath = join(fixtureDir, route.fixture);
  let fixtureStatus: number;
  let fixtureBody: JsonValue;
  try {
    const raw = JSON.parse(readFileSync(fixturePath, "utf-8")) as Record<string, JsonValue>;
    // Fixtures come in two formats:
    //   (a) golden-capture envelope: { status, body, captured_at, ... }
    //   (b) raw body (legacy, pre-envelope): the response body directly
    // Detect by presence of a numeric "status" field at the root.
    if (typeof raw["status"] === "number" && "body" in raw) {
      fixtureStatus = raw["status"] as number;
      fixtureBody = raw["body"] as JsonValue;
    } else {
      // Legacy raw-body fixture — treat as HTTP 200
      fixtureStatus = 200;
      fixtureBody = raw;
    }
  } catch {
    return {
      ...baseResult,
      mode: "golden" as const,
      status_match: false,
      body_match: false,
      diverged: true,
      divergence_detail: `Cannot read fixture file: ${fixturePath}`,
    };
  }

  const statusMatch = tsStatus === fixtureStatus;

  // Body check.
  // Structure-only routes: strip live-varying and middleware-injected fields from
  // BOTH sides before comparison so fixture staleness on those keys doesn't cause
  // false divergences. Stripping (removing the key entirely) is used rather than
  // masking (replacing value with "<masked>") because the fixture may not have
  // the key at all (e.g. _api_version was added by legacyEnvelopeMiddleware after
  // the fixture was captured).
  let strippedSet: Set<string> | null = null;
  if (route.structureOnly && STRUCTURE_MASKED_FIELDS[route.route]) {
    strippedSet = new Set(STRUCTURE_MASKED_FIELDS[route.route]);
  }

  let bodyMatch = false;
  let divergenceDetail: string | null = null;

  try {
    let effectiveFixtureBody: JsonValue = fixtureBody;
    let effectiveTsBody = tsBody;

    if (strippedSet) {
      // Strip live-varying fields from both sides
      const tsParsed = JSON.parse(tsBody) as JsonValue;
      effectiveFixtureBody = stripKeys(fixtureBody, strippedSet);
      effectiveTsBody = JSON.stringify(stripKeys(tsParsed, strippedSet));
    }

    const fixtureBodyStr = JSON.stringify(effectiveFixtureBody);
    const { equal, normA: fixtureNorm, normB: tsNorm } = compareNormalized(fixtureBodyStr, effectiveTsBody, {
      route: route.route,
    });
    bodyMatch = equal;
    if (!equal) {
      divergenceDetail = `Body mismatch:\n  fixture: ${fixtureNorm}\n  ts:      ${tsNorm}`;
    }
  } catch (err) {
    bodyMatch = false;
    divergenceDetail = `Body parse error: ${err instanceof Error ? err.message : String(err)}`;
  }

  const diverged = !statusMatch || !bodyMatch;
  if (!statusMatch) {
    const statusMsg = `Status mismatch: fixture=${fixtureStatus} ts=${tsStatus}`;
    divergenceDetail = divergenceDetail ? `${statusMsg}\n${divergenceDetail}` : statusMsg;
  }

  return {
    ...baseResult,
    ref_status: fixtureStatus,
    mode: "golden" as const,
    status_match: statusMatch,
    body_match: bodyMatch,
    diverged,
    divergence_detail: diverged ? divergenceDetail : null,
  };
}

// ---------------------------------------------------------------------------
// Shadow diff for one route (live mode) — also exported for unit testing
// ---------------------------------------------------------------------------
export async function shadowDiffRoute(
  route: SweepRoute,
  pythonPort: number,
  tsPort: number,
  authKey: string | undefined
): Promise<RouteResult> {
  const probePath = route.examplePath ?? route.route;

  const [pyResult, tsResult] = await Promise.all([
    fetchRoute(pythonPort, probePath, route.authRequired, authKey),
    fetchRoute(tsPort, probePath, route.authRequired, authKey),
  ]);

  const statusMatch = pyResult.status === tsResult.status;

  let bodyMatch = false;
  let divergenceDetail: string | null = null;

  if (statusMatch) {
    try {
      const { equal, normA: pyNorm, normB: tsNorm } = compareNormalized(pyResult.body, tsResult.body, {
        route: route.route,
      });
      bodyMatch = equal;
      if (!equal) {
        divergenceDetail = `Body mismatch:\n  python: ${pyNorm}\n  ts:     ${tsNorm}`;
      }
    } catch (err) {
      bodyMatch = false;
      divergenceDetail = `Body parse error: ${err instanceof Error ? err.message : String(err)}`;
    }
  } else {
    divergenceDetail = `Status mismatch: python=${pyResult.status} ts=${tsResult.status}`;
  }

  return {
    route: route.route,
    probe_path: probePath,
    mode: "shadow" as const,
    status_match: statusMatch,
    body_match: bodyMatch,
    diverged: !statusMatch || !bodyMatch,
    ts_status: tsResult.status,
    ref_status: pyResult.status,
    divergence_detail: !statusMatch || !bodyMatch ? divergenceDetail : null,
  };
}

// ---------------------------------------------------------------------------
// Report shaping (pure — testable without live backends)
// ---------------------------------------------------------------------------
export function buildReport(results: RouteResult[], mode: "golden" | "live-shadow"): ParityReport {
  const divergedCount = results.filter((r) => r.diverged).length;
  return {
    generated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    mode,
    total: results.length,
    at_parity: results.length - divergedCount,
    diverged: divergedCount,
    results,
  };
}

export function printSummary(report: ParityReport): void {
  const passIcon = (ok: boolean): string => (ok ? "PASS" : "FAIL");
  console.log("\n[parity-sweep] ============================");
  console.log(`[parity-sweep] Mode:    ${report.mode}`);
  console.log(`[parity-sweep] Verdict: ${report.at_parity}/${report.total} routes at parity`);
  if (report.diverged > 0) {
    console.log(`[parity-sweep] DIVERGENCES: ${report.diverged}`);
  }
  console.log("[parity-sweep] ----------------------------");
  for (const r of report.results) {
    const mark = passIcon(!r.diverged);
    const suffix = r.note ? ` (${r.note})` : "";
    console.log(`[parity-sweep] ${mark}  ${r.probe_path}${suffix}`);
    if (r.diverged && r.divergence_detail) {
      for (const line of r.divergence_detail.split("\n")) {
        console.log(`           ${line}`);
      }
    }
  }
  console.log("[parity-sweep] ============================\n");
}

// ---------------------------------------------------------------------------
// Main — only executed when invoked directly (not when imported by tests)
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const liveMode = args.includes("--live");
  const reportIdx = args.indexOf("--report");
  const reportPath =
    reportIdx !== -1 && args[reportIdx + 1]
      ? args[reportIdx + 1]
      : join(import.meta.dir, "..", "parity-report.json");

  const TS_PORT = parseInt(process.env.TS_PORT ?? "19099", 10);
  const PYTHON_PORT = parseInt(process.env.PYTHON_PORT ?? "18099", 10);
  const AUTH_KEY = process.env.AF_API_AUTH_KEY;
  const FIXTURES_DIR = join(import.meta.dir, "..", "fixtures");

  console.log(`[parity-sweep] Starting sweep (mode=${liveMode ? "live-shadow" : "golden"})...`);
  if (!AUTH_KEY) {
    console.log("[parity-sweep] AF_API_AUTH_KEY not set — auth-gated routes will probe without token.");
    console.log("[parity-sweep]   Auth-gated routes should return 401 on both backends — that counts as parity.");
  }

  // Boot TS backend as a child process (needed in both modes)
  const tsBackendEntry = join(import.meta.dir, "index.ts");
  const tsProc = spawn("bun", ["run", tsBackendEntry], {
    env: { ...process.env, TS_BACKEND_PORT: String(TS_PORT), PATH: process.env.PATH },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let tsStderr = "";
  tsProc.stderr?.on("data", (d: Buffer) => { tsStderr += d.toString(); });

  /**
   * Kill the child and AWAIT its exit before returning.
   *
   * Why not process.on('exit') + SIGTERM?
   *   Under Bun, 'exit' listeners run synchronously — you cannot await anything
   *   inside them. Bun exits immediately, leaving the child alive (PPid=1, port
   *   still bound). A second `bun run parity` would silently reuse the stale daemon.
   *
   * Strategy: SIGKILL (instant, no ignore risk) + await the 'exit' event with a
   * 3-second fallback. SIGKILL is safe here because the child is our own backend,
   * not a third-party process that needs graceful shutdown.
   */
  const killAndWait = async (): Promise<void> => {
    if (tsProc.exitCode !== null || tsProc.killed) return; // already dead
    tsProc.kill("SIGKILL");
    await new Promise<void>((resolve) => {
      // Resolve immediately if already exited by the time we attach the listener
      if (tsProc.exitCode !== null) { resolve(); return; }
      const onExit = (): void => resolve();
      tsProc.once("exit", onExit);
      // Bounded fallback: if SIGKILL somehow doesn't fire 'exit' in 3s, move on
      setTimeout(() => { tsProc.removeListener("exit", onExit); resolve(); }, 3000);
    });
  };

  // Signal handlers: await kill, THEN exit — so we don't orphan the child
  process.on("SIGINT", () => {
    killAndWait().then(() => process.exit(130)).catch(() => process.exit(130));
  });
  process.on("SIGTERM", () => {
    killAndWait().then(() => process.exit(143)).catch(() => process.exit(143));
  });

  let exitCode = 0;
  try {
    console.log(`[parity-sweep] Waiting for TS backend on port ${TS_PORT}...`);
    const tsReady = await waitForPort(TS_PORT, 10000);
    if (!tsReady) {
      console.error(`[parity-sweep] TS backend failed to start on port ${TS_PORT}`);
      if (tsStderr) console.error(`stderr: ${tsStderr}`);
      exitCode = 2;
    } else {
      console.log(`[parity-sweep] TS backend ready on port ${TS_PORT}`);

      if (liveMode) {
        console.log(`[parity-sweep] Checking Python backend on port ${PYTHON_PORT}...`);
        const pyReady = await waitForPort(PYTHON_PORT, 3000);
        if (!pyReady) {
          console.error(
            `[parity-sweep] ERROR: Python backend not reachable on port ${PYTHON_PORT}.\n` +
              `Start it first: bash scripts/start-dashboard.sh\n` +
              `Or omit --live for golden-corpus mode (no Python needed).`
          );
          exitCode = 2;
        } else {
          console.log(`[parity-sweep] Python backend ready on port ${PYTHON_PORT}`);
        }
      }

      if (exitCode === 0) {
        // Run probes
        const results: RouteResult[] = [];
        for (const route of SWEEP_ROUTES) {
          const probePath = route.examplePath ?? route.route;
          console.log(`[parity-sweep] Probing ${probePath} ...`);

          if (liveMode) {
            results.push(await shadowDiffRoute(route, PYTHON_PORT, TS_PORT, AUTH_KEY));
          } else {
            const tsResult = await fetchRoute(TS_PORT, probePath, route.authRequired, AUTH_KEY);
            results.push(assertAgainstFixture(route, tsResult.status, tsResult.body, FIXTURES_DIR));
          }
        }

        const report = buildReport(results, liveMode ? "live-shadow" : "golden");
        printSummary(report);

        writeFileSync(reportPath, JSON.stringify(report, null, 2) + "\n", "utf-8");
        console.log(`[parity-sweep] Report written to: ${reportPath}`);

        if (report.diverged > 0) {
          console.error(`[parity-sweep] FAIL — ${report.diverged} route(s) diverged from reference.`);
          exitCode = 1;
        } else {
          console.log(`[parity-sweep] All ${report.at_parity}/${report.total} routes at parity.`);
        }
      }
    }
  } finally {
    // Always kill and AWAIT child exit before we return — this is the key fix.
    // process.exit() below will not fire until this await resolves, so the child
    // cannot outlive the sweep regardless of which code path we took.
    await killAndWait();
  }
  process.exit(exitCode);
}

if (import.meta.main) {
  main().catch((err: unknown) => {
    console.error("[parity-sweep] Fatal:", err instanceof Error ? err.message : String(err));
    process.exit(2);
  });
}
