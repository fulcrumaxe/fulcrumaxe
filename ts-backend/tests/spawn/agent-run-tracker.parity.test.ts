/**
 * tests/spawn/agent-run-tracker.parity.test.ts
 *
 * Parity test: runs sequences of start/complete/reconcile against BOTH the
 * Python CLI (backend/agent_run_tracker.py) and the TS implementation
 * (src/spawn/agent-run-tracker.ts), each in a separate temp state dir,
 * then asserts the resulting agent_run table rows are equivalent.
 *
 * Run: bun test tests/spawn/ --timeout 120000
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { mkdirSync, rmSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { DuckDBInstance } from "@duckdb/node-api";
import { startRun, completeRun } from "../../src/spawn/agent-run-tracker.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Resolve from this file: tests/spawn/ → ts-backend/ → repo root
// import.meta.url is the path of THIS file; go up 4 levels from the file itself:
//   tests/spawn/agent-run-tracker.parity.test.ts → tests/spawn/ → tests/ → ts-backend/ → repo root
const _thisFile = new URL(import.meta.url).pathname;
const REPO_ROOT = join(_thisFile, "..", "..", "..", "..");
const TS_ENTRY = join(REPO_ROOT, "ts-backend", "src", "spawn", "agent-run-tracker.ts");
const PY_ENTRY = join(REPO_ROOT, "backend", "agent_run_tracker.py");

function makeTempDir(label: string): string {
  const dir = join(tmpdir(), `art-parity-${label}-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

async function runProcess(
  cmd: string[],
  env: Record<string, string>
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const proc = Bun.spawn(cmd, {
    env: { ...process.env, ...env },
    stdout: "pipe",
    stderr: "pipe",
  });
  const timeout = setTimeout(() => proc.kill(), 45_000);
  await proc.exited;
  clearTimeout(timeout);
  const stdout = await new Response(proc.stdout).text();
  const stderr = await new Response(proc.stderr).text();
  return { exitCode: proc.exitCode ?? 0, stdout, stderr };
}

// Use STATS_DB_PATH directly — avoids the backend.state_paths ImportError
// that occurs when running Python via an absolute path (no repo root in sys.path).
// Both Python and TS resolve STATS_DB_PATH first (before AUTONOMOUS_TEAM_STATE_DIR),
// so this is the most reliable override for tests.
async function runPy(
  args: string[],
  stateDir: string
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const dbPath = join(stateDir, "stats.duckdb");
  return runProcess(
    ["python3", PY_ENTRY, ...args],
    { STATS_DB_PATH: dbPath, AUTONOMOUS_TEAM_STATE_DIR: stateDir }
  );
}

async function runTs(
  args: string[],
  stateDir: string
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const dbPath = join(stateDir, "stats.duckdb");
  return runProcess(
    ["bun", "run", TS_ENTRY, ...args],
    { STATS_DB_PATH: dbPath, AUTONOMOUS_TEAM_STATE_DIR: stateDir }
  );
}

// ---------------------------------------------------------------------------
// Row reader — reads agent_run rows from a stats.duckdb in a given state dir
// ---------------------------------------------------------------------------

interface AgentRunRow {
  agent_id: string;
  role: string;
  discussion: number | null;
  pr: number | null;
  verdict: string | null;
  model: string | null;
  input_tok: number | null;
  output_tok: number | null;
  cache_read: number | null;
  cache_write: number | null;
  cache_creation_tokens: number | null;
  blocked_reason: string | null;
  event_id: string | null;
  first_write_turn: number | null;
  total_turns: number | null;
  routed_via: string | null;
  auto_routed: boolean | null;
  has_start_ts: boolean;
  has_end_ts: boolean;
  duration_s_present: boolean; // true if non-null
}

async function readRows(stateDir: string): Promise<AgentRunRow[]> {
  const dbFile = join(stateDir, "stats.duckdb"); // STATS_DB_PATH equivalent
  if (!existsSync(dbFile)) return [];

  const inst = await DuckDBInstance.create(dbFile, { access_mode: "READ_ONLY" });
  const conn = await inst.connect();
  try {
    // Check if table exists
    const tables = await conn.runAndReadAll(
      "SELECT table_name FROM information_schema.tables WHERE table_name='agent_run'"
    );
    if ((tables.getRows() as unknown[][]).length === 0) return [];

    const result = await conn.runAndReadAll(
      `SELECT agent_id, role, discussion, pr, verdict, model,
              input_tok, output_tok, cache_read, cache_write,
              cache_creation_tokens, blocked_reason, event_id,
              first_write_turn, total_turns, routed_via, auto_routed,
              start_ts IS NOT NULL AS has_start_ts,
              end_ts   IS NOT NULL AS has_end_ts,
              duration_s IS NOT NULL AS duration_s_present
       FROM agent_run
       ORDER BY agent_id`
    );

    const rows = result.getRows() as unknown[][];
    const cols = result.columnNames() as string[];

    return rows.map((r) => {
      const row: Record<string, unknown> = {};
      for (let i = 0; i < cols.length; i++) {
        row[cols[i]] = r[i];
      }
      // Normalise bigint → number
      const normalise = (v: unknown): unknown => {
        if (typeof v === "bigint") return Number(v);
        return v;
      };
      return {
        agent_id: String(row["agent_id"] ?? ""),
        role: String(row["role"] ?? ""),
        discussion: row["discussion"] !== null && row["discussion"] !== undefined
          ? Number(normalise(row["discussion"])) : null,
        pr: row["pr"] !== null && row["pr"] !== undefined
          ? Number(normalise(row["pr"])) : null,
        verdict: row["verdict"] !== null && row["verdict"] !== undefined
          ? String(row["verdict"]) : null,
        model: row["model"] !== null && row["model"] !== undefined
          ? String(row["model"]) : null,
        input_tok: row["input_tok"] !== null && row["input_tok"] !== undefined
          ? Number(normalise(row["input_tok"])) : null,
        output_tok: row["output_tok"] !== null && row["output_tok"] !== undefined
          ? Number(normalise(row["output_tok"])) : null,
        cache_read: row["cache_read"] !== null && row["cache_read"] !== undefined
          ? Number(normalise(row["cache_read"])) : null,
        cache_write: row["cache_write"] !== null && row["cache_write"] !== undefined
          ? Number(normalise(row["cache_write"])) : null,
        cache_creation_tokens: row["cache_creation_tokens"] !== null && row["cache_creation_tokens"] !== undefined
          ? Number(normalise(row["cache_creation_tokens"])) : null,
        blocked_reason: row["blocked_reason"] !== null && row["blocked_reason"] !== undefined
          ? String(row["blocked_reason"]) : null,
        event_id: row["event_id"] !== null && row["event_id"] !== undefined
          ? String(row["event_id"]) : null,
        first_write_turn: row["first_write_turn"] !== null && row["first_write_turn"] !== undefined
          ? Number(normalise(row["first_write_turn"])) : null,
        total_turns: row["total_turns"] !== null && row["total_turns"] !== undefined
          ? Number(normalise(row["total_turns"])) : null,
        routed_via: row["routed_via"] !== null && row["routed_via"] !== undefined
          ? String(row["routed_via"]) : null,
        auto_routed: row["auto_routed"] !== null && row["auto_routed"] !== undefined
          ? Boolean(row["auto_routed"]) : null,
        has_start_ts: Boolean(row["has_start_ts"]),
        has_end_ts: Boolean(row["has_end_ts"]),
        duration_s_present: Boolean(row["duration_s_present"]),
      } satisfies AgentRunRow;
    });
  } finally {
    try { conn.closeSync(); } catch { /* ignore */ }
    try { inst.closeSync(); } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// Test fixture
// ---------------------------------------------------------------------------

let pyDir = "";
let tsDir = "";

beforeEach(() => {
  pyDir = makeTempDir("py");
  tsDir = makeTempDir("ts");
});

afterEach(() => {
  try { rmSync(pyDir, { recursive: true, force: true }); } catch { /* ignore */ }
  try { rmSync(tsDir, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ---------------------------------------------------------------------------
// Test 1: start then complete a run (happy path)
// ---------------------------------------------------------------------------

describe("parity: start + complete run", () => {
  it("produces identical rows in Python and TS dbs", async () => {
    const agentId = `executor-835-${Date.now()}`;
    const startArgs = [
      "start",
      "--agent-id", agentId,
      "--role", "executor",
      "--discussion", "835",
      "--pr", "42",
      "--event-id", agentId,
      "--model", "claude-sonnet-4-6",
    ];
    const completeArgs = [
      "complete",
      "--agent-id", agentId,
      "--verdict", "done",
      "--input-tokens", "62000",
      "--output-tokens", "8400",
      "--cache-read", "0",
      "--cache-write", "0",
      "--cache-creation-tokens", "100",
      "--first-write-turn", "3",
      "--total-turns", "25",
    ];

    // Run Python
    const pyStart = await runPy(startArgs, pyDir);
    expect(pyStart.exitCode).toBe(0);
    const pyComplete = await runPy(completeArgs, pyDir);
    expect(pyComplete.exitCode).toBe(0);

    // Run TS
    const tsStart = await runTs(startArgs, tsDir);
    expect(tsStart.exitCode).toBe(0);
    const tsComplete = await runTs(completeArgs, tsDir);
    expect(tsComplete.exitCode).toBe(0);

    // Compare rows
    const pyRows = await readRows(pyDir);
    const tsRows = await readRows(tsDir);

    expect(pyRows.length).toBe(1);
    expect(tsRows.length).toBe(1);

    const py = pyRows[0];
    const ts = tsRows[0];

    console.log("Python row:", JSON.stringify(py, null, 2));
    console.log("TS row:", JSON.stringify(ts, null, 2));

    expect(ts.agent_id).toBe(py.agent_id);
    expect(ts.role).toBe(py.role);
    expect(ts.discussion).toBe(py.discussion);
    expect(ts.pr).toBe(py.pr);
    expect(ts.verdict).toBe(py.verdict);
    expect(ts.model).toBe(py.model);
    expect(ts.input_tok).toBe(py.input_tok);
    expect(ts.output_tok).toBe(py.output_tok);
    expect(ts.cache_read).toBe(py.cache_read);
    expect(ts.cache_write).toBe(py.cache_write);
    expect(ts.cache_creation_tokens).toBe(py.cache_creation_tokens);
    expect(ts.first_write_turn).toBe(py.first_write_turn);
    expect(ts.total_turns).toBe(py.total_turns);
    expect(ts.event_id).toBe(py.event_id);
    expect(ts.has_start_ts).toBe(py.has_start_ts);
    expect(ts.has_end_ts).toBe(py.has_end_ts);
    expect(ts.duration_s_present).toBe(py.duration_s_present);
  });
});

// ---------------------------------------------------------------------------
// Test 2: complete without prior start (idempotent UPSERT creates row)
// ---------------------------------------------------------------------------

describe("parity: complete-only (no prior start)", () => {
  it("creates a row with start_ts = end_ts and an honest (NULL) duration", async () => {
    const agentId = `code-reviewer-nod-${Date.now()}`;
    const completeArgs = [
      "complete",
      "--agent-id", agentId,
      "--verdict", "pass",
      "--input-tokens", "5000",
      "--output-tokens", "1200",
    ];

    await runPy(completeArgs, pyDir);
    await runTs(completeArgs, tsDir);

    const pyRows = await readRows(pyDir);
    const tsRows = await readRows(tsDir);

    expect(pyRows.length).toBe(1);
    expect(tsRows.length).toBe(1);

    const py = pyRows[0];
    const ts = tsRows[0];

    expect(ts.role).toBe(py.role); // both "orphan-unmatched" — no start_run() row matched this agent_id in either lane
    expect(ts.verdict).toBe(py.verdict);
    expect(ts.input_tok).toBe(py.input_tok);
    expect(ts.output_tok).toBe(py.output_tok);
    expect(ts.has_start_ts).toBe(py.has_start_ts); // true (start_ts = end_ts)
    expect(ts.has_end_ts).toBe(py.has_end_ts);     // true
    // false in both lanes: no start_run() row and no recoverable start time,
    // so duration_s is NULL (unknown), never a guessed 0 (D#2316 PR-b).
    expect(ts.duration_s_present).toBe(py.duration_s_present);
  });
});

// ---------------------------------------------------------------------------
// Test 3: start + reconcile (ghost close)
// ---------------------------------------------------------------------------

describe("parity: start + reconcile", () => {
  it("closes stale ghost row with verdict=reconciled-stale", async () => {
    const agentId = `executor-nod-${Date.now()}`;
    const startArgs = [
      "start",
      "--agent-id", agentId,
      "--role", "executor",
    ];

    await runPy(startArgs, pyDir);
    await runTs(startArgs, tsDir);

    // Use --db-path to target each separate DB explicitly
    // (STATS_DB_PATH env var set by runPy/runTs handles the start, but reconcile
    // also accepts --db-path for explicit override)
    const pyRecArgs = ["reconcile", "--stale-after-min", "0", "--db-path", join(pyDir, "stats.duckdb")];
    const tsRecArgs = ["reconcile", "--stale-after-min", "0", "--db-path", join(tsDir, "stats.duckdb")];

    const pyRec = await runProcess(["python3", PY_ENTRY, ...pyRecArgs], {});
    const tsRec = await runProcess(["bun", "run", TS_ENTRY, ...tsRecArgs], {});

    // Both should report closing 1 row
    expect(pyRec.stdout.trim()).toBe("reconciled: 1 rows");
    expect(tsRec.stdout.trim()).toBe("reconciled: 1 rows");

    const pyRows = await readRows(pyDir);
    const tsRows = await readRows(tsDir);

    expect(pyRows.length).toBe(1);
    expect(tsRows.length).toBe(1);

    expect(pyRows[0].verdict).toBe("reconciled-stale");
    expect(tsRows[0].verdict).toBe("reconciled-stale");
    expect(tsRows[0].verdict).toBe(pyRows[0].verdict);
    expect(tsRows[0].has_end_ts).toBe(pyRows[0].has_end_ts); // both true
    expect(tsRows[0].duration_s_present).toBe(pyRows[0].duration_s_present);
  });
});

// ---------------------------------------------------------------------------
// Test 4: reconcile does not close live IDs
// ---------------------------------------------------------------------------

describe("parity: reconcile respects live-ids", () => {
  it("does not close a row whose agent_id is in live-ids", async () => {
    const agentId = `executor-nod-${Date.now()}`;
    await runPy(["start", "--agent-id", agentId, "--role", "executor"], pyDir);
    await runTs(["start", "--agent-id", agentId, "--role", "executor"], tsDir);

    // Use --db-path to target each dir explicitly; agent is in --live-ids so not closed
    const pyRec = await runProcess(
      ["python3", PY_ENTRY, "reconcile", "--stale-after-min", "0",
       "--db-path", join(pyDir, "stats.duckdb"), "--live-ids", agentId],
      {}
    );
    const tsRec = await runProcess(
      ["bun", "run", TS_ENTRY, "reconcile", "--stale-after-min", "0",
       "--db-path", join(tsDir, "stats.duckdb"), "--live-ids", agentId],
      {}
    );

    expect(pyRec.stdout.trim()).toBe("reconciled: 0 rows");
    expect(tsRec.stdout.trim()).toBe("reconciled: 0 rows");

    const pyRows = await readRows(pyDir);
    const tsRows = await readRows(tsDir);

    // Row still open (end_ts is null)
    expect(pyRows[0].has_end_ts).toBe(false);
    expect(tsRows[0].has_end_ts).toBe(pyRows[0].has_end_ts);
  });
});

// ---------------------------------------------------------------------------
// Test 5: exit codes — missing args
// ---------------------------------------------------------------------------

describe("parity: exit codes on missing required args", () => {
  it("start without --agent-id exits non-zero", async () => {
    const py = await runPy(["start", "--role", "executor"], pyDir);
    const ts = await runTs(["start", "--role", "executor"], tsDir);
    // Both should be non-zero
    expect(py.exitCode).not.toBe(0);
    expect(ts.exitCode).not.toBe(0);
  });

  it("complete without --agent-id exits non-zero", async () => {
    const py = await runPy(["complete", "--verdict", "done"], pyDir);
    const ts = await runTs(["complete", "--verdict", "done"], tsDir);
    expect(py.exitCode).not.toBe(0);
    expect(ts.exitCode).not.toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Test 6: backfill (empty audit log → 0 rows processed)
// ---------------------------------------------------------------------------

describe("parity: backfill with empty audit log", () => {
  it("reports 0 rows processed", async () => {
    // Point backfill at a non-existent audit path so it finds nothing
    const fakeAudit = join(tsDir, "empty.jsonl");
    const backfillArgs = ["backfill", "--audit-path", fakeAudit];

    const py = await runPy(backfillArgs, pyDir);
    const ts = await runTs(backfillArgs, tsDir);

    expect(py.stdout.trim()).toBe("backfill: 0 rows processed");
    expect(ts.stdout.trim()).toBe("backfill: 0 rows processed");
    expect(py.exitCode).toBe(0);
    expect(ts.exitCode).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Test 7: programmatic API (TS-only) — startRun + completeRun
// ---------------------------------------------------------------------------

describe("programmatic API: startRun + completeRun", () => {
  it("inserts and updates a row correctly", async () => {
    const agentId = `api-test-${Date.now()}`;

    // Point startRun/completeRun at tsDir via STATS_DB_PATH
    const origDbPath = process.env["STATS_DB_PATH"];
    process.env["STATS_DB_PATH"] = join(tsDir, "stats.duckdb");

    try {
      await startRun({
        agentId,
        role: "security-reviewer",
        discussion: 999,
        pr: 77,
        eventId: agentId,
        model: "claude-opus-4",
      });

      await completeRun({
        agentId,
        verdict: "pass",
        inputTok: 10000,
        outputTok: 2000,
        cacheRead: 500,
        cacheWrite: 100,
        firstWriteTurn: 5,
        totalTurns: 30,
        routedVia: "cc",
        autoRouted: false,
      });
    } finally {
      if (origDbPath !== undefined) {
        process.env["STATS_DB_PATH"] = origDbPath;
      } else {
        delete process.env["STATS_DB_PATH"];
      }
    }

    const rows = await readRows(tsDir);
    expect(rows.length).toBe(1);

    const row = rows[0];
    expect(row.agent_id).toBe(agentId);
    expect(row.role).toBe("security-reviewer");
    expect(row.discussion).toBe(999);
    expect(row.pr).toBe(77);
    expect(row.verdict).toBe("pass");
    expect(row.model).toBe("claude-opus-4");
    expect(row.input_tok).toBe(10000);
    expect(row.output_tok).toBe(2000);
    expect(row.cache_read).toBe(500);
    expect(row.cache_write).toBe(100);
    expect(row.first_write_turn).toBe(5);
    expect(row.total_turns).toBe(30);
    expect(row.routed_via).toBe("cc");
    expect(row.auto_routed).toBe(false);
    expect(row.event_id).toBe(agentId);
    expect(row.has_start_ts).toBe(true);
    expect(row.has_end_ts).toBe(true);
    expect(row.duration_s_present).toBe(true);

    console.log("Programmatic API row:", JSON.stringify(row, null, 2));
  });
});

// ---------------------------------------------------------------------------
// Test 8: token validation — negative / non-int values are rejected
// ---------------------------------------------------------------------------

describe("parity: invalid token values rejected", () => {
  it("negative input-tokens is silently discarded", async () => {
    const agentId = `bad-tokens-${Date.now()}`;
    const args = [
      "complete", "--agent-id", agentId,
      "--verdict", "done",
      "--input-tokens", "-5",
      "--output-tokens", "100",
    ];

    await runPy(args, pyDir);
    await runTs(args, tsDir);

    const pyRows = await readRows(pyDir);
    const tsRows = await readRows(tsDir);

    // Both should store null for input_tok (rejected) and 100 for output_tok
    expect(pyRows[0].input_tok).toBeNull();
    expect(tsRows[0].input_tok).toBeNull();
    expect(pyRows[0].output_tok).toBe(100);
    expect(tsRows[0].output_tok).toBe(100);
  });
});
