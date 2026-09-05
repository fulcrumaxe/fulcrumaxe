/**
 * tests/config/repo-plane-argv.test.ts
 *
 * ts-backend's code-plane retarget, proved where it matters: in the argv `gh`
 * is actually called with.
 *
 * tests/config/repo-planes.test.ts already covers the resolvers — that
 * resolveCodeRepo() follows "code_repo" and resolveDiscussionRepo() does not.
 * That is necessary and not sufficient. A module can import the right function
 * and still hand `gh` the other value, and no resolver test can see it. So this
 * one runs a real module in a subprocess against a `gh` shim that records its
 * argv, and reads the recording.
 *
 * A subprocess rather than an in-process import because the slug is a
 * module-level constant: it is bound once, at import time, from AF_REPO_ROOT.
 * Setting the environment after the module is loaded proves nothing.
 *
 * WHY THERE IS NO "ZERO INVOCATIONS ON AN UNRESOLVED PLANE" CASE HERE
 *
 * The bash half needs one, because `gh --repo ""` is not an error — gh exits 0
 * after resolving from the checkout's own remote — so an empty slug has to stop
 * the caller before gh runs. TypeScript cannot reach that state:
 * resolveCodeRepo() falls through resolveRepo() to DEFAULT_REPO, a constant. So
 * the guard the bash sites needed has no counterpart here, and a test asserting
 * zero invocations would pass for a reason unrelated to anything this change
 * did. The property that actually replaces it is asserted below instead: the
 * code plane is never empty, whatever the configuration.
 *
 * Run: bun test tests/config/repo-plane-argv.test.ts
 */

import { describe, it, expect, afterEach } from "bun:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { resolveCodeRepo, resolveRepo } from "../../src/config/repo.js";

const _DIR = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(_DIR, "..", "..", "src");

const PRIVATE = "owner/private-twin";
const SCRATCH = "scratch-org/scratch-code-repo";

const tmpDirs: string[] = [];
const ORIGINAL = {
  AF_REPO_ROOT: process.env["AF_REPO_ROOT"],
  GH_REPO: process.env["GH_REPO"],
  _REPO: process.env["_REPO"],
};

afterEach(() => {
  for (const [k, v] of Object.entries(ORIGINAL)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  while (tmpDirs.length) rmSync(tmpDirs.pop()!, { recursive: true, force: true });
});

/** A repo root carrying config.json, plus a `gh` on PATH that logs its argv. */
function scratchTree(config: Record<string, string>): {
  root: string;
  bindir: string;
  log: string;
} {
  const root = mkdtempSync(join(tmpdir(), "plane-argv-"));
  tmpDirs.push(root);
  mkdirSync(join(root, ".autonomous-team"), { recursive: true });
  writeFileSync(
    join(root, ".autonomous-team", "config.json"),
    JSON.stringify(config)
  );

  const bindir = join(root, "shimbin");
  mkdirSync(bindir, { recursive: true });
  const log = join(root, "gh.log");
  const shim = join(bindir, "gh");
  // Exits 0 and prints an empty JSON array: a shim that failed would let a
  // broken call site pass this test for the wrong reason.
  writeFileSync(
    shim,
    `#!/usr/bin/env bash\nprintf '%s\\n' "$*" >> ${log}\necho '[]'\nexit 0\n`
  );
  chmodSync(shim, 0o755);
  return { root, bindir, log };
}

function runDora(config: Record<string, string>): string[] {
  const { root, bindir, log } = scratchTree(config);

  // computeCfr() returns "n/a" without querying anything when there are no
  // releases, so without this the Discussion-plane assertion below would pass
  // by observing zero calls — the vacuous shape this whole workstream keeps
  // finding. One release file is enough to reach the query.
  const releases = join(root, ".autonomous-team", "releases");
  mkdirSync(releases, { recursive: true });
  writeFileSync(
    join(releases, "rel-1.json"),
    JSON.stringify({
      id: "rel-1",
      pr_numbers: [1],
      merged_at: new Date().toISOString(),
      created_at: new Date().toISOString(),
      title: "t",
    })
  );
  const driver = join(root, "driver.ts");
  writeFileSync(
    driver,
    `import { handleDora } from ${JSON.stringify(join(SRC, "rpc", "stats-dora.ts"))};\n` +
      `await handleDora({});\n`
  );
  const r = spawnSync("bun", [driver], {
    encoding: "utf-8",
    timeout: 60_000,
    env: {
      ...process.env,
      AF_REPO_ROOT: root,
      AUTONOMOUS_TEAM_DIR: join(root, ".autonomous-team"),
      PATH: `${bindir}:${process.env["PATH"] ?? ""}`,
      GH_REPO: "",
      _REPO: "",
    },
  });
  if (r.status !== 0) {
    throw new Error(
      `driver failed (${r.status}): ${r.stderr?.slice(-1500)}`
    );
  }
  return existsSync(log)
    ? readFileSync(log, "utf-8").split("\n").filter(Boolean)
    : [];
}

describe("the code plane reaches gh's argv", () => {
  it("is a no-op with code_repo unset — the slug is what it always was", () => {
    const calls = runDora({ repo: PRIVATE });
    const prList = calls.filter((c) => c.startsWith("pr list"));
    expect(prList.length).toBeGreaterThan(0);
    for (const c of prList) expect(c).toContain(PRIVATE);
  });

  it("follows code_repo when it is set", () => {
    const calls = runDora({ repo: PRIVATE, code_repo: SCRATCH });
    const prList = calls.filter((c) => c.startsWith("pr list"));
    expect(prList.length).toBeGreaterThan(0);
    for (const c of prList) {
      expect(c).toContain(SCRATCH);
      expect(c).not.toContain(PRIVATE);
    }
  });

  it("leaves the discussions query on the Discussion plane", () => {
    // Both halves, or this proves only that something moved. An over-broad
    // substitution would drag the bug-Discussion query onto the code plane and
    // still pass the assertion above.
    const calls = runDora({ repo: PRIVATE, code_repo: SCRATCH });
    const graphql = calls.filter((c) => c.includes("discussions("));
    expect(graphql.length).toBeGreaterThan(0);
    for (const c of graphql) {
      expect(c).toContain(`owner:"${PRIVATE.split("/")[0]}"`);
      expect(c).not.toContain(SCRATCH.split("/")[0]);
    }
  });
});

describe("the code plane is never empty", () => {
  // This is the property that stands in for the bash side's
  // _require_code_repo guard. Stated as a test rather than as a sentence,
  // because the absence of a guard is otherwise indistinguishable from an
  // oversight.
  it("resolves to something even with nothing configured at all", () => {
    const root = mkdtempSync(join(tmpdir(), "plane-empty-"));
    tmpDirs.push(root);
    process.env["AF_REPO_ROOT"] = root;
    delete process.env["GH_REPO"];
    delete process.env["_REPO"];
    expect(resolveCodeRepo()).not.toBe("");
    expect(resolveCodeRepo()).toBe(resolveRepo());
  });
});
