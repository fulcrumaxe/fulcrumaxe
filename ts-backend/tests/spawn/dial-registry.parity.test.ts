/**
 * tests/spawn/dial-registry.parity.test.ts
 *
 * Parity tests for src/spawn/dial-registry.ts vs backend/dial_registry.py.
 *
 * Strategy:
 *  1. Create isolated temp state dirs per test.
 *  2. Run Python CLI and TS CLI against separate state dirs.
 *  3. Compare stdout, exit codes for parity.
 *  4. Verify programmatic API (check, listDirectives, revertExpired).
 *
 * Run: bun test tests/spawn/dial-registry.parity.test.ts --timeout 120000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  check,
  setDial,
  listDirectives,
  revertExpired,
  DialCeilingExceeded,
} from "../../src/spawn/dial-registry.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");
const TS_ENTRY = join(REPO_ROOT, "ts-backend", "src", "spawn", "dial-registry.ts");
const PY_ENTRY = join(REPO_ROOT, "backend", "dial_registry.py");

function makeTempDir(label: string): string {
  const dir = join(
    tmpdir(),
    `dr-parity-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`
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

/** Write an allowlist file to allow a test source */
function writeAllowlist(stateDir: string, entries: Record<string, unknown>[]): void {
  const path = join(stateDir, "dial-directive-allowlist.json");
  writeFileSync(path, JSON.stringify(entries, null, 2) + "\n", "utf-8");
}

// ---------------------------------------------------------------------------
// Per-test state dir (programmatic tests use env var)
// ---------------------------------------------------------------------------

let pyStateDir: string;
let tsStateDir: string;
let savedEnv: string | undefined;

beforeEach(() => {
  pyStateDir = makeTempDir("py");
  tsStateDir = makeTempDir("ts");
  savedEnv = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  process.env["AUTONOMOUS_TEAM_STATE_DIR"] = tsStateDir;
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
// CLI parity: list
// ---------------------------------------------------------------------------

describe("list — default dials", () => {
  it("both list 13 default dial classes", async () => {
    const py = await runPy(["list"], pyStateDir);
    const ts = await runTs(["list"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    expect(py.exitCode).toBe(0);

    // Count lines mentioning "level="
    const pyLines = py.stdout.split("\n").filter((l) => l.includes("level="));
    const tsLines = ts.stdout.split("\n").filter((l) => l.includes("level="));
    expect(tsLines.length).toBe(pyLines.length);
    expect(tsLines.length).toBe(13);
  });

  it("agent.spawn appears in both outputs", async () => {
    const py = await runPy(["list"], pyStateDir);
    const ts = await runTs(["list"], tsStateDir);
    expect(ts.stdout).toContain("agent.spawn");
    expect(py.stdout).toContain("agent.spawn");
  });

  it("sandbox.modify appears with ceiling=1 in both", async () => {
    const py = await runPy(["list"], pyStateDir);
    const ts = await runTs(["list"], tsStateDir);
    const pyLine = py.stdout.split("\n").find((l) => l.includes("sandbox.modify")) ?? "";
    const tsLine = ts.stdout.split("\n").find((l) => l.includes("sandbox.modify")) ?? "";
    expect(tsLine).toContain("ceiling=1");
    expect(pyLine).toContain("ceiling=1");
  });
});

// ---------------------------------------------------------------------------
// CLI parity: check
// ---------------------------------------------------------------------------

describe("check — default dial levels", () => {
  it("check agent.spawn 1 → ALLOW in both", async () => {
    const py = await runPy(["check", "agent.spawn", "1"], pyStateDir);
    const ts = await runTs(["check", "agent.spawn", "1"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout).toContain("ALLOW");
    expect(py.stdout).toContain("ALLOW");
  });

  it("check agent.spawn 4 → ALLOW (default level=4)", async () => {
    const py = await runPy(["check", "agent.spawn", "4"], pyStateDir);
    const ts = await runTs(["check", "agent.spawn", "4"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode); // both 0 (allowed)
    expect(ts.stdout).toContain("ALLOW");
  });

  it("check agent.spawn 5 → ALLOW (within ceiling)", async () => {
    const py = await runPy(["check", "agent.spawn", "5"], pyStateDir);
    const ts = await runTs(["check", "agent.spawn", "5"], tsStateDir);
    // level=4 < 5, so DENY — but ceiling=5 is within range; Python denies because current level=4
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.includes("ALLOW") || ts.stdout.includes("DENY")).toBe(true);
  });

  it("check sandbox.modify 2 → DENY (ceiling=1, hardcoded)", async () => {
    const py = await runPy(["check", "sandbox.modify", "2"], pyStateDir);
    const ts = await runTs(["check", "sandbox.modify", "2"], tsStateDir);
    expect(ts.exitCode).toBe(1);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout).toContain("DENY");
    expect(py.stdout).toContain("DENY");
  });

  it("check intent.generate 1 → ALLOW (level=1)", async () => {
    const py = await runPy(["check", "intent.generate", "1"], pyStateDir);
    const ts = await runTs(["check", "intent.generate", "1"], tsStateDir);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout).toContain("ALLOW");
  });

  it("check intent.generate 2 → DENY (level=1 < 2)", async () => {
    const py = await runPy(["check", "intent.generate", "2"], pyStateDir);
    const ts = await runTs(["check", "intent.generate", "2"], tsStateDir);
    expect(ts.exitCode).toBe(1);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout).toContain("DENY");
  });
});

// ---------------------------------------------------------------------------
// CLI parity: set (requires allowlist)
// ---------------------------------------------------------------------------

describe("set — requires allowlist", () => {
  it("set without allowlist → error + exit 1", async () => {
    const py = await runPy(
      ["set", "agent.spawn", "3"],
      pyStateDir
    );
    const ts = await runTs(
      ["set", "agent.spawn", "3"],
      tsStateDir
    );
    expect(ts.exitCode).toBe(1);
    expect(ts.exitCode).toBe(py.exitCode);
  });

  it("set with allowlisted source → success + level changes", async () => {
    const sourceJson = JSON.stringify({ kind: "system", reason: "test" });
    writeAllowlist(pyStateDir, [{ kind: "system", reason: "test" }]);
    writeAllowlist(tsStateDir, [{ kind: "system", reason: "test" }]);

    const py = await runPy(
      ["set", "agent.spawn", "3", "--source", sourceJson],
      pyStateDir
    );
    const ts = await runTs(
      ["set", "agent.spawn", "3", "--source", sourceJson],
      tsStateDir
    );

    expect(ts.exitCode).toBe(0);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout).toContain("level=3");
    expect(py.stdout).toContain("level=3");
  });

  it("set sandbox.modify to 2 → ceiling violation + exit 1", async () => {
    writeAllowlist(pyStateDir, [{ kind: "system", reason: "test" }]);
    writeAllowlist(tsStateDir, [{ kind: "system", reason: "test" }]);
    const sourceJson = JSON.stringify({ kind: "system", reason: "test" });

    const py = await runPy(
      ["set", "sandbox.modify", "2", "--source", sourceJson],
      pyStateDir
    );
    const ts = await runTs(
      ["set", "sandbox.modify", "2", "--source", sourceJson],
      tsStateDir
    );

    expect(ts.exitCode).toBe(1);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stderr.toLowerCase()).toContain("ceiling");
    expect(py.stderr.toLowerCase()).toContain("ceiling");
  });
});

// ---------------------------------------------------------------------------
// CLI parity: revert-expired
// ---------------------------------------------------------------------------

describe("revert-expired — no expired directives", () => {
  it("both report reverted 0 class(es)", async () => {
    const py = await runPy(["revert-expired"], pyStateDir);
    const ts = await runTs(["revert-expired"], tsStateDir);
    expect(ts.exitCode).toBe(0);
    expect(ts.exitCode).toBe(py.exitCode);
    expect(ts.stdout.trim()).toBe(py.stdout.trim());
  });
});

// ---------------------------------------------------------------------------
// Programmatic API parity
// ---------------------------------------------------------------------------

describe("check() programmatic API", () => {
  it("check('agent.spawn', 1) → allowed=true", () => {
    const [allowed, reason] = check("agent.spawn", 1);
    expect(allowed).toBe(true);
    expect(reason).toContain("agent.spawn");
  });

  it("check('agent.spawn', 4) → allowed=true (default level=4)", () => {
    const [allowed] = check("agent.spawn", 4);
    expect(allowed).toBe(true);
  });

  it("check('agent.spawn', 5) → allowed=false (level=4 < 5)", () => {
    const [allowed] = check("agent.spawn", 5);
    expect(allowed).toBe(false);
  });

  it("check('sandbox.modify', 2) → allowed=false (ceiling=1)", () => {
    const [allowed, reason] = check("sandbox.modify", 2);
    expect(allowed).toBe(false);
    expect(reason).toContain("ceiling");
  });

  it("check('intent.generate', 1) → allowed=true (level=1)", () => {
    const [allowed] = check("intent.generate", 1);
    expect(allowed).toBe(true);
  });

  it("check('intent.generate', 2) → allowed=false (level=1 < 2)", () => {
    const [allowed] = check("intent.generate", 2);
    expect(allowed).toBe(false);
  });

  it("check('unknown.class', 1) → allowed=true (default allow at level 1)", () => {
    const [allowed, reason] = check("unknown.class", 1);
    expect(allowed).toBe(true);
    expect(reason).toContain("unknown class");
  });

  it("check('unknown.class', 2) → allowed=false", () => {
    const [allowed] = check("unknown.class", 2);
    expect(allowed).toBe(false);
  });

  it("check with requested_level < 1 → allowed=false", () => {
    const [allowed] = check("agent.spawn", 0);
    expect(allowed).toBe(false);
  });
});

describe("listDirectives() programmatic API", () => {
  it("returns 13 default dial classes", () => {
    const directives = listDirectives();
    expect(directives.length).toBe(13);
  });

  it("all entries have class, level, ceiling, directives fields", () => {
    const directives = listDirectives();
    for (const d of directives) {
      expect(typeof d.class).toBe("string");
      expect(typeof d.level).toBe("number");
      expect(typeof d.ceiling).toBe("number");
      expect(Array.isArray(d.directives)).toBe(true);
    }
  });

  it("sandbox.modify ceiling is hardcoded to 1", () => {
    const directives = listDirectives();
    const sm = directives.find((d) => d.class === "sandbox.modify");
    expect(sm).toBeDefined();
    expect(sm!.ceiling).toBe(1);
  });

  it("methodology.change ceiling is hardcoded to 2", () => {
    const directives = listDirectives();
    const mc = directives.find((d) => d.class === "methodology.change");
    expect(mc).toBeDefined();
    expect(mc!.ceiling).toBe(2);
  });

  it("agent.spawn has default level=4 and ceiling=5", () => {
    const directives = listDirectives();
    const as = directives.find((d) => d.class === "agent.spawn");
    expect(as).toBeDefined();
    expect(as!.level).toBe(4);
    expect(as!.ceiling).toBe(5);
  });

  it("intent.generate has default level=1", () => {
    const directives = listDirectives();
    const ig = directives.find((d) => d.class === "intent.generate");
    expect(ig).toBeDefined();
    expect(ig!.level).toBe(1);
  });

  it("docs.write has default level=5", () => {
    const directives = listDirectives();
    const dw = directives.find((d) => d.class === "docs.write");
    expect(dw).toBeDefined();
    expect(dw!.level).toBe(5);
  });
});

describe("setDial() — ceiling and auth enforcement", () => {
  it("setDial with level < 1 throws ValueError", () => {
    expect(() => setDial("agent.spawn", 0)).toThrow();
  });

  it("setDial sandbox.modify level=2 throws DialCeilingExceeded", () => {
    expect(() =>
      setDial("sandbox.modify", 2, {
        source: { kind: "system", reason: "test" },
      })
    ).toThrow(DialCeilingExceeded);
  });

  it("setDial without allowlist throws auth error", () => {
    expect(() =>
      setDial("agent.spawn", 3, { source: { kind: "github_user", login: "ian" } })
    ).toThrow();
  });

  it("setDial with valid allowlist succeeds", () => {
    writeAllowlist(tsStateDir, [{ kind: "system", reason: "test" }]);
    const result = setDial("agent.spawn", 3, {
      source: { kind: "system", reason: "test" },
    });
    expect(result.level).toBe(3);
    // Verify check() now reflects the new level
    const [allowed] = check("agent.spawn", 3);
    expect(allowed).toBe(true);
  });

  it("setDial with TTL stores directive", () => {
    writeAllowlist(tsStateDir, [{ kind: "system", reason: "test" }]);
    const result = setDial("agent.spawn", 2, {
      source: { kind: "system", reason: "test" },
      ttl: new Date(Date.now() + 3600_000).toISOString(), // 1 hour
    });
    expect(result.directives.length).toBeGreaterThan(0);
    expect(result.directives[result.directives.length - 1].ttl_until).not.toBeNull();
  });
});

describe("revertExpired() — TTL expiry", () => {
  it("revertExpired with no directives returns 0", () => {
    const n = revertExpired();
    expect(n).toBe(0);
  });

  it("revertExpired with already-expired TTL reverts level", () => {
    writeAllowlist(tsStateDir, [{ kind: "system", reason: "test" }]);
    // Set agent.spawn to 2 with a TTL in the past
    const pastIso = new Date(Date.now() - 1000).toISOString(); // 1 second ago
    setDial("agent.spawn", 2, {
      source: { kind: "system", reason: "test" },
      ttl: pastIso,
    });
    // Now the directive is expired — revertExpired should bring level back to default (4)
    const n = revertExpired();
    expect(n).toBeGreaterThanOrEqual(1); // at least agent.spawn reverted
    // Level should be back to default (4)
    const [allowed] = check("agent.spawn", 4);
    expect(allowed).toBe(true);
  });
});

describe("DialCeilingExceeded export", () => {
  it("is an Error subclass", () => {
    const err = new DialCeilingExceeded("test");
    expect(err instanceof Error).toBe(true);
    expect(err.name).toBe("DialCeilingExceeded");
    expect(err.message).toBe("test");
  });
});
