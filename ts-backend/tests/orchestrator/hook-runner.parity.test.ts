/**
 * tests/orchestrator/hook-runner.parity.test.ts
 *
 * Parity tests for src/orchestrator/hook-runner.ts vs
 * backend/orchestrator/hook_runner.py.
 *
 * Strategy:
 *   - Test HookRunner behaviour with absent scripts (best-effort / non-fatal contract).
 *   - Verify that postAgent and preSpawn calls do not throw on missing scripts.
 *   - Verify arg construction via a test fixture script that echoes its args.
 *
 * Run: bun test tests/orchestrator/hook-runner.parity.test.ts --timeout 60000
 */

import { describe, it, expect } from "bun:test";
import { mkdirSync, rmSync, writeFileSync, chmodSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { HookRunner, type RunResult } from "../../src/orchestrator/hook-runner.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTempRepo(label: string): string {
  const dir = join(
    tmpdir(),
    `hook-runner-test-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
  mkdirSync(join(dir, "scripts"), { recursive: true });
  return dir;
}

function makeEchoScript(dir: string, name: string): string {
  const path = join(dir, "scripts", name);
  writeFileSync(path, "#!/bin/bash\necho \"$@\"\n", "utf-8");
  chmodSync(path, 0o755);
  return path;
}

function makeFailScript(dir: string, name: string): string {
  const path = join(dir, "scripts", name);
  writeFileSync(path, "#!/bin/bash\nexit 1\n", "utf-8");
  chmodSync(path, 0o755);
  return path;
}

function sampleResult(overrides: Partial<RunResult> = {}): RunResult {
  return {
    agentId: "executor-42-1700000000",
    verdict: "done",
    role: "executor",
    discussion: 42,
    pr: 99,
    inputTokens: 1000,
    outputTokens: 500,
    error: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// preSpawn — non-fatal contract
// ---------------------------------------------------------------------------

describe("HookRunner.preSpawn — non-fatal on missing script", () => {
  it("returns false when pre-spawn-check.sh does not exist", () => {
    const root = makeTempRepo("pre-missing");
    try {
      const hr = new HookRunner(root);
      const result = hr.preSpawn({ role: "executor", discussion: 42 });
      expect(result).toBe(false);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("returns true when pre-spawn-check.sh exits 0", () => {
    const root = makeTempRepo("pre-ok");
    try {
      makeEchoScript(root, "pre-spawn-check.sh");
      const hr = new HookRunner(root);
      const result = hr.preSpawn({ role: "docs-writer", discussion: 1 });
      expect(result).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("returns false when pre-spawn-check.sh exits non-zero", () => {
    const root = makeTempRepo("pre-fail");
    try {
      makeFailScript(root, "pre-spawn-check.sh");
      const hr = new HookRunner(root);
      const result = hr.preSpawn({ role: "executor" });
      expect(result).toBe(false);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("handles null discussion gracefully", () => {
    const root = makeTempRepo("pre-null-disc");
    try {
      makeEchoScript(root, "pre-spawn-check.sh");
      const hr = new HookRunner(root);
      // Should not throw
      const result = hr.preSpawn({ role: "docs-writer", discussion: null });
      expect(typeof result).toBe("boolean");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// postAgent — non-fatal contract
// ---------------------------------------------------------------------------

describe("HookRunner.postAgent — non-fatal on missing script", () => {
  it("does not throw when post-agent-hook.sh does not exist", () => {
    const root = makeTempRepo("post-missing");
    try {
      const hr = new HookRunner(root);
      // Must not throw
      expect(() => hr.postAgent(sampleResult())).not.toThrow();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("does not throw when post-agent-hook.sh exits non-zero", () => {
    const root = makeTempRepo("post-fail");
    try {
      makeFailScript(root, "post-agent-hook.sh");
      const hr = new HookRunner(root);
      expect(() => hr.postAgent(sampleResult())).not.toThrow();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// postAgent — arg construction (verify args passed to script)
// ---------------------------------------------------------------------------

describe("HookRunner.postAgent — arg construction", () => {
  it("passes --event-id, --verdict, --role to hook script", () => {
    const root = makeTempRepo("post-args");
    // Create a script that writes args to a temp file for inspection
    const outFile = join(root, "hook-args.txt");
    const scriptPath = join(root, "scripts", "post-agent-hook.sh");
    writeFileSync(
      scriptPath,
      `#!/bin/bash\necho "$@" > "${outFile}"\n`,
      "utf-8"
    );
    chmodSync(scriptPath, 0o755);

    try {
      const hr = new HookRunner(root);
      hr.postAgent(sampleResult({ agentId: "myagent-123", verdict: "done", role: "executor" }));

      const written = readFileSync(outFile, "utf-8").trim();
      expect(written).toContain("--event-id");
      expect(written).toContain("myagent-123");
      expect(written).toContain("--verdict");
      expect(written).toContain("done");
      expect(written).toContain("--role");
      expect(written).toContain("executor");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("passes --discussion and --pr when present", () => {
    const root = makeTempRepo("post-disc-pr");
    const outFile = join(root, "hook-args.txt");
    const scriptPath = join(root, "scripts", "post-agent-hook.sh");
    writeFileSync(
      scriptPath,
      `#!/bin/bash\necho "$@" > "${outFile}"\n`,
      "utf-8"
    );
    chmodSync(scriptPath, 0o755);

    try {
      const hr = new HookRunner(root);
      hr.postAgent(sampleResult({ discussion: 42, pr: 99 }));

      const written = readFileSync(outFile, "utf-8").trim();
      expect(written).toContain("--discussion");
      expect(written).toContain("42");
      expect(written).toContain("--pr");
      expect(written).toContain("99");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("omits --discussion and --pr when null", () => {
    const root = makeTempRepo("post-no-disc-pr");
    const outFile = join(root, "hook-args.txt");
    const scriptPath = join(root, "scripts", "post-agent-hook.sh");
    writeFileSync(
      scriptPath,
      `#!/bin/bash\necho "$@" > "${outFile}"\n`,
      "utf-8"
    );
    chmodSync(scriptPath, 0o755);

    try {
      const hr = new HookRunner(root);
      hr.postAgent(sampleResult({ discussion: null, pr: null }));

      const written = readFileSync(outFile, "utf-8").trim();
      expect(written).not.toContain("--discussion");
      expect(written).not.toContain("--pr");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// Constructor — repo root resolution
// ---------------------------------------------------------------------------

describe("HookRunner — constructor", () => {
  it("accepts an explicit repo root path", () => {
    const root = makeTempRepo("ctor-explicit");
    try {
      mkdirSync(join(root, "scripts"), { recursive: true });
      const hr = new HookRunner(root);
      // preSpawn on missing script → false (non-fatal), not a throw
      expect(() => hr.preSpawn({ role: "executor" })).not.toThrow();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("resolves repo root from module path when no arg given", () => {
    // Just check it doesn't throw
    const hr = new HookRunner();
    expect(() => hr.preSpawn({ role: "executor" })).not.toThrow();
  });
});
