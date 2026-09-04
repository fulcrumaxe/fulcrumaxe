/**
 * tests/spawn/fresh-body-read.parity.test.ts
 *
 * Parity/regression tests for src/spawn/fresh-body-read.ts (D#1794).
 *
 * D#1794 is the ts-backend counterpart of PR #1783: the Python spawn gate used to
 * read a Discussion body through a 300s cache with no `--fresh` opt-in, so a PM
 * flipping STATUS to SPEC_READY could be immediately followed by a gate that still
 * saw the pre-flip body. PR #1783 fixed the Python gate sites by passing `--fresh`
 * to `backend/discussion_cache.py get-body` and treating exit code 3 (fresh fetch
 * failed, stdout holds a stale fallback) as a hard block rather than silently
 * passing the stale body through.
 *
 * This module (fresh-body-read.ts) is the equivalent for the two TS gate-time call
 * sites (spawn-agent.ts's checkSpecReadiness, pre-spawn-check.ts's checkSpecReadiness).
 * These tests exercise `readFreshBody()` directly against a fake `python3` executable
 * placed first on PATH, so no live GitHub/GraphQL access is required and the whole
 * suite runs offline, exactly as this Discussion's not-live-yet TS path requires.
 *
 * Two layers, per the Spec:
 *   1. Argv assertion — the fake executable records the argv it was invoked with;
 *      we assert "--fresh" is present. This is the literal thing PR #1783 added on
 *      the Python side and this PR adds on the TS side.
 *   2. Exit-code classification — the fake executable is driven through exit 0
 *      (live), exit 3 with a stale body on stdout (stale fallback), exit 1 (nothing
 *      available), and a missing-script case, asserting readFreshBody() classifies
 *      each correctly. This is the half that catches the old bare `catch {}`
 *      swallowing the exit-3 case indistinguishably from "python3 is missing".
 *
 * Before this PR, `ts-backend/src/spawn/fresh-body-read.ts` did not exist at all, so
 * this file cannot be run unmodified against `origin/main` — there is nothing to
 * `bun test` there. The PR body explains this rather than forcing an artificial
 * before/after diff: the "before" behavior is fully captured by the fact that
 * `spawn-agent.ts` and `pre-spawn-check.ts` on main call `execFileSync("python3",
 * [cacheScript, "get-body", String(discussion)], ...)` with no "--fresh" in argv at
 * all — see acceptance criterion 1's grep table in the PR body.
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, writeFileSync, chmodSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { readFreshBody } from "../../src/spawn/fresh-body-read.js";

// ---------------------------------------------------------------------------
// Fixture harness: a fake `python3` on PATH that stands in for
// `backend/discussion_cache.py get-body <n> --fresh`.
//
// Behavior is selected via FRESH_BODY_TEST_MODE:
//   live    -> prints a live body, exits 0
//   stale   -> prints a stale fallback body, exits 3   (the D#1778 contract)
//   empty   -> prints nothing, exits 1
//   crash   -> exits 2 with no recognizable stdout (generic failure, not the
//              documented stale-fallback case)
//
// Every invocation appends its full argv (space-joined) as one line to
// FRESH_BODY_TEST_MARKER, so tests can assert on what was actually passed
// on the command line (the "--fresh" flag, in particular).
// ---------------------------------------------------------------------------

let tempDir: string;
let repoRoot: string;
let binDir: string;
let markerFile: string;
let origPath: string | undefined;

function writeFakePython3(): void {
  binDir = join(tempDir, "bin");
  mkdirSync(binDir, { recursive: true });
  const script = `#!/usr/bin/env bash
echo "$@" >> "${markerFile}"
case "\${FRESH_BODY_TEST_MODE:-}" in
  live)
    echo "LIVE_BODY_CONTENT"
    exit 0
    ;;
  stale)
    echo "STALE_BODY_FALLBACK"
    exit 3
    ;;
  empty)
    exit 1
    ;;
  crash)
    echo "unexpected failure" 1>&2
    exit 2
    ;;
  *)
    exit 1
    ;;
esac
`;
  const fakePython3 = join(binDir, "python3");
  writeFileSync(fakePython3, script, { encoding: "utf-8" });
  chmodSync(fakePython3, 0o755);
}

beforeEach(() => {
  tempDir = join(tmpdir(), `fresh-body-read-test-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  repoRoot = join(tempDir, "repo");
  mkdirSync(join(repoRoot, "backend"), { recursive: true });
  // readFreshBody only checks existsSync() on this path before shelling out —
  // content is irrelevant because the fake python3 below ignores it too.
  writeFileSync(join(repoRoot, "backend", "discussion_cache.py"), "# fixture placeholder\n");

  markerFile = join(tempDir, "argv.log");
  writeFileSync(markerFile, "");
  writeFakePython3();

  origPath = process.env["PATH"];
  process.env["PATH"] = `${binDir}:${origPath ?? ""}`;
});

afterEach(() => {
  process.env["PATH"] = origPath;
  delete process.env["FRESH_BODY_TEST_MODE"];
  rmSync(tempDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Layer 1: argv assertion
// ---------------------------------------------------------------------------

describe("readFreshBody — argv", () => {
  it("passes --fresh and the correct get-body argv to the cache script", () => {
    process.env["FRESH_BODY_TEST_MODE"] = "live";
    readFreshBody(repoRoot, 1794);

    const argvLine = readFileSync(markerFile, "utf-8").trim();
    expect(argvLine).toContain("get-body");
    expect(argvLine).toContain("1794");
    expect(argvLine).toContain("--fresh");
  });

  it("invokes the cache script at backend/discussion_cache.py under repoRoot", () => {
    process.env["FRESH_BODY_TEST_MODE"] = "live";
    readFreshBody(repoRoot, 42);

    const argvLine = readFileSync(markerFile, "utf-8").trim();
    expect(argvLine).toContain(join(repoRoot, "backend", "discussion_cache.py"));
  });
});

// ---------------------------------------------------------------------------
// Layer 2: exit-code classification
// ---------------------------------------------------------------------------

describe("readFreshBody — exit-code classification", () => {
  it("classifies exit 0 as live and returns the printed body", () => {
    process.env["FRESH_BODY_TEST_MODE"] = "live";
    const result = readFreshBody(repoRoot, 1);

    expect(result.status).toBe("live");
    if (result.status === "live") {
      expect(result.body.trim()).toBe("LIVE_BODY_CONTENT");
    }
  });

  it("classifies exit 3 as stale and preserves the stale body from stdout — this is the D#1794 fix", () => {
    process.env["FRESH_BODY_TEST_MODE"] = "stale";
    const result = readFreshBody(repoRoot, 2);

    // This is the exact case a bare `catch {}` (the pre-D#1794 code) could not
    // distinguish from "python3 is missing": the process exits non-zero, but
    // stdout genuinely holds a body — it's just not a fresh one.
    expect(result.status).toBe("stale");
    if (result.status === "stale") {
      expect(result.body.trim()).toBe("STALE_BODY_FALLBACK");
    }
  });

  it("classifies exit 1 (nothing available) as unavailable", () => {
    process.env["FRESH_BODY_TEST_MODE"] = "empty";
    const result = readFreshBody(repoRoot, 3);

    expect(result.status).toBe("unavailable");
  });

  it("classifies a non-3 crash (exit 2, no stale body) as unavailable, not stale", () => {
    process.env["FRESH_BODY_TEST_MODE"] = "crash";
    const result = readFreshBody(repoRoot, 4);

    expect(result.status).toBe("unavailable");
  });

  it("classifies a missing cache script as unavailable without shelling out at all", () => {
    rmSync(join(repoRoot, "backend", "discussion_cache.py"));
    process.env["FRESH_BODY_TEST_MODE"] = "live";

    const result = readFreshBody(repoRoot, 5);

    expect(result.status).toBe("unavailable");
    // No argv should have been recorded — the module must not shell out when
    // the script doesn't exist.
    expect(readFileSync(markerFile, "utf-8").trim()).toBe("");
  });
});

// ---------------------------------------------------------------------------
// Comment-hygiene regression: the stale "same as bash does" claim this
// Discussion's acceptance criterion 3 requires removed must stay removed.
// ---------------------------------------------------------------------------

describe("stale-comment regression", () => {
  it("spawn-agent.ts and pre-spawn-check.ts no longer claim a false bash equivalence", () => {
    const thisFile = new URL(import.meta.url).pathname;
    // tests/spawn/ -> tests/ -> ts-backend/
    const tsBackendRoot = join(thisFile, "..", "..", "..");
    const spawnAgentSrc = readFileSync(
      join(tsBackendRoot, "src", "spawn", "spawn-agent.ts"),
      "utf-8"
    );
    const preSpawnCheckSrc = readFileSync(
      join(tsBackendRoot, "src", "spawn", "pre-spawn-check.ts"),
      "utf-8"
    );
    expect(spawnAgentSrc).not.toContain("same as bash does");
    expect(preSpawnCheckSrc).not.toContain("same as bash does");
  });

  it("the two gate-time call sites route through readFreshBody instead of inlining execFileSync for get-body", () => {
    const thisFile = new URL(import.meta.url).pathname;
    const tsBackendRoot = join(thisFile, "..", "..", "..");
    const spawnAgentSrc = readFileSync(
      join(tsBackendRoot, "src", "spawn", "spawn-agent.ts"),
      "utf-8"
    );
    const preSpawnCheckSrc = readFileSync(
      join(tsBackendRoot, "src", "spawn", "pre-spawn-check.ts"),
      "utf-8"
    );
    expect(spawnAgentSrc).toContain('from "./fresh-body-read.js"');
    expect(preSpawnCheckSrc).toContain('from "./fresh-body-read.js"');
    // The old bare inline call: execFileSync("python3", [cacheScript, "get-body", ...
    // with no "--fresh" must no longer appear at the two gate sites.
    expect(spawnAgentSrc).not.toContain(
      'execFileSync("python3", [cacheScript, "get-body", String(discussion)]'
    );
    expect(preSpawnCheckSrc).not.toContain(
      'execFileSync("python3", [cacheScript, "get-body", String(discussion)]'
    );
  });

  it("discussion-status.ts is untouched — it deliberately mirrors backend/discussion_status.py, which also omits --fresh", () => {
    const thisFile = new URL(import.meta.url).pathname;
    const tsBackendRoot = join(thisFile, "..", "..", "..");
    const discussionStatusSrc = readFileSync(
      join(tsBackendRoot, "src", "spawn", "discussion-status.ts"),
      "utf-8"
    );
    // Both of its get-body sites must still call get-body with no --fresh flag —
    // "fixing" them would create the exact TS/Python divergence this Discussion
    // exists to prevent, since backend/discussion_status.py also has no --fresh.
    expect(discussionStatusSrc).not.toContain("fresh-body-read");
    expect(discussionStatusSrc).not.toContain("--fresh");
  });

  it("discussion-cache.ts (the bun-native cache) gained no --fresh CLI/getBody support — no caller needs it", () => {
    const thisFile = new URL(import.meta.url).pathname;
    const tsBackendRoot = join(thisFile, "..", "..", "..");
    const discussionCacheSrc = readFileSync(
      join(tsBackendRoot, "src", "spawn", "discussion-cache.ts"),
      "utf-8"
    );
    // The module already uses "fresh" in an unrelated sense (isFresh() / TTL
    // freshness). What must NOT appear is a --fresh CLI flag or a `fresh`
    // parameter threaded through getBody().
    expect(discussionCacheSrc).not.toContain("--fresh");
    expect(discussionCacheSrc).not.toContain("fresh: boolean");
    expect(discussionCacheSrc).not.toContain("fresh = false");
  });
});
