/**
 * loop-phased-step5-snapshot.test.ts — the TS port must ignore a stale snapshot.
 *
 * The snapshot is a cache in front of the live Discussions query. Reading it
 * without an age check lets a days-old file decide which Discussion gets an
 * executor. Two directions matter equally:
 *
 *   - stale snapshot  -> ignored, falls through to GraphQL
 *   - fresh snapshot  -> fast path, no GraphQL subprocess
 *
 * Without the second test, "ignore stale snapshots" could be satisfied by
 * deleting the optimisation outright, which would be a different bug.
 *
 * GraphQL detection: a fake `gh` placed first on PATH that appends its argv to a
 * log file. No network is touched either way. (Deliberately NOT mock.module —
 * bun's module registry is process-wide, so mocking node:child_process here
 * would break every other suite in the same `bun test` run.)
 */

import { describe, expect, test, afterEach, beforeEach } from "bun:test";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { _getSpecReadyDiscussions } from "../../src/loop/loop-phased-step5.js";

const SPEC_READY_BODY = "<!-- STATUS:SPEC_READY --> some spec content";

function isoAgo(seconds: number): string {
  return new Date(Date.now() - seconds * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

let dir: string;
let ghLog: string;
const savedPath = process.env["PATH"];
const savedSnapshotPath = process.env["SNAPSHOT_PATH"];

function writeSnapshot(ageSeconds: number, num: number): string {
  const p = join(dir, "loop-snapshot.json");
  writeFileSync(
    p,
    JSON.stringify({
      generated_at: isoAgo(ageSeconds),
      discussions: [{ number: num, title: `Feature ${num}`, body: SPEC_READY_BODY }],
    }),
  );
  return p;
}

/** Put a fake `gh` first on PATH; it logs its argv and returns an empty result. */
function installGhShim(): void {
  ghLog = join(dir, "gh-log");
  writeFileSync(ghLog, "");

  const shimDir = join(dir, "shim");
  mkdirSync(shimDir, { recursive: true });

  const gh = join(shimDir, "gh");
  writeFileSync(
    gh,
    `#!/usr/bin/env bash\necho "$*" >> "${ghLog}"\necho '{"data":{"repository":{"discussions":{"nodes":[]}}}}'\n`,
  );
  chmodSync(gh, 0o755);

  process.env["PATH"] = `${shimDir}:${savedPath}`;
}

/** True when a discussions GraphQL query was actually sent to `gh`. */
function graphqlQueried(): boolean {
  return existsSync(ghLog) && readFileSync(ghLog, "utf-8").includes("discussions");
}

describe("_getSpecReadyDiscussions snapshot freshness", () => {
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "step5-snap-"));
    delete process.env["SPEC_READY_MOCK"];
    installGhShim();
  });

  afterEach(() => {
    if (savedPath === undefined) delete process.env["PATH"];
    else process.env["PATH"] = savedPath;
    if (savedSnapshotPath === undefined) delete process.env["SNAPSHOT_PATH"];
    else process.env["SNAPSHOT_PATH"] = savedSnapshotPath;
    rmSync(dir, { recursive: true, force: true });
  });

  test("the gh shim is actually reachable (guards the assertions below)", () => {
    // If this fails, every "no GraphQL fired" assertion here is vacuous.
    process.env["SNAPSHOT_PATH"] = join(dir, "nope.json");
    _getSpecReadyDiscussions();
    expect(graphqlQueried()).toBe(true);
  });

  test("fresh snapshot: fast path, no GraphQL subprocess", () => {
    process.env["SNAPSHOT_PATH"] = writeSnapshot(30, 90126);

    const result = _getSpecReadyDiscussions();

    expect(result).toEqual([{ number: 90126, title: "Feature 90126" }]);
    expect(graphqlQueried()).toBe(false);
  });

  test("stale snapshot: ignored, falls through to GraphQL", () => {
    process.env["SNAPSHOT_PATH"] = writeSnapshot(5 * 24 * 3600, 90125);

    const result = _getSpecReadyDiscussions();

    // The five-day-old discussion must not be routed...
    expect(result.find((d) => d.number === 90125)).toBeUndefined();
    // ...and the live query must have been made instead.
    expect(graphqlQueried()).toBe(true);
  });

  test("snapshot just past MAX_AGE is already ignored", () => {
    process.env["SNAPSHOT_PATH"] = writeSnapshot(601, 90127);

    const result = _getSpecReadyDiscussions();

    expect(result.find((d) => d.number === 90127)).toBeUndefined();
    expect(graphqlQueried()).toBe(true);
  });

  test("snapshot just inside MAX_AGE still takes the fast path", () => {
    process.env["SNAPSHOT_PATH"] = writeSnapshot(500, 90128);

    const result = _getSpecReadyDiscussions();

    expect(result).toEqual([{ number: 90128, title: "Feature 90128" }]);
    expect(graphqlQueried()).toBe(false);
  });

  test("snapshot with no generated_at is treated as stale", () => {
    const p = join(dir, "loop-snapshot.json");
    writeFileSync(
      p,
      JSON.stringify({
        discussions: [{ number: 90129, title: "Undatable", body: SPEC_READY_BODY }],
      }),
    );
    process.env["SNAPSHOT_PATH"] = p;

    const result = _getSpecReadyDiscussions();

    expect(result.find((d) => d.number === 90129)).toBeUndefined();
    expect(graphqlQueried()).toBe(true);
  });
});
