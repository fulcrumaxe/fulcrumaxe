/**
 * tests/config/repo-root.test.ts
 *
 * Acceptance tests for src/config/repo-root.ts (D#1825).
 *
 * The load-bearing check here is parity with backend/repo_root.py: a
 * resolver that collapses repoRoot()/mainRepoRoot() into one answer would
 * pass every marker-based check below and still be wrong inside a linked
 * git worktree, where the two answers genuinely differ. See "byte-identical
 * with the Python resolver" below.
 *
 * Run: bun test tests/config/repo-root.test.ts --timeout 60000
 */

import { describe, it, expect, afterEach } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  repoRoot,
  mainRepoRoot,
  _clearCaches,
  ENV_REPO_ROOT,
  ENV_AF_REPO_ROOT,
} from "../../src/config/repo-root.js";

// Independent of the module under test: this test file's own location,
// walked up to the checkout root, used only to anchor the Python
// subprocess below at the same checkout the TS module is measured from.
const _TEST_FILE_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT_FOR_PYTHON = resolve(_TEST_FILE_DIR, "..", "..", "..");

const SRC_PATH = resolve(_TEST_FILE_DIR, "..", "..", "src", "config", "repo-root.ts");

const ORIGINAL_ENV_REPO_ROOT = process.env[ENV_REPO_ROOT];
const ORIGINAL_AF_REPO_ROOT = process.env[ENV_AF_REPO_ROOT];
const ORIGINAL_PATH = process.env["PATH"];

function resetEnv(): void {
  if (ORIGINAL_ENV_REPO_ROOT === undefined) delete process.env[ENV_REPO_ROOT];
  else process.env[ENV_REPO_ROOT] = ORIGINAL_ENV_REPO_ROOT;
  if (ORIGINAL_AF_REPO_ROOT === undefined) delete process.env[ENV_AF_REPO_ROOT];
  else process.env[ENV_AF_REPO_ROOT] = ORIGINAL_AF_REPO_ROOT;
  if (ORIGINAL_PATH === undefined) delete process.env["PATH"];
  else process.env["PATH"] = ORIGINAL_PATH;
  _clearCaches();
}

afterEach(resetEnv);

async function runPython(code: string): Promise<string> {
  const proc = Bun.spawn(["python3", "-c", code], {
    cwd: REPO_ROOT_FOR_PYTHON,
    stdout: "pipe",
    stderr: "pipe",
  });
  const timeout = setTimeout(() => proc.kill(), 15_000);
  const exitCode = await proc.exited;
  clearTimeout(timeout);
  const stdout = (await new Response(proc.stdout).text()).trim();
  const stderr = await new Response(proc.stderr).text();
  if (exitCode !== 0) {
    throw new Error(`python3 exited ${exitCode}: ${stderr}`);
  }
  return stdout;
}

function normalise(p: string): string {
  return p.endsWith("/") ? p.slice(0, -1) : p;
}

describe("repoRoot() — marker-based resolution", () => {
  it("resolves to a path containing backend/discussion_cache.py", () => {
    resetEnv();
    const root = repoRoot();
    expect(existsSync(resolve(root, "backend", "discussion_cache.py"))).toBe(true);
  });
});

describe("repoRoot()/mainRepoRoot() — parity with backend/repo_root.py", () => {
  it("repoRoot() is byte-identical to Python's repo_root() from the same checkout", async () => {
    resetEnv();
    const tsAnswer = normalise(repoRoot());
    const pyAnswer = normalise(
      await runPython("from backend.repo_root import repo_root; print(repo_root())")
    );
    expect(tsAnswer).toBe(pyAnswer);
  });

  it("mainRepoRoot() is byte-identical to Python's main_repo_root() from the same checkout", async () => {
    resetEnv();
    const tsAnswer = normalise(mainRepoRoot());
    const pyAnswer = normalise(
      await runPython(
        "from backend.repo_root import main_repo_root; print(main_repo_root())"
      )
    );
    expect(tsAnswer).toBe(pyAnswer);
  });
});

describe("repoRoot() — environment override precedence", () => {
  it("AUTONOMOUS_TEAM_REPO_ROOT alone wins", () => {
    resetEnv();
    delete process.env[ENV_AF_REPO_ROOT];
    process.env[ENV_REPO_ROOT] = "/tmp/fake-root";
    _clearCaches();
    expect(repoRoot()).toBe("/tmp/fake-root");
  });

  it("AF_REPO_ROOT alone (AUTONOMOUS_TEAM_REPO_ROOT unset) wins", () => {
    resetEnv();
    delete process.env[ENV_REPO_ROOT];
    process.env[ENV_AF_REPO_ROOT] = "/tmp/fake-root";
    _clearCaches();
    expect(repoRoot()).toBe("/tmp/fake-root");
  });

  it("AUTONOMOUS_TEAM_REPO_ROOT wins when both are set", () => {
    resetEnv();
    process.env[ENV_REPO_ROOT] = "/tmp/canonical-fake-root";
    process.env[ENV_AF_REPO_ROOT] = "/tmp/af-fake-root";
    _clearCaches();
    expect(repoRoot()).toBe("/tmp/canonical-fake-root");
  });
});

describe("repoRoot() — git-unavailable floor", () => {
  it("still returns a non-empty path, and does not throw, with no git on PATH", () => {
    resetEnv();
    delete process.env[ENV_REPO_ROOT];
    delete process.env[ENV_AF_REPO_ROOT];
    process.env["PATH"] = "/nonexistent-empty-dir-for-test";
    _clearCaches();
    let result = "";
    expect(() => {
      result = repoRoot();
    }).not.toThrow();
    expect(result.length).toBeGreaterThan(0);
  });
});

describe("src/config/repo-root.ts — module docstring", () => {
  const src = readFileSync(SRC_PATH, "utf-8");

  it("states which answer a file-locating caller wants (repoRoot)", () => {
    expect(src).toContain("wants repoRoot()");
  });

  it("states which answer a containment/authorisation question wants (mainRepoRoot)", () => {
    expect(src).toContain("a containment or\n * authorisation question) wants mainRepoRoot() instead");
  });

  it("states neither environment override is load-bearing for containment/authorisation", () => {
    expect(src).toContain(
      "Neither environment override is load-bearing for any containment or\n * authorisation decision"
    );
  });
});
