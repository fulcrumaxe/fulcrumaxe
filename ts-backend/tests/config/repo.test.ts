/**
 * tests/config/repo.test.ts
 *
 * DEFAULT_REPO used to name this project's pre-rename slug, kept in place by
 * a "FROZEN RULE" that reserved the literal to open-source/export.sh's
 * substitution pass. D#2348 retires that pass, and the frozen value only ever
 * resolved through GitHub's rename redirect — a wrong target that could never
 * announce itself as one.
 *
 * These exercise resolveRepo() for real rather than grepping the source: the
 * fallback arm is the one the redirect was hiding, so it is the one worth
 * running.
 *
 * Run: bun test tests/config/repo.test.ts
 */

import { describe, it, expect, afterEach } from "bun:test";
import { mkdtempSync, readFileSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { DEFAULT_REPO, resolveRepo, repoOwner, repoName } from "../../src/config/repo.js";

const _TEST_FILE_DIR = dirname(fileURLToPath(import.meta.url));
const SRC_PATH = resolve(_TEST_FILE_DIR, "..", "..", "src", "config", "repo.ts");

const ORIGINAL = {
  AF_REPO_ROOT: process.env["AF_REPO_ROOT"],
  GH_REPO: process.env["GH_REPO"],
  _REPO: process.env["_REPO"],
};

const tmpDirs: string[] = [];

function emptyRepoRoot(): string {
  // A directory with no .autonomous-team/config.json, so configJsonRepo()
  // returns null and resolution falls through to the env vars and the
  // constant. This is the arm the stale literal actually lived on.
  const dir = mkdtempSync(join(tmpdir(), "repo-config-"));
  tmpDirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const [key, value] of Object.entries(ORIGINAL)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  while (tmpDirs.length) rmSync(tmpDirs.pop()!, { recursive: true, force: true });
});

describe("DEFAULT_REPO", () => {
  it("names this project's current slug, not the pre-rename one", () => {
    expect(DEFAULT_REPO).toBe("autonomous-agent-7/fulcrumaxe");
  });

  it("is what resolveRepo() actually returns when nothing else is configured", () => {
    process.env["AF_REPO_ROOT"] = emptyRepoRoot();
    delete process.env["GH_REPO"];
    delete process.env["_REPO"];

    expect(resolveRepo()).toBe("autonomous-agent-7/fulcrumaxe");

    // Halves derived from the constant, not spelled out again: the export's
    // identifier rewrite only recognises the owner/name pair, so a bare owner
    // literal here would survive into the public tree and assert the wrong
    // thing there.
    const [owner, name] = DEFAULT_REPO.split("/");
    expect(repoOwner()).toBe(owner!);
    expect(repoName()).toBe(name!);
  });

  it("carries no reference to the retired export substitution pass", () => {
    const source = readFileSync(SRC_PATH, "utf-8");
    expect(source).not.toContain("FROZEN RULE");
    expect(source).not.toContain("autonomous-forever");
  });
});

describe("resolveRepo precedence (unchanged by D#2348 PR-a)", () => {
  it("prefers .autonomous-team/config.json over everything else", () => {
    const root = emptyRepoRoot();
    mkdirSync(join(root, ".autonomous-team"), { recursive: true });
    writeFileSync(
      join(root, ".autonomous-team", "config.json"),
      JSON.stringify({ repo: "adopter/their-repo" }),
    );
    process.env["AF_REPO_ROOT"] = root;
    process.env["GH_REPO"] = "env/gh-repo";

    expect(resolveRepo()).toBe("adopter/their-repo");
  });

  it("falls back to GH_REPO, then _REPO, before the constant", () => {
    process.env["AF_REPO_ROOT"] = emptyRepoRoot();

    process.env["GH_REPO"] = "env/gh-repo";
    process.env["_REPO"] = "env/underscore-repo";
    expect(resolveRepo()).toBe("env/gh-repo");

    delete process.env["GH_REPO"];
    expect(resolveRepo()).toBe("env/underscore-repo");

    delete process.env["_REPO"];
    expect(resolveRepo()).toBe(DEFAULT_REPO);
  });
});
