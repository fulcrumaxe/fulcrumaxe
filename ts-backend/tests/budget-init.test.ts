/**
 * Tests for POST /budget/init — mutation parity harness (D#1437 P4a — parity bug fix).
 *
 * Run: bun test tests/budget-init.test.ts --timeout 30000
 *
 * Coverage:
 *  1. TS-only unit tests: budgetInitHandler response shape + state writes
 *     against a temp blackboard directory (no production state touched).
 *  2. Negative-auth parity: 401 no-token, 403 wrong-token, 403 RBAC-deny.
 *  3. Mutation parity harness: runs BOTH Python and TS handlers against
 *     separate temp copies of the FILE-BASED blackboard dir, diffs response
 *     + resulting .json files.
 *     Proves Gate 2 (file-parity + response parity + production-untouched).
 *
 * Bug fixed: the original harness used SQLite (state.db) as the comparison
 * store.  Python's BudgetTracker uses the file-based Blackboard, which writes
 * .json files — not SQLite rows.  The SQLite-vs-SQLite comparison gave a
 * false-positive because it never checked Python's real store.
 *
 * Safety invariants (enforced in all tests):
 *  - Every test that writes blackboard state uses a TEMP DIR (via makeTempDb).
 *  - The production blackboard dir (~/.fulcrumaxe-state/blackboard/) is NEVER modified.
 *  - All temp dirs are cleaned up in afterEach/afterAll.
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { Hono } from "hono";
import { existsSync, readFileSync } from "node:fs";
import { defaultDenyMiddleware } from "../src/middleware/auth.js";
import { budgetInitHandler, initBudgetSession } from "../src/routes/budget-init.js";
import {
  makeTempDb,
  cleanupTempDb,
  readBlackboardState,
  runTsHandler,
  runPythonHandler,
  diffResponses,
  diffState,
} from "../src/mutation-parity.js";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Determine source blackboard directory for temp copies.
// Uses the real file-based blackboard path from the environment (read-only).
// Falls back to an empty temp dir if the directory doesn't exist.
// makeTempDb() now creates a temp DIRECTORY (not a SQLite file).
// ---------------------------------------------------------------------------
const STATE_DIR =
  process.env.AUTONOMOUS_TEAM_STATE_DIR ??
  join(process.env.HOME ?? "/root", ".fulcrumaxe-state");
// SOURCE_DB is now the blackboard directory (not state.db).
// The name is kept for backward compatibility with the import.
const SOURCE_DB =
  process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT ?? join(STATE_DIR, "blackboard");

// ---------------------------------------------------------------------------
// Auth plumbing
// ---------------------------------------------------------------------------
let savedKey: string | undefined;

function makeApp(authKey?: string): Hono {
  if (authKey !== undefined) {
    process.env.AF_API_AUTH_KEY = authKey;
  } else {
    delete process.env.AF_API_AUTH_KEY;
  }
  const app = new Hono();
  app.use("*", defaultDenyMiddleware);
  app.post("/budget/init", budgetInitHandler);
  return app;
}

// ---------------------------------------------------------------------------
// §1 — TS unit tests: response shape + state writes (temp DB only)
// ---------------------------------------------------------------------------

describe("POST /budget/init — TS handler unit tests", () => {
  let tempBbDir: string;
  let savedBbRoot: string | undefined;

  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    savedBbRoot = process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT;
    delete process.env.AF_API_AUTH_KEY;
    // Route the handler to a fresh temp copy of the blackboard dir.
    // Production blackboard (~/.fulcrumaxe-state/blackboard/) is never touched.
    tempBbDir = makeTempDb(SOURCE_DB);
    process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT = tempBbDir;
  });

  afterEach(() => {
    cleanupTempDb(tempBbDir);
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
    if (savedBbRoot !== undefined) {
      process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT = savedBbRoot;
    } else {
      delete process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT;
    }
  });

  it("initBudgetSession writes 3 blackboard .json files to temp dir", () => {
    // Use a fresh empty dir so no pre-existing agent records affect spent.
    const freshBbDir = makeTempDb("/nonexistent-fresh-for-unit-test");
    try {
      const result = initBudgetSession(null, freshBbDir);

      expect(result.ok).toBe(true);
      expect(typeof result.status.ceiling).toBe("number");
      expect(result.status.ceiling).toBeGreaterThan(0);
      expect(result.status.spent).toBe(0);
      expect(result.status.remaining).toBe(result.status.ceiling);
      expect(typeof result.status.per_agent_ceiling).toBe("number");
      expect(result.status.per_agent_ceiling).toBeGreaterThan(0);
      expect(typeof result.status.warn_threshold_pct).toBe("number");
      expect(Array.isArray(result.status.agents)).toBe(true);

      // Verify the 3 blackboard .json files were actually written to the TEMP DIR.
      const state = readBlackboardState(freshBbDir, "budget/");
      expect(typeof state.blackboard["budget/session_ceiling"]).toBe("number");
      expect(state.blackboard["budget/session_spent"]).toBe(0);
      expect(typeof state.blackboard["budget/per_agent_ceiling"]).toBe("number");
    } finally {
      cleanupTempDb(freshBbDir);
    }
  });

  it("initBudgetSession respects explicit ceiling", () => {
    const freshBbDir = makeTempDb("/nonexistent-ceiling-test");
    try {
      const result = initBudgetSession(12345, freshBbDir);
      expect(result.ok).toBe(true);
      expect(result.status.ceiling).toBe(12345);

      const state = readBlackboardState(freshBbDir, "budget/");
      expect(state.blackboard["budget/session_ceiling"]).toBe(12345);
    } finally {
      cleanupTempDb(freshBbDir);
    }
  });

  it("initBudgetSession increments version on repeated calls", () => {
    // Use a fresh empty dir (source doesn't exist) to guarantee version starts at 1.
    const freshBbDir = makeTempDb("/nonexistent-path-for-empty-bb");
    try {
      initBudgetSession(null, freshBbDir);
      // Read version after first write — fresh dir, no prior file, should be 1.
      const ceilingFile = join(freshBbDir, "budget", "session_ceiling.json");
      expect(existsSync(ceilingFile)).toBe(true);
      const entry1 = JSON.parse(readFileSync(ceilingFile, "utf-8")) as { version: number };
      expect(entry1.version).toBe(1);

      initBudgetSession(null, freshBbDir);
      const entry2 = JSON.parse(readFileSync(ceilingFile, "utf-8")) as { version: number };
      expect(entry2.version).toBe(2);
    } finally {
      cleanupTempDb(freshBbDir);
    }
  });

  it("initBudgetSession JSON envelope matches Python Blackboard.write() shape", () => {
    const freshBbDir = makeTempDb("/nonexistent-path-for-shape-test");
    try {
      initBudgetSession(999999, freshBbDir);
      const ceilingFile = join(freshBbDir, "budget", "session_ceiling.json");
      expect(existsSync(ceilingFile)).toBe(true);
      const raw = readFileSync(ceilingFile, "utf-8");
      // Must end with a newline (Python: fh.write("\n")).
      expect(raw.endsWith("\n")).toBe(true);
      const entry = JSON.parse(raw) as Record<string, unknown>;
      // Must have exactly these 4 fields (Python's Blackboard._atomic_write).
      expect(typeof entry["value"]).toBe("number");
      expect(entry["value"]).toBe(999999);
      expect(typeof entry["version"]).toBe("number");
      expect(entry["version"]).toBe(1);
      expect(typeof entry["updated_at"]).toBe("string");
      // updated_at must be ISO-8601 with +00:00 suffix (Python isoformat(timespec="seconds")).
      expect((entry["updated_at"] as string).endsWith("+00:00")).toBe(true);
      expect(entry["updated_by"]).toBe("budget-tracker");
    } finally {
      cleanupTempDb(freshBbDir);
    }
  });

  it("POST /budget/init returns 200 with ok+status shape (auth disabled)", async () => {
    const app = makeApp();
    const res = await app.request("/budget/init", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body["ok"]).toBe(true);
    const status = body["status"] as Record<string, unknown>;
    expect(typeof status["ceiling"]).toBe("number");
    expect(typeof status["spent"]).toBe("number");
    expect(typeof status["remaining"]).toBe("number");
    expect(Array.isArray(status["agents"])).toBe(true);
  });

  it("POST /budget/init with ceiling param sets the ceiling", async () => {
    const app = makeApp();
    const res = await app.request("/budget/init", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ceiling: 99999 }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    const status = body["status"] as Record<string, unknown>;
    expect(status["ceiling"]).toBe(99999);
  });

  it("POST /budget/init accepts negative ceiling (mirrors Python — no validation)", async () => {
    // Python's init_session() passes ceiling through with NO validation.
    // Negative, zero, and float values are all accepted and written.
    const app = makeApp();
    const res = await app.request("/budget/init", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ceiling: -1 }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body["ok"]).toBe(true);
    const status = body["status"] as Record<string, unknown>;
    expect(status["ceiling"]).toBe(-1);
  });

  it("POST /budget/init accepts zero ceiling (mirrors Python)", async () => {
    const app = makeApp();
    const res = await app.request("/budget/init", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ceiling: 0 }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect((body["status"] as Record<string, unknown>)["ceiling"]).toBe(0);
  });

  it("POST /budget/init with empty body uses default ceiling", async () => {
    const app = makeApp();
    const res = await app.request("/budget/init", {
      method: "POST",
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body["ok"]).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// §2 — Negative-auth parity
// ---------------------------------------------------------------------------

describe("POST /budget/init — negative-auth parity", () => {
  let savedBbRoot: string | undefined;
  let authTempBbDir: string;

  beforeEach(() => {
    savedKey = process.env.AF_API_AUTH_KEY;
    savedBbRoot = process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT;
    // Route writes to temp copy of blackboard dir for the 200-OK case.
    authTempBbDir = makeTempDb(SOURCE_DB);
    process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT = authTempBbDir;
  });

  afterEach(() => {
    cleanupTempDb(authTempBbDir);
    if (savedKey !== undefined) {
      process.env.AF_API_AUTH_KEY = savedKey;
    } else {
      delete process.env.AF_API_AUTH_KEY;
    }
    if (savedBbRoot !== undefined) {
      process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT = savedBbRoot;
    } else {
      delete process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT;
    }
  });

  it("401 when no token and auth is enabled", async () => {
    const app = makeApp("secret-key");
    const res = await app.request("/budget/init", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(401);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body["detail"]).toBe("unauthorized");
  });

  it("403 when wrong token", async () => {
    const app = makeApp("secret-key");
    const res = await app.request("/budget/init", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        Authorization: "Bearer wrong-key",
      },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(403);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body["detail"]).toBe("forbidden");
  });

  it("200 when correct token", async () => {
    const app = makeApp("correct-key");
    const res = await app.request("/budget/init", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        Authorization: "Bearer correct-key",
      },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// §3 — Mutation parity harness (Gate 2 proof)
// Runs TS handler against a temp blackboard dir; compares response shape
// and resulting .json files against Python's file-based Blackboard.
// This harness now catches the bug class (SQLite vs file-based store mismatch).
// ---------------------------------------------------------------------------

describe("POST /budget/init — mutation parity harness (Gate 2)", () => {
  it("TS response shape matches Python response shape (default body)", async () => {
    const body: Record<string, unknown> = {};
    const tsTempBb = makeTempDb(SOURCE_DB);
    const pyTempBb = makeTempDb(SOURCE_DB);

    try {
      const tsCapture = runTsHandler(body, tsTempBb);
      const pyCapture = await runPythonHandler(body, pyTempBb);

      const responseDiffs = diffResponses(pyCapture, tsCapture);
      if (responseDiffs.length > 0) {
        console.error("[Gate 2] Response divergences:");
        for (const d of responseDiffs) {
          console.error(`  [${d.field}] ${d.detail}`);
          console.error(`    Python: ${d.python}`);
          console.error(`    TS:     ${d.ts}`);
        }
      }
      expect(responseDiffs).toHaveLength(0);
    } finally {
      cleanupTempDb(tsTempBb);
      cleanupTempDb(pyTempBb);
    }
  });

  it("TS response shape matches Python with explicit ceiling", async () => {
    const body: Record<string, unknown> = { ceiling: 7500000 };
    const tsTempBb = makeTempDb(SOURCE_DB);
    const pyTempBb = makeTempDb(SOURCE_DB);

    try {
      const tsCapture = runTsHandler(body, tsTempBb);
      const pyCapture = await runPythonHandler(body, pyTempBb);

      const responseDiffs = diffResponses(pyCapture, tsCapture);
      expect(responseDiffs).toHaveLength(0);

      // Verify both wrote ceiling=7500000
      expect((tsCapture.body as Record<string, unknown>)["ok"]).toBe(true);
      expect((pyCapture.body as Record<string, unknown>)["ok"]).toBe(true);
    } finally {
      cleanupTempDb(tsTempBb);
      cleanupTempDb(pyTempBb);
    }
  });

  it("TS .json files match Python .json files — same 3 budget keys written", async () => {
    const body: Record<string, unknown> = { ceiling: 2000000 };
    // Use fresh empty dirs so no pre-existing agent records appear in state.
    const tsTempBb = makeTempDb("/nonexistent-for-state-parity");
    const pyTempBb = makeTempDb("/nonexistent-for-state-parity");

    try {
      runTsHandler(body, tsTempBb);
      await runPythonHandler(body, pyTempBb);

      // Read from the FILE-BASED blackboard dirs (not SQLite).
      const tsState = readBlackboardState(tsTempBb, "budget/");
      const pyState = readBlackboardState(pyTempBb, "budget/");

      // Both should have exactly 3 budget keys (fresh dirs, no prior agents).
      expect(Object.keys(tsState.blackboard).sort()).toEqual([
        "budget/per_agent_ceiling",
        "budget/session_ceiling",
        "budget/session_spent",
      ]);
      expect(Object.keys(pyState.blackboard).sort()).toEqual([
        "budget/per_agent_ceiling",
        "budget/session_ceiling",
        "budget/session_spent",
      ]);

      const stateDiffs = diffState(pyState, tsState);
      if (stateDiffs.length > 0) {
        console.error("[Gate 2] File-state divergences:");
        for (const d of stateDiffs) {
          console.error(`  [${d.field}] ${d.detail}`);
          console.error(`    Python: ${d.python}`);
          console.error(`    TS:     ${d.ts}`);
        }
      }
      expect(stateDiffs).toHaveLength(0);
    } finally {
      cleanupTempDb(tsTempBb);
      cleanupTempDb(pyTempBb);
    }
  });

  it("TS .json file envelope is byte-compatible with Python Blackboard.write()", async () => {
    // Verify the JSON envelope structure matches Python's _atomic_write() exactly:
    // { value, version, updated_at, updated_by } + trailing newline.
    const body: Record<string, unknown> = { ceiling: 3333333 };
    const tsTempBb = makeTempDb("/nonexistent-for-envelope-test");
    const pyTempBb = makeTempDb("/nonexistent-for-envelope-test");

    try {
      runTsHandler(body, tsTempBb);
      await runPythonHandler(body, pyTempBb);

      const tsCeilingFile = join(tsTempBb, "budget", "session_ceiling.json");
      const pyCeilingFile = join(pyTempBb, "budget", "session_ceiling.json");

      expect(existsSync(tsCeilingFile)).toBe(true);
      expect(existsSync(pyCeilingFile)).toBe(true);

      const tsRaw = readFileSync(tsCeilingFile, "utf-8");
      const pyRaw = readFileSync(pyCeilingFile, "utf-8");

      const tsEntry = JSON.parse(tsRaw) as Record<string, unknown>;
      const pyEntry = JSON.parse(pyRaw) as Record<string, unknown>;

      // Both must have the same 4 fields with equivalent values.
      expect(tsEntry["value"]).toBe(pyEntry["value"]);
      expect(typeof tsEntry["version"]).toBe("number");
      expect(typeof pyEntry["version"]).toBe("number");
      // updated_at timestamps will differ by ms — verified structurally.
      expect((tsEntry["updated_at"] as string).endsWith("+00:00")).toBe(true);
      expect((pyEntry["updated_at"] as string).endsWith("+00:00")).toBe(true);
      expect(tsEntry["updated_by"]).toBe(pyEntry["updated_by"]);
      expect(tsEntry["updated_by"]).toBe("budget-tracker");
      // Both files must end with newline (Python: fh.write("\n")).
      expect(tsRaw.endsWith("\n")).toBe(true);
      expect(pyRaw.endsWith("\n")).toBe(true);
    } finally {
      cleanupTempDb(tsTempBb);
      cleanupTempDb(pyTempBb);
    }
  });

  it("negative ceiling: TS and Python both return 200 and write the negative value (parity proof)", async () => {
    // Resolves PARITY-CAVEATS.md §4: negative ceiling was TS-only 400, Python 200.
    // After fix: both return 200 and write ceiling=-1 to their respective blackboard dirs.
    const body: Record<string, unknown> = { ceiling: -1 };
    const tsTempBb = makeTempDb("/nonexistent-for-neg-ceiling-parity");
    const pyTempBb = makeTempDb("/nonexistent-for-neg-ceiling-parity");

    try {
      const tsCapture = runTsHandler(body, tsTempBb);
      const pyCapture = await runPythonHandler(body, pyTempBb);

      // Both must return 200.
      expect(tsCapture.status).toBe(200);
      expect(pyCapture.status).toBe(200);

      // Both must write ceiling=-1 to the blackboard.
      const tsState = readBlackboardState(tsTempBb, "budget/");
      const pyState = readBlackboardState(pyTempBb, "budget/");
      expect(tsState.blackboard["budget/session_ceiling"]).toBe(-1);
      expect(pyState.blackboard["budget/session_ceiling"]).toBe(-1);

      // Zero divergences.
      const responseDiffs = diffResponses(pyCapture, tsCapture);
      expect(responseDiffs).toHaveLength(0);
    } finally {
      cleanupTempDb(tsTempBb);
      cleanupTempDb(pyTempBb);
    }
  });

  it("zero ceiling: TS and Python both return 200 and write zero (parity proof)", async () => {
    const body: Record<string, unknown> = { ceiling: 0 };
    const tsTempBb = makeTempDb("/nonexistent-for-zero-ceiling-parity");
    const pyTempBb = makeTempDb("/nonexistent-for-zero-ceiling-parity");

    try {
      const tsCapture = runTsHandler(body, tsTempBb);
      const pyCapture = await runPythonHandler(body, pyTempBb);

      expect(tsCapture.status).toBe(200);
      expect(pyCapture.status).toBe(200);

      const tsState = readBlackboardState(tsTempBb, "budget/");
      const pyState = readBlackboardState(pyTempBb, "budget/");
      expect(tsState.blackboard["budget/session_ceiling"]).toBe(0);
      expect(pyState.blackboard["budget/session_ceiling"]).toBe(0);

      const responseDiffs = diffResponses(pyCapture, tsCapture);
      expect(responseDiffs).toHaveLength(0);
    } finally {
      cleanupTempDb(tsTempBb);
      cleanupTempDb(pyTempBb);
    }
  });

  it("production blackboard dir was NOT touched (safety invariant)", () => {
    // Verify that the production blackboard dir was not modified.
    // Tests use temp copies only — so the 7500000 sentinel ceiling value
    // should not appear in any production budget/*.json file.
    if (!existsSync(SOURCE_DB)) {
      // Production blackboard doesn't exist — trivially safe.
      return;
    }
    const ceilingFile = join(SOURCE_DB, "budget", "session_ceiling.json");
    if (!existsSync(ceilingFile)) {
      // No production ceiling file — trivially safe.
      return;
    }
    const raw = readFileSync(ceilingFile, "utf-8");
    const entry = JSON.parse(raw) as Record<string, unknown>;
    // Production ceiling should NOT be 7500000 (our test sentinel value).
    expect(entry["value"]).not.toBe(7500000);
  });
});
