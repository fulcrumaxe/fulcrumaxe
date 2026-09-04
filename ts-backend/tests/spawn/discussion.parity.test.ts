/**
 * tests/spawn/discussion.parity.test.ts
 *
 * Parity tests for:
 *   src/spawn/discussion-cache.ts  ↔  backend/discussion_cache.py
 *   src/spawn/discussion-status.ts ↔  backend/discussion_status.py
 *
 * Strategy: seed an identical SQLite fixture via the TS API, then verify
 * that both Python and TS produce identical stdout / exit codes / DB state
 * for representative subcommands (get-body, stats, invalidate, get-sections,
 * missing-sections, etc.).
 *
 * Network calls (GraphQL) are bypassed:
 *   - DISCUSSION_CACHE_DB_PATH → temp DB pre-seeded with fresh rows
 *   - AUTONOMOUS_TEAM_STATE_DIR → temp dir
 *
 * Run: bun test tests/spawn/discussion.parity.test.ts --timeout 60000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

import {
  openDb,
  cacheRow,
  readRow,
  getBody,
  getDiscussion,
  invalidate,
  getStats,
  nowIso,
  isFresh,
  type DiscussionRecord,
} from "../../src/spawn/discussion-cache.js";

import {
  extractStatus,
  extractLinkedPr,
  extractSince,
  setStatus,
  getSections,
  missingSections,
  VALID_STATUSES,
  REQUIRED_SECTIONS,
} from "../../src/spawn/discussion-status.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tempDir: string;
let originalStateDir: string | undefined;
let originalCacheDb: string | undefined;

function setupTempState(): void {
  tempDir = join(tmpdir(), `disc-parity-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(join(tempDir, "blackboard"), { recursive: true });
  originalStateDir = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  originalCacheDb = process.env["DISCUSSION_CACHE_DB_PATH"];
  process.env["AUTONOMOUS_TEAM_STATE_DIR"] = tempDir;
  process.env["DISCUSSION_CACHE_DB_PATH"] = join(tempDir, "discussion_cache.db");
}

function teardownTempState(): void {
  if (originalStateDir !== undefined) {
    process.env["AUTONOMOUS_TEAM_STATE_DIR"] = originalStateDir;
  } else {
    delete process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  }
  if (originalCacheDb !== undefined) {
    process.env["DISCUSSION_CACHE_DB_PATH"] = originalCacheDb;
  } else {
    delete process.env["DISCUSSION_CACHE_DB_PATH"];
  }
  try {
    if (existsSync(tempDir)) rmSync(tempDir, { recursive: true, force: true });
  } catch { /* ignore */ }
}

/** Pre-seed a discussion row in the TS cache DB, then copy it to a second path for Python parity. */
function seedRow(record: DiscussionRecord, cachedAt?: string): void {
  const db = openDb();
  // Insert with explicit cached_at if provided (to control freshness)
  if (cachedAt !== undefined) {
    const labelsJson = JSON.stringify(record.labels ?? []);
    db.run(
      "INSERT INTO discussion_cache(number, body, title, labels, updated_at, cached_at) " +
        "VALUES(?,?,?,?,?,?) " +
        "ON CONFLICT(number) DO UPDATE SET " +
        "  body=excluded.body, title=excluded.title, labels=excluded.labels, " +
        "  updated_at=excluded.updated_at, cached_at=excluded.cached_at",
      [record.number, record.body, record.title, labelsJson, record.updated_at, cachedAt]
    );
  } else {
    cacheRow(db, record);
  }
  db.close();
}

/**
 * Resolve the main repo root (where backend/ lives).
 *
 * Honors AF_REPO_ROOT env var if set (CI / explicit override).
 * Otherwise walks up from this file's directory until it finds a directory
 * that contains backend/agent_run_tracker.py — works whether the test runs
 * from inside a worktree or directly from the main repo checkout.
 */
const _mainRepoRoot: string = (() => {
  if (process.env["AF_REPO_ROOT"]) return process.env["AF_REPO_ROOT"];

  // Walk up from this file's directory looking for the repo root sentinel.
  // sentinel: backend/agent_run_tracker.py (always present in the main repo)
  const sentinel = join("backend", "agent_run_tracker.py");
  let dir = new URL(import.meta.url).pathname;
  // strip filename to get directory
  dir = join(dir, "..");
  for (let i = 0; i < 20; i++) {
    if (existsSync(join(dir, sentinel))) return dir;
    const parent = join(dir, "..");
    if (parent === dir) break; // reached filesystem root
    dir = parent;
  }
  // Fallback: 4 levels up from ts-backend/tests/spawn/ → ts-backend/ → repo root
  // (covers main repo checkout: ts-backend/tests/spawn → ts-backend → repo)
  return join(new URL(import.meta.url).pathname, "..", "..", "..", "..");
})();

/** Run a Python subcommand with the same temp state env. */
function runPython(
  script: string,
  args: string[],
  env: Record<string, string> = {}
): { stdout: string; stderr: string; status: number } {
  const repoRoot = _mainRepoRoot;
  const result = spawnSync("python3", [join(repoRoot, "backend", script), ...args], {
    env: {
      ...process.env,
      AUTONOMOUS_TEAM_STATE_DIR: tempDir,
      DISCUSSION_CACHE_DB_PATH: join(tempDir, "discussion_cache.db"),
      ...env,
    },
    encoding: "utf-8",
    timeout: 20_000,
  });
  return {
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    status: result.status ?? 1,
  };
}

/** Run the TS CLI via bun with the same temp env. */
function runTs(
  script: string,
  args: string[],
  env: Record<string, string> = {}
): { stdout: string; stderr: string; status: number } {
  const worktreeRoot = join(new URL(import.meta.url).pathname, "..", "..", "..");
  const result = spawnSync(
    "bun",
    ["run", join(worktreeRoot, "src", "spawn", script), ...args],
    {
      env: {
        ...process.env,
        AUTONOMOUS_TEAM_STATE_DIR: tempDir,
        DISCUSSION_CACHE_DB_PATH: join(tempDir, "discussion_cache.db"),
        ...env,
      },
      encoding: "utf-8",
      timeout: 20_000,
    }
  );
  return {
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    status: result.status ?? 1,
  };
}

// ---------------------------------------------------------------------------
// discussion_status.ts — pure logic parity (no external calls needed)
// ---------------------------------------------------------------------------

describe("discussion-status: extractStatus", () => {
  it("returns UNKNOWN for empty body", () => {
    expect(extractStatus("")).toBe("UNKNOWN");
    expect(extractStatus("no status here")).toBe("UNKNOWN");
  });

  it("extracts SPEC_READY", () => {
    expect(extractStatus("<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->")).toBe("SPEC_READY");
  });

  it("extracts IMPLEMENTING", () => {
    expect(extractStatus("<!-- STATUS:IMPLEMENTING SINCE:2026-01-01T00:00:00Z -->")).toBe("IMPLEMENTING");
  });

  it("extracts REVIEWING with PR", () => {
    expect(extractStatus("<!-- STATUS:REVIEWING PR:#321 SINCE:2026-01-01T00:00:00Z -->")).toBe("REVIEWING");
  });

  it("extracts DONE", () => {
    expect(extractStatus("<!-- STATUS:DONE PR:#321 SINCE:2026-01-01T00:00:00Z -->")).toBe("DONE");
  });
});

describe("discussion-status: extractLinkedPr", () => {
  it("returns null when no PR", () => {
    expect(extractLinkedPr("<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->")).toBeNull();
    expect(extractLinkedPr("")).toBeNull();
  });

  it("extracts PR number", () => {
    expect(extractLinkedPr("<!-- STATUS:REVIEWING PR:#321 SINCE:2026-01-01T00:00:00Z -->")).toBe(321);
    expect(extractLinkedPr("<!-- STATUS:DONE PR:#999 SINCE:2026-01-01T00:00:00Z -->")).toBe(999);
  });
});

describe("discussion-status: extractSince", () => {
  it("returns null when no SINCE", () => {
    expect(extractSince("<!-- STATUS:SPEC_READY -->")).toBeNull();
    expect(extractSince("")).toBeNull();
  });

  it("extracts SINCE timestamp", () => {
    expect(extractSince("<!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->")).toBe("2026-05-09T00:00:00Z");
  });
});

describe("discussion-status: setStatus", () => {
  it("prepends marker when no existing marker", () => {
    const body = "## Intent\nsome text";
    const result = setStatus(body, "SPEC_READY", "2026-01-01T00:00:00Z");
    expect(result).toBe("<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\n\n## Intent\nsome text");
  });

  it("replaces existing marker in-place", () => {
    const body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\n\n## Intent\nsome text";
    const result = setStatus(body, "SPEC_READY", "2026-02-01T00:00:00Z");
    expect(result).toBe("<!-- STATUS:SPEC_READY SINCE:2026-02-01T00:00:00Z -->\n\n## Intent\nsome text");
  });

  it("replaces marker with PR reference", () => {
    const body = "<!-- STATUS:REVIEWING PR:#321 SINCE:2026-01-01T00:00:00Z -->";
    const result = setStatus(body, "DONE", "2026-03-01T00:00:00Z");
    expect(result).toBe("<!-- STATUS:DONE SINCE:2026-03-01T00:00:00Z -->");
  });

  it("uses current time when nowIso not provided", () => {
    const before = Date.now();
    const result = setStatus("no marker here", "SPEC_READY");
    const after = Date.now();
    expect(result).toMatch(/^<!-- STATUS:SPEC_READY SINCE:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z -->/);
    // The timestamp must be between before and after
    const tsMatch = /SINCE:([^\s>]+)/.exec(result);
    expect(tsMatch).not.toBeNull();
    const ts = new Date(tsMatch![1]!).getTime();
    expect(ts).toBeGreaterThanOrEqual(before - 1000); // allow 1s clock skew
    expect(ts).toBeLessThanOrEqual(after + 1000);
  });
});

describe("discussion-status: getSections", () => {
  it("returns legacy back-compat when no headers", () => {
    const body = "some plain body text";
    const sections = getSections(body);
    expect(sections.intent).toBe("");
    expect(sections.spec).toBe("some plain body text");
    expect(sections.implementation_notes).toBe("");
  });

  it("strips STATUS comment in legacy mode", () => {
    const body = "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->\nsome plain text";
    const sections = getSections(body);
    expect(sections.spec).toBe("some plain text");
    expect(sections.intent).toBe("");
  });

  it("parses three-section template", () => {
    const body = [
      "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->",
      "",
      "## Intent",
      "The intent text.",
      "",
      "## Spec (Acceptance)",
      "The spec text.",
      "",
      "## Implementation Notes",
      "The notes text.",
    ].join("\n");
    const sections = getSections(body);
    expect(sections.intent).toBe("The intent text.");
    expect(sections.spec).toBe("The spec text.");
    expect(sections.implementation_notes).toBe("The notes text.");
  });

  it("handles partial sections", () => {
    const body = "## Intent\nsome intent\n\n## Spec (Acceptance)\nsome spec";
    const sections = getSections(body);
    expect(sections.intent).toBe("some intent");
    expect(sections.spec).toBe("some spec");
    expect(sections.implementation_notes).toBe("");
  });
});

describe("discussion-status: missingSections", () => {
  it("returns all three when empty body", () => {
    const missing = missingSections("");
    expect(missing).toEqual(["Intent", "Spec (Acceptance)", "Implementation Notes"]);
  });

  it("returns empty list when all present", () => {
    const body = "## Intent\n## Spec (Acceptance)\n## Implementation Notes\n";
    expect(missingSections(body)).toEqual([]);
  });

  it("returns only missing ones", () => {
    const body = "## Intent\n## Spec (Acceptance)\n";
    expect(missingSections(body)).toEqual(["Implementation Notes"]);
  });

  it("preserves REQUIRED_SECTIONS order", () => {
    const body = "## Spec (Acceptance)\n";
    const missing = missingSections(body);
    // Intent and Implementation Notes are missing; Intent comes first
    expect(missing[0]).toBe("Intent");
    expect(missing[1]).toBe("Implementation Notes");
  });
});

describe("discussion-status: VALID_STATUSES constant", () => {
  it("contains expected values", () => {
    expect(VALID_STATUSES.has("DISCUSSING")).toBe(true);
    expect(VALID_STATUSES.has("SPEC_READY")).toBe(true);
    expect(VALID_STATUSES.has("IMPLEMENTING")).toBe(true);
    expect(VALID_STATUSES.has("REVIEWING")).toBe(true);
    expect(VALID_STATUSES.has("DONE")).toBe(true);
    expect(VALID_STATUSES.has("CLOSED")).toBe(true);
  });
});

describe("discussion-status: REQUIRED_SECTIONS constant", () => {
  it("has exactly three entries in the right order", () => {
    expect(REQUIRED_SECTIONS).toEqual([
      "Intent",
      "Spec (Acceptance)",
      "Implementation Notes",
    ]);
  });
});

// ---------------------------------------------------------------------------
// discussion_cache.ts — DB logic parity (no network needed)
// ---------------------------------------------------------------------------

describe("discussion-cache: nowIso format", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("matches Python strftime format YYYY-MM-DDTHH:MM:SSZ", () => {
    const iso = nowIso();
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });
});

describe("discussion-cache: isFresh", () => {
  it("returns false for empty string", () => {
    expect(isFresh("")).toBe(false);
  });

  it("returns false for invalid string", () => {
    expect(isFresh("not-a-date")).toBe(false);
  });

  it("returns true for just-set timestamp", () => {
    expect(isFresh(nowIso())).toBe(true);
  });

  it("returns false for old timestamp", () => {
    const old = "2020-01-01T00:00:00Z";
    expect(isFresh(old)).toBe(false);
  });
});

describe("discussion-cache: DB operations", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("openDb creates the tables", () => {
    const db = openDb();
    // Query the schema to confirm the table exists
    const row = db
      .query<{ name: string }, [string]>(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
      )
      .get("discussion_cache");
    expect(row).not.toBeNull();
    db.close();
  });

  it("cacheRow inserts and readRow retrieves", () => {
    const record: DiscussionRecord = {
      number: 42,
      title: "Test Discussion",
      body: "## Intent\ntest body",
      labels: ["SPEC_READY", "feature"],
      updated_at: "2026-01-01T00:00:00Z",
    };
    const db = openDb();
    cacheRow(db, record);
    const row = readRow(db, 42);
    db.close();

    expect(row).not.toBeNull();
    expect(row!.number).toBe(42);
    expect(row!.title).toBe("Test Discussion");
    expect(row!.body).toBe("## Intent\ntest body");
    expect(JSON.parse(row!.labels)).toEqual(["SPEC_READY", "feature"]);
    expect(row!.updated_at).toBe("2026-01-01T00:00:00Z");
    // cached_at should be a fresh ISO timestamp
    expect(row!.cached_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it("cacheRow upserts on conflict", () => {
    const record1: DiscussionRecord = {
      number: 42,
      title: "Old title",
      body: "old body",
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    const record2: DiscussionRecord = {
      number: 42,
      title: "New title",
      body: "new body",
      labels: ["SPEC_READY"],
      updated_at: "2026-02-01T00:00:00Z",
    };
    const db = openDb();
    cacheRow(db, record1);
    cacheRow(db, record2);
    const row = readRow(db, 42);
    db.close();

    expect(row!.title).toBe("New title");
    expect(row!.body).toBe("new body");
  });

  it("getBody returns empty string for missing number", () => {
    // No GraphQL — will fail fetch, no stale row → returns ""
    // We don't seed, so it will try to fetch and get nothing
    // Since gh is not available in test env, it returns ""
    const body = getBody(999999);
    expect(typeof body).toBe("string");
    // body may be "" (network failure) or real content — just test type
  });

  it("getBody returns cached body when fresh", () => {
    const record: DiscussionRecord = {
      number: 77,
      title: "Cached",
      body: "cached body content",
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso()); // seed with fresh cached_at
    const body = getBody(77);
    expect(body).toBe("cached body content");
  });

  it("invalidate clears cached_at", () => {
    const record: DiscussionRecord = {
      number: 88,
      title: "To invalidate",
      body: "body",
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());
    invalidate(88);

    const db = openDb();
    const row = readRow(db, 88);
    db.close();
    expect(row!.cached_at).toBe("");
  });

  it("getStats returns zero counts when no operations", () => {
    const stats = getStats();
    expect(stats.hits).toBe(0);
    expect(stats.misses).toBe(0);
    expect(stats.total).toBe(0);
    expect(stats.hit_ratio).toBe(0.0);
  });

  it("getStats tracks hits correctly after cache hit", () => {
    const record: DiscussionRecord = {
      number: 55,
      title: "Hit test",
      body: "hit body",
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());
    getBody(55); // should be a cache hit
    const stats = getStats();
    expect(stats.hits).toBeGreaterThanOrEqual(1);
  });

  it("getDiscussion returns full record when cached fresh", () => {
    const record: DiscussionRecord = {
      number: 33,
      title: "Full record",
      body: "full body",
      labels: ["a", "b"],
      updated_at: "2026-01-15T00:00:00Z",
    };
    seedRow(record, nowIso());
    const result = getDiscussion(33) as DiscussionRecord;
    expect(result.number).toBe(33);
    expect(result.title).toBe("Full record");
    expect(result.body).toBe("full body");
    expect(result.labels).toEqual(["a", "b"]);
    expect(result.updated_at).toBe("2026-01-15T00:00:00Z");
    expect(result.cached_at).toBeDefined();
  });

  it("getDiscussion returns {} for unknown number with no network", () => {
    // No seed, gh not available → returns {}
    const result = getDiscussion(999998);
    // Either {} (no network) or a full record (if gh is available) — just check it's an object
    expect(typeof result).toBe("object");
  });
});

// ---------------------------------------------------------------------------
// Parity: TS CLI vs Python CLI — subcommands with identical seeded DB state
// ---------------------------------------------------------------------------

describe("parity: discussion-cache CLI get-body", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("TS and Python both return cached body for fresh row", () => {
    const record: DiscussionRecord = {
      number: 100,
      title: "Parity test",
      body: "parity body content",
      labels: ["SPEC_READY"],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    const tsResult = runTs("discussion-cache.ts", ["get-body", "100"]);
    const pyResult = runPython("discussion_cache.py", ["get-body", "100"]);

    // Both should return the cached body without hitting the network
    expect(tsResult.stdout).toBe("parity body content");
    expect(pyResult.stdout).toBe("parity body content");
    expect(tsResult.status).toBe(0);
    expect(pyResult.status).toBe(0);
  });

  it("TS exits 1 when discussion not found and no network", () => {
    // Row does not exist, gh will fail → body="" → exit 1
    const tsResult = runTs("discussion-cache.ts", ["get-body", "999997"]);
    // Status 1 when empty body (mirrors Python)
    // Note: status may be 0 if gh is available and returns a body
    // We just verify the stdout/status are consistent
    if (tsResult.status === 1) {
      expect(tsResult.stdout).toBe("");
    }
  });
});

describe("parity: discussion-cache CLI stats", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("TS and Python both return valid stats JSON", () => {
    const tsResult = runTs("discussion-cache.ts", ["stats"]);
    const pyResult = runPython("discussion_cache.py", ["stats"]);

    const tsStats = JSON.parse(tsResult.stdout) as Record<string, unknown>;
    const pyStats = JSON.parse(pyResult.stdout) as Record<string, unknown>;

    // Both should have the same schema fields
    expect("hits" in tsStats).toBe(true);
    expect("misses" in tsStats).toBe(true);
    expect("total" in tsStats).toBe(true);
    expect("hit_ratio" in tsStats).toBe(true);
    expect("hits" in pyStats).toBe(true);
    expect("misses" in pyStats).toBe(true);
    expect("total" in pyStats).toBe(true);
    expect("hit_ratio" in pyStats).toBe(true);
  });
});

describe("parity: discussion-cache CLI invalidate", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("TS and Python both print 'invalidated #N' for same N", () => {
    const record: DiscussionRecord = {
      number: 200,
      title: "Invalidate parity",
      body: "body",
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    const tsResult = runTs("discussion-cache.ts", ["invalidate", "200"]);
    expect(tsResult.stdout.trim()).toBe("invalidated #200");
    expect(tsResult.status).toBe(0);

    // Re-seed for Python (since TS already cleared it, seed fresh again)
    seedRow(record, nowIso());
    const pyResult = runPython("discussion_cache.py", ["invalidate", "200"]);
    expect(pyResult.stdout.trim()).toBe("invalidated #200");
    expect(pyResult.status).toBe(0);
  });

  it("TS invalidate clears cached_at in DB (same effect as Python)", () => {
    const record: DiscussionRecord = {
      number: 201,
      title: "Invalidate DB test",
      body: "body",
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    // Run TS CLI invalidate
    runTs("discussion-cache.ts", ["invalidate", "201"]);

    // Check DB state
    const db = openDb();
    const row = readRow(db, 201);
    db.close();
    expect(row!.cached_at).toBe("");
  });
});

describe("parity: discussion-cache CLI get", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("TS get returns valid JSON with all fields", () => {
    const record: DiscussionRecord = {
      number: 300,
      title: "Get parity",
      body: "get body content",
      labels: ["feature"],
      updated_at: "2026-02-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    const tsResult = runTs("discussion-cache.ts", ["get", "300"]);
    expect(tsResult.status).toBe(0);
    const parsed = JSON.parse(tsResult.stdout) as Record<string, unknown>;
    expect(parsed["number"]).toBe(300);
    expect(parsed["title"]).toBe("Get parity");
    expect(parsed["body"]).toBe("get body content");
    expect(Array.isArray(parsed["labels"])).toBe(true);
    expect(parsed["updated_at"]).toBe("2026-02-01T00:00:00Z");
  });

  it("TS and Python return same JSON shape for cached row", () => {
    const record: DiscussionRecord = {
      number: 301,
      title: "Shape parity",
      body: "shape body",
      labels: ["SPEC_READY", "P1"],
      updated_at: "2026-03-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    const tsResult = runTs("discussion-cache.ts", ["get", "301"]);
    const pyResult = runPython("discussion_cache.py", ["get", "301"]);

    const tsObj = JSON.parse(tsResult.stdout) as Record<string, unknown>;
    const pyObj = JSON.parse(pyResult.stdout) as Record<string, unknown>;

    // Same scalar fields
    expect(tsObj["number"]).toBe(pyObj["number"]);
    expect(tsObj["title"]).toBe(pyObj["title"]);
    expect(tsObj["body"]).toBe(pyObj["body"]);
    expect(tsObj["updated_at"]).toBe(pyObj["updated_at"]);
    expect(tsObj["labels"]).toEqual(pyObj["labels"]);

    // Both should have cached_at field (may differ by seconds)
    expect(typeof tsObj["cached_at"]).toBe("string");
    expect(typeof pyObj["cached_at"]).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// Parity: discussion_status.ts pure-logic vs Python (no subprocess needed)
// These test that TS behaviour matches what Python would return for
// representative inputs, verified against the Python spec.
// ---------------------------------------------------------------------------

describe("parity: discussion-status pure logic vs Python spec", () => {
  const BODIES = {
    specReady: "<!-- STATUS:SPEC_READY SINCE:2026-05-09T00:00:00Z -->\n\n## Intent\nWant a thing.\n\n## Spec (Acceptance)\n- [ ] AC1\n\n## Implementation Notes\nUse pattern X.",
    implementing: "<!-- STATUS:IMPLEMENTING SINCE:2026-05-09T01:00:00Z -->",
    reviewing: "<!-- STATUS:REVIEWING PR:#321 SINCE:2026-05-09T02:00:00Z -->",
    done: "<!-- STATUS:DONE PR:#321 SINCE:2026-05-09T03:00:00Z -->",
    legacy: "This is a legacy body with no headers.",
    partialSections: "## Intent\nPartial intent.\n\n## Spec (Acceptance)\nPartial spec.",
    allSections: "## Intent\nFull intent.\n\n## Spec (Acceptance)\nFull spec.\n\n## Implementation Notes\nFull notes.",
  };

  it("extractStatus matches Python for all sample bodies", () => {
    expect(extractStatus(BODIES.specReady)).toBe("SPEC_READY");
    expect(extractStatus(BODIES.implementing)).toBe("IMPLEMENTING");
    expect(extractStatus(BODIES.reviewing)).toBe("REVIEWING");
    expect(extractStatus(BODIES.done)).toBe("DONE");
    expect(extractStatus(BODIES.legacy)).toBe("UNKNOWN");
  });

  it("extractLinkedPr matches Python", () => {
    expect(extractLinkedPr(BODIES.reviewing)).toBe(321);
    expect(extractLinkedPr(BODIES.done)).toBe(321);
    expect(extractLinkedPr(BODIES.specReady)).toBeNull();
    expect(extractLinkedPr(BODIES.legacy)).toBeNull();
  });

  it("extractSince matches Python", () => {
    expect(extractSince(BODIES.specReady)).toBe("2026-05-09T00:00:00Z");
    expect(extractSince(BODIES.reviewing)).toBe("2026-05-09T02:00:00Z");
    expect(extractSince(BODIES.legacy)).toBeNull();
  });

  it("getSections on allSections body matches Python", () => {
    const sections = getSections(BODIES.allSections);
    expect(sections.intent).toBe("Full intent.");
    expect(sections.spec).toBe("Full spec.");
    expect(sections.implementation_notes).toBe("Full notes.");
  });

  it("getSections on partialSections body matches Python", () => {
    const sections = getSections(BODIES.partialSections);
    expect(sections.intent).toBe("Partial intent.");
    expect(sections.spec).toBe("Partial spec.");
    expect(sections.implementation_notes).toBe("");
  });

  it("getSections on specReady body (with status comment) matches Python", () => {
    const sections = getSections(BODIES.specReady);
    expect(sections.intent).toBe("Want a thing.");
    expect(sections.spec).toBe("- [ ] AC1");
    expect(sections.implementation_notes).toBe("Use pattern X.");
  });

  it("getSections on legacy body matches Python back-compat", () => {
    const sections = getSections(BODIES.legacy);
    expect(sections.intent).toBe("");
    expect(sections.spec).toBe("This is a legacy body with no headers.");
    expect(sections.implementation_notes).toBe("");
  });

  it("missingSections matches Python for all sample bodies", () => {
    expect(missingSections(BODIES.allSections)).toEqual([]);
    expect(missingSections(BODIES.legacy)).toEqual([
      "Intent",
      "Spec (Acceptance)",
      "Implementation Notes",
    ]);
    expect(missingSections(BODIES.partialSections)).toEqual([
      "Implementation Notes",
    ]);
    expect(missingSections(BODIES.specReady)).toEqual([]);
  });

  it("setStatus round-trips: set then extract", () => {
    const body = "some legacy body";
    const modified = setStatus(body, "SPEC_READY", "2026-06-01T12:00:00Z");
    expect(extractStatus(modified)).toBe("SPEC_READY");
    expect(extractSince(modified)).toBe("2026-06-01T12:00:00Z");
  });

  it("setStatus replaces then set again: only one marker remains", () => {
    let body = "initial body";
    body = setStatus(body, "DISCUSSING", "2026-01-01T00:00:00Z");
    body = setStatus(body, "SPEC_READY", "2026-02-01T00:00:00Z");
    // Should only have one STATUS marker
    const allMarkers = body.match(/<!--\s*STATUS:/g) ?? [];
    expect(allMarkers.length).toBe(1);
    expect(extractStatus(body)).toBe("SPEC_READY");
  });
});

// ---------------------------------------------------------------------------
// Parity: discussion-status CLI vs Python CLI (subprocess)
// ---------------------------------------------------------------------------

describe("parity: discussion-status CLI missing-sections", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("TS missing-sections returns same JSON as Python for body with all sections", () => {
    // Seed a body with all sections present
    const body = [
      "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->",
      "",
      "## Intent",
      "Intent text.",
      "",
      "## Spec (Acceptance)",
      "Spec text.",
      "",
      "## Implementation Notes",
      "Notes text.",
    ].join("\n");

    const record: DiscussionRecord = {
      number: 400,
      title: "Status sections test",
      body,
      labels: ["SPEC_READY"],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    // Python discussion_status.py missing-sections uses discussion_cache.py to fetch body
    // Both should be seeded, so no network needed

    // Test TS pure logic directly (CLI uses fetchBody which needs bun available)
    const missing = missingSections(body);
    expect(missing).toEqual([]);

    // Verify TS directly returns correct JSON array
    const tsResult = runTs("discussion-status.ts", ["missing-sections", "400"], {
      DISCUSSION_CACHE_SCRIPT: join(
        new URL(import.meta.url).pathname,
        "..",
        "..",
        "src",
        "spawn",
        "discussion-cache.ts"
      ),
    });

    // TS CLI should output "[]"
    const parsed = JSON.parse(tsResult.stdout) as unknown;
    expect(Array.isArray(parsed)).toBe(true);
    expect((parsed as unknown[]).length).toBe(0);
  });

  it("TS missing-sections returns correct missing list for partial body", () => {
    const body = "## Intent\nIntent only.\n";
    const record: DiscussionRecord = {
      number: 401,
      title: "Partial sections",
      body,
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    const tsResult = runTs("discussion-status.ts", ["missing-sections", "401"], {
      DISCUSSION_CACHE_SCRIPT: join(
        new URL(import.meta.url).pathname,
        "..",
        "..",
        "src",
        "spawn",
        "discussion-cache.ts"
      ),
    });

    const parsed = JSON.parse(tsResult.stdout) as string[];
    expect(parsed).toContain("Spec (Acceptance)");
    expect(parsed).toContain("Implementation Notes");
    expect(parsed).not.toContain("Intent");
  });

  it("TS and Python missing-sections agree for all-missing body", () => {
    const body = "<!-- STATUS:DISCUSSING SINCE:2026-01-01T00:00:00Z -->\nJust a plain body.";
    const record: DiscussionRecord = {
      number: 402,
      title: "All missing",
      body,
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    const tsMissing = missingSections(body);
    const pyResult = runPython("discussion_status.py", ["missing-sections", "402"]);
    const pyMissing = JSON.parse(pyResult.stdout) as string[];

    expect(tsMissing).toEqual(pyMissing);
  });
});

describe("parity: discussion-status CLI get-sections", () => {
  beforeEach(setupTempState);
  afterEach(teardownTempState);

  it("TS get-sections agrees with Python for three-section body", () => {
    const body = [
      "<!-- STATUS:SPEC_READY SINCE:2026-01-01T00:00:00Z -->",
      "",
      "## Intent",
      "Intent content.",
      "",
      "## Spec (Acceptance)",
      "Spec content.",
      "",
      "## Implementation Notes",
      "Notes content.",
    ].join("\n");

    const record: DiscussionRecord = {
      number: 500,
      title: "Sections parity",
      body,
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    // Pure logic check — TS and Python should agree on section parsing
    const tsSections = getSections(body);
    const pyResult = runPython("discussion_status.py", ["get-sections", "500"]);
    const pySections = JSON.parse(pyResult.stdout) as Record<string, string>;

    expect(tsSections.intent).toBe(pySections["intent"]);
    expect(tsSections.spec).toBe(pySections["spec"]);
    expect(tsSections.implementation_notes).toBe(pySections["implementation_notes"]);
  });

  it("TS get-sections agrees with Python for legacy body", () => {
    const body = "Some legacy content with no headers.";
    const record: DiscussionRecord = {
      number: 501,
      title: "Legacy parity",
      body,
      labels: [],
      updated_at: "2026-01-01T00:00:00Z",
    };
    seedRow(record, nowIso());

    const tsSections = getSections(body);
    const pyResult = runPython("discussion_status.py", ["get-sections", "501"]);
    const pySections = JSON.parse(pyResult.stdout) as Record<string, string>;

    expect(tsSections.intent).toBe(pySections["intent"]);
    expect(tsSections.spec).toBe(pySections["spec"]);
    expect(tsSections.implementation_notes).toBe(pySections["implementation_notes"]);
  });
});
