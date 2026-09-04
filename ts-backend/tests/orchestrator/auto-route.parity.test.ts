/**
 * tests/orchestrator/auto-route.parity.test.ts
 *
 * Parity tests for src/orchestrator/auto-route.ts vs
 * backend/orchestrator/auto_route.py.
 *
 * Strategy:
 *   - Test shouldAutoRoute against Python should_auto_route using env var manipulation.
 *   - Compare CLI outputs from Python vs TS for identical env states.
 *
 * Run: bun test tests/orchestrator/auto-route.parity.test.ts --timeout 60000
 */

import { describe, it, expect, afterEach } from "bun:test";
import { spawnSync } from "node:child_process";
import { join } from "node:path";

import { shouldAutoRoute } from "../../src/orchestrator/auto-route.js";

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");

// ---------------------------------------------------------------------------
// Helper: call Python should_auto_route via one-liner
// ---------------------------------------------------------------------------

function pyShouldAutoRoute(role: string, sdkAutoRoute: "0" | "1" | ""): boolean {
  const env: Record<string, string> = { ...process.env as Record<string, string> };
  if (sdkAutoRoute === "") {
    delete env["SDK_AUTO_ROUTE"];
  } else {
    env["SDK_AUTO_ROUTE"] = sdkAutoRoute;
  }

  const result = spawnSync(
    "python3",
    [
      "-c",
      `from backend.orchestrator.auto_route import should_auto_route; ` +
      `print(should_auto_route(${JSON.stringify(role)}))`,
    ],
    { encoding: "utf-8", timeout: 15_000, cwd: REPO_ROOT, env }
  );
  return result.stdout.trim() === "True";
}

// ---------------------------------------------------------------------------
// State restoration helper
// ---------------------------------------------------------------------------

let _savedAutoRoute: string | undefined;

afterEach(() => {
  if (_savedAutoRoute !== undefined) {
    process.env["SDK_AUTO_ROUTE"] = _savedAutoRoute;
  } else {
    delete process.env["SDK_AUTO_ROUTE"];
  }
});

function withAutoRoute(value: "0" | "1" | "", fn: () => void): void {
  _savedAutoRoute = process.env["SDK_AUTO_ROUTE"];
  if (value === "") {
    delete process.env["SDK_AUTO_ROUTE"];
  } else {
    process.env["SDK_AUTO_ROUTE"] = value;
  }
  fn();
}

// ---------------------------------------------------------------------------
// Tests (mirror Python doctest examples from auto_route.py)
// ---------------------------------------------------------------------------

describe("shouldAutoRoute — SDK_AUTO_ROUTE=0 (disabled)", () => {
  it("docs-writer → false", () => {
    withAutoRoute("0", () => {
      expect(shouldAutoRoute("docs-writer")).toBe(false);
      expect(pyShouldAutoRoute("docs-writer", "0")).toBe(false);
    });
  });

  it("executor → false", () => {
    withAutoRoute("0", () => {
      expect(shouldAutoRoute("executor")).toBe(false);
      expect(pyShouldAutoRoute("executor", "0")).toBe(false);
    });
  });
});

describe("shouldAutoRoute — SDK_AUTO_ROUTE=1 (enabled)", () => {
  it("docs-writer → true", () => {
    withAutoRoute("1", () => {
      expect(shouldAutoRoute("docs-writer")).toBe(true);
      expect(pyShouldAutoRoute("docs-writer", "1")).toBe(true);
    });
  });

  it("executor → false (role gate)", () => {
    withAutoRoute("1", () => {
      expect(shouldAutoRoute("executor")).toBe(false);
      expect(pyShouldAutoRoute("executor", "1")).toBe(false);
    });
  });

  it("code-reviewer → false (role gate)", () => {
    withAutoRoute("1", () => {
      expect(shouldAutoRoute("code-reviewer")).toBe(false);
      expect(pyShouldAutoRoute("code-reviewer", "1")).toBe(false);
    });
  });

  it("mission-analyst → true", () => {
    withAutoRoute("1", () => {
      expect(shouldAutoRoute("mission-analyst")).toBe(true);
      expect(pyShouldAutoRoute("mission-analyst", "1")).toBe(true);
    });
  });

  it("quality-sweep → true", () => {
    withAutoRoute("1", () => {
      expect(shouldAutoRoute("quality-sweep")).toBe(true);
      expect(pyShouldAutoRoute("quality-sweep", "1")).toBe(true);
    });
  });

  it("feedback-scanner → true", () => {
    withAutoRoute("1", () => {
      expect(shouldAutoRoute("feedback-scanner")).toBe(true);
      expect(pyShouldAutoRoute("feedback-scanner", "1")).toBe(true);
    });
  });

  it("run-analyst → true", () => {
    withAutoRoute("1", () => {
      expect(shouldAutoRoute("run-analyst")).toBe(true);
      expect(pyShouldAutoRoute("run-analyst", "1")).toBe(true);
    });
  });
});

describe("shouldAutoRoute — SDK_AUTO_ROUTE unset", () => {
  it("docs-writer → false (no env var)", () => {
    withAutoRoute("", () => {
      expect(shouldAutoRoute("docs-writer")).toBe(false);
      expect(pyShouldAutoRoute("docs-writer", "")).toBe(false);
    });
  });

  it("unknown-role → false", () => {
    withAutoRoute("", () => {
      expect(shouldAutoRoute("unknown-role")).toBe(false);
    });
  });
});

describe("shouldAutoRoute — all eligible roles parity with Python", () => {
  const eligibleRoles = [
    "docs-writer",
    "run-analyst",
    "quality-sweep",
    "feedback-scanner",
    "mission-analyst",
  ];

  for (const role of eligibleRoles) {
    it(`${role}: TS matches Python when SDK_AUTO_ROUTE=1`, () => {
      withAutoRoute("1", () => {
        const ts = shouldAutoRoute(role);
        const py = pyShouldAutoRoute(role, "1");
        expect(ts).toBe(py);
        expect(ts).toBe(true);
      });
    });

    it(`${role}: TS matches Python when SDK_AUTO_ROUTE=0`, () => {
      withAutoRoute("0", () => {
        const ts = shouldAutoRoute(role);
        const py = pyShouldAutoRoute(role, "0");
        expect(ts).toBe(py);
        expect(ts).toBe(false);
      });
    });
  }
});
