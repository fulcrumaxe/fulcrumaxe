/**
 * Golden assertion — asserts that the live TS /health response matches
 * the captured golden fixture after normalization.
 *
 * Usage (CI — no Python server needed):
 *   bun run src/golden-assert.ts [--ts-port 19099]
 *
 * This script:
 * 1. Starts the TS backend as a child process.
 * 2. Fetches /health from it.
 * 3. Normalizes the response.
 * 4. Compares it against the captured fixture (fixtures/health.json).
 * 5. Fails (exit 1) on any divergence from the structural fields.
 * 6. Kills the TS backend before exiting.
 *
 * Note: live dynamic fields (loop_last_run, loop_duration_s, loop_idle_rate)
 * are masked in both the fixture and the response — the assertion verifies
 * the response STRUCTURE (ok:true, _api_version:1, malformed_lines:0, field
 * presence), not the live values.
 *
 * Daemon discipline: TS backend is started and stopped within this script.
 * No processes left running.
 */

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { compareNormalized } from "./normalizer.js";

const TS_PORT = parseInt(process.env.TS_PORT ?? "19099", 10);
const FIXTURES_DIR = join(import.meta.dir, "..", "fixtures");

interface GoldenFixture {
  route: string;
  status: number;
  body: unknown;
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
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

async function main(): Promise<void> {
  // Load fixture
  const fixturePath = join(FIXTURES_DIR, "health.json");
  let fixture: GoldenFixture;
  try {
    fixture = JSON.parse(readFileSync(fixturePath, "utf-8")) as GoldenFixture;
  } catch {
    console.error(`[golden-assert] Cannot read fixture: ${fixturePath}`);
    process.exit(1);
  }

  // Start TS backend
  const tsBackendEntry = join(import.meta.dir, "index.ts");
  const tsProc = spawn("bun", ["run", tsBackendEntry], {
    env: { ...process.env, TS_BACKEND_PORT: String(TS_PORT), PATH: process.env.PATH },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let tsStderr = "";
  tsProc.stderr?.on("data", (d: Buffer) => { tsStderr += d.toString(); });

  const cleanup = (): void => {
    if (!tsProc.killed) tsProc.kill("SIGTERM");
  };
  process.on("exit", cleanup);
  process.on("SIGINT", () => { cleanup(); process.exit(130); });
  process.on("SIGTERM", () => { cleanup(); process.exit(143); });

  try {
    const ready = await waitForPort(TS_PORT, 8000);
    if (!ready) {
      console.error(`[golden-assert] TS backend failed to start on port ${TS_PORT}`);
      if (tsStderr) console.error(`stderr: ${tsStderr}`);
      process.exit(2);
    }

    // Fetch /health from TS backend
    const res = await fetch(`http://127.0.0.1:${TS_PORT}/health`, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(5000),
    });
    const body = await res.text();

    // Compare status
    if (res.status !== fixture.status) {
      console.error(`[golden-assert] FAIL: status mismatch. Got ${res.status}, expected ${fixture.status}`);
      process.exit(1);
    }

    // Compare normalized body against fixture
    const fixtureBodyStr = JSON.stringify(fixture.body);
    const { equal, normA: fixtureNorm, normB: tsNorm } = compareNormalized(
      fixtureBodyStr,
      body,
      { route: fixture.route }
    );

    if (!equal) {
      console.error("[golden-assert] FAIL: body diverges from golden fixture after normalization");
      console.error(`  Golden:  ${fixtureNorm}`);
      console.error(`  TS:      ${tsNorm}`);
      process.exit(1);
    }

    console.log("[golden-assert] PASS: /health matches golden fixture after normalization");
    console.log(`  Status: ${res.status}`);
    console.log(`  Normalized: ${tsNorm}`);
    process.exit(0);
  } finally {
    cleanup();
  }
}

main().catch((err: unknown) => {
  console.error("[golden-assert] Fatal:", err instanceof Error ? err.message : String(err));
  process.exit(2);
});
