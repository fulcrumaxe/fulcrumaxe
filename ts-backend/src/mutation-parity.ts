/**
 * Mutation parity harness — P4a (D#1437) — file-based blackboard edition.
 *
 * Purpose: prove that a POST route's TS implementation produces:
 *   (a) the same HTTP response (status + normalized body), AND
 *   (b) the same resulting state (file-based blackboard files after the mutation)
 * ...as the Python reference handler — when both are run against a TEMP COPY
 * of the file-based blackboard directory.  NEVER touches the production blackboard.
 *
 * Bug fixed (confirmed during #1449 investigation):
 *   The original harness compared SQLite rows (state.db) against SQLite rows.
 *   But Python's BudgetTracker uses the FILE-based Blackboard — writing .json
 *   files under <STATE_DIR>/blackboard/.  The SQLite-vs-SQLite comparison gave
 *   a false-positive because it never checked Python's real store.
 *
 *   Fixed by:
 *   - makeTempBbDir()      — cp -r <blackboard-dir> to a temp directory.
 *   - readBlackboardState() — reads .json files from the temp blackboard dir.
 *   - runPythonHandler()   — invokes Python's file-based Blackboard directly.
 *   - runTsHandler()       — passes the temp bbRoot to initBudgetSession().
 *
 * Design:
 *   1. makeTempBbDir()    — cp <bb-dir> to a temp path; return the temp root.
 *   2. runPythonHandler() — subprocess Python using file-based Blackboard
 *                           pointed at the temp dir.
 *   3. runTsHandler()     — call initBudgetSession() with the temp bbRoot.
 *   4. diffResponses()    — compare status codes and normalized response bodies.
 *   5. diffState()        — compare budget/*.json files produced by each handler.
 *   6. runParityCheck()   — orchestrate 1-5 and return a ParityResult.
 *
 * The harness is used by tests/budget-init.test.ts.  It can also be run as a
 * standalone CLI: bun run src/mutation-parity.ts --route /budget/init
 *
 * Safety invariants (enforced, not just documented):
 *   - makeTempBbDir() writes ONLY to /tmp/ — it never writes to STATE_DIR.
 *   - The TS handler receives the temp bbRoot explicitly; it never reads
 *     AUTONOMOUS_TEAM_BLACKBOARD_ROOT from the environment implicitly.
 *   - The Python subprocess is given AUTONOMOUS_TEAM_BLACKBOARD_ROOT pointing
 *     at the temp dir; it cannot reach the production blackboard.
 *   - Temp directories are cleaned up in a finally{} block.
 *
 * Response normalization uses the shared normalizer module (normalizer.ts).
 * State comparison reads the .json files from each temp dir and diffs them
 * after applying the same normalizer (timestamp + key-sort).
 *
 * Backward-compatible aliases:
 *   makeTempDb / cleanupTempDb — kept so existing test imports still compile;
 *   they now operate on blackboard directories rather than SQLite files.
 */

import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  cpSync,
  rmSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { normalize, type JsonValue } from "./normalizer.js";
import { stateDir as sharedStateDir } from "./config/state-paths.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface HttpCapture {
  status: number;
  body: JsonValue;
}

export interface StateCapture {
  /** Blackboard rows keyed by key, value is the entry's inner value field. */
  blackboard: Record<string, JsonValue>;
}

export interface ParityDiff {
  field: "status" | "body" | "state";
  python: JsonValue | number | string;
  ts: JsonValue | number | string;
  detail: string;
}

export interface ParityResult {
  route: string;
  ok: boolean;
  diffs: ParityDiff[];
  pythonResponse: HttpCapture;
  tsResponse: HttpCapture;
  pythonState: StateCapture;
  tsState: StateCapture;
  /** Temp blackboard directories used (never the production dir). */
  tempDbsUsed: string[];
  productionDbTouched: false; // always false — safety invariant
}

// ---------------------------------------------------------------------------
// makeTempBbDir — copy a source blackboard directory to a fresh temp dir.
// Returns the temp blackboard root.
// Caller must call cleanupTempBbDir(path) when done.
//
// Mirrors the old makeTempDb() but operates on directories, not SQLite files.
// If the source directory doesn't exist, creates an empty temp directory.
// ---------------------------------------------------------------------------

export function makeTempBbDir(sourceBbDir: string): string {
  const tempRoot = mkdtempSync(join(tmpdir(), "ts-parity-bb-"));
  if (existsSync(sourceBbDir)) {
    // Recursively copy the blackboard directory into tempRoot.
    cpSync(sourceBbDir, tempRoot, { recursive: true });
  } else {
    // Source doesn't exist — create an empty budget/ subdir so the writer
    // doesn't need to create it from scratch.
    mkdirSync(join(tempRoot, "budget"), { recursive: true });
  }
  return tempRoot;
}

export function cleanupTempBbDir(tempBbDir: string): void {
  try {
    rmSync(tempBbDir, { recursive: true, force: true });
  } catch {
    // Best-effort cleanup
  }
}

// ---------------------------------------------------------------------------
// Backward-compatible aliases so existing test imports compile unchanged.
// makeTempDb/cleanupTempDb now create/remove blackboard directories.
// The "path" is the blackboard root directory (not a .db file).
// ---------------------------------------------------------------------------

export function makeTempDb(sourceBbDir: string): string {
  return makeTempBbDir(sourceBbDir);
}

export function cleanupTempDb(tempBbDir: string): void {
  cleanupTempBbDir(tempBbDir);
}

// ---------------------------------------------------------------------------
// readBlackboardState — snapshot the blackboard files from a bb directory.
// Used to diff state AFTER the mutation.
//
// Fixed: previously read from a SQLite file; now reads the .json files that
// Python's file-based Blackboard actually writes.
// ---------------------------------------------------------------------------

export function readBlackboardState(
  bbRoot: string,
  keyPrefix: string,
): StateCapture {
  if (!existsSync(bbRoot)) return { blackboard: {} };

  const blackboard: Record<string, JsonValue> = {};

  // Scan recursively under bbRoot for *.json files whose key matches the prefix.
  function scanDir(dir: string, relPrefix: string): void {
    if (!existsSync(dir)) return;
    try {
      const entries = readdirSync(dir, { withFileTypes: true });
      for (const ent of entries) {
        if (ent.name === ".locks") continue; // skip lock dir
        const fullPath = join(dir, ent.name);
        if (ent.isDirectory()) {
          scanDir(fullPath, relPrefix + ent.name + "/");
        } else if (ent.isFile() && ent.name.endsWith(".json")) {
          const key = relPrefix + ent.name.slice(0, -5);
          if (key.startsWith(keyPrefix)) {
            try {
              const raw = readFileSync(fullPath, "utf-8");
              const entry = JSON.parse(raw) as Record<string, JsonValue>;
              // Extract the inner .value field (mirrors Python Blackboard.read()).
              blackboard[key] =
                "value" in entry ? entry["value"] ?? null : (entry as JsonValue);
            } catch {
              // Skip malformed files
            }
          }
        }
      }
    } catch {
      // Permission error or race — skip
    }
  }

  scanDir(bbRoot, "");
  return { blackboard };
}

// ---------------------------------------------------------------------------
// diffResponses — compare HTTP captures after normalization.
// ---------------------------------------------------------------------------

export function diffResponses(
  python: HttpCapture,
  ts: HttpCapture,
): ParityDiff[] {
  const diffs: ParityDiff[] = [];

  if (python.status !== ts.status) {
    diffs.push({
      field: "status",
      python: python.status,
      ts: ts.status,
      detail: `HTTP status mismatch: Python=${python.status} TS=${ts.status}`,
    });
    return diffs; // Body shape may differ on different status codes
  }

  const pyNorm = JSON.stringify(normalize(python.body, { route: "/budget/init" }));
  const tsNorm = JSON.stringify(normalize(ts.body, { route: "/budget/init" }));

  if (pyNorm !== tsNorm) {
    diffs.push({
      field: "body",
      python: pyNorm,
      ts: tsNorm,
      detail: "Normalized response body mismatch",
    });
  }

  return diffs;
}

// ---------------------------------------------------------------------------
// diffState — compare blackboard state captures after normalization.
// ---------------------------------------------------------------------------

export function diffState(
  python: StateCapture,
  ts: StateCapture,
): ParityDiff[] {
  const diffs: ParityDiff[] = [];

  const allKeys = new Set([
    ...Object.keys(python.blackboard),
    ...Object.keys(ts.blackboard),
  ]);

  for (const key of allKeys) {
    const pyVal = python.blackboard[key] ?? null;
    const tsVal = ts.blackboard[key] ?? null;
    const pyNorm = JSON.stringify(normalize(pyVal));
    const tsNorm = JSON.stringify(normalize(tsVal));
    if (pyNorm !== tsNorm) {
      diffs.push({
        field: "state",
        python: pyNorm,
        ts: tsNorm,
        detail: `Blackboard key mismatch: ${key}`,
      });
    }
  }

  return diffs;
}

// ---------------------------------------------------------------------------
// runTsHandler — invoke the TS budget-init handler in-process against a
// temp DB copy.  Returns the HTTP capture.
// ---------------------------------------------------------------------------

import { initBudgetSession, type BudgetInitResult } from "./routes/budget-init.js";

/**
 * Run the TS budget-init handler in-process against a temp blackboard directory.
 *
 * The second parameter is the temp blackboard root (a directory), not a DB path.
 * Backward-compatible alias: the parameter was previously called tempDbPath but
 * now refers to the temp blackboard dir.
 */
export function runTsHandler(
  body: Record<string, unknown>,
  tempBbRoot: string,
): HttpCapture {
  // Python passes body.get("ceiling") straight to init_session() — no validation.
  // Accept any numeric value (positive, zero, negative, float). No 400 guard.
  const ceiling =
    "ceiling" in body && body["ceiling"] !== undefined && body["ceiling"] !== null
      ? Number(body["ceiling"])
      : null;

  const result: BudgetInitResult = initBudgetSession(ceiling, tempBbRoot);
  return {
    status: 200,
    body: result as unknown as JsonValue,
  };
}

// ---------------------------------------------------------------------------
// runPythonHandler — invoke Python's BudgetTracker using the FILE-BASED
// Blackboard against a temp blackboard directory.
//
// Fixed: the original harness used SqliteBlackboard (state.db).  Python's
// BudgetTracker uses the file-based Blackboard class.  This now passes the
// temp blackboard root directory to Blackboard(root=<tempBbRoot>) so the
// Python subprocess writes to the same store type as production.
// ---------------------------------------------------------------------------

export async function runPythonHandler(
  body: Record<string, unknown>,
  tempBbRoot: string,
): Promise<HttpCapture> {
  // Python invocation script — imports budget.py directly, uses the temp bb dir.
  const ceiling =
    "ceiling" in body && body["ceiling"] !== undefined && body["ceiling"] !== null
      ? String(body["ceiling"])
      : "None";

  // Resolve repo root: src/mutation-parity.ts → src/ → ts-backend/ → repo root (worktree)
  // import.meta.dir is ts-backend/src/ so we go up 2 levels to reach the worktree root.
  const REPO_ROOT_FOR_PY = join(import.meta.dir, "..", "..");

  // Script uses the FILE-BASED Blackboard(root=<tempBbRoot>) — the same class
  // that Python's BudgetTracker uses in production.  We pass the temp directory
  // as the root so the subprocess cannot reach the production blackboard.
  const script = `
import sys, json
sys.path.insert(0, ${JSON.stringify(REPO_ROOT_FOR_PY)})
from backend.blackboard import Blackboard
from backend.budget import BudgetTracker

# Use file-based Blackboard pointed at the temp directory.
bb = Blackboard(root=${JSON.stringify(tempBbRoot)})
bt = BudgetTracker(bb=bb)
ceiling = ${ceiling}
bt.init_session(ceiling=ceiling)
status = bt.get_status()
result = {"ok": True, "status": status}
print(json.dumps(result))
`;

  const proc = Bun.spawn(["python3", "-c", script], {
    env: { ...process.env, AUTONOMOUS_TEAM_BLACKBOARD_ROOT: tempBbRoot },
    stdout: "pipe",
    stderr: "pipe",
  });

  const timeout = setTimeout(() => {
    proc.kill();
  }, 15000);

  const exitCode = await proc.exited;
  clearTimeout(timeout);

  if (exitCode !== 0) {
    const stderr = await new Response(proc.stderr).text();
    return {
      status: 500,
      body: { detail: `Python handler failed: ${stderr.slice(0, 200)}` } as JsonValue,
    };
  }

  const stdout = await new Response(proc.stdout).text();
  try {
    const parsed = JSON.parse(stdout.trim()) as JsonValue;
    return { status: 200, body: parsed };
  } catch {
    return {
      status: 500,
      body: { detail: `Python output parse error: ${stdout.slice(0, 200)}` } as JsonValue,
    };
  }
}

// ---------------------------------------------------------------------------
// runParityCheck — orchestrate the full parity check for POST /budget/init.
// Both Python and TS get their OWN temp copies of the file-based blackboard
// directory so they don't interfere with each other.
// Neither touches the production blackboard directory.
// ---------------------------------------------------------------------------

export async function runParityCheck(opts: {
  body: Record<string, unknown>;
  /** Source blackboard directory to copy (reads production files, never writes). */
  sourceDbPath: string;
  keyPrefix?: string;
}): Promise<ParityResult> {
  const { body, sourceDbPath, keyPrefix = "budget/" } = opts;

  // Make TWO separate temp copies — one for Python, one for TS.
  // This ensures a write-ordering race between the two can't mask a divergence.
  const pyTempBb = makeTempBbDir(sourceDbPath);
  const tsTempBb = makeTempBbDir(sourceDbPath);
  const tempDbsUsed = [pyTempBb, tsTempBb];

  try {
    // Run TS handler (sync, in-process) — writes to tsTempBb.
    const tsResponse = runTsHandler(body, tsTempBb);

    // Run Python handler (async subprocess) — writes to pyTempBb.
    const pythonResponse = await runPythonHandler(body, pyTempBb);

    // Snapshot state AFTER mutation — reads the .json files.
    const pythonState = readBlackboardState(pyTempBb, keyPrefix);
    const tsState = readBlackboardState(tsTempBb, keyPrefix);

    // Diff responses and state.
    const responseDiffs = diffResponses(pythonResponse, tsResponse);
    const stateDiffs = diffState(pythonState, tsState);
    const allDiffs = [...responseDiffs, ...stateDiffs];

    return {
      route: "/budget/init",
      ok: allDiffs.length === 0,
      diffs: allDiffs,
      pythonResponse,
      tsResponse,
      pythonState,
      tsState,
      tempDbsUsed,
      productionDbTouched: false,
    };
  } finally {
    cleanupTempBbDir(pyTempBb);
    cleanupTempBbDir(tsTempBb);
  }
}

// ---------------------------------------------------------------------------
// CLI entry point — for standalone parity proof.
// Usage: bun run src/mutation-parity.ts [--ceiling N]
// ---------------------------------------------------------------------------

if (import.meta.main) {
  const args = process.argv.slice(2);
  const ceilingIdx = args.indexOf("--ceiling");
  const ceilingRaw = ceilingIdx >= 0 ? args[ceilingIdx + 1] : undefined;
  const body: Record<string, unknown> =
    ceilingRaw !== undefined ? { ceiling: Number(ceilingRaw) } : {};

  const stateDir = sharedStateDir();
  // Use file-based blackboard directory (never state.db).
  const sourceBbDir =
    process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT ?? join(stateDir, "blackboard");

  console.log("[mutation-parity] Running POST /budget/init parity check");
  console.log(`[mutation-parity] Source blackboard dir: ${sourceBbDir} (NEVER modified)`);
  console.log(`[mutation-parity] Request body: ${JSON.stringify(body)}`);
  console.log("");

  const result = await runParityCheck({ body, sourceDbPath: sourceBbDir });

  console.log(`[mutation-parity] TS response:     status=${result.tsResponse.status}`);
  console.log(`[mutation-parity] Python response:  status=${result.pythonResponse.status}`);
  console.log(`[mutation-parity] TS body:     ${JSON.stringify(result.tsResponse.body)}`);
  console.log(`[mutation-parity] Python body:  ${JSON.stringify(result.pythonResponse.body)}`);
  console.log("");
  console.log(`[mutation-parity] TS state (budget/* files):`);
  for (const [k, v] of Object.entries(result.tsState.blackboard)) {
    console.log(`  ${k}: ${JSON.stringify(v)}`);
  }
  console.log(`[mutation-parity] Python state (budget/* files):`);
  for (const [k, v] of Object.entries(result.pythonState.blackboard)) {
    console.log(`  ${k}: ${JSON.stringify(v)}`);
  }
  console.log("");

  if (result.ok) {
    console.log("[mutation-parity] RESULT: PASS — zero divergence");
    console.log("[mutation-parity] Production blackboard dir was NOT touched.");
  } else {
    console.log(`[mutation-parity] RESULT: FAIL — ${result.diffs.length} divergence(s):`);
    for (const d of result.diffs) {
      console.log(`  [${d.field}] ${d.detail}`);
      console.log(`    Python: ${d.python}`);
      console.log(`    TS:     ${d.ts}`);
    }
    console.log("[mutation-parity] Production blackboard dir was NOT touched.");
    process.exit(1);
  }
}
