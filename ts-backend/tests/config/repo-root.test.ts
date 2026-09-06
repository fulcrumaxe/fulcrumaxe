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
 * What this file can and cannot detect, and why the fixture exists
 * ----------------------------------------------------------------
 * The parity checks above are only *able* to see a collapsed resolver when
 * they are run somewhere the two answers actually differ. Collapsing
 * mainRepoRoot() to `return repoRoot()` and running this file measures:
 *
 *   linked git worktree   1 fail  — mainRepoRoot() named the worktree
 *   plain `git clone`     0 fail  — 10/10 green, the collapse invisible
 *
 * That is not flakiness, it is structural. mainRepoRoot() walks
 * git-common-dir to find the checkout a linked working tree was branched
 * from; in a plain clone there is nothing for it to find, so a collapsed
 * implementation returns the right answer for the wrong reason and is
 * indistinguishable from a correct one.
 *
 * The environment that gates merges is the plain-clone case: CI checks the
 * repo out with actions/checkout@v4, an ordinary (and shallow) checkout. So
 * for as long as this file only measured the checkout it happened to be
 * invoked from, the one place a collapsed resolver had to be caught before
 * reaching main was the one place this file was blind — while its name and
 * its green result both implied otherwise.
 *
 * "mainRepoRoot() — measured against a linked worktree this file builds
 * itself" below closes that. It runs `git worktree add --detach` into a temp
 * directory and points the resolver at the result, so the divergence is
 * supplied by the test rather than inherited from wherever it was run. The
 * collapse above now fails in both contexts, and the checks stop depending
 * on the caller's checkout layout to mean anything. It needs a writable
 * .git (to register the worktree) and removes what it creates, pass or fail.
 *
 * Run: bun test tests/config/repo-root.test.ts --timeout 60000
 */

import { describe, it, expect, afterEach, afterAll } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
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

async function runPython(
  code: string,
  extraEnv: Record<string, string> = {}
): Promise<string> {
  const proc = Bun.spawn(["python3", "-c", code], {
    cwd: REPO_ROOT_FOR_PYTHON,
    env: { ...process.env, ...extraEnv },
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

// ---------------------------------------------------------------------------
// The fixture that makes the two parity checks above discriminate in a plain
// clone — see "What this file can and cannot detect" at the top of the file.
// ---------------------------------------------------------------------------

function git(args: string[], cwd: string): { status: number; stderr: string } {
  const proc = Bun.spawnSync(["git", ...args], { cwd, stdout: "pipe", stderr: "pipe" });
  return { status: proc.exitCode, stderr: new TextDecoder().decode(proc.stderr).trim() };
}

describe("mainRepoRoot() — measured against a linked worktree this file builds itself", () => {
  let tmpParent: string | null = null;
  let worktree: string | null = null;

  afterAll(() => {
    // Registered as a hook, not written inline, so it also runs when an
    // assertion below fails. A leaked worktree would outlive the run twice
    // over: as a temp directory, and as an entry in the shared .git.
    if (worktree !== null) {
      git(["worktree", "remove", "--force", worktree], REPO_ROOT_FOR_PYTHON);
      git(["worktree", "prune"], REPO_ROOT_FOR_PYTHON);
      worktree = null;
    }
    if (tmpParent !== null) {
      rmSync(tmpParent, { recursive: true, force: true });
      tmpParent = null;
    }
  });

  it("names the main checkout while repoRoot() names the worktree", async () => {
    tmpParent = mkdtempSync(join(tmpdir(), "repo-root-parity-"));
    // git names the admin entry under <git-common-dir>/worktrees/ after this
    // path's basename, so the basename has to be unique per run — a fixed one
    // would have two concurrent suites contending for the same entry.
    worktree = join(tmpParent, `wt-${basename(tmpParent)}`);

    // --detach keeps this off the branch namespace entirely: nothing to
    // collide with, and nothing that needs history a shallow clone lacks.
    const added = git(
      ["worktree", "add", "--detach", worktree, "HEAD"],
      REPO_ROOT_FOR_PYTHON
    );
    if (added.status !== 0) {
      throw new Error(`git worktree add failed (${added.status}): ${added.stderr}`);
    }

    // Point both resolvers at the throwaway worktree with the same override,
    // so each answers about the tree this test built rather than about the
    // checkout the suite happened to be invoked from. The override is what
    // keeps the *in-tree* modules under measurement — the worktree holds
    // committed content, so importing its copy would silently exempt any
    // uncommitted change to the resolver from this check.
    resetEnv();
    process.env[ENV_REPO_ROOT] = worktree;
    _clearCaches();

    const tsRepo = normalise(repoRoot());
    const tsMain = normalise(mainRepoRoot());
    const pyMain = normalise(
      await runPython(
        "from backend.repo_root import main_repo_root; print(main_repo_root())",
        { [ENV_REPO_ROOT]: worktree }
      )
    );

    // Preconditions, asserted rather than assumed: the fixture is pointed at
    // and the divergence it exists to supply is really there. Without these a
    // worktree that failed to become a linked tree would make the assertions
    // below vacuous instead of red.
    expect(tsRepo).toBe(normalise(worktree));
    expect(pyMain).not.toBe(normalise(worktree));

    // The relationship, never a literal path: this runs on developer hosts,
    // in agent worktrees and on ubuntu-latest, and a hardcoded path or prefix
    // would be green on exactly one of them. A resolver that collapses the
    // two answers fails here regardless of which checkout the suite ran from.
    expect(tsMain).not.toBe(tsRepo);
    expect(tsMain).toBe(pyMain);
    // The one per-test bound in this file. Not a workaround for a slow
    // assertion: alone among these tests this one checks out the whole tree,
    // so its cost tracks repo size and host speed rather than being constant.
    // The configured suite default (30s, see package.json) already covers it
    // — this keeps the file honest under a bare `bun test`, whose 5s default
    // is the one tests/meta/timeout-governs.test.ts exists to detect.
  }, 60_000);
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
