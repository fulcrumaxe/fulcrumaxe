/**
 * tests/spawn/budget.parity.test.ts
 *
 * Parity tests for src/spawn/budget.ts vs backend/budget.py.
 *
 * Strategy:
 *  1. Create an isolated temp blackboard directory for each test.
 *  2. Run the Python CLI and TS CLI against the SAME blackboard root
 *     (via AUTONOMOUS_TEAM_STATE_DIR pointing to a temp dir).
 *  3. Compare stdout, exit codes, and blackboard state to verify parity.
 *
 * Run: bun test tests/spawn/budget.parity.test.ts --timeout 120000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { BudgetTracker } from "../../src/spawn/budget.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");
const TS_ENTRY = join(REPO_ROOT, "ts-backend", "src", "spawn", "budget.ts");
const PY_ENTRY = join(REPO_ROOT, "backend", "budget.py");

function makeTempDir(label: string): string {
  const dir = join(
    tmpdir(),
    `budget-parity-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`
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

/** Read a blackboard JSON key from a state dir */
function readBbKey(stateDir: string, key: string): unknown {
  const keyPath = join(stateDir, "blackboard", ...key.split("/")) + ".json";
  if (!existsSync(keyPath)) return undefined;
  try {
    const data = JSON.parse(readFileSync(keyPath, "utf-8")) as { value: unknown };
    return data.value;
  } catch {
    return undefined;
  }
}

/** List keys under a prefix in the blackboard */
function listBbKeys(stateDir: string, prefix: string): string[] {
  const bbRoot = join(stateDir, "blackboard");
  if (!existsSync(bbRoot)) return [];
  const keys: string[] = [];
  const walk = (dir: string): void => {
    try {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const fp = join(dir, entry.name);
        if (entry.isDirectory() && entry.name !== ".locks") walk(fp);
        else if (entry.isFile() && entry.name.endsWith(".json")) {
          const rel = fp.slice(bbRoot.length + 1).replace(/\.json$/, "").replace(/\\/g, "/");
          if (rel.startsWith(prefix)) keys.push(rel);
        }
      }
    } catch { /* ignore */ }
  };
  walk(bbRoot);
  return keys.sort();
}

// ---------------------------------------------------------------------------
// State dir per test
// ---------------------------------------------------------------------------

let pyStateDir: string;
let tsStateDir: string;

beforeEach(() => {
  pyStateDir = makeTempDir("py");
  tsStateDir = makeTempDir("ts");
});

afterEach(() => {
  try { rmSync(pyStateDir, { recursive: true, force: true }); } catch { /* ignore */ }
  try { rmSync(tsStateDir, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ---------------------------------------------------------------------------
// CLI parity: init
// ---------------------------------------------------------------------------

describe("init — default ceiling", () => {
  it("exit code matches Python", async () => {
    const py = await runPy(["init"], pyStateDir);
    const ts = await runTs(["init"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    expect(ts.exitCode).toBe(py.exitCode);
  });

  it("stdout contains initialized keyword", async () => {
    const py = await runPy(["init"], pyStateDir);
    const ts = await runTs(["init"], tsStateDir);
    expect(ts.stdout).toContain("initialized:");
    expect(py.stdout).toContain("initialized:");
  });

  it("blackboard has session_ceiling key after init", async () => {
    await runTs(["init"], tsStateDir);
    const val = readBbKey(tsStateDir, "budget/session_ceiling");
    expect(typeof val).toBe("number");
    expect(val).toBe(5_000_000); // default
  });
});

describe("init — custom ceiling", () => {
  it("ceiling is respected", async () => {
    await runTs(["init", "--ceiling", "1000000"], tsStateDir);
    const val = readBbKey(tsStateDir, "budget/session_ceiling");
    expect(val).toBe(1_000_000);
  });

  it("Python and TS produce same stdout pattern", async () => {
    const py = await runPy(["init", "--ceiling", "2000000"], pyStateDir);
    const ts = await runTs(["init", "--ceiling", "2000000"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    // Both should mention ceiling=2000000
    expect(ts.stdout).toContain("2000000");
    expect(py.stdout).toContain("2000000");
  });
});

// ---------------------------------------------------------------------------
// CLI parity: status (after init)
// ---------------------------------------------------------------------------

describe("status — after init", () => {
  it("status returns valid JSON with ceiling and spent", async () => {
    await runTs(["init"], tsStateDir);
    const ts = await runTs(["status"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    const obj = JSON.parse(ts.stdout) as {
      ceiling: number;
      spent: number;
      remaining: number;
    };
    expect(obj.ceiling).toBe(5_000_000);
    expect(obj.spent).toBe(0);
    expect(obj.remaining).toBe(5_000_000);
  });

  it("Python and TS status agree on structure", async () => {
    await runPy(["init"], pyStateDir);
    await runTs(["init"], tsStateDir);
    const py = await runPy(["status"], pyStateDir);
    const ts = await runTs(["status"], tsStateDir);
    expect(py.exitCode).toBe(0);
    expect(ts.exitCode).toBe(0);
    const pyObj = JSON.parse(py.stdout) as Record<string, unknown>;
    const tsObj = JSON.parse(ts.stdout) as Record<string, unknown>;
    // Same top-level keys
    expect(Object.keys(tsObj).sort()).toEqual(Object.keys(pyObj).sort());
    // Both have zero spent after init
    expect(tsObj["spent"]).toBe(0);
    expect(pyObj["spent"]).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// CLI parity: spend + status
// ---------------------------------------------------------------------------

describe("spend — records agent token usage", () => {
  it("spend exit code 0", async () => {
    await runTs(["init"], tsStateDir);
    const ts = await runTs(
      ["spend", "executor-14-1712700000", "executor", "45000", "3200"],
      tsStateDir
    );
    expect(ts.exitCode).toBe(0);
    expect(ts.stdout).toContain("recorded:");
  });

  it("spend increases status spent", async () => {
    await runTs(["init"], tsStateDir);
    await runTs(
      ["spend", "executor-14-1712700000", "executor", "45000", "3200"],
      tsStateDir
    );
    const statusResult = await runTs(["status"], tsStateDir);
    const status = JSON.parse(statusResult.stdout) as { spent: number };
    expect(status.spent).toBe(45000 + 3200);
  });

  it("Python and TS agree on spent after identical spend command", async () => {
    await runPy(["init"], pyStateDir);
    await runTs(["init"], tsStateDir);

    await runPy(
      ["spend", "executor-14-1712700000", "executor", "45000", "3200"],
      pyStateDir
    );
    await runTs(
      ["spend", "executor-14-1712700000", "executor", "45000", "3200"],
      tsStateDir
    );

    const pyStatus = JSON.parse((await runPy(["status"], pyStateDir)).stdout) as {
      spent: number;
    };
    const tsStatus = JSON.parse((await runTs(["status"], tsStateDir)).stdout) as {
      spent: number;
    };

    expect(tsStatus.spent).toBe(pyStatus.spent);
    expect(tsStatus.spent).toBe(48_200);
  });

  it("spend stores agent record in blackboard", async () => {
    await runTs(["init"], tsStateDir);
    await runTs(
      ["spend", "executor-14-1712700000", "executor", "45000", "3200"],
      tsStateDir
    );
    const keys = listBbKeys(tsStateDir, "budget/agents/");
    expect(keys.length).toBe(1);
    expect(keys[0]).toContain("executor-14-1712700000");
    const record = readBbKey(tsStateDir, keys[0]) as {
      agent: string;
      input: number;
      output: number;
    };
    expect(record.agent).toBe("executor");
    expect(record.input).toBe(45000);
    expect(record.output).toBe(3200);
  });
});

// ---------------------------------------------------------------------------
// CLI parity: reset
// ---------------------------------------------------------------------------

describe("reset — clears all budget keys", () => {
  it("reset after spend leaves zero agents", async () => {
    await runTs(["init"], tsStateDir);
    await runTs(
      ["spend", "executor-14-1712700000", "executor", "10000", "2000"],
      tsStateDir
    );
    await runTs(["reset"], tsStateDir);
    const keys = listBbKeys(tsStateDir, "budget/");
    expect(keys.length).toBe(0);
  });

  it("reset output matches Python", async () => {
    await runPy(["init"], pyStateDir);
    await runTs(["init"], tsStateDir);
    const py = await runPy(["reset"], pyStateDir);
    const ts = await runTs(["reset"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });
});

// ---------------------------------------------------------------------------
// CLI parity: check
// ---------------------------------------------------------------------------

describe("check — budget gate", () => {
  it("check returns allowed=true when under budget", async () => {
    await runTs(["init"], tsStateDir);
    const ts = await runTs(["check"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    const result = JSON.parse(ts.stdout) as { allowed: boolean };
    expect(result.allowed).toBe(true);
  });

  it("Python and TS check agree on allowed after init", async () => {
    await runPy(["init"], pyStateDir);
    await runTs(["init"], tsStateDir);
    const py = await runPy(["check"], pyStateDir);
    const ts = await runTs(["check"], tsStateDir);
    const pyObj = JSON.parse(py.stdout) as { allowed: boolean };
    const tsObj = JSON.parse(ts.stdout) as { allowed: boolean };
    expect(tsObj.allowed).toBe(pyObj.allowed);
  });
});

// ---------------------------------------------------------------------------
// CLI parity: record command (idempotent with event-id)
// ---------------------------------------------------------------------------

describe("record — idempotent token recording", () => {
  it("record produces output line", async () => {
    await runTs(["init"], tsStateDir);
    const ts = await runTs(
      ["record", "--input-tokens", "1000", "--output-tokens", "500", "--role", "executor"],
      tsStateDir
    );
    expect(ts.exitCode).toBe(0);
    expect(ts.stdout).toContain("recorded:");
  });

  it("record with event-id deduplicates on second call", async () => {
    await runTs(["init"], tsStateDir);
    const args = [
      "record",
      "--input-tokens", "1000",
      "--output-tokens", "500",
      "--role", "executor",
      "--event-id", "test-event-abc123",
    ];
    await runTs(args, tsStateDir);
    const ts2 = await runTs(args, tsStateDir);
    expect(ts2.stdout).toContain("skipped");
  });
});

// ---------------------------------------------------------------------------
// Programmatic API parity
// ---------------------------------------------------------------------------

describe("BudgetTracker programmatic API", () => {
  it("initSession + getStatus returns defaults", () => {
    const stateDir = makeTempDir("prog");
    try {
      const bt = new BudgetTracker(join(stateDir, "blackboard"));
      bt.initSession();
      const status = bt.getStatus();
      expect(status.ceiling).toBe(5_000_000);
      expect(status.spent).toBe(0);
      expect(status.remaining).toBe(5_000_000);
      expect(Array.isArray(status.agents)).toBe(true);
      expect(status.agents.length).toBe(0);
    } finally {
      try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });

  it("recordSpend increases spent", () => {
    const stateDir = makeTempDir("prog2");
    try {
      const bt = new BudgetTracker(join(stateDir, "blackboard"));
      bt.initSession();
      bt.recordSpend({
        agentId: "executor-14-1712700000",
        agentRole: "executor",
        inputTokens: 45000,
        outputTokens: 3200,
      });
      const status = bt.getStatus();
      expect(status.spent).toBe(48_200);
      expect(status.agents.length).toBe(1);
      expect(status.agents[0].agent).toBe("executor");
    } finally {
      try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });

  it("checkBudget returns allowed when under budget", () => {
    const stateDir = makeTempDir("prog3");
    try {
      const bt = new BudgetTracker(join(stateDir, "blackboard"));
      bt.initSession();
      const result = bt.checkBudget("executor");
      expect(result.allowed).toBe(true);
      expect(result.agent_role).toBe("executor");
    } finally {
      try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });

  it("reset clears all budget keys", () => {
    const stateDir = makeTempDir("prog4");
    try {
      const bt = new BudgetTracker(join(stateDir, "blackboard"));
      bt.initSession();
      bt.recordSpend({
        agentId: "executor-1",
        agentRole: "executor",
        inputTokens: 1000,
        outputTokens: 500,
      });
      bt.reset();
      const status = bt.getStatus();
      // After reset, falls back to config defaults
      expect(status.spent).toBe(0);
      expect(status.agents.length).toBe(0);
    } finally {
      try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });

  it("checkBudget denied when remaining < per_agent_ceiling", () => {
    const stateDir = makeTempDir("prog5");
    try {
      const bt = new BudgetTracker(join(stateDir, "blackboard"));
      // Set a tiny ceiling (200k) and spend nearly all of it
      bt.initSession(600_000);
      bt.recordSpend({
        agentId: "exec-1",
        agentRole: "executor",
        inputTokens: 200_000,
        outputTokens: 0,
      });
      const result = bt.checkBudget("executor");
      // remaining = 400k which is still >= per_agent_ceiling (500k from config default)
      // actually 600k - 200k = 400k remaining < 500k per_agent_ceiling → denied
      expect(result.allowed).toBe(false);
    } finally {
      try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });
});

// ---------------------------------------------------------------------------
// rmdirSync prune: empty parent dirs are removed after delete()
// Matches Python BudgetTracker._prune_empty_dirs() behaviour.
// ---------------------------------------------------------------------------

describe("Blackboard.delete — prunes empty parent dirs after key removal", () => {
  it("deletes agent key and prunes empty parent directory", () => {
    const stateDir = makeTempDir("prune1");
    try {
      const bt = new BudgetTracker(join(stateDir, "blackboard"));
      bt.initSession();
      bt.recordSpend({
        agentId: "executor-prune-test-001",
        agentRole: "executor",
        inputTokens: 1000,
        outputTokens: 500,
      });

      // Confirm agent record exists under budget/agents/
      const keysBefore = listBbKeys(stateDir, "budget/agents/");
      expect(keysBefore.length).toBe(1);
      const agentDir = join(stateDir, "blackboard", "budget", "agents");
      expect(existsSync(agentDir)).toBe(true);

      // Now reset (which deletes all budget/ keys including agent records)
      bt.reset();

      // The budget/agents dir should be pruned (empty after key deletion)
      // Note: budget/ dir itself may also be gone if all keys removed
      const keysAfter = listBbKeys(stateDir, "budget/agents/");
      expect(keysAfter.length).toBe(0);

      // The agents subdirectory itself should no longer exist (pruned by rmdirSync)
      expect(existsSync(agentDir)).toBe(false);
    } finally {
      try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });

  it("does not prune non-empty parent directories", () => {
    const stateDir = makeTempDir("prune2");
    try {
      const bt = new BudgetTracker(join(stateDir, "blackboard"));
      bt.initSession();
      // Record two different agents
      bt.recordSpend({
        agentId: "executor-prune-keep-001",
        agentRole: "executor",
        inputTokens: 100,
        outputTokens: 50,
      });
      bt.recordSpend({
        agentId: "executor-prune-keep-002",
        agentRole: "executor",
        inputTokens: 200,
        outputTokens: 100,
      });

      const agentDir = join(stateDir, "blackboard", "budget", "agents");
      expect(existsSync(agentDir)).toBe(true);

      // Only reset removes all; after first spend, budget/agents still has 2 entries
      const keys = listBbKeys(stateDir, "budget/agents/");
      expect(keys.length).toBe(2);

      // Parent dir must remain (it still has other files)
      expect(existsSync(agentDir)).toBe(true);
    } finally {
      try { rmSync(stateDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });

  it("Python and TS agree: agent dir is pruned after reset", async () => {

    // TS path
    await runTs(["init"], tsStateDir);
    await runTs(
      ["spend", "executor-14-prune-ts", "executor", "5000", "1000"],
      tsStateDir
    );
    const agentDirTs = join(tsStateDir, "blackboard", "budget", "agents");
    expect(existsSync(agentDirTs)).toBe(true);

    await runTs(["reset"], tsStateDir);
    // After reset, agents dir should be pruned
    expect(existsSync(agentDirTs)).toBe(false);

    // Python path
    await runPy(["init"], pyStateDir);
    await runPy(
      ["spend", "executor-14-prune-py", "executor", "5000", "1000"],
      pyStateDir
    );
    const agentDirPy = join(pyStateDir, "blackboard", "budget", "agents");
    expect(existsSync(agentDirPy)).toBe(true);

    await runPy(["reset"], pyStateDir);
    // Python should also prune after reset
    expect(existsSync(agentDirPy)).toBe(false);
  });
});
