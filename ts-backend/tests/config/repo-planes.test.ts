/**
 * tests/config/repo-planes.test.ts
 *
 * Tests for resolveCodeRepo() / resolveDiscussionRepo() in src/config/repo.ts.
 *
 * Two properties. First, inertness: with neither "code_repo" nor
 * "discussion_repo" set in .autonomous-team/config.json — the state of every
 * tree today — both accessors must return exactly what resolveRepo() returns,
 * so introducing the vocabulary retargets no call site.
 *
 * Second, the asymmetry that is not inert. resolveCodeRepo() keeps the full
 * precedence chain including DEFAULT_REPO, because every checkout has a code
 * repo. resolveDiscussionRepo() stops short of DEFAULT_REPO and returns "" —
 * a forked adopter has no private twin, so an empty Discussion plane is a
 * legitimate answer, and inheriting the hard-coded slug would point a fork's
 * Discussion reads at our repo (the D#1870 hazard).
 *
 * Every case runs against a throwaway tree pointed at by AF_REPO_ROOT. Nothing
 * here reads the live .autonomous-team tree.
 *
 * Run: bun test tests/config/repo-planes.test.ts
 */

import { describe, it, expect, afterEach } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  DEFAULT_REPO,
  resolveRepo,
  resolveCodeRepo,
  resolveDiscussionRepo,
} from "../../src/config/repo.js";

const ORIGINAL_AF_REPO_ROOT = process.env["AF_REPO_ROOT"];
const ORIGINAL_GH_REPO = process.env["GH_REPO"];
const ORIGINAL_UNDERSCORE_REPO = process.env["_REPO"];

const created: string[] = [];

/** Point AF_REPO_ROOT at a fresh tree, optionally with a config.json. */
function fakeTree(config: Record<string, unknown> | null): void {
  const root = mkdtempSync(join(tmpdir(), "repo-planes-"));
  created.push(root);
  mkdirSync(join(root, ".autonomous-team"), { recursive: true });
  if (config !== null) {
    writeFileSync(
      join(root, ".autonomous-team", "config.json"),
      JSON.stringify(config),
    );
  }
  process.env["AF_REPO_ROOT"] = root;
  delete process.env["GH_REPO"];
  delete process.env["_REPO"];
}

function restore(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

afterEach(() => {
  restore("AF_REPO_ROOT", ORIGINAL_AF_REPO_ROOT);
  restore("GH_REPO", ORIGINAL_GH_REPO);
  restore("_REPO", ORIGINAL_UNDERSCORE_REPO);
  for (const dir of created.splice(0)) {
    rmSync(dir, { recursive: true, force: true });
  }
});

describe("both keys absent — the accessors are inert", () => {
  it("agrees with resolveRepo() when config.json only has repo", () => {
    fakeTree({ repo: "owner/only" });
    expect(resolveCodeRepo()).toBe(resolveRepo());
    expect(resolveDiscussionRepo()).toBe(resolveRepo());
    expect(resolveCodeRepo()).toBe("owner/only");
  });

  it("agrees with resolveRepo() when the slug comes from GH_REPO", () => {
    fakeTree(null);
    process.env["GH_REPO"] = "owner/from-env";
    expect(resolveCodeRepo()).toBe(resolveRepo());
    expect(resolveDiscussionRepo()).toBe(resolveRepo());
    expect(resolveDiscussionRepo()).toBe("owner/from-env");
  });

  it("falls back through _REPO the same way resolveRepo() does", () => {
    fakeTree(null);
    process.env["_REPO"] = "owner/underscore";
    expect(resolveCodeRepo()).toBe("owner/underscore");
    expect(resolveDiscussionRepo()).toBe("owner/underscore");
  });
});

describe("AUTONOMOUS_TEAM_REPO is not a lever here", () => {
  // backend/_repo.py treats AUTONOMOUS_TEAM_REPO as the highest-priority
  // override, so it is tempting to describe it as a system-wide revert. It is
  // not: this resolver has never read that variable at any precedence, and
  // config.json outranks the environment here anyway (frozen under D#1632).
  // Pinned executably so the claim cannot drift back into prose. The Python
  // half is backend/tests/test_repo_planes.py's precedence section.

  it("ignores AUTONOMOUS_TEAM_REPO entirely", () => {
    const saved = process.env["AUTONOMOUS_TEAM_REPO"];
    try {
      fakeTree({ repo: "owner/private", code_repo: "owner/public" });
      process.env["AUTONOMOUS_TEAM_REPO"] = "owner/kill-switch";
      expect(resolveCodeRepo()).toBe("owner/public");
      expect(resolveDiscussionRepo()).toBe("owner/private");
      expect(resolveRepo()).toBe("owner/private");
    } finally {
      restore("AUTONOMOUS_TEAM_REPO", saved);
    }
  });

  it("ignores it even with nothing else configured", () => {
    const saved = process.env["AUTONOMOUS_TEAM_REPO"];
    try {
      fakeTree(null);
      process.env["AUTONOMOUS_TEAM_REPO"] = "owner/kill-switch";
      // Falls all the way through to DEFAULT_REPO rather than the env var.
      expect(resolveCodeRepo()).toBe(DEFAULT_REPO);
      expect(resolveDiscussionRepo()).toBe("");
    } finally {
      restore("AUTONOMOUS_TEAM_REPO", saved);
    }
  });
});

describe("keys present — the two planes separate", () => {
  it("code_repo wins for the code plane", () => {
    fakeTree({ repo: "owner/private", code_repo: "owner/public" });
    expect(resolveCodeRepo()).toBe("owner/public");
  });

  it("discussion_repo wins for the Discussion plane", () => {
    fakeTree({ repo: "owner/public", discussion_repo: "owner/private" });
    expect(resolveDiscussionRepo()).toBe("owner/private");
  });

  it("resolves one config to two different answers", () => {
    fakeTree({
      repo: "owner/legacy",
      code_repo: "owner/public",
      discussion_repo: "owner/private",
    });
    expect(resolveCodeRepo()).toBe("owner/public");
    expect(resolveDiscussionRepo()).toBe("owner/private");
  });
});

describe("the asymmetry: empty is a valid Discussion plane", () => {
  it("returns the empty string rather than throwing when nothing resolves", () => {
    fakeTree(null);
    expect(resolveDiscussionRepo()).toBe("");
  });

  it("never inherits DEFAULT_REPO for the Discussion plane", () => {
    fakeTree(null);
    expect(resolveDiscussionRepo()).not.toBe(DEFAULT_REPO);
    expect(resolveDiscussionRepo()).toBe("");
  });

  it("still hands the code plane DEFAULT_REPO, since every checkout has one", () => {
    fakeTree(null);
    expect(resolveCodeRepo()).toBe(DEFAULT_REPO);
    expect(resolveCodeRepo()).toBe(resolveRepo());
  });
});

describe("unusable values mean 'not configured', not a crash", () => {
  it("ignores a non-string code_repo", () => {
    fakeTree({ repo: "owner/r", code_repo: 42 });
    expect(resolveCodeRepo()).toBe("owner/r");
  });

  it("ignores an empty-string discussion_repo", () => {
    fakeTree({ repo: "owner/r", discussion_repo: "" });
    expect(resolveDiscussionRepo()).toBe("owner/r");
  });

  it("survives malformed JSON", () => {
    const root = mkdtempSync(join(tmpdir(), "repo-planes-bad-"));
    created.push(root);
    mkdirSync(join(root, ".autonomous-team"), { recursive: true });
    writeFileSync(join(root, ".autonomous-team", "config.json"), "{not json");
    process.env["AF_REPO_ROOT"] = root;
    delete process.env["GH_REPO"];
    delete process.env["_REPO"];
    expect(resolveDiscussionRepo()).toBe("");
    expect(resolveCodeRepo()).toBe(DEFAULT_REPO);
  });
});
