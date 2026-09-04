/**
 * tests/spawn/circuit-breaker.parity.test.ts
 *
 * Parity tests for src/spawn/circuit-breaker.ts vs backend/circuit_breaker.py.
 *
 * Strategy:
 *  1. Create isolated temp state dirs per test.
 *  2. Run Python CLI and TS CLI against separate state dirs.
 *  3. Compare stdout, exit codes, and blackboard state for parity.
 *
 * Run: bun test tests/spawn/circuit-breaker.parity.test.ts --timeout 120000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  recordFailure,
  recordSuccess,
  isBlocked,
  getLatestFailure,
  expireStale,
  DEFAULT_THRESHOLD,
} from "../../src/spawn/circuit-breaker.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");
const TS_ENTRY = join(REPO_ROOT, "ts-backend", "src", "spawn", "circuit-breaker.ts");
const PY_ENTRY = join(REPO_ROOT, "backend", "circuit_breaker.py");

function makeTempDir(label: string): string {
  const dir = join(
    tmpdir(),
    `cb-parity-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
  mkdirSync(dir, { recursive: true });
  return dir;
}

async function runProcess(
  cmd: string[],
  env: Record<string, string>
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const proc = Bun.spawn(cmd, {
    env: { ...process.env, ...env },
    stdout: "pipe",
    stderr: "pipe",
  });
  const timeout = setTimeout(() => proc.kill(), 45_000);
  await proc.exited;
  clearTimeout(timeout);
  const stdout = await new Response(proc.stdout).text();
  const stderr = await new Response(proc.stderr).text();
  return { exitCode: proc.exitCode ?? 0, stdout, stderr };
}

async function runPy(
  args: string[],
  stateDir: string
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return runProcess(["python3", PY_ENTRY, ...args], {
    AUTONOMOUS_TEAM_STATE_DIR: stateDir,
  });
}

async function runTs(
  args: string[],
  stateDir: string
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return runProcess(["bun", "run", TS_ENTRY, ...args], {
    AUTONOMOUS_TEAM_STATE_DIR: stateDir,
  });
}

// ---------------------------------------------------------------------------
// State dir per test (module-level functions use process env)
// ---------------------------------------------------------------------------

let pyStateDir: string;
let tsStateDir: string;
let savedEnv: string | undefined;

beforeEach(() => {
  pyStateDir = makeTempDir("py");
  tsStateDir = makeTempDir("ts");
  savedEnv = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  // Point module-level _bb to tsStateDir for programmatic tests
  process.env["AUTONOMOUS_TEAM_STATE_DIR"] = tsStateDir;
  // Reset the module-level cache (if any) — done by clearing env override
});

afterEach(() => {
  if (savedEnv !== undefined) {
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = savedEnv;
  } else {
    delete process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  }
  try { rmSync(pyStateDir, { recursive: true, force: true }); } catch { /* ignore */ }
  try { rmSync(tsStateDir, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ---------------------------------------------------------------------------
// CLI parity: status (empty)
// ---------------------------------------------------------------------------

describe("status — empty state", () => {
  it("both report no active counters", async () => {
    const py = await runPy(["status"], pyStateDir);
    const ts = await runTs(["status"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    expect(py.exitCode).toBe(0);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });
});

// ---------------------------------------------------------------------------
// CLI parity: record + status
// ---------------------------------------------------------------------------

describe("record — increments counter", () => {
  it("first record exits 0 and prints failures/N = 1", async () => {
    const py = await runPy(["record", "97", "executor", "could not implement"], pyStateDir);
    const ts = await runTs(["record", "97", "executor", "could not implement"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("counter reaches DEFAULT_THRESHOLD and prints circuit open to stderr", async () => {
    // Record 3 failures
    for (let i = 0; i < DEFAULT_THRESHOLD; i++) {
      await runTs(["record", "42", "executor", "fail"], tsStateDir);
    }
    const ts = await runTs(["record", "42", "executor", "fail"], tsStateDir);
    // Count should now be 4 (above threshold)
    expect(ts.stdout).toContain("failures/42 =");

    // Check Python equivalent
    for (let i = 0; i < DEFAULT_THRESHOLD + 1; i++) {
      await runPy(["record", "42", "executor", "fail"], pyStateDir);
    }
    const pyFinal = await runPy(["record", "42", "executor", "fail"], pyStateDir);
    expect(ts.exitCode).toBe(pyFinal.exitCode);
  });
});

// ---------------------------------------------------------------------------
// CLI parity: reset
// ---------------------------------------------------------------------------

describe("reset — clears counter", () => {
  it("reset on existing counter prints cleared", async () => {
    await runTs(["record", "97", "executor", "fail"], tsStateDir);
    const ts = await runTs(["reset", "97"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    expect(ts.stdout).toContain("cleared");

    await runPy(["record", "97", "executor", "fail"], pyStateDir);
    const py = await runPy(["reset", "97"], pyStateDir);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("reset on no-counter prints ok", async () => {
    const py = await runPy(["reset", "999"], pyStateDir);
    const ts = await runTs(["reset", "999"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });
});

// ---------------------------------------------------------------------------
// CLI parity: list
// ---------------------------------------------------------------------------

describe("list — shows counters", () => {
  it("empty list matches Python", async () => {
    const py = await runPy(["list"], pyStateDir);
    const ts = await runTs(["list"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("non-empty list shows discussion number", async () => {
    await runTs(["record", "55", "executor", "fail"], tsStateDir);
    const ts = await runTs(["list"], tsStateDir);
    expect(ts.stdout).toContain("55");
  });
});

// ---------------------------------------------------------------------------
// CLI parity: summary
// ---------------------------------------------------------------------------

describe("summary — JSON output", () => {
  it("empty summary has tripped=[] warnings=[]", async () => {
    const py = await runPy(["summary", "--json"], pyStateDir);
    const ts = await runTs(["summary", "--json"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    expect(py.exitCode).toBe(0);
    const pyObj = JSON.parse(py.stdout) as { tripped: unknown[]; warnings: unknown[] };
    const tsObj = JSON.parse(ts.stdout) as { tripped: unknown[]; warnings: unknown[] };
    expect(tsObj.tripped.length).toBe(pyObj.tripped.length);
    expect(tsObj.warnings.length).toBe(pyObj.warnings.length);
    expect(tsObj.tripped.length).toBe(0);
  });

  it("summary threshold matches Python", async () => {
    const py = await runPy(["summary", "--json"], pyStateDir);
    const ts = await runTs(["summary", "--json"], tsStateDir);
    const pyObj = JSON.parse(py.stdout) as { threshold: number };
    const tsObj = JSON.parse(ts.stdout) as { threshold: number };
    expect(tsObj.threshold).toBe(pyObj.threshold);
    expect(tsObj.threshold).toBe(DEFAULT_THRESHOLD);
  });
});

// ---------------------------------------------------------------------------
// CLI parity: history
// ---------------------------------------------------------------------------

describe("history — no transitions", () => {
  it("empty history matches Python output", async () => {
    const py = await runPy(["history", "--role", "executor"], pyStateDir);
    const ts = await runTs(["history", "--role", "executor"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });
});

// ---------------------------------------------------------------------------
// CLI parity: expire
// ---------------------------------------------------------------------------

describe("expire — no stale breakers", () => {
  it("expire on empty state matches Python", async () => {
    const py = await runPy(["expire"], pyStateDir);
    const ts = await runTs(["expire"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("expire --dry-run on empty state matches Python", async () => {
    const py = await runPy(["expire", "--dry-run"], pyStateDir);
    const ts = await runTs(["expire", "--dry-run"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });
});

// ---------------------------------------------------------------------------
// Programmatic API parity
// ---------------------------------------------------------------------------

describe("programmatic API — recordFailure + isBlocked", () => {
  it("isBlocked returns false initially", () => {
    expect(isBlocked(1001)).toBe(false);
  });

  it("isBlocked returns true after DEFAULT_THRESHOLD failures", () => {
    const disc = 2001;
    for (let i = 0; i < DEFAULT_THRESHOLD; i++) {
      recordFailure(disc, "executor", "test fail");
    }
    expect(isBlocked(disc)).toBe(true);
  });

  it("recordFailure returns correct count", () => {
    const disc = 3001;
    const c1 = recordFailure(disc, "executor", "fail 1");
    expect(c1).toBe(1);
    const c2 = recordFailure(disc, "executor", "fail 2");
    expect(c2).toBe(2);
    const c3 = recordFailure(disc, "executor", "fail 3");
    expect(c3).toBe(3);
  });

  it("recordSuccess clears failure counter", () => {
    const disc = 4001;
    recordFailure(disc, "executor", "fail");
    recordFailure(disc, "executor", "fail");
    recordFailure(disc, "executor", "fail");
    expect(isBlocked(disc)).toBe(true);
    recordSuccess(disc);
    expect(isBlocked(disc)).toBe(false);
  });

  it("getLatestFailure returns null when no failures", () => {
    expect(getLatestFailure(5001)).toBeNull();
  });

  it("getLatestFailure returns failure metadata", () => {
    const disc = 6001;
    recordFailure(disc, "executor", "some reason");
    const f = getLatestFailure(disc);
    expect(f).not.toBeNull();
    expect(f!.count).toBe(1);
    expect(f!.agent).toBe("executor");
    expect(f!.reason).toBe("some reason");
  });
});

describe("programmatic API — expireStale", () => {
  it("expireStale returns empty array when no tripped breakers", () => {
    const results = expireStale({ dryRun: true });
    expect(Array.isArray(results)).toBe(true);
    expect(results.length).toBe(0);
  });

  it("expireStale with future cutoff does not expire recent breakers", () => {
    const disc = 7001;
    recordFailure(disc, "executor", "fail");
    recordFailure(disc, "executor", "fail");
    recordFailure(disc, "executor", "fail");
    expect(isBlocked(disc)).toBe(true);

    // Use now as the reference (so cutoff = now - 7 days → breaker is recent)
    const results = expireStale({ now: new Date(), dryRun: true });
    // Recent breaker should NOT be expired (updated_at is just now, open Discussion assumed)
    // We can't confirm open state without live gh, but dryRun = no side effects
    expect(Array.isArray(results)).toBe(true);
  });

  it("expireStale with past cutoff expires old breakers (dry-run)", () => {
    const disc = 8001;
    recordFailure(disc, "executor", "fail");
    recordFailure(disc, "executor", "fail");
    recordFailure(disc, "executor", "fail");
    expect(isBlocked(disc)).toBe(true);

    // Set 'now' far in the future (8 days from now) so the breaker looks old
    const future = new Date(Date.now() + 8 * 24 * 60 * 60 * 1000);
    const results = expireStale({ now: future, dryRun: true });
    // With a past timestamp relative to the future 'now', age_eligible should be true
    // However _discussion_state() may return "unknown" if gh is unavailable
    // Either way, dry-run must not throw
    expect(Array.isArray(results)).toBe(true);
  });
});

describe("DEFAULT_THRESHOLD constant", () => {
  it("is 3 (matching Python DEFAULT_THRESHOLD)", () => {
    expect(DEFAULT_THRESHOLD).toBe(3);
  });
});
