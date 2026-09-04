/**
 * tests/meta/timeout-governs.test.ts
 *
 * D#2276 — Gate 2 differential proving the configured suite default
 * (`bun run test`, package.json's --timeout 30000) is what governs a slow
 * subprocess, and NOT any spawnSync-level bound the test also happens to
 * declare.
 *
 * Per D#2149, a suite passing on a fast/quiet host proves nothing about a
 * slow one. This file makes the claim falsifiable both ways:
 *
 *   A — spawns a subprocess that sleeps ~6000ms, with a deliberately
 *       GENEROUS spawnSync `timeout: 45_000` and NO per-test bun timeout
 *       override. If the spawnSync bound were what governed, this would
 *       always pass regardless of bun's per-test timeout. It doesn't:
 *       run under the repo's configured invocation (bun run test,
 *       --timeout 30000) it passes; run with bun's bare --timeout 5000 it
 *       is killed. That's the differential — same file, same code path,
 *       only the per-test bound varies.
 *
 *   B — proves the "run with --timeout 5000, it fails" side of that claim
 *       by actually doing it: spawns `bun test <this file> -t <A's name>
 *       --timeout 5000` as a subprocess and asserts it exits non-zero with
 *       a bun timeout marker in its output. This is the check-actually-
 *       fires half — a check only ever observed passing is unfalsified.
 *
 * A is also the permanent regression guard: if the configured default in
 * ts-backend/package.json ever reverts to bun's bare 5000ms, A starts
 * failing under the repo's own `bun run test` invocation.
 */

import { describe, it, expect } from "bun:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";

const THIS_FILE = fileURLToPath(import.meta.url);
// tests/meta/timeout-governs.test.ts -> tests/meta -> tests -> ts-backend
const TS_BACKEND_ROOT = join(dirname(THIS_FILE), "..", "..");
const THIS_FILE_REL = relative(TS_BACKEND_ROOT, THIS_FILE);

// Kept as a constant so A's `it()` name and B's `-t` filter can never drift
// apart from each other.
const SLOW_SUBPROCESS_TEST_NAME =
  "A - governs a ~6s subprocess under the configured suite default, with a generous spawnSync bound and no per-test override";

describe("bun test timeout governance (D#2276 Gate 2 differential)", () => {
  it(SLOW_SUBPROCESS_TEST_NAME, () => {
    // Deliberately generous spawnSync bound (45_000ms) so it provably
    // cannot be what fires here — only bun's own per-test timeout can.
    // No third argument to it() — this relies entirely on the ambient
    // default, which is 30_000ms under `bun run test` and bun's bare
    // 5000ms under a plain `bun test`.
    const result = spawnSync("bash", ["-c", "sleep 6"], {
      encoding: "utf-8",
      timeout: 45_000,
    });
    expect(result.status).toBe(0);
  });

  it(
    "B - proves --timeout 5000 kills A (D#2149 differential: same file, same code path, only the per-test bound varies)",
    () => {
      // Runs A in a fresh bun process pinned to bun's bare 5000ms default,
      // isolated via -t so only A executes. A's subprocess sleeps ~6000ms,
      // so this must be killed by bun's per-test timeout — proving the
      // spawnSync 45_000ms bound inside A is not what governs it.
      const result = spawnSync(
        "bun",
        ["test", THIS_FILE_REL, "-t", SLOW_SUBPROCESS_TEST_NAME, "--timeout", "5000"],
        {
          cwd: TS_BACKEND_ROOT,
          encoding: "utf-8",
          timeout: 20_000, // generous bound on this outer subprocess itself
        }
      );

      const combinedOutput = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;

      expect(result.status).not.toBe(0);
      expect(combinedOutput).toMatch(/timed out after 5000ms/i);
    },
    20_000 // generous per-test bound: the inner run takes ~5-6.5s to be killed
  );
});
