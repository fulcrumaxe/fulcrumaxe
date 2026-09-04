/**
 * Live-shadow differential proxy (D#1437 Spec P0 DoD #5).
 *
 * Fans a request to BOTH the Python reference backend and the TS backend,
 * then diffs status + parity headers + normalized body.
 *
 * Usage:
 *   bun run src/shadow-proxy.ts [--python-port 18099] [--ts-port 19099] [--route /health]
 *
 * Exit codes:
 *   0 — zero divergence (all smoke routes match)
 *   1 — divergence found (details printed to stdout)
 *   2 — could not reach one or both backends
 *
 * Daemon discipline: this script starts the TS backend as a child process for
 * the duration of the diff, then kills it. The Python backend must already be
 * running (it is the reference; we do NOT start it here).
 *
 * No processes are left running after this script exits.
 */

import { spawn } from "node:child_process";
import { join } from "node:path";
import { compareNormalized } from "./normalizer.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PYTHON_PORT = parseInt(process.env.PYTHON_PORT ?? "18099", 10);
const TS_PORT = parseInt(process.env.TS_PORT ?? "19099", 10);

// Parity-relevant headers to compare (lowercase names; volatile headers excluded)
const PARITY_HEADERS = ["content-type"];

// Smoke routes for P0 + P2.
// P2 adds auth-gated routes — supply AF_API_AUTH_KEY env var to include them.
// When AF_API_AUTH_KEY is unset, only /health is tested (no auth to pass).
const AUTH_KEY = process.env.AF_API_AUTH_KEY;
const SMOKE_ROUTES = AUTH_KEY
  ? [
      "/health",
      "/sessions",
      "/sessions/current",
      "/spawn-queue",
      "/spawn-queue/pending",
      "/spawn-queue/active",
      "/spawn-blocks",
    ]
  : ["/health"];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function waitForPort(port: number, timeoutMs = 5000): Promise<boolean> {
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
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

interface RouteResult {
  status: number;
  headers: Record<string, string>;
  body: string;
}

async function fetchRoute(port: number, route: string): Promise<RouteResult> {
  const url = `http://127.0.0.1:${port}${route}`;
  const reqHeaders: Record<string, string> = { Accept: "application/json" };
  // Pass auth token for auth-gated routes (P2)
  if (AUTH_KEY) {
    reqHeaders["Authorization"] = `Bearer ${AUTH_KEY}`;
  }
  const res = await fetch(url, {
    headers: reqHeaders,
    signal: AbortSignal.timeout(5000),
  });
  const body = await res.text();
  const headers: Record<string, string> = {};
  for (const h of PARITY_HEADERS) {
    const v = res.headers.get(h);
    if (v) headers[h] = v;
  }
  return { status: res.status, headers, body };
}

interface DiffResult {
  route: string;
  diverged: boolean;
  status_match: boolean;
  headers_match: boolean;
  body_match: boolean;
  python_status: number;
  ts_status: number;
  python_norm: string;
  ts_norm: string;
  python_headers: Record<string, string>;
  ts_headers: Record<string, string>;
}

async function diffRoute(route: string): Promise<DiffResult> {
  const [pyResult, tsResult] = await Promise.all([
    fetchRoute(PYTHON_PORT, route),
    fetchRoute(TS_PORT, route),
  ]);

  const statusMatch = pyResult.status === tsResult.status;

  const headersMatch = PARITY_HEADERS.every((h) => {
    const pyVal = (pyResult.headers[h] ?? "").toLowerCase();
    const tsVal = (tsResult.headers[h] ?? "").toLowerCase();
    // Content-type: both should contain application/json; ignore charset differences
    if (h === "content-type") {
      return pyVal.includes("application/json") && tsVal.includes("application/json");
    }
    return pyVal === tsVal;
  });

  const { equal: bodyMatch, normA: pyNorm, normB: tsNorm } = compareNormalized(
    pyResult.body,
    tsResult.body,
    { route }
  );

  return {
    route,
    diverged: !statusMatch || !headersMatch || !bodyMatch,
    status_match: statusMatch,
    headers_match: headersMatch,
    body_match: bodyMatch,
    python_status: pyResult.status,
    ts_status: tsResult.status,
    python_norm: pyNorm,
    ts_norm: tsNorm,
    python_headers: pyResult.headers,
    ts_headers: tsResult.headers,
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  // Parse args
  const args = process.argv.slice(2);
  const routesArg = args.includes("--route")
    ? [args[args.indexOf("--route") + 1]]
    : SMOKE_ROUTES;

  console.log("[shadow-proxy] Starting TS backend for diff...");

  // Start TS backend as a child process
  const tsBackendEntry = join(import.meta.dir, "index.ts");
  const tsProc = spawn("bun", ["run", tsBackendEntry], {
    env: { ...process.env, TS_BACKEND_PORT: String(TS_PORT), PATH: process.env.PATH },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let tsStdout = "";
  let tsStderr = "";
  tsProc.stdout?.on("data", (d: Buffer) => { tsStdout += d.toString(); });
  tsProc.stderr?.on("data", (d: Buffer) => { tsStderr += d.toString(); });

  // Ensure TS process is killed on exit
  const cleanup = (): void => {
    if (!tsProc.killed) {
      tsProc.kill("SIGTERM");
    }
  };
  process.on("exit", cleanup);
  process.on("SIGINT", () => { cleanup(); process.exit(130); });
  process.on("SIGTERM", () => { cleanup(); process.exit(143); });

  try {
    // Verify Python backend is up
    console.log(`[shadow-proxy] Checking Python backend on port ${PYTHON_PORT}...`);
    const pyReady = await waitForPort(PYTHON_PORT, 3000);
    if (!pyReady) {
      console.error(
        `[shadow-proxy] ERROR: Python backend not reachable on port ${PYTHON_PORT}.\n` +
          `Start it first: bash scripts/start-dashboard.sh`
      );
      process.exit(2);
    }
    console.log(`[shadow-proxy] Python backend ready on port ${PYTHON_PORT}`);

    // Wait for TS backend to be ready
    console.log(`[shadow-proxy] Waiting for TS backend on port ${TS_PORT}...`);
    const tsReady = await waitForPort(TS_PORT, 8000);
    if (!tsReady) {
      console.error(
        `[shadow-proxy] ERROR: TS backend did not start on port ${TS_PORT}.\n` +
          `stdout: ${tsStdout}\nstderr: ${tsStderr}`
      );
      process.exit(2);
    }
    console.log(`[shadow-proxy] TS backend ready on port ${TS_PORT}`);

    // Run diffs
    let anyDivergence = false;
    for (const route of routesArg) {
      console.log(`\n[shadow-proxy] Diffing ${route} ...`);
      const result = await diffRoute(route);

      if (result.diverged) {
        anyDivergence = true;
        console.error(`[shadow-proxy] DIVERGENCE on ${route}`);
        if (!result.status_match) {
          console.error(`  Status: Python=${result.python_status} TS=${result.ts_status} MISMATCH`);
        }
        if (!result.headers_match) {
          console.error(`  Headers: Python=${JSON.stringify(result.python_headers)} TS=${JSON.stringify(result.ts_headers)} MISMATCH`);
        }
        if (!result.body_match) {
          console.error(`  Body divergence:`);
          console.error(`    Python normalized: ${result.python_norm}`);
          console.error(`    TS normalized:     ${result.ts_norm}`);
        }
      } else {
        console.log(`[shadow-proxy] ${route} OK — zero divergence`);
        console.log(`  Status: ${result.python_status} (both backends match)`);
        console.log(`  Headers: ${JSON.stringify(result.python_headers)} (both match)`);
        console.log(`  Normalized body: ${result.python_norm}`);
      }
    }

    process.exit(anyDivergence ? 1 : 0);
  } finally {
    cleanup();
  }
}

main().catch((err: unknown) => {
  console.error("[shadow-proxy] Fatal:", err instanceof Error ? err.message : String(err));
  process.exit(2);
});
