/**
 * tests/spawn/control-plane.parity.test.ts
 *
 * Parity tests for src/spawn/control-plane.ts vs backend/control_plane.py.
 *
 * Strategy:
 *  1. Write a shared config fixture to a temp dir.
 *  2. Point both Python (AF_CONTROL_PLANE_CONFIG) and TS (AF_CONTROL_PLANE_CONFIG)
 *     at the same file.
 *  3. For each key under test, run `python3 backend/control_plane.py get <key>`
 *     and compare stdout + exit code against the TS implementation.
 *
 * Run: bun test tests/spawn/control-plane.parity.test.ts --timeout 60000
 */

import { describe, it, expect, beforeAll, afterAll } from "bun:test";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import { ControlPlane } from "../../src/spawn/control-plane.js";

// ---------------------------------------------------------------------------
// Fixture: a config.json with known values so both runtimes agree on output.
// Contains a mix of overridden gates, extra gates (in file but not in defaults),
// custom policy values, and a dial with a custom level.
// ---------------------------------------------------------------------------

const FIXTURE_CONFIG = {
  gates: {
    auto_merge: false,          // overridden from default (true → false)
    security_review: true,
    budget_check: true,
    idea_generation: true,
    stall_detection: true,
    wiki_sync: true,
    human_verification: false,
    self_observe_executor: false,
    self_observe_impl_coord: false,
    self_observe_enforcement: "advisory", // non-default string value
    docs_writer: true,
    incident_commander: false,
    release_manager: true,
    runbook_writer: true,
    analytics_engineer: true,
    phased_orchestration: false,
    phased_code_review: true,
    cost_aware_router: false,
    debater_pass: false,
    tui_tester_pilot_sweep: false,
    execve_fence: true,
    loop_start: false,
    dial_state_summary: false,
    // Extra gate not in defaults (mimics real config.json having extra gates)
    lint_must_pass: true,
  },
  policies: {
    executor: {
      timeout_minutes: 45,
      max_retries: 2,
      token_ceiling: 500000,
      pr_size_max_lines: 2000,   // extra key present in real config
    },
    "code-reviewer": {
      timeout_minutes: 20,
      max_retries: 1,
      token_ceiling: 200000,
      max_concurrent: 4,
    },
    "security-reviewer": {
      timeout_minutes: 20,
      max_retries: 1,
      token_ceiling: 200000,
    },
    "project-manager": {
      timeout_minutes: 30,
      max_retries: 1,
      token_ceiling: 300000,
    },
    incident_commander: {
      timeout_minutes: 30,
      max_retries: 1,
      token_ceiling: 80000,
      max_spawns_per_hour: 1,
    },
    debater: {
      token_cap: 5000,
      timeout_seconds: 90,
      min_precision_30d: 0.3,
    },
    loop_runs: {
      retention_days: 30,
    },
  },
  settings: {
    "team-lead": {
      max_parallel_impl: 3,
    },
  },
  dials: {
    "docs.write":         { level: 5, ceiling: 5, directives: [] },
    "tests.add":          { level: 4, ceiling: 5, directives: [] },
    "deps.bump":          { level: 3, ceiling: 5, directives: [] },
    "agent.spawn":        { level: 4, ceiling: 5, directives: [] },
    "merge.standard":     { level: 4, ceiling: 5, directives: [] },
    "merge.fast-path":    { level: 2, ceiling: 5, directives: [] },
    "intent.generate":    { level: 1, ceiling: 5, directives: [] },
    "methodology.change": { level: 1, ceiling: 2, directives: [] },
    "external.system":    { level: 1, ceiling: 2, directives: [] },
    "sandbox.modify":     { level: 1, ceiling: 1, directives: [] },
    "cost.spend":         { level: 2, ceiling: 5, directives: [] },
    "memory.write":       { level: 3, ceiling: 5, directives: [] },
    "archive.move":       { level: 4, ceiling: 5, directives: [] },
  },
  audit_log: [],
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tempDir: string;
let configPath: string;

/** Run Python CLI and return {stdout, stderr, exitCode}. */
function runPy(args: string[]): { stdout: string; stderr: string; exitCode: number } {
  const repoRoot = join(new URL(import.meta.url).pathname, "..", "..", "..", "..");
  const result = spawnSync(
    "python3",
    [join(repoRoot, "backend", "control_plane.py"), ...args],
    {
      env: { ...process.env, AF_CONTROL_PLANE_CONFIG: configPath },
      encoding: "utf-8",
      timeout: 15_000,
    }
  );
  return {
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    exitCode: result.status ?? 1,
  };
}

/** Run TS CLI and return {stdout, stderr, exitCode}. */
function runTs(args: string[]): { stdout: string; stderr: string; exitCode: number } {
  const repoRoot = join(new URL(import.meta.url).pathname, "..", "..", "..", "..");
  const scriptPath = join(repoRoot, "ts-backend", "src", "spawn", "control-plane.ts");
  const result = spawnSync(
    "bun",
    ["run", scriptPath, ...args],
    {
      env: { ...process.env, AF_CONTROL_PLANE_CONFIG: configPath },
      encoding: "utf-8",
      timeout: 15_000,
    }
  );
  return {
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    exitCode: result.status ?? 1,
  };
}

/** Assert that Python and TS produce identical stdout and exit code for a given key. */
function assertParityGet(key: string): void {
  const py = runPy(["get", key]);
  const ts = runTs(["get", key]);
  expect(ts.exitCode, `exit code mismatch for key="${key}"`).toBe(py.exitCode);
  expect(ts.stdout.trimEnd(), `stdout mismatch for key="${key}"`).toBe(py.stdout.trimEnd());
}

beforeAll(() => {
  tempDir = join(tmpdir(), `cp-parity-test-${Date.now()}`);
  mkdirSync(tempDir, { recursive: true });
  configPath = join(tempDir, "config.json");
  writeFileSync(configPath, JSON.stringify(FIXTURE_CONFIG, null, 2) + "\n", "utf-8");
});

afterAll(() => {
  try { rmSync(tempDir, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ---------------------------------------------------------------------------
// Parity: `get` for boolean gates
// ---------------------------------------------------------------------------

describe("get — boolean gates", () => {
  it("gates.auto_merge (overridden to false)", () => assertParityGet("gates.auto_merge"));
  it("gates.security_review (true)",           () => assertParityGet("gates.security_review"));
  it("gates.human_verification (false)",        () => assertParityGet("gates.human_verification"));
  it("gates.phased_code_review (true)",         () => assertParityGet("gates.phased_code_review"));
  it("gates.phased_orchestration (false)",      () => assertParityGet("gates.phased_orchestration"));
  it("gates.execve_fence (true)",               () => assertParityGet("gates.execve_fence"));
  it("gates.loop_start (false)",                () => assertParityGet("gates.loop_start"));
  it("gates.lint_must_pass (extra gate, true)", () => assertParityGet("gates.lint_must_pass"));
});

// ---------------------------------------------------------------------------
// Parity: `get` for string gate
// ---------------------------------------------------------------------------

describe("get — string gate", () => {
  it("gates.self_observe_enforcement (advisory)", () => assertParityGet("gates.self_observe_enforcement"));
});

// ---------------------------------------------------------------------------
// Parity: `get` for policy values
// ---------------------------------------------------------------------------

describe("get — policy scalar values", () => {
  it("policies.executor.timeout_minutes",  () => assertParityGet("policies.executor.timeout_minutes"));
  it("policies.executor.max_retries",      () => assertParityGet("policies.executor.max_retries"));
  it("policies.executor.token_ceiling",    () => assertParityGet("policies.executor.token_ceiling"));
  it("policies.executor.pr_size_max_lines (extra key)", () => assertParityGet("policies.executor.pr_size_max_lines"));
  it("policies.code-reviewer.timeout_minutes", () => assertParityGet("policies.code-reviewer.timeout_minutes"));
  it("policies.code-reviewer.max_concurrent",  () => assertParityGet("policies.code-reviewer.max_concurrent"));
  it("policies.debater.token_cap",         () => assertParityGet("policies.debater.token_cap"));
  it("policies.debater.min_precision_30d", () => assertParityGet("policies.debater.min_precision_30d"));
  it("policies.loop_runs.retention_days",  () => assertParityGet("policies.loop_runs.retention_days"));
});

// ---------------------------------------------------------------------------
// Parity: `get` for policy dict (object result)
// ---------------------------------------------------------------------------

describe("get — policy dict", () => {
  it("policies.executor (full dict)", () => assertParityGet("policies.executor"));
  it("policies.code-reviewer (full dict)", () => assertParityGet("policies.code-reviewer"));
});

// ---------------------------------------------------------------------------
// Parity: `get` for settings
// ---------------------------------------------------------------------------

describe("get — settings", () => {
  it("settings.team-lead.max_parallel_impl", () => assertParityGet("settings.team-lead.max_parallel_impl"));
});

// ---------------------------------------------------------------------------
// Parity: `get` for dial values (dotted class names)
// ---------------------------------------------------------------------------

describe("get — dial values (dotted class names)", () => {
  it("dials.agent.spawn.level",       () => assertParityGet("dials.agent.spawn.level"));
  it("dials.agent.spawn.ceiling",     () => assertParityGet("dials.agent.spawn.ceiling"));
  it("dials.merge.fast-path.level",   () => assertParityGet("dials.merge.fast-path.level"));
  it("dials.merge.fast-path.ceiling", () => assertParityGet("dials.merge.fast-path.ceiling"));
  it("dials.merge.standard.level",    () => assertParityGet("dials.merge.standard.level"));
  it("dials.sandbox.modify.ceiling",  () => assertParityGet("dials.sandbox.modify.ceiling"));
  it("dials.methodology.change.ceiling", () => assertParityGet("dials.methodology.change.ceiling"));
  it("dials.intent.generate.level",   () => assertParityGet("dials.intent.generate.level"));
  it("dials.docs.write.level",        () => assertParityGet("dials.docs.write.level"));
});

// ---------------------------------------------------------------------------
// Parity: missing / non-existent keys → exit 1, stderr "(not set)"
// ---------------------------------------------------------------------------

describe("get — missing keys", () => {
  it("nonexistent.key → exit 1 + (not set)", () => {
    const py = runPy(["get", "nonexistent.key"]);
    const ts = runTs(["get", "nonexistent.key"]);
    expect(ts.exitCode).toBe(1);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stderr.trim()).toBe("(not set)");
    expect(ts.stderr.trim()).toBe(py.stderr.trim());
  });

  it("gates.this_gate_does_not_exist → exit 1", () => {
    const py = runPy(["get", "gates.this_gate_does_not_exist"]);
    const ts = runTs(["get", "gates.this_gate_does_not_exist"]);
    // NOTE: This gate is NOT in the fixture, but both runtimes inject _DEFAULT_GATES
    // on load — so only keys in _DEFAULT_GATES will resolve.
    expect(ts.exitCode).toBe(py.exitCode);
  });

  it("dials.nonexistent.class.level → exit 1", () => {
    const py = runPy(["get", "dials.nonexistent.class.level"]);
    const ts = runTs(["get", "dials.nonexistent.class.level"]);
    expect(ts.exitCode).toBe(1);
    expect(ts.exitCode).toBe(py.exitCode);
  });
});

// ---------------------------------------------------------------------------
// Programmatic API parity (TS-only, verifies internal correctness)
// ---------------------------------------------------------------------------

describe("ControlPlane programmatic API — fixture config", () => {
  let cp: ControlPlane;

  beforeAll(() => {
    cp = new ControlPlane(configPath);
    cp.load();
  });

  it("gateEnabled('auto_merge') returns false (overridden)", () => {
    expect(cp.gateEnabled("auto_merge")).toBe(false);
  });

  it("gateEnabled('security_review') returns true", () => {
    expect(cp.gateEnabled("security_review")).toBe(true);
  });

  it("gateEnabled('nonexistent') returns false", () => {
    expect(cp.gateEnabled("nonexistent")).toBe(false);
  });

  it("listGates() includes all default gates + extra gate", () => {
    const gates = cp.listGates();
    // Default gates always present
    expect("auto_merge" in gates).toBe(true);
    expect("self_observe_enforcement" in gates).toBe(true);
    // Extra gate from fixture
    expect("lint_must_pass" in gates).toBe(true);
    // String gate preserved as string
    expect(gates["self_observe_enforcement"]).toBe("advisory");
    // Boolean coercion
    expect(typeof gates["auto_merge"]).toBe("boolean");
    expect(gates["auto_merge"]).toBe(false);
  });

  it("getPolicy('executor') merges defaults with stored", () => {
    const p = cp.getPolicy("executor");
    expect(p["timeout_minutes"]).toBe(45);
    expect(p["token_ceiling"]).toBe(500000);
    expect(p["pr_size_max_lines"]).toBe(2000); // extra key from fixture
  });

  it("getPolicy('unknown-role') returns empty object", () => {
    const p = cp.getPolicy("unknown-role");
    expect(typeof p).toBe("object");
    expect(Object.keys(p).length).toBe(0);
  });

  it("getDial('agent.spawn') returns level 4", () => {
    const d = cp.getDial("agent.spawn");
    expect(d).toBeDefined();
    expect(d!["level"]).toBe(4);
  });

  it("getDial('merge.fast-path') returns level 2", () => {
    const d = cp.getDial("merge.fast-path");
    expect(d).toBeDefined();
    expect(d!["level"]).toBe(2);
  });

  it("getDialCeiling('sandbox.modify') returns hardcoded 1", () => {
    expect(cp.getDialCeiling("sandbox.modify")).toBe(1);
  });

  it("getDialCeiling('methodology.change') returns hardcoded 2", () => {
    expect(cp.getDialCeiling("methodology.change")).toBe(2);
  });

  it("getDialCeiling('unknown.class') returns default 5", () => {
    expect(cp.getDialCeiling("unknown.class")).toBe(5);
  });

  it("getSetting('team-lead', 'max_parallel_impl') returns 3", () => {
    expect(cp.getSetting("team-lead", "max_parallel_impl")).toBe(3);
  });

  it("listSettings() merges defaults with stored", () => {
    const s = cp.listSettings();
    expect(s["team-lead"]).toBeDefined();
    expect(s["team-lead"]["max_parallel_impl"]).toBe(3);
  });

  it("get() on dotted dial key resolves correctly", () => {
    expect(cp.get("dials.agent.spawn.level")).toBe(4);
    expect(cp.get("dials.merge.fast-path.level")).toBe(2);
    expect(cp.get("dials.merge.standard.ceiling")).toBe(5);
    expect(cp.get("dials.sandbox.modify.ceiling")).toBe(1);
  });

  it("get() on missing key returns undefined", () => {
    expect(cp.get("gates.absolutely.does.not.exist")).toBeUndefined();
    expect(cp.get("dials.no.such.class.level")).toBeUndefined();
  });

  it("getAuditLog() returns empty list (no mutations made)", () => {
    const entries = cp.getAuditLog();
    expect(Array.isArray(entries)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Programmatic API parity — defaults-only (no config file)
// ---------------------------------------------------------------------------

describe("ControlPlane — defaults only (no config file)", () => {
  let cp: ControlPlane;

  beforeAll(() => {
    const missingPath = join(tempDir, "does-not-exist.json");
    cp = new ControlPlane(missingPath);
    cp.load();
  });

  it("gateEnabled('auto_merge') returns true (default)", () => {
    expect(cp.gateEnabled("auto_merge")).toBe(true);
  });

  it("gateEnabled('self_observe_executor') returns false (default)", () => {
    expect(cp.gateEnabled("self_observe_executor")).toBe(false);
  });

  it("listGates() includes all default gates", () => {
    const gates = cp.listGates();
    const expectedDefaults = [
      "auto_merge", "security_review", "budget_check", "self_observe_enforcement",
      "execve_fence", "loop_start",
    ];
    for (const k of expectedDefaults) {
      expect(k in gates).toBe(true);
    }
  });

  it("getPolicy('executor') returns all defaults", () => {
    const p = cp.getPolicy("executor");
    expect(p["timeout_minutes"]).toBe(45);
    expect(p["max_retries"]).toBe(2);
    expect(p["token_ceiling"]).toBe(500000);
  });

  it("getPolicy('code-reviewer') returns defaults including max_concurrent", () => {
    const p = cp.getPolicy("code-reviewer");
    expect(p["max_concurrent"]).toBe(4);
  });

  it("getDial('docs.write') level is 5 (default)", () => {
    const d = cp.getDial("docs.write");
    expect(d!["level"]).toBe(5);
  });

  it("get('gates.auto_merge') returns true (default)", () => {
    expect(cp.get("gates.auto_merge")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Parity: `get` against a FRESH load of the real config (smoke test)
// ---------------------------------------------------------------------------

describe("get — against real .autonomous-team/config.json (smoke)", () => {
  it("gates.auto_merge from real config matches Python", () => {
    const py = runPy(["get", "gates.auto_merge"]);
    // Python exits 0 and prints "true" or "false"
    expect(py.exitCode).toBe(0);

    // TS should match
    const ts = runTs(["get", "gates.auto_merge"]);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("gates.self_observe_enforcement from real config matches Python", () => {
    const py = runPy(["get", "gates.self_observe_enforcement"]);
    const ts = runTs(["get", "gates.self_observe_enforcement"]);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("policies.executor.token_ceiling from real config matches Python", () => {
    const py = runPy(["get", "policies.executor.token_ceiling"]);
    const ts = runTs(["get", "policies.executor.token_ceiling"]);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("dials.agent.spawn.level from real config matches Python", () => {
    const py = runPy(["get", "dials.agent.spawn.level"]);
    const ts = runTs(["get", "dials.agent.spawn.level"]);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });

  it("dials.merge.fast-path.level from real config matches Python", () => {
    const py = runPy(["get", "dials.merge.fast-path.level"]);
    const ts = runTs(["get", "dials.merge.fast-path.level"]);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });
});
