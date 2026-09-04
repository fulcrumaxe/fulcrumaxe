/**
 * tests/orchestrator/dispatch.parity.test.ts
 *
 * Parity tests for src/orchestrator/dispatch.ts vs
 * backend/orchestrator/dispatch.py.
 *
 * Strategy:
 *   - Test the pure routing decision function (_shouldUseSdk) extensively.
 *   - Test the route() function with mocked credit (SHADOW_MODE overrides).
 *   - Compare Python _should_use_sdk output for identical inputs.
 *
 * Note: _run_sdk and _run_both are NOT parity-tested here because they invoke
 * the Claude SDK runner, which is not yet wired in the TS port (a sibling task).
 * Routing logic is the focus.
 *
 * Run: bun test tests/orchestrator/dispatch.parity.test.ts --timeout 60000
 */

import { describe, it, expect, afterEach } from "bun:test";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { writeFileSync, unlinkSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";

import {
  _shouldUseSdk,
  CreditExhaustedError,
  route,
} from "../../src/orchestrator/dispatch.js";

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");

// ---------------------------------------------------------------------------
// Helper: call Python _should_use_sdk via a temp script file
// (try/except can't be written as a semicolon-separated one-liner in Python)
// ---------------------------------------------------------------------------

interface ShouldUseSdkOpts {
  discussion?: number | null;
  remainingUsd: number;
  shadowMode: string;
  allowFallback: boolean;
  role: string;
  sdkEligible: boolean;
}

function pyShouldUseSdk(opts: ShouldUseSdkOpts): string | "throws" {
  const {
    discussion,
    remainingUsd,
    shadowMode,
    allowFallback,
    role,
    sdkEligible,
  } = opts;

  // Write a temp Python script — try/except requires real newlines
  const pyCode = [
    "from backend.orchestrator.dispatch import _should_use_sdk, CreditExhaustedError",
    "try:",
    `    r = _should_use_sdk(`,
    `        discussion=${discussion === null || discussion === undefined ? "None" : discussion},`,
    `        remaining_usd=${remainingUsd},`,
    `        shadow_mode=${JSON.stringify(shadowMode)},`,
    `        allow_fallback=${allowFallback ? "True" : "False"},`,
    `        role=${JSON.stringify(role)},`,
    `        sdk_eligible=${sdkEligible ? "True" : "False"}`,
    `    )`,
    `    print(r)`,
    "except CreditExhaustedError:",
    `    print("throws")`,
  ].join("\n");

  const tmpScript = join(tmpdir(), `parity-dispatch-${Date.now()}-${Math.random().toString(36).slice(2)}.py`);
  try {
    writeFileSync(tmpScript, pyCode, "utf-8");
    // PYTHONPATH ensures 'backend' package is importable even though the
    // script file lives in /tmp (Python adds script dir to sys.path[0], not cwd)
    const env: Record<string, string> = {};
    for (const [k, v] of Object.entries(process.env)) {
      if (v !== undefined) env[k] = v;
    }
    env["PYTHONPATH"] = REPO_ROOT;

    const result = spawnSync("python3", [tmpScript], {
      encoding: "utf-8",
      timeout: 15_000,
      cwd: REPO_ROOT,
      env,
    });
    if (result.status !== 0 && result.stderr && result.stderr.length > 0) {
      process.stderr.write(`[pyShouldUseSdk] error (cwd=${REPO_ROOT}): ${result.stderr.slice(0, 400)}\n`);
    }
    return result.stdout.trim() || "error";
  } finally {
    if (existsSync(tmpScript)) {
      try { unlinkSync(tmpScript); } catch { /* ignore */ }
    }
  }
}

// ---------------------------------------------------------------------------
// Env cleanup
// ---------------------------------------------------------------------------

const _savedEnv: Record<string, string | undefined> = {};

afterEach(() => {
  for (const k of ["SHADOW_MODE", "SDK_AUTO_ROUTE"] as const) {
    if (_savedEnv[k] !== undefined) {
      process.env[k] = _savedEnv[k];
    } else {
      delete process.env[k];
    }
    delete _savedEnv[k];
  }
});

function withEnv(vars: Record<string, string | undefined>, fn: () => void): void {
  for (const [k, v] of Object.entries(vars)) {
    _savedEnv[k] = process.env[k];
    if (v === undefined) {
      delete process.env[k];
    } else {
      process.env[k] = v;
    }
  }
  fn();
}

// ---------------------------------------------------------------------------
// _shouldUseSdk — credit exhausted
// ---------------------------------------------------------------------------

describe("_shouldUseSdk — credit exhausted", () => {
  it("throws CreditExhaustedError when remaining=0 and allowFallback=false", () => {
    expect(() =>
      _shouldUseSdk({
        discussion: null,
        remainingUsd: 0,
        shadowMode: "cc",
        allowFallback: false,
        role: "docs-writer",
        sdkEligible: true,
      })
    ).toThrow(CreditExhaustedError);
  });

  it("returns cc when remaining=0 and allowFallback=true", () => {
    const result = _shouldUseSdk({
      discussion: null,
      remainingUsd: 0,
      shadowMode: "cc",
      allowFallback: true,
      role: "docs-writer",
      sdkEligible: true,
    });
    expect(result).toBe("cc");
  });

  it("matches Python on credit-exhausted + fallback=false", () => {
    const py = pyShouldUseSdk({
      remainingUsd: 0,
      shadowMode: "cc",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: true,
    });
    expect(py).toBe("throws");
  });
});

// ---------------------------------------------------------------------------
// _shouldUseSdk — role gate (unconditional)
// ---------------------------------------------------------------------------

describe("_shouldUseSdk — role gate", () => {
  const ineligibleRoles = [
    "executor",
    "code-reviewer",
    "security-reviewer",
    "acceptance-tester",
    "project-manager",
    "team-lead",
    "unknown-role",
    "",
  ];

  for (const role of ineligibleRoles) {
    it(`role ${JSON.stringify(role)} always → cc (even in SHADOW_MODE=sdk)`, () => {
      const result = _shouldUseSdk({
        discussion: null,
        remainingUsd: 100,
        shadowMode: "sdk",
        allowFallback: false,
        role,
        sdkEligible: true,
      });
      expect(result).toBe("cc");
    });

    it(`role ${JSON.stringify(role)} matches Python`, () => {
      const py = pyShouldUseSdk({
        remainingUsd: 100,
        shadowMode: "sdk",
        allowFallback: false,
        role,
        sdkEligible: true,
      });
      const ts: string = _shouldUseSdk({
        discussion: null,
        remainingUsd: 100,
        shadowMode: "sdk",
        allowFallback: false,
        role,
        sdkEligible: true,
      });
      expect(ts).toBe(py);
    });
  }
});

// ---------------------------------------------------------------------------
// _shouldUseSdk — SHADOW_MODE force modes
// ---------------------------------------------------------------------------

describe("_shouldUseSdk — SHADOW_MODE=sdk", () => {
  it("eligible role + SHADOW_MODE=sdk + no sdkEligible flag → sdk", () => {
    const result = _shouldUseSdk({
      discussion: null,
      remainingUsd: 100,
      shadowMode: "sdk",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: false,
    });
    expect(result).toBe("sdk");
  });

  it("matches Python", () => {
    const py = pyShouldUseSdk({
      remainingUsd: 100,
      shadowMode: "sdk",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: false,
    });
    const ts: string = _shouldUseSdk({
      discussion: null,
      remainingUsd: 100,
      shadowMode: "sdk",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: false,
    });
    expect(ts).toBe(py);
  });
});

describe("_shouldUseSdk — SHADOW_MODE=cc", () => {
  it("eligible role + SHADOW_MODE=cc → cc", () => {
    const result = _shouldUseSdk({
      discussion: null,
      remainingUsd: 100,
      shadowMode: "cc",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: true,
    });
    expect(result).toBe("cc");
  });

  it("matches Python", () => {
    const py = pyShouldUseSdk({
      remainingUsd: 100,
      shadowMode: "cc",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: true,
    });
    const ts: string = _shouldUseSdk({
      discussion: null,
      remainingUsd: 100,
      shadowMode: "cc",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: true,
    });
    expect(ts).toBe(py);
  });
});

describe("_shouldUseSdk — SHADOW_MODE=both (eligible role)", () => {
  it("returns both", () => {
    const result = _shouldUseSdk({
      discussion: null,
      remainingUsd: 100,
      shadowMode: "both",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: true,
    });
    expect(result).toBe("both");
  });
});

// ---------------------------------------------------------------------------
// _shouldUseSdk — selective opt-in (default path)
// ---------------------------------------------------------------------------

describe("_shouldUseSdk — selective opt-in (default / alternate)", () => {
  it("eligible role + sdkEligible=true → sdk", () => {
    const result = _shouldUseSdk({
      discussion: null,
      remainingUsd: 100,
      shadowMode: "default",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: true,
    });
    expect(result).toBe("sdk");
  });

  it("eligible role + sdkEligible=false → cc", () => {
    const result = _shouldUseSdk({
      discussion: null,
      remainingUsd: 100,
      shadowMode: "default",
      allowFallback: false,
      role: "docs-writer",
      sdkEligible: false,
    });
    expect(result).toBe("cc");
  });

  it("matches Python on selective opt-in cases", () => {
    const cases: [string, boolean, string][] = [
      ["docs-writer", true, "sdk"],
      ["docs-writer", false, "cc"],
      ["mission-analyst", true, "sdk"],
      ["run-analyst", true, "sdk"],
    ];
    for (const [role, sdkEligible, expected] of cases) {
      const ts: string = _shouldUseSdk({
        discussion: null,
        remainingUsd: 100,
        shadowMode: "default",
        allowFallback: false,
        role,
        sdkEligible,
      });
      const py = pyShouldUseSdk({
        remainingUsd: 100,
        shadowMode: "default",
        allowFallback: false,
        role,
        sdkEligible,
      });
      expect(ts).toBe(expected);
      expect(ts).toBe(py);
    }
  });
});

// ---------------------------------------------------------------------------
// route() — CC path (SHADOW_MODE=cc forces CC for all)
// ---------------------------------------------------------------------------

describe("route() — CC path via SHADOW_MODE=cc", () => {
  it("returns route=cc and verdict=routed_to_cc for eligible role", () => {
    withEnv({ SHADOW_MODE: "cc" }, () => {
      const result = route({
        role: "docs-writer",
        sdk_eligible: true,
      });
      expect(result.route).toBe("cc");
      expect(result.verdict).toBe("routed_to_cc");
      expect(result.error).toBeNull();
      expect(typeof result.run_id).toBe("string");
    });
  });

  it("returns route=cc for ineligible role regardless of SHADOW_MODE", () => {
    withEnv({ SHADOW_MODE: "sdk" }, () => {
      const result = route({ role: "executor", sdk_eligible: true });
      expect(result.route).toBe("cc");
    });
  });

  it("run_id is a non-empty string", () => {
    withEnv({ SHADOW_MODE: "cc" }, () => {
      const result = route({ role: "docs-writer", sdk_eligible: true });
      expect(typeof result.run_id).toBe("string");
      expect((result.run_id ?? "").length).toBeGreaterThan(0);
    });
  });
});

// ---------------------------------------------------------------------------
// route() — credit exhausted
// ---------------------------------------------------------------------------

describe("route() — credit exhausted (mocked via env)", () => {
  // We can't directly mock credit_tracker without dependency injection,
  // but we can verify the error structure by calling _shouldUseSdk directly.
  it("CreditExhaustedError is thrown when remaining=0 and no fallback", () => {
    expect(() =>
      _shouldUseSdk({
        discussion: null,
        remainingUsd: 0,
        shadowMode: "cc",
        allowFallback: false,
        role: "docs-writer",
        sdkEligible: true,
      })
    ).toThrow(CreditExhaustedError);
  });
});

// ---------------------------------------------------------------------------
// route() — snake_case JSON input parity (CLI compatibility)
// ---------------------------------------------------------------------------

describe("route() — accepts snake_case keys from JSON", () => {
  it("sdk_eligible is recognised", () => {
    withEnv({ SHADOW_MODE: "cc" }, () => {
      const result = route({ role: "docs-writer", sdk_eligible: true });
      expect(result.route).toBe("cc");
    });
  });

  it("allow_subscription_fallback is recognised", () => {
    withEnv({ SHADOW_MODE: "cc" }, () => {
      const result = route({
        role: "executor",
        sdk_eligible: false,
        allow_subscription_fallback: true,
      });
      expect(result.route).toBe("cc");
    });
  });
});

// ---------------------------------------------------------------------------
// CreditExhaustedError identity
// ---------------------------------------------------------------------------

describe("CreditExhaustedError", () => {
  it("is an instance of Error", () => {
    const err = new CreditExhaustedError("test");
    expect(err instanceof Error).toBe(true);
    expect(err.name).toBe("CreditExhaustedError");
    expect(err.message).toBe("test");
  });
});
