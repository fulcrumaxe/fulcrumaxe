/**
 * DuckDB Bun/Node spike harness.
 *
 * Tests whether @duckdb/node-api works correctly and stably under Bun 1.3.14,
 * running the same representative queries used by stats_reader.py and
 * agent_run_reader.py against the real stats.duckdb.
 *
 * Usage:
 *   bun run ts-backend/spikes/duckdb/harness.ts           # Bun
 *   node --experimental-strip-types ts-backend/spikes/duckdb/harness.ts  # Node
 *
 * Exit 0 = all checks passed.  Exit 1 = failure.
 *
 * API notes (@duckdb/node-api v1.5.x "Neo"):
 *   - DuckDBInstance.create(path, options) → instance
 *   - instance.connect()                   → connection
 *   - connection.closeSync() / disconnectSync()
 *   - instance.closeSync()
 *   - connection.runAndReadAll(sql)        → DuckDBMaterializedResult
 *   - result.getRows()                     → unknown[][]
 *   - connection.prepare(sql)              → DuckDBPreparedStatement
 *   - stmt.bindVarchar(1-indexed, val)
 *   - stmt.runAndReadAll()                 → DuckDBMaterializedResult
 *   - stmt.destroySync()
 */

import { DuckDBInstance } from "@duckdb/node-api";
import { homedir } from "os";
import { join } from "path";
import process from "process";

const DB_PATH =
  process.env.STATS_DB_PATH ??
  join(homedir(), ".autonomous-forever-state", "stats.duckdb");

const RUNTIME =
  typeof Bun !== "undefined" ? `Bun ${Bun.version}` : `Node ${process.version}`;

// Python baseline values captured 2026-05-23 for comparison
const PYTHON_BASELINE = {
  metricSummaryRowCount: 16,
  agentRunCountMin: 3540, // may grow; test for >=
  tokenSumInputMin: 527056, // may grow; test for >=
  maxInputTokMin: 80000,
  agentRunMaxDurationMin: 240000, // Python saw 249042
};

type Result = {
  name: string;
  passed: boolean;
  note: string;
};

async function openConn() {
  const instance = await DuckDBInstance.create(DB_PATH, {
    access_mode: "READ_ONLY",
  });
  const conn = await instance.connect();
  // Attach close helper so callers can do cleanup uniformly
  return { conn, instance };
}

function closeConn(h: { conn: ReturnType<typeof openConn> extends Promise<infer T> ? T : never; instance: unknown }) {
  try { (h.conn as { closeSync: () => void }).closeSync(); } catch { /* ignore */ }
  try { (h.instance as { closeSync: () => void }).closeSync(); } catch { /* ignore */ }
}

// ──────────────────────────────────────────────────────────────────────────────
// Q1: Latest metric values (window-function + ORDER BY + timestamp type)
// Mirrors stats_reader.summary()
// ──────────────────────────────────────────────────────────────────────────────
async function testQ1(): Promise<Result> {
  const h = await openConn();
  try {
    const reader = await h.conn.runAndReadAll(`
      SELECT metric, value, unit, ts
      FROM (
        SELECT metric, value, unit, ts,
               ROW_NUMBER() OVER (PARTITION BY metric ORDER BY ts DESC) AS rn
        FROM metric_event
      ) t
      WHERE rn = 1
      ORDER BY metric
    `);
    const rows = reader.getRows();
    const rowCount = rows.length;
    // @duckdb/node-api returns timestamps as DuckDBTimestampValue objects (not JS Date).
    // They have .micros (BigInt since epoch) and .toString() → "YYYY-MM-DD HH:MM:SS.mmm".
    // This is consistent between Bun and Node — document as a driver-level conversion requirement.
    const tsField = rows[0]?.[3] as { micros?: bigint; toString?: () => string } | null;
    const hasMicros = typeof tsField?.micros === "bigint";
    const tsStr = tsField?.toString?.() ?? "";
    // Verify the timestamp converts to a plausible year (2025 or 2026)
    const tsYearOk = tsStr.startsWith("202");
    // Spot-check first row has a non-null metric string
    const firstMetric = rows[0]?.[0];
    const metricIsString = typeof firstMetric === "string" && firstMetric.length > 0;
    const ok =
      rowCount >= PYTHON_BASELINE.metricSummaryRowCount &&
      hasMicros &&
      tsYearOk &&
      metricIsString;
    return {
      name: "Q1: metric_event summary (window fn + timestamps)",
      passed: ok,
      note: `rows=${rowCount} (>=${PYTHON_BASELINE.metricSummaryRowCount}), ts.constructor=${(tsField as { constructor?: { name?: string } })?.constructor?.name}, hasMicros=${hasMicros}, tsStr="${tsStr.substring(0, 24)}", firstMetric=${String(firstMetric)}`,
    };
  } finally {
    closeConn(h);
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Q2: APPROX_QUANTILE over agent_run.duration_s (float aggregates + COUNT)
// Mirrors agent_run_reader.duration_percentiles()
// COUNT(*) returns bigint in node-api — this is the key numeric fidelity test
// ──────────────────────────────────────────────────────────────────────────────
async function testQ2(): Promise<Result> {
  const h = await openConn();
  try {
    const reader = await h.conn.runAndReadAll(`
      SELECT
        COUNT(*)         AS n,
        MIN(duration_s)  AS min_s,
        MAX(duration_s)  AS max_s,
        AVG(duration_s)  AS mean_s,
        APPROX_QUANTILE(duration_s, 0.50) AS p50,
        APPROX_QUANTILE(duration_s, 0.90) AS p90,
        APPROX_QUANTILE(duration_s, 0.99) AS p99
      FROM agent_run
      WHERE end_ts IS NOT NULL
    `);
    const rows = reader.getRows();
    const [n, _minS, maxS, meanS, p50, p90, p99] = rows[0] ?? [];
    // COUNT returns BigInt in this driver
    const nNum = typeof n === "bigint" ? Number(n) : Number(n ?? 0);
    const maxNum = Number(maxS ?? 0);
    const ok =
      nNum >= PYTHON_BASELINE.agentRunCountMin &&
      maxNum > PYTHON_BASELINE.agentRunMaxDurationMin &&
      typeof p50 === "number" &&
      typeof p90 === "number" &&
      typeof p99 === "number";
    return {
      name: "Q2: agent_run APPROX_QUANTILE float aggregates + bigint COUNT",
      passed: ok,
      note: `n=${nNum} (bigint=${typeof n === "bigint"}, >=${PYTHON_BASELINE.agentRunCountMin}), max_s=${maxNum.toFixed(2)}, p50=${Number(p50).toFixed(4)}, p90=${Number(p90).toFixed(4)}, p99=${Number(p99).toFixed(2)}, mean=${Number(meanS).toFixed(4)}`,
    };
  } finally {
    closeConn(h);
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Q3: int64/bigint token SUM columns — numeric fidelity under Bun
// Mirrors token aggregation in stats routes
// ──────────────────────────────────────────────────────────────────────────────
async function testQ3(): Promise<Result> {
  const h = await openConn();
  try {
    const reader = await h.conn.runAndReadAll(`
      SELECT SUM(input_tok), SUM(output_tok), MAX(input_tok)
      FROM agent_run
    `);
    const rows = reader.getRows();
    const [sumInput, sumOutput, maxInput] = rows[0] ?? [];
    const sumInputNum = typeof sumInput === "bigint" ? Number(sumInput) : Number(sumInput ?? 0);
    const maxInputNum = typeof maxInput === "bigint" ? Number(maxInput) : Number(maxInput ?? 0);
    const ok =
      sumInputNum >= PYTHON_BASELINE.tokenSumInputMin &&
      maxInputNum >= PYTHON_BASELINE.maxInputTokMin;
    return {
      name: "Q3: agent_run int64 SUM/MAX token columns (fidelity)",
      passed: ok,
      note: `SUM(input_tok)=${sumInputNum} (>=${PYTHON_BASELINE.tokenSumInputMin}), SUM(output_tok)=${Number(sumOutput ?? 0)}, MAX(input_tok)=${maxInputNum} (>=${PYTHON_BASELINE.maxInputTokMin})`,
    };
  } finally {
    closeConn(h);
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Q4: Parametrised query with CAST(? AS TIMESTAMP)
// Mirrors stats_reader.series() — the exact pattern the route uses
// ──────────────────────────────────────────────────────────────────────────────
async function testQ4(): Promise<Result> {
  const h = await openConn();
  let stmt: Awaited<ReturnType<typeof h.conn.prepare>> | undefined;
  try {
    stmt = await h.conn.prepare(`
      SELECT ts, value
      FROM metric_event
      WHERE metric = ?
        AND ts >= CAST(? AS TIMESTAMP)
      ORDER BY ts DESC
      LIMIT 10
    `);
    // 30 days ago
    const cutoff = new Date(Date.now() - 30 * 24 * 3600 * 1000)
      .toISOString()
      .replace("T", " ")
      .slice(0, 19);
    stmt.bindVarchar(1, "scan_to_spawn_ratio");
    stmt.bindVarchar(2, cutoff);
    const reader = await stmt.runAndReadAll();
    const rows = reader.getRows();
    const ok = rows.length >= 1;
    const tsField = rows[0]?.[0] as { micros?: bigint; toString?: () => string } | null;
    const hasMicros = typeof tsField?.micros === "bigint";
    const tsStr = tsField?.toString?.() ?? "";
    return {
      name: "Q4: parametrised CAST(? AS TIMESTAMP) + timestamp result",
      passed: ok && hasMicros,
      note: `rows=${rows.length} (>=1), ts.constructor=${(tsField as { constructor?: { name?: string } })?.constructor?.name}, hasMicros=${hasMicros}, ts="${tsStr.substring(0, 24)}"`,
    };
  } finally {
    if (stmt) try { stmt.destroySync(); } catch { /* ignore */ }
    closeConn(h);
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Stability: sequential + concurrent queries — bun#17216 microtask concern
// ──────────────────────────────────────────────────────────────────────────────
async function testStability(): Promise<Result> {
  const SEQUENTIAL = 50;
  const CONCURRENT = 20;
  let seqErrors = 0;
  const seqErrorMsgs: string[] = [];

  for (let i = 0; i < SEQUENTIAL; i++) {
    try {
      const h = await openConn();
      await h.conn.runAndReadAll("SELECT COUNT(*) FROM metric_event");
      closeConn(h);
    } catch (e) {
      seqErrors++;
      if (seqErrorMsgs.length < 2) seqErrorMsgs.push(e instanceof Error ? e.message : String(e));
    }
  }

  const results = await Promise.allSettled(
    Array.from({ length: CONCURRENT }, async () => {
      const h = await openConn();
      try {
        const reader = await h.conn.runAndReadAll("SELECT COUNT(*) FROM agent_run");
        return reader.getRows()[0]?.[0];
      } finally {
        closeConn(h);
      }
    })
  );
  const concErrors = results.filter((r) => r.status === "rejected").length;
  const concErrorMsgs = results
    .filter((r): r is PromiseRejectedResult => r.status === "rejected")
    .slice(0, 2)
    .map((r) => String(r.reason));

  const ok = seqErrors === 0 && concErrors === 0;
  const errDetails = [...seqErrorMsgs, ...concErrorMsgs].join("; ");
  return {
    name: `Stability: ${SEQUENTIAL} sequential + ${CONCURRENT} concurrent`,
    passed: ok,
    note: `seq_errors=${seqErrors}, conc_errors=${concErrors}${errDetails ? "; " + errDetails : ""}`,
  };
}

// ──────────────────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────────────────
async function main() {
  console.log(`\n=== DuckDB spike harness (${RUNTIME}) ===`);
  console.log(`DB: ${DB_PATH}\n`);

  const tests = [testQ1, testQ2, testQ3, testQ4, testStability];
  const results: Result[] = [];

  for (const t of tests) {
    try {
      const r = await t();
      results.push(r);
      const mark = r.passed ? "PASS" : "FAIL";
      console.log(`[${mark}] ${r.name}`);
      console.log(`     ${r.note}`);
    } catch (e) {
      const err = e instanceof Error ? e.message : String(e);
      results.push({ name: t.name, passed: false, note: `THROWN: ${err}` });
      console.log(`[FAIL] ${t.name}`);
      console.log(`     THROWN: ${err}`);
    }
  }

  const passed = results.filter((r) => r.passed).length;
  const total = results.length;
  console.log(`\n${passed}/${total} checks passed`);

  if (passed === total) {
    console.log(`VERDICT: GO (${RUNTIME} — install OK, correct results, stable)`);
    process.exit(0);
  } else {
    const failed = results.filter((r) => !r.passed).map((r) => r.name);
    console.log(`VERDICT: NO-GO — failed: ${failed.join("; ")}`);
    process.exit(1);
  }
}

main().catch((e) => {
  console.error("Harness crashed:", e);
  process.exit(1);
});
