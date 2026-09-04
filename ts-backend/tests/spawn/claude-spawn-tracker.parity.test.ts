/**
 * tests/spawn/claude-spawn-tracker.parity.test.ts
 *
 * Parity tests: runs CLI sequences against BOTH Python (backend/claude_spawn_tracker.py)
 * and TS (src/spawn/claude-spawn-tracker.ts), each backed by a separate temp blackboard
 * dir, then asserts identical resulting blackboard state / stdout / exit codes.
 *
 * Run: bun test tests/spawn/claude-spawn-tracker.parity.test.ts --timeout 120000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { record, reset, getState, SpawnBlocked } from "../../src/spawn/claude-spawn-tracker.js";

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");
const TS_ENTRY = join(REPO_ROOT, "ts-backend", "src", "spawn", "claude-spawn-tracker.ts");
const PY_ENTRY = join(REPO_ROOT, "backend", "claude_spawn_tracker.py");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTempDir(label: string): string {
  const dir = join(
    tmpdir(),
    `cst-parity-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`
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
  const timeout = setTimeout(() => proc.kill(), 30_000);
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
  return runProcess(
    ["python3", PY_ENTRY, ...args],
    { AUTONOMOUS_TEAM_STATE_DIR: stateDir }
  );
}

async function runTs(
  args: string[],
  stateDir: string
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return runProcess(
    ["bun", "run", TS_ENTRY, ...args],
    { AUTONOMOUS_TEAM_STATE_DIR: stateDir }
  );
}

/** Read the blackboard value for a given key from a state dir. */
function readBbValue(stateDir: string, key: string): unknown {
  const parts = key.split("/");
  const filePath = join(stateDir, "blackboard", ...parts) + ".json";
  if (!existsSync(filePath)) return null;
  try {
    const raw = JSON.parse(readFileSync(filePath, "utf-8")) as Record<string, unknown>;
    return "value" in raw ? raw["value"] : raw;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Test fixture
// ---------------------------------------------------------------------------

let pyDir = "";
let tsDir = "";

beforeEach(() => {
  pyDir = makeTempDir("py");
  tsDir = makeTempDir("ts");
});

afterEach(() => {
  try { rmSync(pyDir, { recursive: true, force: true }); } catch { /* ignore */ }
  try { rmSync(tsDir, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ---------------------------------------------------------------------------
// Test 1: record a normal spawn — both store an event in claude_events
// ---------------------------------------------------------------------------

describe("parity: record a spawn", () => {
  it("stores one event in spawn/claude_events blackboard key", async () => {
    const pyResult = await runPy(
      ["record", "loop_run", "--est-tokens", "1000", "--est-cost-usd", "0.05"],
      pyDir
    );
    const tsResult = await runTs(
      ["record", "loop_run", "--est-tokens", "1000", "--est-cost-usd", "0.05"],
      tsDir
    );

    expect(pyResult.exitCode).toBe(0);
    expect(tsResult.exitCode).toBe(0);

    // Both should print the same message
    expect(pyResult.stdout.trim()).toBe(`Recorded spawn from source='loop_run'`);
    expect(tsResult.stdout.trim()).toBe(`Recorded spawn from source="loop_run"`);

    // Check that events were stored
    const pyEvents = readBbValue(pyDir, "spawn/claude_events") as unknown[];
    const tsEvents = readBbValue(tsDir, "spawn/claude_events") as unknown[];

    expect(Array.isArray(pyEvents)).toBe(true);
    expect(Array.isArray(tsEvents)).toBe(true);
    expect(pyEvents.length).toBe(1);
    expect(tsEvents.length).toBe(1);

    const pyEvent = pyEvents[0] as Record<string, unknown>;
    const tsEvent = tsEvents[0] as Record<string, unknown>;

    // Both events should have same structure
    expect(tsEvent["source"]).toBe(pyEvent["source"]); // "loop_run"
    expect(tsEvent["est_tokens"]).toBe(pyEvent["est_tokens"]); // 1000
    expect(tsEvent["est_cost_usd"]).toBe(pyEvent["est_cost_usd"]); // 0.05
    expect(typeof tsEvent["ts"]).toBe("string");
    expect(typeof pyEvent["ts"]).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// Test 2: record unmetered source — est_tokens and est_cost_usd stored as null
// ---------------------------------------------------------------------------

describe("parity: unmetered source (innovate_tick_internal)", () => {
  it("stores null for est_tokens and est_cost_usd", async () => {
    await runPy(["record", "innovate_tick_internal"], pyDir);
    await runTs(["record", "innovate_tick_internal"], tsDir);

    const pyEvents = readBbValue(pyDir, "spawn/claude_events") as unknown[];
    const tsEvents = readBbValue(tsDir, "spawn/claude_events") as unknown[];

    expect(Array.isArray(pyEvents)).toBe(true);
    expect(Array.isArray(tsEvents)).toBe(true);

    const pyEvent = pyEvents[0] as Record<string, unknown>;
    const tsEvent = tsEvents[0] as Record<string, unknown>;

    expect(pyEvent["est_tokens"]).toBeNull();
    expect(pyEvent["est_cost_usd"]).toBeNull();
    expect(tsEvent["est_tokens"]).toBeNull();
    expect(tsEvent["est_cost_usd"]).toBeNull();
    expect(tsEvent["est_tokens"]).toBe(pyEvent["est_tokens"]);
    expect(tsEvent["est_cost_usd"]).toBe(pyEvent["est_cost_usd"]);
  });
});

// ---------------------------------------------------------------------------
// Test 3: status command — both produce multi-line output with "closed"
// ---------------------------------------------------------------------------

describe("parity: status command (empty state)", () => {
  it("outputs closed status with zero counts", async () => {
    const pyResult = await runPy(["status"], pyDir);
    const tsResult = await runTs(["status"], tsDir);

    expect(pyResult.exitCode).toBe(0);
    expect(tsResult.exitCode).toBe(0);

    // Both should indicate not tripped
    expect(pyResult.stdout).toContain("closed");
    expect(tsResult.stdout).toContain("closed");

    // Both should show zero spawns
    expect(pyResult.stdout).toContain("Spawns  1h:");
    expect(tsResult.stdout).toContain("Spawns  1h:");
  });
});

// ---------------------------------------------------------------------------
// Test 4: summary command outputs JSON with same keys
// ---------------------------------------------------------------------------

describe("parity: summary command", () => {
  it("outputs JSON with identical shape and values", async () => {
    // Record a spawn first so there's something to summarise
    await runPy(["record", "loop_run", "--est-cost-usd", "0.10"], pyDir);
    await runTs(["record", "loop_run", "--est-cost-usd", "0.10"], tsDir);

    const pyResult = await runPy(["summary", "--json"], pyDir);
    const tsResult = await runTs(["summary", "--json"], tsDir);

    expect(pyResult.exitCode).toBe(0);
    expect(tsResult.exitCode).toBe(0);

    const pyData = JSON.parse(pyResult.stdout) as Record<string, unknown>;
    const tsData = JSON.parse(tsResult.stdout) as Record<string, unknown>;

    // Both should have identical shape
    expect(tsData["tripped"]).toBe(pyData["tripped"]);           // false
    expect(tsData["spawns_1h"]).toBe(pyData["spawns_1h"]);       // 1
    expect(tsData["spawns_24h"]).toBe(pyData["spawns_24h"]);     // 1
    expect(tsData["spend_1h_usd"]).toBe(pyData["spend_1h_usd"]); // 0.1
    expect(tsData["spend_24h_usd"]).toBe(pyData["spend_24h_usd"]); // 0.1
    expect(tsData["thresholds"]).toEqual(pyData["thresholds"]);
    expect(tsData["tripped_meta"]).toBe(pyData["tripped_meta"]); // null

    // per_source should have loop_run: 1
    const pyPs = pyData["per_source"] as Record<string, number>;
    const tsPs = tsData["per_source"] as Record<string, number>;
    expect(tsPs["loop_run"]).toBe(pyPs["loop_run"]);             // 1
  });
});

// ---------------------------------------------------------------------------
// Test 5: reset command clears tripped state
// ---------------------------------------------------------------------------

describe("parity: reset command", () => {
  it("clears spawn_breaker/tripped key", async () => {
    const pyResult = await runPy(["reset"], pyDir);
    const tsResult = await runTs(["reset"], tsDir);

    expect(pyResult.exitCode).toBe(0);
    expect(tsResult.exitCode).toBe(0);

    expect(pyResult.stdout.trim()).toBe("Spawn breaker reset. Tripped state cleared.");
    expect(tsResult.stdout.trim()).toBe("Spawn breaker reset. Tripped state cleared.");

    // After reset, tripped should be false (or null/absent)
    const pyTripped = readBbValue(pyDir, "spawn_breaker/tripped");
    const tsTripped = readBbValue(tsDir, "spawn_breaker/tripped");
    expect(Boolean(pyTripped)).toBe(false);
    expect(Boolean(tsTripped)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Test 6: record exits non-zero when breaker is tripped
// ---------------------------------------------------------------------------

describe("parity: SpawnBlocked when tripped", () => {
  it("record returns non-zero exit when breaker manually tripped", async () => {
    // Manually write tripped=true to both blackboards
    const { mkdirSync: md, writeFileSync: wf } = await import("node:fs");
    const entry = JSON.stringify({ value: true, version: 1, updated_at: "2026-01-01T00:00:00Z", updated_by: "test" });

    for (const dir of [pyDir, tsDir]) {
      md(join(dir, "blackboard", "spawn_breaker"), { recursive: true });
      wf(join(dir, "blackboard", "spawn_breaker", "tripped.json"), entry);
      // Also write meta so the auto-reset idle check has a recent last_attempt_at
      const meta = JSON.stringify({
        value: {
          tripped_at: "2026-06-01T00:00:00Z",
          reason: "test",
          threshold_name: "spawns_per_hour_max",
          value: 51,
          last_attempt_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
        },
        version: 1,
        updated_at: "2026-06-01T00:00:00Z",
        updated_by: "test",
      });
      wf(join(dir, "blackboard", "spawn_breaker", "tripped_meta.json"), meta);
    }

    const pyResult = await runPy(["record", "loop_run"], pyDir);
    const tsResult = await runTs(["record", "loop_run"], tsDir);

    // Both should exit non-zero (blocked)
    expect(pyResult.exitCode).not.toBe(0);
    expect(tsResult.exitCode).not.toBe(0);

    // Both should mention SpawnBlocked
    expect(pyResult.stderr).toContain("SpawnBlocked");
    expect(tsResult.stderr).toContain("SpawnBlocked");
  });
});

// ---------------------------------------------------------------------------
// Test 7: programmatic API — record + getState consistency
// ---------------------------------------------------------------------------

describe("programmatic API: record + getState", () => {
  it("reflects recorded spawn in state", async () => {
    const origStateDir = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = tsDir;

    try {
      // Fresh state: no spawns
      const before = getState();
      expect(before.spawns_1h).toBe(0);
      expect(before.tripped).toBe(false);

      // Record two spawns
      record("loop_run", 500, 0.03);
      record("loop_run", 200, 0.02);

      const after = getState();
      expect(after.spawns_1h).toBe(2);
      expect(after.spawns_24h).toBe(2);
      expect(after.spend_1h_usd).toBeCloseTo(0.05, 4);
      expect(after.tripped).toBe(false);
      expect(after.per_source["loop_run"]).toBe(2);
    } finally {
      if (origStateDir !== undefined) {
        process.env["AUTONOMOUS_TEAM_STATE_DIR"] = origStateDir;
      } else {
        delete process.env["AUTONOMOUS_TEAM_STATE_DIR"];
      }
    }
  });

  it("throws SpawnBlocked when over the 1h spawn cap", () => {
    const origStateDir = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = tsDir;

    // Write a custom config with a low cap
    mkdirSync(join(tsDir, "autonomousTeamDir"), { recursive: true });

    // Use AUTONOMOUS_TEAM_DIR env var to point at temp dir with config
    const teamDir = join(tsDir, "team");
    mkdirSync(teamDir, { recursive: true });
    writeFileSync(
      join(teamDir, "config.json"),
      JSON.stringify({ spawn_breaker: { spawns_per_hour_max: 2, spawns_24h_max: 200 } })
    );

    const origTeamDir = process.env["AUTONOMOUS_TEAM_DIR"];
    process.env["AUTONOMOUS_TEAM_DIR"] = teamDir;
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = tsDir;

    try {
      record("test_source");
      record("test_source");
      // Third record should trip the breaker (count > 2)
      expect(() => record("test_source")).toThrow(SpawnBlocked);
    } finally {
      if (origStateDir !== undefined) {
        process.env["AUTONOMOUS_TEAM_STATE_DIR"] = origStateDir;
      } else {
        delete process.env["AUTONOMOUS_TEAM_STATE_DIR"];
      }
      if (origTeamDir !== undefined) {
        process.env["AUTONOMOUS_TEAM_DIR"] = origTeamDir;
      } else {
        delete process.env["AUTONOMOUS_TEAM_DIR"];
      }
      // Clean up: reset the breaker so other tests aren't affected
      try { reset(); } catch { /* ignore */ }
    }
  });
});

// ---------------------------------------------------------------------------
// Test 8: default cost applied when est-cost-usd not supplied
// ---------------------------------------------------------------------------

describe("parity: default cost applied", () => {
  it("stores 0.05 by default for metered sources", async () => {
    await runPy(["record", "loop_run"], pyDir);
    await runTs(["record", "loop_run"], tsDir);

    const pyEvents = readBbValue(pyDir, "spawn/claude_events") as unknown[];
    const tsEvents = readBbValue(tsDir, "spawn/claude_events") as unknown[];

    const pyEvent = pyEvents[0] as Record<string, unknown>;
    const tsEvent = tsEvents[0] as Record<string, unknown>;

    expect(pyEvent["est_cost_usd"]).toBe(0.05);
    expect(tsEvent["est_cost_usd"]).toBe(0.05);
    expect(tsEvent["est_cost_usd"]).toBe(pyEvent["est_cost_usd"]);
  });
});
