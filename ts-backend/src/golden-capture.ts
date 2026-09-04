/**
 * Golden-corpus capture tool (D#1437 Spec P0 DoD #4).
 *
 * Captures canonical request→response fixtures from the live Python reference
 * backend and stores them under version control in ts-backend/fixtures/.
 *
 * Usage:
 *   bun run src/golden-capture.ts [--python-port 18099] [--route /health]
 *
 * Daemon discipline: this script does NOT start the Python backend. It reads
 * from a RUNNING Python server. If the server is not up, it fails fast with a
 * clear error. No daemons started, no processes left running.
 *
 * Fixture format (fixtures/<route-slug>.json):
 * {
 *   "captured_at": "2026-05-23T...",
 *   "python_port": 18099,
 *   "route": "/health",
 *   "status": 200,
 *   "headers": { "content-type": "..." },
 *   "body": { ...normalized response... },
 *   "raw_body": "...",
 *   "mask_applied": ["loop_last_run", "loop_duration_s", "loop_idle_rate"]
 * }
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { normalize } from "./normalizer.js";
import type { JsonValue } from "./normalizer.js";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PYTHON_PORT = parseInt(process.env.PYTHON_PORT ?? "18099", 10);
const FIXTURES_DIR = join(import.meta.dir, "..", "fixtures");

// Parity-relevant headers (exclude volatile ones like date, server, etc.)
const PARITY_HEADERS = ["content-type"];

interface GoldenFixture {
  captured_at: string;
  python_port: number;
  route: string;
  status: number;
  headers: Record<string, string>;
  body: JsonValue;
  raw_body: string;
  mask_applied: string[];
}

async function captureRoute(route: string): Promise<GoldenFixture> {
  const url = `http://127.0.0.1:${PYTHON_PORT}${route}`;
  console.log(`[golden-capture] Fetching ${url} ...`);

  let res: Response;
  try {
    res = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(5000),
    });
  } catch (err) {
    const msg =
      err instanceof Error ? err.message : String(err);
    throw new Error(
      `[golden-capture] Cannot reach Python backend at ${url}: ${msg}\n` +
        `Make sure the Python server is running: bash scripts/start-dashboard.sh`
    );
  }

  const rawBody = await res.text();
  const parsedBody = JSON.parse(rawBody) as JsonValue;

  // Extract parity-relevant headers
  const headers: Record<string, string> = {};
  for (const h of PARITY_HEADERS) {
    const v = res.headers.get(h);
    if (v) headers[h] = v;
  }

  // Normalize with masking for route
  const normalizedBody = normalize(parsedBody, { route });

  // Determine which fields were masked
  const maskApplied: string[] = [];
  if (typeof parsedBody === "object" && parsedBody !== null && !Array.isArray(parsedBody)) {
    if (typeof normalizedBody === "object" && normalizedBody !== null && !Array.isArray(normalizedBody)) {
      for (const k of Object.keys(normalizedBody)) {
        if ((normalizedBody as Record<string, JsonValue>)[k] === "<masked>") {
          maskApplied.push(k);
        }
      }
    }
  }

  const fixture: GoldenFixture = {
    captured_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    python_port: PYTHON_PORT,
    route,
    status: res.status,
    headers,
    body: normalizedBody,
    raw_body: rawBody,
    mask_applied: maskApplied,
  };

  return fixture;
}

function routeToFilename(route: string): string {
  return route.replace(/^\//, "").replace(/\//g, "-") || "root";
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const routeArg = args[args.indexOf("--route") + 1] ?? "/health";

  mkdirSync(FIXTURES_DIR, { recursive: true });

  const fixture = await captureRoute(routeArg);
  const filename = `${routeToFilename(routeArg)}.json`;
  const outPath = join(FIXTURES_DIR, filename);

  writeFileSync(outPath, JSON.stringify(fixture, null, 2) + "\n", "utf-8");

  console.log(`[golden-capture] Fixture saved: ${outPath}`);
  console.log(`[golden-capture] Status: ${fixture.status}`);
  console.log(`[golden-capture] Fields masked: ${fixture.mask_applied.join(", ") || "(none)"}`);
  console.log(`[golden-capture] Normalized body: ${JSON.stringify(fixture.body)}`);
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
