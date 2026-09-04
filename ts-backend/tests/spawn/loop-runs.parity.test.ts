/**
 * tests/spawn/loop-runs.parity.test.ts
 *
 * Parity tests: runs CLI sequences against BOTH Python (backend/loop_runs.py)
 * and TS (src/spawn/loop-runs.ts), then asserts identical resulting JSON shapes,
 * stdout patterns, and exit codes.
 *
 * Python's loop_runs.py derives repo root from __file__ (always the real repo),
 * so Python writes to .autonomous-team/loop-runs/ in the real repo. We read those
 * files back and clean up after each test.
 *
 * TS uses AF_REPO_ROOT env var to redirect to a temp dir per test so it never
 * pollutes the real repo. Python state is cleaned up after each test.
 *
 * Run: bun test tests/spawn/loop-runs.parity.test.ts --timeout 120000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, existsSync, readFileSync, writeFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { cmdStart, cmdFinish, cmdTail, latestFailingRunPath, runsDir } from "../../src/spawn/loop-runs.js";

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");
const TS_ENTRY = join(REPO_ROOT, "ts-backend", "src", "spawn", "loop-runs.ts");
const PY_ENTRY = join(REPO_ROOT, "backend", "loop_runs.py");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTempDir(label: string): string {
  const dir = join(
    tmpdir(),
    `lr-parity-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`
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

/** Run Python loop_runs.py. Python always writes to the real repo's loop-runs dir. */
async function runPy(
  args: string[]
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return runProcess(["python3", PY_ENTRY, ...args], {});
}

/** Run TS loop-runs.ts using AF_REPO_ROOT to redirect to tsRoot. */
async function runTs(
  args: string[],
  tsRoot: string
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  return runProcess(["bun", "run", TS_ENTRY, ...args], { AF_REPO_ROOT: tsRoot });
}

interface LoopRunFile {
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  duration_s: number | null;
  last_stderr_lines: string[];
}

function readRunFile(path: string): LoopRunFile {
  return JSON.parse(readFileSync(path, "utf-8")) as LoopRunFile;
}

// ---------------------------------------------------------------------------
// Test fixture: per-test cleanup
// Collect paths created by Python tests so we can remove them.
// ---------------------------------------------------------------------------

let pyCreatedFiles: string[] = [];
let tsRoot = "";

beforeEach(() => {
  pyCreatedFiles = [];
  tsRoot = makeTempDir("ts");
  mkdirSync(join(tsRoot, ".autonomous-team", "loop-runs"), { recursive: true });
});

afterEach(() => {
  // Clean up files Python wrote to the real loop-runs dir
  for (const f of pyCreatedFiles) {
    try { if (existsSync(f)) unlinkSync(f); } catch { /* ignore */ }
  }
  pyCreatedFiles = [];
  try { rmSync(tsRoot, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ---------------------------------------------------------------------------
// Test 1: start creates a stub JSON file with the right shape
// ---------------------------------------------------------------------------

describe("parity: start command creates a stub", () => {
  it("produces a JSON stub file with correct shape (Python and TS)", async () => {
    const pyResult = await runPy(["start"]);
    const tsResult = await runTs(["start"], tsRoot);

    expect(pyResult.exitCode).toBe(0);
    expect(tsResult.exitCode).toBe(0);

    // Both print a path ending in .json
    const pyPath = pyResult.stdout.trim();
    const tsPath = tsResult.stdout.trim();
    expect(pyPath).toMatch(/\.json$/);
    expect(tsPath).toMatch(/\.json$/);

    // Track Python file for cleanup
    if (existsSync(pyPath)) pyCreatedFiles.push(pyPath);

    const pyStub = readRunFile(pyPath);
    const tsStub = readRunFile(tsPath);

    // Both should have started_at set, and null for finished_at / exit_code
    expect(typeof pyStub.started_at).toBe("string");
    expect(typeof tsStub.started_at).toBe("string");
    expect(pyStub.finished_at).toBeNull();
    expect(tsStub.finished_at).toBeNull();
    expect(pyStub.exit_code).toBeNull();
    expect(tsStub.exit_code).toBeNull();
    expect(pyStub.duration_s).toBeNull();
    expect(tsStub.duration_s).toBeNull();
    expect(pyStub.last_stderr_lines).toEqual([]);
    expect(tsStub.last_stderr_lines).toEqual([]);

    // Timestamps should match format "%Y-%m-%dT%H:%M:%SZ"
    expect(pyStub.started_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    expect(tsStub.started_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);

    console.log("Python stub:", JSON.stringify(pyStub));
    console.log("TS stub:", JSON.stringify(tsStub));
  });
});

// ---------------------------------------------------------------------------
// Test 2: start + finish with exit_code=0
// ---------------------------------------------------------------------------

describe("parity: start + finish (exit 0)", () => {
  it("finalises the file with exit_code=0 and duration_s>=0", async () => {
    const pyStart = await runPy(["start"]);
    const tsStart = await runTs(["start"], tsRoot);

    expect(pyStart.exitCode).toBe(0);
    expect(tsStart.exitCode).toBe(0);

    const pyFile = pyStart.stdout.trim();
    const tsFile = tsStart.stdout.trim();
    if (existsSync(pyFile)) pyCreatedFiles.push(pyFile);

    const pyFinish = await runPy(["finish", "--file", pyFile, "--exit", "0"]);
    const tsFinish = await runTs(["finish", "--file", tsFile, "--exit", "0"], tsRoot);

    expect(pyFinish.exitCode).toBe(0);
    expect(tsFinish.exitCode).toBe(0);

    const py = readRunFile(pyFile);
    const ts = readRunFile(tsFile);

    expect(py.exit_code).toBe(0);
    expect(ts.exit_code).toBe(0);
    expect(ts.exit_code).toBe(py.exit_code);

    expect(typeof py.finished_at).toBe("string");
    expect(typeof ts.finished_at).toBe("string");
    expect(py.finished_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    expect(ts.finished_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);

    expect(py.duration_s).not.toBeNull();
    expect(ts.duration_s).not.toBeNull();
    expect(py.duration_s! >= 0).toBe(true);
    expect(ts.duration_s! >= 0).toBe(true);
    // duration_s should be very close (same second-level timestamp math)
    expect(Math.abs(ts.duration_s! - py.duration_s!)).toBeLessThanOrEqual(2);

    expect(py.last_stderr_lines).toEqual([]);
    expect(ts.last_stderr_lines).toEqual([]);

    console.log("Python finished:", JSON.stringify(py));
    console.log("TS finished:", JSON.stringify(ts));
  });
});

// ---------------------------------------------------------------------------
// Test 3: start + finish with stderr file
// ---------------------------------------------------------------------------

describe("parity: start + finish with stderr", () => {
  it("captures last_stderr_lines from a stderr file", async () => {
    const stderrFile = join(tsRoot, "test-stderr.txt");
    writeFileSync(stderrFile, "error line 1\nerror line 2\nerror line 3\n");

    const pyStart = await runPy(["start"]);
    const tsStart = await runTs(["start"], tsRoot);
    const pyFile = pyStart.stdout.trim();
    const tsFile = tsStart.stdout.trim();
    if (existsSync(pyFile)) pyCreatedFiles.push(pyFile);

    const pyFinish = await runPy(
      ["finish", "--file", pyFile, "--exit", "1", "--stderr", stderrFile]
    );
    const tsFinish = await runTs(
      ["finish", "--file", tsFile, "--exit", "1", "--stderr", stderrFile],
      tsRoot
    );

    expect(pyFinish.exitCode).toBe(0);
    expect(tsFinish.exitCode).toBe(0);

    const py = readRunFile(pyFile);
    const ts = readRunFile(tsFile);

    expect(py.exit_code).toBe(1);
    expect(ts.exit_code).toBe(1);
    expect(py.last_stderr_lines).toEqual(["error line 1", "error line 2", "error line 3"]);
    expect(ts.last_stderr_lines).toEqual(py.last_stderr_lines);
  });
});

// ---------------------------------------------------------------------------
// Test 4: tail on empty directory
// ---------------------------------------------------------------------------

describe("parity: tail on empty TS directory", () => {
  it("prints 'no loop runs recorded yet'", async () => {
    const tsResult = await runTs(["tail"], tsRoot);
    expect(tsResult.exitCode).toBe(0);
    expect(tsResult.stdout.trim()).toBe("no loop runs recorded yet");
  });
});

// ---------------------------------------------------------------------------
// Test 5: tail --failures-only when no files returns "no loop runs recorded yet"
// (Python and TS both short-circuit before the failures-only filter when empty)
// ---------------------------------------------------------------------------

describe("parity: tail --failures-only on empty TS directory", () => {
  it("prints 'no loop runs recorded yet' (same as empty tail)", async () => {
    // Python also prints "no loop runs recorded yet" when there are no files
    // (it checks `if not files` before applying the failures_only filter).
    const tsResult = await runTs(["tail", "--failures-only"], tsRoot);
    expect(tsResult.exitCode).toBe(0);
    expect(tsResult.stdout.trim()).toBe("no loop runs recorded yet");
  });
});

// ---------------------------------------------------------------------------
// Test 6: "no failed loop runs recorded yet" when files exist but all pass
// ---------------------------------------------------------------------------

describe("parity: tail --failures-only with only successful runs", () => {
  it("prints 'no failed loop runs recorded yet' when files exist but all have exit 0", async () => {
    // Create a successful run stub so the dir is not empty
    const dir = runsDir(tsRoot);
    const successFile = join(dir, "2026-06-01T09-00-00Z.json");
    writeFileSync(
      successFile,
      JSON.stringify({
        started_at: "2026-06-01T09:00:00Z",
        finished_at: "2026-06-01T09:01:00Z",
        exit_code: 0,
        duration_s: 60,
        last_stderr_lines: [],
      }, null, 2)
    );

    const tsResult = await runTs(["tail", "--failures-only"], tsRoot);
    expect(tsResult.exitCode).toBe(0);
    expect(tsResult.stdout.trim()).toBe("no failed loop runs recorded yet");
  });
});

// ---------------------------------------------------------------------------
// Test 7: finish exits non-zero for missing file — parity
// ---------------------------------------------------------------------------

describe("parity: finish on missing file", () => {
  it("both Python and TS exit 1 with error on stderr", async () => {
    const fakePath = join(tsRoot, "nonexistent.json");
    const pyResult = await runPy(["finish", "--file", fakePath, "--exit", "0"]);
    const tsResult = await runTs(["finish", "--file", fakePath, "--exit", "0"], tsRoot);

    expect(pyResult.exitCode).toBe(1);
    expect(tsResult.exitCode).toBe(1);

    expect(pyResult.stderr).toContain("file not found");
    expect(tsResult.stderr).toContain("file not found");
  });
});

// ---------------------------------------------------------------------------
// Test 8: programmatic API — cmdStart + cmdFinish + cmdTail
// ---------------------------------------------------------------------------

describe("programmatic API: cmdStart + cmdFinish + cmdTail", () => {
  it("creates, finalises, and tails a loop run", () => {
    const lines: string[] = [];
    const origWrite = process.stdout.write.bind(process.stdout);
    (process.stdout as unknown as { write: (s: string) => boolean }).write = (s: string) => {
      lines.push(s);
      return true;
    };

    let filePath: string;
    try {
      filePath = cmdStart(tsRoot);
    } finally {
      (process.stdout as unknown as { write: typeof origWrite }).write = origWrite;
    }

    expect(filePath).toMatch(/\.json$/);
    expect(existsSync(filePath)).toBe(true);

    const stub = readRunFile(filePath);
    expect(stub.started_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    expect(stub.finished_at).toBeNull();
    expect(stub.exit_code).toBeNull();
    expect(stub.duration_s).toBeNull();
    expect(stub.last_stderr_lines).toEqual([]);

    const rc = cmdFinish({ file: filePath, exit: 42 });
    expect(rc).toBe(0);

    const finished = readRunFile(filePath);
    expect(finished.exit_code).toBe(42);
    expect(finished.finished_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    expect(typeof finished.duration_s).toBe("number");
    expect(finished.duration_s! >= 0).toBe(true);

    // Tail output
    const tailLines: string[] = [];
    const origLog = console.log;
    console.log = (...args: unknown[]) => tailLines.push(args.join(" "));
    try {
      const tailRc = cmdTail({ n: 10, repoRoot: tsRoot });
      expect(tailRc).toBe(0);
    } finally {
      console.log = origLog;
    }

    expect(tailLines.some((l) => l.includes("42"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Test 9: latestFailingRunPath returns path of most recent failed run
// ---------------------------------------------------------------------------

describe("latestFailingRunPath", () => {
  it("returns null when no failed runs", () => {
    const result = latestFailingRunPath(tsRoot);
    expect(result).toBeNull();
  });

  it("returns path of latest failed run", () => {
    const dir = runsDir(tsRoot);
    const failedFile = join(dir, "2026-06-01T10-00-00Z.json");
    writeFileSync(
      failedFile,
      JSON.stringify({
        started_at: "2026-06-01T10:00:00Z",
        finished_at: "2026-06-01T10:01:00Z",
        exit_code: 1,
        duration_s: 60,
        last_stderr_lines: ["error"],
      }, null, 2)
    );

    const result = latestFailingRunPath(tsRoot);
    expect(result).toBe(failedFile);
  });

  it("ignores runs with exit_code=0", () => {
    const dir = runsDir(tsRoot);
    const successFile = join(dir, "2026-06-01T09-00-00Z.json");
    writeFileSync(
      successFile,
      JSON.stringify({
        started_at: "2026-06-01T09:00:00Z",
        finished_at: "2026-06-01T09:01:00Z",
        exit_code: 0,
        duration_s: 60,
        last_stderr_lines: [],
      }, null, 2)
    );

    const result = latestFailingRunPath(tsRoot);
    expect(result).toBeNull();
  });

  it("returns the newest (last alphabetically) failed run when multiple exist", () => {
    const dir = runsDir(tsRoot);
    const olderFailed = join(dir, "2026-06-01T09-00-00Z.json");
    const newerFailed = join(dir, "2026-06-01T10-00-00Z.json");
    for (const [f, ec] of [[olderFailed, 1], [newerFailed, 2]] as [string, number][]) {
      writeFileSync(
        f,
        JSON.stringify({
          started_at: "2026-06-01T09:00:00Z",
          finished_at: "2026-06-01T09:01:00Z",
          exit_code: ec,
          duration_s: 60,
          last_stderr_lines: [],
        }, null, 2)
      );
    }

    const result = latestFailingRunPath(tsRoot);
    // Python uses reverse=True on sorted files → newest first → returns newerFailed
    expect(result).toBe(newerFailed);
  });
});

// ---------------------------------------------------------------------------
// Test 10: tail shows non-stub rows only
// ---------------------------------------------------------------------------

describe("parity: tail skips stub (unfinished) runs", () => {
  it("tail skips runs with null exit_code", () => {
    const dir = runsDir(tsRoot);

    // Write a stub (unfinished)
    const stubFile = join(dir, "2026-06-01T08-00-00Z.json");
    writeFileSync(
      stubFile,
      JSON.stringify({
        started_at: "2026-06-01T08:00:00Z",
        finished_at: null,
        exit_code: null,
        duration_s: null,
        last_stderr_lines: [],
      }, null, 2)
    );

    const lines: string[] = [];
    const origLog = console.log;
    console.log = (...args: unknown[]) => lines.push(args.join(" "));
    try {
      cmdTail({ n: 10, repoRoot: tsRoot });
    } finally {
      console.log = origLog;
    }

    // Should report "no loop runs recorded yet" since the stub is skipped
    expect(lines.some((l) => l.includes("no loop runs recorded yet"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Test 11: tsToFilename round-trip matches Python _ts_to_filename
// ---------------------------------------------------------------------------

describe("parity: tsToFilename", () => {
  it("converts ISO8601 colons to hyphens and appends .json", async () => {
    const { tsToFilename } = await import("../../src/spawn/loop-runs.js");

    const ts = "2026-05-15T02:46:00Z";
    const result = tsToFilename(ts);
    expect(result).toBe("2026-05-15T02-46-00Z.json");

    // Already ends with .json — should not double-append
    const withExt = "2026-05-15T02-46-00Z.json";
    expect(tsToFilename(withExt)).toBe("2026-05-15T02-46-00Z.json");
  });
});
