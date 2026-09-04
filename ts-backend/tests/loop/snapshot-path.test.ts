/**
 * snapshot-path.test.ts — the TS resolver must agree with backend/snapshot_path.py.
 *
 * These two files are the only places the default snapshot location is spelled
 * out. If they ever disagree, the bash and TS step-5 implementations read
 * different files and the parity runs recorded in parity-history.jsonl diverge
 * for a reason that has nothing to do with the logic being compared.
 */

import { describe, expect, test, afterEach, beforeEach } from "bun:test";
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { join, dirname } from "node:path";

import {
  MAX_AGE_SECONDS,
  SNAPSHOT_FILENAME,
  isSnapshotStale,
  resolveSnapshotPath,
  snapshotAgeSeconds,
  stateDir,
} from "../../src/loop/snapshot-path.js";

const REPO_ROOT = join(dirname(new URL(import.meta.url).pathname), "..", "..", "..");

function pythonResolves(env: Record<string, string>): string {
  const r = spawnSync("python3", [join(REPO_ROOT, "backend", "snapshot_path.py")], {
    encoding: "utf-8",
    env: { ...process.env, ...env },
    timeout: 30_000,
  });
  return (r.stdout ?? "").trim();
}

function isoAgo(seconds: number): string {
  return new Date(Date.now() - seconds * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

describe("resolveSnapshotPath", () => {
  const saved = { ...process.env };

  beforeEach(() => {
    delete process.env["SNAPSHOT_PATH"];
    delete process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  });

  afterEach(() => {
    process.env = { ...saved };
  });

  test("defaults to the state dir, not /tmp", () => {
    const p = resolveSnapshotPath();
    expect(p).toBe(join(homedir(), ".fulcrumaxe-state", SNAPSHOT_FILENAME));
    expect(p.startsWith("/tmp/")).toBe(false);
  });

  test("honours AUTONOMOUS_TEAM_STATE_DIR", () => {
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = "/tmp/x1";
    expect(resolveSnapshotPath()).toBe("/tmp/x1/loop-snapshot.json");
    expect(stateDir()).toBe("/tmp/x1");
  });

  test("SNAPSHOT_PATH wins over the state dir", () => {
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = "/tmp/x1";
    process.env["SNAPSHOT_PATH"] = "/tmp/x2.json";
    expect(resolveSnapshotPath()).toBe("/tmp/x2.json");
  });
});

describe("parity with backend/snapshot_path.py", () => {
  const saved = { ...process.env };
  afterEach(() => {
    process.env = { ...saved };
  });

  test("default path matches Python", () => {
    delete process.env["SNAPSHOT_PATH"];
    delete process.env["AUTONOMOUS_TEAM_STATE_DIR"];
    expect(resolveSnapshotPath()).toBe(pythonResolves({}));
  });

  test("state-dir override matches Python", () => {
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = "/tmp/parity-state";
    delete process.env["SNAPSHOT_PATH"];
    expect(resolveSnapshotPath()).toBe(
      pythonResolves({ AUTONOMOUS_TEAM_STATE_DIR: "/tmp/parity-state" }),
    );
  });

  test("SNAPSHOT_PATH override matches Python", () => {
    process.env["SNAPSHOT_PATH"] = "/tmp/parity-explicit.json";
    expect(resolveSnapshotPath()).toBe(
      pythonResolves({ SNAPSHOT_PATH: "/tmp/parity-explicit.json" }),
    );
  });

  test("MAX_AGE_SECONDS matches Python", () => {
    const r = spawnSync(
      "python3",
      ["-c", "from backend.snapshot_path import MAX_AGE_SECONDS; print(MAX_AGE_SECONDS)"],
      { encoding: "utf-8", cwd: REPO_ROOT, timeout: 30_000 },
    );
    expect(Number((r.stdout ?? "").trim())).toBe(MAX_AGE_SECONDS);
  });
});

describe("staleness", () => {
  test("a fresh snapshot is not stale", () => {
    expect(isSnapshotStale({ generated_at: isoAgo(5) })).toBe(false);
  });

  test("a snapshot past MAX_AGE is stale", () => {
    expect(isSnapshotStale({ generated_at: isoAgo(MAX_AGE_SECONDS + 60) })).toBe(true);
  });

  test("a five-day-old snapshot is stale", () => {
    expect(isSnapshotStale({ generated_at: isoAgo(5 * 24 * 3600) })).toBe(true);
  });

  test("an undatable snapshot is treated as stale, not as fresh", () => {
    expect(snapshotAgeSeconds({})).toBeNull();
    expect(isSnapshotStale({})).toBe(true);
    expect(isSnapshotStale({ generated_at: "not-a-date" })).toBe(true);
  });

  test("legacy snapshot_at is accepted", () => {
    expect(isSnapshotStale({ snapshot_at: isoAgo(5) })).toBe(false);
  });
});
