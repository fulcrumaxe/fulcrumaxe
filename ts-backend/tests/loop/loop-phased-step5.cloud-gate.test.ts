/**
 * EB-10e: the TypeScript step-5 merge asks the cloud before it squash-merges.
 *
 * Every case runs against a throwaway engine tree (AF_REPO_ROOT) with its own state dir and a stub `gh`
 * on PATH that records its calls, so nothing touches a real checkout, state dir or repo.
 * Two kinds of hook: a stub (exit code under test control) and the real cloud-decide.sh, which proves
 * the halt file refuses and that an unreachable cloud fails closed.
 */
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { cloudMergeGate, cloudOperateOn, mergeGated } from "../../src/loop/loop-phased-step5.js";

const ENGINE = resolve(dirname(new URL(import.meta.url).pathname), "..", "..", "..");
const SHA = "abcdef0123456789abcdef0123456789abcdef01";
const KEYS = ["AF_REPO_ROOT", "PATH", "AUTONOMOUS_TEAM_STATE_DIR", "XDG_RUNTIME_DIR", "SPAWN_AGENT", "GH_MERGE", "STUB_LOG", "STUB_HEAD_SHA", "HOOK_RC", "HOOK_LOG"];
const MERGE_CALL = "pr " + "merge";

let saved: Record<string, string | undefined>;
let root: string;
let tmp: string;

function write(path: string, text: string, mode = 0o644): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, text);
  chmodSync(path, mode);
}

function config(operate: boolean | "absent"): void {
  const cfg = operate === "absent" ? { repo: "o/r" } : { repo: "o/r", cloud_bridge: { operate, repo_gh_id: 4242 } };
  write(join(root, ".autonomous-team", "config.json"), JSON.stringify(cfg));
}

function lines(path: string): string[] {
  return existsSync(path) ? readFileSync(path, "utf-8").split("\n").filter(Boolean) : [];
}
const calls = () => lines(process.env["STUB_LOG"]!);
const merges = () => calls().filter((c) => c.startsWith(MERGE_CALL));
const hookCalls = () => lines(process.env["HOOK_LOG"]!);

beforeEach(() => {
  saved = Object.fromEntries(KEYS.map((k) => [k, process.env[k]]));
  tmp = mkdtempSync(join(tmpdir(), "fx-eb10e-"));
  root = join(tmp, "tree");
  const state = join(tmp, "state");
  mkdirSync(state, { mode: 0o700 });
  // The shared helper is the real one; the hook is a stub unless a test installs the real tree.
  mkdirSync(join(root, "scripts", "lib"), { recursive: true });
  cpSync(join(ENGINE, "scripts", "lib", "cloud-merge-gate.sh"), join(root, "scripts", "lib", "cloud-merge-gate.sh"));
  write(join(root, "scripts", "engine-emit.sh"), '#!/usr/bin/env bash\necho "emit $*" >> "$HOOK_LOG"\n', 0o755);
  write(join(root, "scripts", "hooks", "pre-merge.d", "cloud-decide.sh"),
    '#!/usr/bin/env bash\necho "hook $*" >> "$HOOK_LOG"\nexit "${HOOK_RC:-0}"\n', 0o755);
  const bin = join(tmp, "bin");
  write(join(bin, "gh"),
    '#!/usr/bin/env bash\necho "$*" >> "$STUB_LOG"\nif [ "$1 $2" = "pr view" ]; then echo "$STUB_HEAD_SHA"; fi\nexit 0\n', 0o755);
  Object.assign(process.env, {
    AF_REPO_ROOT: root,
    PATH: `${bin}:${process.env["PATH"]}`,
    AUTONOMOUS_TEAM_STATE_DIR: state,
    STUB_LOG: join(tmp, "gh.log"),
    STUB_HEAD_SHA: SHA,
    HOOK_LOG: join(tmp, "hook.log"),
  });
  delete process.env["SPAWN_AGENT"];
  delete process.env["GH_MERGE"];
  delete process.env["HOOK_RC"];
  delete process.env["XDG_RUNTIME_DIR"];
  config(true);
});

afterEach(() => {
  for (const k of KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  rmSync(tmp, { recursive: true, force: true });
});

function installRealHook(): void {
  cpSync(join(ENGINE, "scripts", "lib"), join(root, "scripts", "lib"), { recursive: true });
  cpSync(join(ENGINE, "scripts", "hooks", "pre-merge.d", "cloud-decide.sh"), join(root, "scripts", "hooks", "pre-merge.d", "cloud-decide.sh"));
  write(join(root, "scripts", "rotate-team-log.sh"), "#!/usr/bin/env bash\nexit 0\n", 0o755);
}

describe("operate flag", () => {
  it("is on only when config.json says so", () => {
    config(true);
    expect(cloudOperateOn()).toBe(true);
    config(false);
    expect(cloudOperateOn()).toBe(false);
    config("absent");
    expect(cloudOperateOn()).toBe(false);
    rmSync(join(root, ".autonomous-team", "config.json"));
    expect(cloudOperateOn()).toBe(false);
  });
});

describe("mergeGated, hook exit mapped", () => {
  for (const rc of [0, 3, 4, 5, 1, 99]) {
    it(`hook exit ${rc}`, () => {
      process.env["HOOK_RC"] = String(rc);
      const out = mergeGated(77, 842);
      const go = rc === 0;
      expect(out).toBe(go ? 0 : rc === 3 || rc === 4 ? rc : 5);
      expect(merges().length).toBe(go ? 1 : 0);
      // a refusal says merge_lane refused; a go-ahead says nothing
      expect(hookCalls().some((l) => l.startsWith("emit merge_lane") && l.includes("--state refused"))).toBe(!go);
    });
  }

  it("decides on the live head and pins the merge to it", () => {
    expect(mergeGated(77, 842)).toBe(0);
    expect(hookCalls()[0]).toBe(`hook --pr 77 --head-sha ${SHA} --discussion 842`);
    expect(merges().length).toBe(1);
    expect(merges()[0]!.endsWith(`--match-head-commit ${SHA}`)).toBe(true);
  });

  it("an unreadable live head is passed on empty, never invented, and the hook refuses", () => {
    process.env["STUB_HEAD_SHA"] = "";
    process.env["HOOK_RC"] = "5";
    expect(mergeGated(77, 842)).toBe(5);
    expect(merges()).toEqual([]);
  });
});

describe("operate off or absent: no change, no cloud call", () => {
  for (const op of [false, "absent"] as const) {
    it(`operate ${op}`, () => {
      config(op);
      installRealHook(); // the real hook with operate off must exit 0 without touching the socket
      process.env["XDG_RUNTIME_DIR"] = join(tmp, "run"); // no socket there: asking would fail closed
      expect(mergeGated(77, 842)).toBe(0);
      expect(merges().length).toBe(1);
      expect(merges()[0]).toMatch(/^pr merge 77 --squash --delete-branch --repo \S+$/);
      expect(calls().some((c) => c.includes("headRefOid"))).toBe(false);
      expect(hookCalls().filter((l) => l.startsWith("emit"))).toEqual([]);
    });
  }

  it("a missing hook with operate off is not an error", () => {
    config(false);
    rmSync(join(root, "scripts", "hooks"), { recursive: true });
    expect(cloudMergeGate(77, SHA, 842)).toBe(0);
  });
});

describe("fail closed", () => {
  it("hook missing with operate on refuses", () => {
    rmSync(join(root, "scripts", "hooks"), { recursive: true });
    expect(mergeGated(77, 842)).toBe(5);
    expect(merges()).toEqual([]);
  });

  it("helper missing with operate on refuses; with operate off it does not", () => {
    rmSync(join(root, "scripts", "lib", "cloud-merge-gate.sh"));
    expect(cloudMergeGate(77, SHA, 842)).toBe(5);
    config(false);
    expect(cloudMergeGate(77, SHA, 842)).toBe(0);
  });
});

describe("real hook", () => {
  beforeEach(installRealHook);

  it("the halt file refuses before the cloud is asked", () => {
    write(join(process.env["AUTONOMOUS_TEAM_STATE_DIR"]!, "engine-halt.json"), JSON.stringify({ halted: true, halt_epoch: 3 }), 0o600);
    expect(mergeGated(77, 842)).toBe(4);
    expect(merges()).toEqual([]);
  });

  it("an unreachable cloud fails closed", () => {
    process.env["XDG_RUNTIME_DIR"] = join(tmp, "run"); // no socket there
    expect(mergeGated(77, 842)).toBe(5);
    expect(merges()).toEqual([]);
  });
});
