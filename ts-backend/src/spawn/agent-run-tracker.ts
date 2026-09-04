/**
 * spawn/agent-run-tracker.ts — Per-agent run tracking backed by DuckDB.
 *
 * Mirrors backend/agent_run_tracker.py 1:1.
 *
 * Records start and end of every agent spawn in the `agent_run` table of
 * `stats.duckdb`.  All writes are non-fatal — a DuckDB failure is logged to
 * stderr and swallowed so the caller's main task always succeeds.
 *
 * # Agent-ID canonical key contract
 * ===================================
 * Every agent_run row is keyed by agent_id, which MUST be identical between
 * startRun() and completeRun() for the UPSERT to merge them into one row.
 *
 * Canonical format:  "{role}-{discussion|nod}-{unix_timestamp}"
 *   e.g.  "executor-834-1715000000"
 *
 * Schema (also created by ensureSchema):
 *
 *   CREATE TABLE agent_run (
 *       agent_id               VARCHAR PRIMARY KEY,
 *       role                   VARCHAR NOT NULL,
 *       discussion             INTEGER,
 *       pr                     INTEGER,
 *       start_ts               TIMESTAMPTZ NOT NULL,
 *       end_ts                 TIMESTAMPTZ,
 *       duration_s             DOUBLE,
 *       verdict                VARCHAR,
 *       model                  VARCHAR,
 *       input_tok              INTEGER,
 *       output_tok             INTEGER,
 *       cache_read             INTEGER,
 *       cache_write            INTEGER,
 *       cache_creation_tokens  INTEGER,
 *       blocked_reason         VARCHAR,
 *       event_id               VARCHAR,
 *       first_write_turn       INTEGER,
 *       total_turns            INTEGER,
 *       routed_via             TEXT,
 *       auto_routed            BOOLEAN
 *   );
 *
 * CLI usage (mirrors Python CLI exactly):
 *
 *   bun run src/spawn/agent-run-tracker.ts start \
 *       --agent-id executor-635-1715000000 \
 *       --role executor \
 *       --discussion 635 \
 *       --pr 42 \
 *       --event-id executor-635-1715000000 \
 *       --model claude-sonnet-4-6
 *
 *   bun run src/spawn/agent-run-tracker.ts complete \
 *       --agent-id executor-635-1715000000 \
 *       --verdict done \
 *       --input-tokens 62000 \
 *       --output-tokens 8400 \
 *       --cache-read 0 \
 *       --cache-write 0
 *
 *   bun run src/spawn/agent-run-tracker.ts backfill
 *
 *   bun run src/spawn/agent-run-tracker.ts reconcile \
 *       --live-ids executor-835-1715000001 \
 *       --stale-after-min 30
 */

import { DuckDBInstance } from "@duckdb/node-api";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { statsDb, auditJsonl } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// DB path (mirrors agent_run_tracker._db_path logic)
// ---------------------------------------------------------------------------

function dbPath(): string {
  const env = process.env["STATS_DB_PATH"];
  if (env) return env;

  return statsDb();
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

type DuckDbConn = Awaited<ReturnType<InstanceType<typeof DuckDBInstance>["connect"]>>;

async function ensureSchema(conn: DuckDbConn): Promise<void> {
  await conn.run(`
    CREATE TABLE IF NOT EXISTS agent_run (
        agent_id               VARCHAR PRIMARY KEY,
        role                   VARCHAR NOT NULL,
        discussion             INTEGER,
        pr                     INTEGER,
        start_ts               TIMESTAMPTZ NOT NULL,
        end_ts                 TIMESTAMPTZ,
        duration_s             DOUBLE,
        verdict                VARCHAR,
        model                  VARCHAR,
        input_tok              INTEGER,
        output_tok             INTEGER,
        cache_read             INTEGER,
        cache_write            INTEGER,
        cache_creation_tokens  INTEGER,
        blocked_reason         VARCHAR,
        event_id               VARCHAR,
        first_write_turn       INTEGER,
        total_turns            INTEGER,
        routed_via             TEXT,
        auto_routed            BOOLEAN
    )
  `);

  // Idempotent column migrations — mirrors Python _ensure_schema
  try {
    const result = await conn.runAndReadAll(
      "SELECT column_name FROM information_schema.columns WHERE table_name='agent_run'"
    );
    const rows = result.getRows() as unknown[][];
    const cols = new Set(rows.map((r) => String(r[0])));

    if (!cols.has("cache_creation_tokens")) {
      await conn.run("ALTER TABLE agent_run ADD COLUMN cache_creation_tokens INTEGER");
    }
    if (!cols.has("first_write_turn")) {
      await conn.run("ALTER TABLE agent_run ADD COLUMN first_write_turn INTEGER");
    }
    if (!cols.has("total_turns")) {
      await conn.run("ALTER TABLE agent_run ADD COLUMN total_turns INTEGER");
    }
    if (!cols.has("routed_via")) {
      await conn.run("ALTER TABLE agent_run ADD COLUMN routed_via TEXT");
    }
    if (!cols.has("auto_routed")) {
      await conn.run("ALTER TABLE agent_run ADD COLUMN auto_routed BOOLEAN");
    }
  } catch {
    // migration is best-effort; table may not exist yet on first call
  }

  await conn.run(
    "CREATE INDEX IF NOT EXISTS idx_agent_run_role_start ON agent_run(role, start_ts)"
  );
  await conn.run(
    "CREATE INDEX IF NOT EXISTS idx_agent_run_pr ON agent_run(pr)"
  );
}

// ---------------------------------------------------------------------------
// Connection helpers
// ---------------------------------------------------------------------------

async function openConn(path: string): Promise<{
  conn: DuckDbConn;
  inst: InstanceType<typeof DuckDBInstance>;
}> {
  mkdirSync(dirname(path), { recursive: true });
  const inst = await DuckDBInstance.create(path);
  const conn = await inst.connect();
  return { conn, inst };
}

function closeConn(conn: DuckDbConn, inst: InstanceType<typeof DuckDBInstance>): void {
  try { conn.closeSync(); } catch { /* ignore */ }
  try { inst.closeSync(); } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Token validation (mirrors _validate_token_count)
// ---------------------------------------------------------------------------

function validateTokenCount(value: number | null | undefined, field: string): number | null {
  if (value === null || value === undefined) return null;
  if (!Number.isInteger(value)) {
    process.stderr.write(
      `agent_run_tracker: rejecting non-int ${field}=${JSON.stringify(value)} (expected non-negative int)\n`
    );
    return null;
  }
  if (value < 0) {
    process.stderr.write(
      `agent_run_tracker: rejecting negative ${field}=${value} (expected non-negative int)\n`
    );
    return null;
  }
  return value;
}

// ---------------------------------------------------------------------------
// ISO timestamp helper
// ---------------------------------------------------------------------------

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function toIso(d: Date): string {
  return d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

// ---------------------------------------------------------------------------
// Public write API
// ---------------------------------------------------------------------------

export interface StartRunParams {
  agentId: string;
  role: string;
  discussion?: number | null;
  pr?: number | null;
  eventId?: string | null;
  model?: string | null;
}

/**
 * Insert a new agent_run row with start_ts=now, end_ts=NULL.
 *
 * Non-fatal: exceptions are logged and swallowed.
 * Mirrors backend/agent_run_tracker.start_run().
 */
export async function startRun(params: StartRunParams): Promise<void> {
  const { agentId, role, discussion, pr, eventId, model } = params;
  try {
    const path = dbPath();
    const { conn, inst } = await openConn(path);
    try {
      await ensureSchema(conn);
      const stmt = await conn.prepare(
        `INSERT OR IGNORE INTO agent_run
            (agent_id, role, discussion, pr, start_ts, event_id, model)
         VALUES (?, ?, ?, ?, ?::TIMESTAMPTZ, ?, ?)`
      );
      stmt.bindVarchar(1, agentId);
      stmt.bindVarchar(2, role);
      if (discussion !== null && discussion !== undefined) {
        stmt.bindInteger(3, discussion);
      } else {
        stmt.bindNull(3);
      }
      if (pr !== null && pr !== undefined) {
        stmt.bindInteger(4, pr);
      } else {
        stmt.bindNull(4);
      }
      stmt.bindVarchar(5, nowIso());
      if (eventId !== null && eventId !== undefined) {
        stmt.bindVarchar(6, eventId);
      } else {
        stmt.bindNull(6);
      }
      if (model !== null && model !== undefined) {
        stmt.bindVarchar(7, model);
      } else {
        stmt.bindNull(7);
      }
      await stmt.run();
    } finally {
      closeConn(conn, inst);
    }
  } catch (e) {
    process.stderr.write(`agent_run_tracker.startRun failed (non-fatal): ${String(e)}\n`);
  }
}

export interface CompleteRunParams {
  agentId: string;
  endTs?: Date | null;
  durationS?: number | null;
  verdict?: string | null;
  model?: string | null;
  inputTok?: number | null;
  outputTok?: number | null;
  cacheRead?: number | null;
  cacheWrite?: number | null;
  cacheCreationTokens?: number | null;
  blockedReason?: string | null;
  firstWriteTurn?: number | null;
  totalTurns?: number | null;
  routedVia?: string | null;
  autoRouted?: boolean | null;
}

/**
 * UPSERT an agent_run row with completion data.
 *
 * Uses INSERT ... ON CONFLICT (agent_id) DO UPDATE so the call is idempotent
 * and works whether or not startRun() ran first.
 *
 * Mirrors backend/agent_run_tracker.complete_run().
 */
export async function completeRun(params: CompleteRunParams): Promise<void> {
  const {
    agentId,
    endTs: endTsParam,
    durationS,
    verdict,
    model,
    blockedReason,
    routedVia,
    autoRouted,
  } = params;

  // Validate all token fields before touching the DB
  const inputTok = validateTokenCount(params.inputTok, "input_tok");
  const outputTok = validateTokenCount(params.outputTok, "output_tok");
  const cacheRead = validateTokenCount(params.cacheRead, "cache_read");
  const cacheWrite = validateTokenCount(params.cacheWrite, "cache_write");
  const cacheCreationTokens = validateTokenCount(params.cacheCreationTokens, "cache_creation_tokens");
  const firstWriteTurn = validateTokenCount(params.firstWriteTurn, "first_write_turn");
  const totalTurns = validateTokenCount(params.totalTurns, "total_turns");

  try {
    const endTs = endTsParam ?? new Date();
    const endTsStr = toIso(endTs);

    const path = dbPath();
    const { conn, inst } = await openConn(path);
    try {
      await ensureSchema(conn);

      // Compute duration from stored start_ts if not supplied
      let computedDuration = durationS ?? null;
      if (computedDuration === null) {
        const selectStmt = await conn.prepare(
          "SELECT start_ts FROM agent_run WHERE agent_id = ?"
        );
        selectStmt.bindVarchar(1, agentId);
        const res = await selectStmt.runAndReadAll();
        try { selectStmt.destroySync(); } catch { /* ignore */ }
        const rows = res.getRows() as unknown[][];
        if (rows.length > 0 && rows[0][0] !== null && rows[0][0] !== undefined) {
          const startVal = rows[0][0];
          let startMs: number;
          if (
            typeof startVal === "object" &&
            startVal !== null &&
            "micros" in (startVal as object)
          ) {
            // DuckDBTimestampValue
            startMs = Number((startVal as { micros: bigint }).micros / 1000n);
          } else if (typeof startVal === "string") {
            startMs = new Date(startVal).getTime();
          } else {
            startMs = NaN;
          }
          if (!isNaN(startMs)) {
            computedDuration = (endTs.getTime() - startMs) / 1000;
          } else {
            // Stored start_ts couldn't be parsed — no recoverable start time.
            // NULL, not 0 (D#2316 PR-b parity fix): a 0s duration reads as a
            // measurement that was never actually taken. Mirrors Python's
            // complete_run(), which now writes NULL in the equivalent case
            // instead of guessing a duration of 0.
            computedDuration = null;
          }
        } else {
          // No existing row (no start_run() row matched this agent_id) and no
          // start_ts was ever recoverable. NULL, not 0 — same fix as above.
          computedDuration = null;
        }
      }

      // INSERT ... ON CONFLICT DO UPDATE makes completeRun idempotent.
      // Role literal below must stay in sync with backend/agent_run_tracker.py's
      // _ORPHAN_ROLE: a brand-new row here means no start_run() row matched this
      // agent_id, same as the Python INSERT branch, so it gets the same queryable
      // sentinel instead of the ambiguous "unknown".
      const stmt = await conn.prepare(`
        INSERT INTO agent_run
            (agent_id, role, start_ts,
             end_ts, duration_s, verdict, model,
             input_tok, output_tok, cache_read, cache_write,
             cache_creation_tokens, blocked_reason, event_id,
             first_write_turn, total_turns, routed_via, auto_routed)
        VALUES (?, 'orphan-unmatched', ?::TIMESTAMPTZ,
                ?::TIMESTAMPTZ, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?)
        ON CONFLICT (agent_id) DO UPDATE SET
            end_ts                = excluded.end_ts,
            duration_s            = COALESCE(excluded.duration_s,     agent_run.duration_s),
            verdict               = COALESCE(excluded.verdict,         agent_run.verdict),
            model                 = COALESCE(excluded.model,           agent_run.model),
            input_tok             = COALESCE(excluded.input_tok,       agent_run.input_tok),
            output_tok            = COALESCE(excluded.output_tok,      agent_run.output_tok),
            cache_read            = COALESCE(excluded.cache_read,      agent_run.cache_read),
            cache_write           = COALESCE(excluded.cache_write,     agent_run.cache_write),
            cache_creation_tokens = COALESCE(excluded.cache_creation_tokens,
                                             agent_run.cache_creation_tokens),
            blocked_reason        = COALESCE(excluded.blocked_reason,  agent_run.blocked_reason),
            first_write_turn      = COALESCE(excluded.first_write_turn,
                                             agent_run.first_write_turn),
            total_turns           = COALESCE(excluded.total_turns,
                                             agent_run.total_turns),
            routed_via            = COALESCE(excluded.routed_via,
                                             agent_run.routed_via),
            auto_routed           = COALESCE(excluded.auto_routed,
                                             agent_run.auto_routed)
      `);

      stmt.bindVarchar(1, agentId);
      stmt.bindVarchar(2, endTsStr);   // start_ts fallback for new rows
      stmt.bindVarchar(3, endTsStr);   // end_ts
      if (computedDuration !== null) {
        stmt.bindDouble(4, computedDuration);
      } else {
        stmt.bindNull(4);
      }
      if (verdict !== null && verdict !== undefined) {
        stmt.bindVarchar(5, verdict);
      } else {
        stmt.bindNull(5);
      }
      if (model !== null && model !== undefined) {
        stmt.bindVarchar(6, model);
      } else {
        stmt.bindNull(6);
      }
      if (inputTok !== null) {
        stmt.bindInteger(7, inputTok);
      } else {
        stmt.bindNull(7);
      }
      if (outputTok !== null) {
        stmt.bindInteger(8, outputTok);
      } else {
        stmt.bindNull(8);
      }
      if (cacheRead !== null) {
        stmt.bindInteger(9, cacheRead);
      } else {
        stmt.bindNull(9);
      }
      if (cacheWrite !== null) {
        stmt.bindInteger(10, cacheWrite);
      } else {
        stmt.bindNull(10);
      }
      if (cacheCreationTokens !== null) {
        stmt.bindInteger(11, cacheCreationTokens);
      } else {
        stmt.bindNull(11);
      }
      if (blockedReason !== null && blockedReason !== undefined) {
        stmt.bindVarchar(12, blockedReason);
      } else {
        stmt.bindNull(12);
      }
      stmt.bindVarchar(13, agentId); // event_id == agent_id for new rows
      if (firstWriteTurn !== null) {
        stmt.bindInteger(14, firstWriteTurn);
      } else {
        stmt.bindNull(14);
      }
      if (totalTurns !== null) {
        stmt.bindInteger(15, totalTurns);
      } else {
        stmt.bindNull(15);
      }
      if (routedVia !== null && routedVia !== undefined) {
        stmt.bindVarchar(16, routedVia);
      } else {
        stmt.bindNull(16);
      }
      if (autoRouted !== null && autoRouted !== undefined) {
        stmt.bindBoolean(17, autoRouted);
      } else {
        stmt.bindNull(17);
      }

      await stmt.run();
    } finally {
      closeConn(conn, inst);
    }
  } catch (e) {
    process.stderr.write(`agent_run_tracker.completeRun failed (non-fatal): ${String(e)}\n`);
  }
}

// ---------------------------------------------------------------------------
// Reconcile ghost open runs
// ---------------------------------------------------------------------------

/**
 * Auto-close open agent_run rows that are stale ghosts.
 *
 * Mirrors backend/agent_run_tracker.reconcile_open_runs().
 * Returns the number of rows closed.
 */
export async function reconcileOpenRuns(opts: {
  liveIds?: string[] | null;
  staleAfterMin?: number | null;
  dbPath?: string | null;
} = {}): Promise<number> {
  const liveIds = opts.liveIds ?? [];
  const staleAfterMin = opts.staleAfterMin ?? 30;
  const path = opts.dbPath ?? dbPath();

  if (!existsSync(path)) {
    return 0;
  }

  try {
    const now = new Date();
    const nowStr = toIso(now);
    const { conn, inst } = await openConn(path);
    try {
      await ensureSchema(conn);

      // Fetch open rows older than the grace window.
      // Use string-concat interval syntax to match Python's INTERVAL (? || ' minutes')
      const intervalStmt = await conn.prepare(
        `SELECT agent_id
         FROM agent_run
         WHERE end_ts IS NULL
           AND start_ts < NOW() - INTERVAL (? || ' minutes')`
      );
      intervalStmt.bindVarchar(1, String(staleAfterMin));
      const result = await intervalStmt.runAndReadAll();
      try { intervalStmt.destroySync(); } catch { /* ignore */ }
      const rows = result.getRows() as unknown[][];
      const liveSet = new Set(liveIds);
      const staleIds = rows
        .map((r) => String(r[0]))
        .filter((id) => !liveSet.has(id));

      if (staleIds.length === 0) {
        return 0;
      }

      let closed = 0;
      for (const agentId of staleIds) {
        const stmt = await conn.prepare(`
          UPDATE agent_run
          SET end_ts    = ?::TIMESTAMPTZ,
              verdict   = 'reconciled-stale',
              duration_s = COALESCE(
                  duration_s,
                  epoch(?::TIMESTAMPTZ - start_ts)
              )
          WHERE agent_id = ? AND end_ts IS NULL
        `);
        stmt.bindVarchar(1, nowStr);
        stmt.bindVarchar(2, nowStr);
        stmt.bindVarchar(3, agentId);
        await stmt.run();
        closed += 1;
      }

      process.stderr.write(
        `reconcileOpenRuns: closed ${closed} stale ghost(s) (staleAfterMin=${staleAfterMin}, live=${liveIds.length})\n`
      );
      return closed;
    } finally {
      closeConn(conn, inst);
    }
  } catch (e) {
    process.stderr.write(`agent_run_tracker.reconcileOpenRuns failed (non-fatal): ${String(e)}\n`);
    return 0;
  }
}

// ---------------------------------------------------------------------------
// Backfill from audit_trail
// ---------------------------------------------------------------------------

function auditLogPath(): string {
  const stateEnv = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  if (stateEnv) {
    const p = join(stateEnv, "audit.jsonl");
    if (existsSync(p)) return p;
  }
  return auditJsonl();
}

interface RunAccumulator {
  agent_id: string;
  role: string | null;
  discussion: number | null;
  pr: number | null;
  start_ts: Date | null;
  end_ts: Date | null;
  verdict: string | null;
  model: string | null;
  input_tok: number | null;
  output_tok: number | null;
  cache_read: number | null;
  cache_write: number | null;
}

/**
 * Reconstruct agent_run rows from audit_trail entries.
 *
 * Mirrors backend/agent_run_tracker.backfill().
 * Returns the number of rows inserted or updated.
 */
export async function backfill(opts: {
  auditPath?: string | null;
  dbPath?: string | null;
} = {}): Promise<number> {
  const auditPath = opts.auditPath ?? auditLogPath();
  const path = opts.dbPath ?? dbPath();

  // Collect entries from audit.jsonl (and audit.jsonl.1 if present)
  const entries: Record<string, unknown>[] = [];
  for (const suffix of ["", ".1"]) {
    const filePath = suffix ? auditPath + suffix : auditPath;
    if (existsSync(filePath)) {
      try {
        const content = readFileSync(filePath, "utf-8");
        for (const line of content.split("\n")) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          try {
            entries.push(JSON.parse(trimmed) as Record<string, unknown>);
          } catch {
            // skip malformed lines
          }
        }
      } catch (e) {
        process.stderr.write(`backfill: could not read ${filePath}: ${String(e)}\n`);
      }
    }
  }

  if (entries.length === 0) {
    process.stderr.write(`backfill: no audit entries found at ${auditPath}\n`);
    return 0;
  }

  // Group events by event_id into run candidates
  const runs = new Map<string, RunAccumulator>();

  for (const entry of entries) {
    let newVal = entry["new"] ?? {};
    if (typeof newVal === "string") {
      try { newVal = JSON.parse(newVal); } catch { newVal = {}; }
    }
    if (typeof newVal !== "object" || newVal === null) newVal = {};
    const nv = newVal as Record<string, unknown>;

    const eid =
      (entry["event_id"] as string | undefined) ??
      (nv["event_id"] as string | undefined);
    if (!eid) continue;

    const action = String(entry["action"] ?? "");
    const source = String(entry["source"] ?? "");

    const isStart =
      ["spawn", "agent_start", "start"].includes(action) ||
      ["spawn_agent", "pre_spawn", "pre-spawn-check"].includes(source);
    const isComplete =
      [
        "agent_done", "complete", "agent_complete", "post_agent",
        "agent_end", "verdict",
      ].includes(action) ||
      ["post_agent_hook", "post-agent-hook"].includes(source);

    if (!isStart && !isComplete) continue;

    if (!runs.has(eid)) {
      runs.set(eid, {
        agent_id: eid,
        role: null,
        discussion: null,
        pr: null,
        start_ts: null,
        end_ts: null,
        verdict: null,
        model: null,
        input_tok: null,
        output_tok: null,
        cache_read: null,
        cache_write: null,
      });
    }

    const run = runs.get(eid)!;

    const role =
      (entry["actor"] as string | undefined) ??
      (nv["role"] as string | undefined) ??
      (nv["agent"] as string | undefined) ??
      null;
    const disc = nv["discussion"];
    const pr = nv["pr"];
    const model = (nv["model"] as string | undefined) ?? null;
    const verdict = (nv["verdict"] as string | undefined) ?? null;

    const tokens = (nv["tokens"] ?? {}) as Record<string, unknown>;
    const inputTok =
      (nv["input_tokens"] as number | undefined) ??
      (tokens["input"] as number | undefined) ??
      null;
    const outputTok =
      (nv["output_tokens"] as number | undefined) ??
      (tokens["output"] as number | undefined) ??
      null;
    const cacheRead =
      (nv["cache_read_tokens"] as number | undefined) ??
      (nv["cache_read"] as number | undefined) ??
      null;
    const cacheWrite =
      (nv["cache_write_tokens"] as number | undefined) ??
      (nv["cache_write"] as number | undefined) ??
      null;

    const tsStr = entry["ts"] as string | undefined;
    let ts: Date | null = null;
    if (tsStr) {
      const parsed = new Date(tsStr.replace(/Z$/, "+00:00"));
      if (!isNaN(parsed.getTime())) ts = parsed;
    }

    // Fill in fields (first non-null wins for start fields)
    if (role && !run.role) run.role = role;
    if (disc !== undefined && disc !== null && run.discussion === null) {
      run.discussion = disc ? Number(disc) : null;
    }
    if (pr !== undefined && pr !== null && run.pr === null) {
      run.pr = pr ? Number(pr) : null;
    }
    if (model && !run.model) run.model = model;

    if (isStart && ts && run.start_ts === null) run.start_ts = ts;
    if (isComplete) {
      if (ts) run.end_ts = ts;
      if (verdict) run.verdict = verdict;
      if (inputTok !== null) run.input_tok = Number(inputTok);
      if (outputTok !== null) run.output_tok = Number(outputTok);
      if (cacheRead !== null) run.cache_read = Number(cacheRead);
      if (cacheWrite !== null) run.cache_write = Number(cacheWrite);
    }
  }

  // Write to DuckDB
  mkdirSync(dirname(path), { recursive: true });
  const { conn, inst } = await openConn(path);
  let count = 0;
  try {
    await ensureSchema(conn);

    for (const run of runs.values()) {
      // Skip if no role or no start_ts — too little data
      if (!run.role || run.start_ts === null) continue;

      // Compute duration if both timestamps known
      let dur: number | null = null;
      if (run.start_ts && run.end_ts) {
        dur = (run.end_ts.getTime() - run.start_ts.getTime()) / 1000;
      }

      const startTsStr = toIso(run.start_ts);
      const endTsStr = run.end_ts ? toIso(run.end_ts) : null;

      // INSERT OR IGNORE for new rows
      // Build SQL with optional end_ts placeholder
      const endTsPlaceholder = endTsStr ? "?::TIMESTAMPTZ" : "NULL";
      const insertSql = `
        INSERT OR IGNORE INTO agent_run
            (agent_id, role, discussion, pr, start_ts, end_ts,
             duration_s, verdict, model, input_tok, output_tok,
             cache_read, cache_write, event_id)
        VALUES (?, ?, ?, ?, ?::TIMESTAMPTZ, ${endTsPlaceholder},
                ?, ?, ?, ?, ?,
                ?, ?, ?)
      `;

      const insertStmt = await conn.prepare(insertSql);
      let idx = 1;
      insertStmt.bindVarchar(idx++, run.agent_id);
      insertStmt.bindVarchar(idx++, run.role);
      if (run.discussion !== null) {
        insertStmt.bindInteger(idx++, run.discussion);
      } else {
        insertStmt.bindNull(idx++);
      }
      if (run.pr !== null) {
        insertStmt.bindInteger(idx++, run.pr);
      } else {
        insertStmt.bindNull(idx++);
      }
      insertStmt.bindVarchar(idx++, startTsStr);
      if (endTsStr) insertStmt.bindVarchar(idx++, endTsStr);
      if (dur !== null) {
        insertStmt.bindDouble(idx++, dur);
      } else {
        insertStmt.bindNull(idx++);
      }
      if (run.verdict) {
        insertStmt.bindVarchar(idx++, run.verdict);
      } else {
        insertStmt.bindNull(idx++);
      }
      if (run.model) {
        insertStmt.bindVarchar(idx++, run.model);
      } else {
        insertStmt.bindNull(idx++);
      }
      if (run.input_tok !== null) {
        insertStmt.bindInteger(idx++, run.input_tok);
      } else {
        insertStmt.bindNull(idx++);
      }
      if (run.output_tok !== null) {
        insertStmt.bindInteger(idx++, run.output_tok);
      } else {
        insertStmt.bindNull(idx++);
      }
      if (run.cache_read !== null) {
        insertStmt.bindInteger(idx++, run.cache_read);
      } else {
        insertStmt.bindNull(idx++);
      }
      if (run.cache_write !== null) {
        insertStmt.bindInteger(idx++, run.cache_write);
      } else {
        insertStmt.bindNull(idx++);
      }
      insertStmt.bindVarchar(idx++, run.agent_id); // event_id == agent_id
      await insertStmt.run();

      // UPDATE end_ts / verdict for existing rows that were open
      if (run.end_ts !== null && endTsStr) {
        const updateStmt = await conn.prepare(`
          UPDATE agent_run SET
              end_ts     = COALESCE(end_ts, ?::TIMESTAMPTZ),
              duration_s = COALESCE(duration_s, ?),
              verdict    = COALESCE(verdict, ?)
          WHERE agent_id = ? AND end_ts IS NULL
        `);
        updateStmt.bindVarchar(1, endTsStr);
        if (dur !== null) {
          updateStmt.bindDouble(2, dur);
        } else {
          updateStmt.bindNull(2);
        }
        if (run.verdict) {
          updateStmt.bindVarchar(3, run.verdict);
        } else {
          updateStmt.bindNull(3);
        }
        updateStmt.bindVarchar(4, run.agent_id);
        await updateStmt.run();
      }

      count += 1;
    }
  } finally {
    closeConn(conn, inst);
  }

  return count;
}

// ---------------------------------------------------------------------------
// CLI entry point
// ---------------------------------------------------------------------------

interface ParsedArgs {
  command: string | null;
  flags: Record<string, string | boolean>;
  rest: string[];  // positional args after command
}

function parseArgs(argv: string[]): ParsedArgs {
  const flags: Record<string, string | boolean> = {};
  let command: string | null = null;
  const rest: string[] = [];
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i];
    if (command === null && !arg.startsWith("--")) {
      command = arg;
      i++;
    } else if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        flags[key] = next;
        i += 2;
      } else {
        flags[key] = true;
        i++;
      }
    } else {
      rest.push(arg);
      i++;
    }
  }
  return { command, flags, rest };
}

function toIntArg(v: string | boolean | undefined): number | null {
  if (v === undefined || v === null || typeof v === "boolean") return null;
  const n = parseInt(String(v), 10);
  return isNaN(n) ? null : n;
}

function toStrArg(v: string | boolean | undefined): string | null {
  if (v === undefined || v === null || typeof v === "boolean") return null;
  return String(v);
}

async function main(argv: string[]): Promise<number> {
  const { command, flags } = parseArgs(argv);

  if (command === "start") {
    const agentId = toStrArg(flags["agent-id"]);
    const role = toStrArg(flags["role"]);
    if (!agentId || !role) {
      process.stderr.write("start: --agent-id and --role are required\n");
      return 1;
    }
    await startRun({
      agentId,
      role,
      discussion: toIntArg(flags["discussion"]),
      pr: toIntArg(flags["pr"]),
      eventId: toStrArg(flags["event-id"]),
      model: toStrArg(flags["model"]),
    });
    return 0;
  }

  if (command === "complete") {
    const agentId = toStrArg(flags["agent-id"]);
    if (!agentId) {
      process.stderr.write("complete: --agent-id is required\n");
      return 1;
    }
    await completeRun({
      agentId,
      verdict: toStrArg(flags["verdict"]),
      model: toStrArg(flags["model"]),
      inputTok: toIntArg(flags["input-tokens"]),
      outputTok: toIntArg(flags["output-tokens"]),
      cacheRead: toIntArg(flags["cache-read"]),
      cacheWrite: toIntArg(flags["cache-write"]),
      cacheCreationTokens: toIntArg(flags["cache-creation-tokens"]),
      blockedReason: toStrArg(flags["blocked-reason"]),
      firstWriteTurn: toIntArg(flags["first-write-turn"]),
      totalTurns: toIntArg(flags["total-turns"]),
    });
    return 0;
  }

  if (command === "backfill") {
    const n = await backfill({
      auditPath: toStrArg(flags["audit-path"]),
      dbPath: toStrArg(flags["db-path"]),
    });
    process.stdout.write(`backfill: ${n} rows processed\n`);
    return 0;
  }

  if (command === "reconcile") {
    // --live-ids: collect all non-flag argv tokens after --live-ids
    let liveIds: string[] | null = null;
    const rawArgv = argv;
    const liveIdIdx = rawArgv.indexOf("--live-ids");
    if (liveIdIdx !== -1) {
      liveIds = [];
      let j = liveIdIdx + 1;
      while (j < rawArgv.length && !rawArgv[j].startsWith("--")) {
        liveIds.push(rawArgv[j]);
        j++;
      }
    }
    const n = await reconcileOpenRuns({
      liveIds,
      staleAfterMin: toIntArg(flags["stale-after-min"]),
      dbPath: toStrArg(flags["db-path"]),
    });
    process.stdout.write(`reconciled: ${n} rows\n`);
    return 0;
  }

  process.stderr.write(
    "agent_run_tracker — start, complete, backfill, or reconcile agent run records\n\n" +
    "  start --agent-id ID --role ROLE [--discussion N] [--pr N] [--event-id ID] [--model M]\n" +
    "  complete --agent-id ID [--verdict V] [--input-tokens N] [--output-tokens N] ...\n" +
    "  backfill [--audit-path PATH] [--db-path PATH]\n" +
    "  reconcile [--live-ids ID...] [--stale-after-min N] [--db-path PATH]\n"
  );
  return 1;
}

// Run CLI when executed directly
if (import.meta.main) {
  const exitCode = await main(process.argv.slice(2));
  process.exit(exitCode);
}
