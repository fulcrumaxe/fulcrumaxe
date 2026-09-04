/**
 * tests/orchestrator/offload-policy.parity.test.ts
 *
 * Parity tests for src/orchestrator/offload-policy.ts vs
 * backend/orchestrator/offload_policy.py.
 *
 * Strategy:
 *   - Run Python module's doctest examples and compare to TS function output.
 *   - Run edge cases against both implementations via subprocess.
 *
 * Run: bun test tests/orchestrator/offload-policy.parity.test.ts --timeout 60000
 */

import { describe, it, expect } from "bun:test";
import { spawnSync } from "node:child_process";
import { join } from "node:path";

import { isOffloadEligible, SDK_ELIGIBLE_ROLES } from "../../src/orchestrator/offload-policy.js";

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");
const PY_ENTRY = join(REPO_ROOT, "backend", "orchestrator", "offload_policy.py");

// ---------------------------------------------------------------------------
// Helper: call Python isOffloadEligible via one-liner
// ---------------------------------------------------------------------------

function pyIsOffloadEligible(role: string, sdkEligible: boolean): boolean {
  const result = spawnSync(
    "python3",
    [
      "-c",
      `from backend.orchestrator.offload_policy import is_offload_eligible; ` +
      `print(is_offload_eligible(${JSON.stringify(role)}, ${sdkEligible ? "True" : "False"}))`,
    ],
    { encoding: "utf-8", timeout: 15_000, cwd: REPO_ROOT }
  );
  return result.stdout.trim() === "True";
}

function pyGetEligibleRoles(): string[] {
  const result = spawnSync(
    "python3",
    [
      "-c",
      `from backend.orchestrator.offload_policy import SDK_ELIGIBLE_ROLES; ` +
      `print(sorted(SDK_ELIGIBLE_ROLES))`,
    ],
    { encoding: "utf-8", timeout: 15_000, cwd: REPO_ROOT }
  );
  // Parse Python repr: ['docs-writer', 'feedback-scanner', ...]
  const raw = result.stdout.trim();
  const match = raw.match(/^\[(.*)\]$/s);
  if (!match) return [];
  return match[1]!.split(",").map((s) => s.trim().replace(/^'|'$/g, ""));
}

// ---------------------------------------------------------------------------
// SDK_ELIGIBLE_ROLES parity
// ---------------------------------------------------------------------------

describe("SDK_ELIGIBLE_ROLES", () => {
  it("TS set matches Python frozenset contents", () => {
    const pyRoles = pyGetEligibleRoles();
    const tsRoles = [...SDK_ELIGIBLE_ROLES].sort();
    expect(tsRoles).toEqual(pyRoles.sort());
  });

  it("contains the five canonical roles", () => {
    expect(SDK_ELIGIBLE_ROLES.has("docs-writer")).toBe(true);
    expect(SDK_ELIGIBLE_ROLES.has("run-analyst")).toBe(true);
    expect(SDK_ELIGIBLE_ROLES.has("quality-sweep")).toBe(true);
    expect(SDK_ELIGIBLE_ROLES.has("feedback-scanner")).toBe(true);
    expect(SDK_ELIGIBLE_ROLES.has("mission-analyst")).toBe(true);
  });

  it("does NOT contain executor or reviewer roles", () => {
    expect(SDK_ELIGIBLE_ROLES.has("executor")).toBe(false);
    expect(SDK_ELIGIBLE_ROLES.has("code-reviewer")).toBe(false);
    expect(SDK_ELIGIBLE_ROLES.has("security-reviewer")).toBe(false);
    expect(SDK_ELIGIBLE_ROLES.has("acceptance-tester")).toBe(false);
    expect(SDK_ELIGIBLE_ROLES.has("project-manager")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// isOffloadEligible parity (doctest examples from Python)
// ---------------------------------------------------------------------------

describe("isOffloadEligible — Python doctest examples", () => {
  const cases: [string, boolean, boolean][] = [
    ["docs-writer", true, true],
    ["docs-writer", false, false],
    ["executor", true, false],
    ["code-reviewer", true, false],
    ["unknown-role", true, false],
    ["mission-analyst", true, true],
    ["feedback-scanner", true, true],
    ["run-analyst", false, false],
    ["quality-sweep", true, true],
  ];

  for (const [role, sdkEligible, expected] of cases) {
    it(`isOffloadEligible(${JSON.stringify(role)}, ${sdkEligible}) → ${expected}`, () => {
      const tsResult = isOffloadEligible(role, sdkEligible);
      const pyResult = pyIsOffloadEligible(role, sdkEligible);
      expect(tsResult).toBe(expected);
      expect(tsResult).toBe(pyResult);
    });
  }
});

// ---------------------------------------------------------------------------
// isOffloadEligible — programmatic edge cases
// ---------------------------------------------------------------------------

describe("isOffloadEligible — edge cases", () => {
  it("empty string role with sdkEligible=true → false", () => {
    expect(isOffloadEligible("", true)).toBe(false);
    expect(pyIsOffloadEligible("", true)).toBe(false);
  });

  it("all eligible roles with sdkEligible=false → false", () => {
    for (const role of SDK_ELIGIBLE_ROLES) {
      expect(isOffloadEligible(role, false)).toBe(false);
    }
  });

  it("all eligible roles with sdkEligible=true → true", () => {
    for (const role of SDK_ELIGIBLE_ROLES) {
      expect(isOffloadEligible(role, true)).toBe(true);
    }
  });

  it("team-lead with sdkEligible=true → false", () => {
    expect(isOffloadEligible("team-lead", true)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Doctest runner: verify Python module's own doctests pass
// ---------------------------------------------------------------------------

describe("Python doctest self-check", () => {
  it("offload_policy.py doctests pass", () => {
    const result = spawnSync(
      "python3",
      ["-m", "doctest", PY_ENTRY, "-v"],
      { encoding: "utf-8", timeout: 30_000, cwd: REPO_ROOT }
    );
    // doctest exits 0 on success
    expect(result.status).toBe(0);
  });
});
