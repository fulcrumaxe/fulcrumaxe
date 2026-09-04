/**
 * duckdb-helpers.ts — thin conversion layer for @duckdb/node-api v1.5.x types.
 *
 * @duckdb/node-api ("Neo") returns non-standard types that need normalizing
 * before they can be used in JSON responses or compared against Python output:
 *
 *   - Timestamps are returned as DuckDBTimestampValue objects with .micros (bigint)
 *     and .toString() → "YYYY-MM-DD HH:MM:SS.mmm"
 *     NOT JS Date objects.
 *
 *   - COUNT(*) and integer aggregates return bigint, not number.
 *
 *   - Float columns (DOUBLE, FLOAT) return JS number as expected.
 *
 * These conversions are documented in the SPIKE-1 FINDINGS.md and are the
 * canonical patterns for all P3 route implementations.
 *
 * Relationship to normalizer.ts rule 4 (int64 exact path):
 *   convertDuckDbRow() calls bigIntToExact() from normalizer.ts for every bigint
 *   value so the conversion happens once, at the boundary, before JSON serialization.
 *   The normalizer then sees plain number or string — no BigInt reaches JSON.stringify.
 */

import { DuckDBInstance } from "@duckdb/node-api";
import { statsDb } from "./config/state-paths.js";
import { bigIntToExact } from "./normalizer.js";

// ---------------------------------------------------------------------------
// Database path — mirrors Python agent_run_reader._db_path() priority order
// ---------------------------------------------------------------------------

function dbPath(): string {
  const env = process.env.STATS_DB_PATH;
  if (env) return env;

  return statsDb();
}

// ---------------------------------------------------------------------------
// Connection factory — per-call read-only, matches Python's per-call pattern.
//
// Python stats_connection.py opens a fresh read-only duckdb connection on every
// call (see the docstring there: "Opens a fresh read-only connection on every
// call and returns it to the caller").  We replicate that pattern here.
//
// Callers MUST call closeConn() in a finally block to release the DuckDB
// read lock promptly — same discipline as Python's try/finally conn.close().
// ---------------------------------------------------------------------------

export type DuckDbHandle = {
  conn: Awaited<ReturnType<InstanceType<typeof DuckDBInstance>["connect"]>>;
  instance: InstanceType<typeof DuckDBInstance>;
};

export async function openReadConn(): Promise<DuckDbHandle> {
  const path = dbPath();
  const instance = await DuckDBInstance.create(path, { access_mode: "READ_ONLY" });
  const conn = await instance.connect();
  return { conn, instance };
}

export function closeConn(h: DuckDbHandle): void {
  try { h.conn.closeSync(); } catch { /* ignore */ }
  try { h.instance.closeSync(); } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Timestamp conversion — DuckDBTimestampValue → ISO-8601 UTC string
//
// @duckdb/node-api v1.5.x returns timestamps as DuckDBTimestampValue objects:
//   { micros: bigint }   (microseconds since Unix epoch, UTC)
//   .toString() → "YYYY-MM-DD HH:MM:SS.mmm"  (local repr, not ISO-8601)
//
// Python duckdb returns datetime objects with tzinfo=UTC, which Python's
// isoformat() serializes as "2026-05-23T17:39:14+00:00".  After passing
// through the Python REST layer it becomes "2026-05-23T17:39:14Z" (via
// datetime.strftime("%Y-%m-%dT%H:%M:%SZ") in _row_to_dict).
//
// This function converts the DuckDB micros-bigint to that canonical form,
// matching Python's output exactly after the normalizer's timestamp rule.
// ---------------------------------------------------------------------------

export interface DuckDbTimestampValue {
  micros: bigint;
  toString(): string;
}

export function isDuckDbTimestamp(val: unknown): val is DuckDbTimestampValue {
  return (
    val !== null &&
    typeof val === "object" &&
    "micros" in (val as object) &&
    typeof (val as { micros: unknown }).micros === "bigint"
  );
}

/**
 * Convert a DuckDBTimestampValue to a canonical ISO-8601 UTC string.
 *
 * The conversion uses the .micros bigint:
 *   milliseconds = Number(micros / 1000n)   (integer division, no precision loss for dates)
 *   new Date(milliseconds).toISOString()    → "YYYY-MM-DDTHH:MM:SS.mmmZ"
 *   strip milliseconds                      → "YYYY-MM-DDTHH:MM:SSZ"
 *
 * This matches Python's isoformat() + normalizer rule 2 canonical form.
 *
 * Note: dividing micros by 1000n (bigint division) is safe for any date in the
 * range representable by JS Date (±100 million days from epoch), which covers
 * all realistic stats.duckdb timestamps.
 */
export function tsToIso(val: DuckDbTimestampValue): string {
  const ms = Number(val.micros / 1000n);
  const d = new Date(ms);
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

// ---------------------------------------------------------------------------
// Row converter — turn a DuckDB raw row (unknown[]) + column names into a dict
//
// Mirrors Python agent_run_reader._row_to_dict():
//   - datetime → ISO-8601 string (via tsToIso)
//   - bigint   → exact number or string (via bigIntToExact from normalizer)
//   - everything else passes through unchanged
// ---------------------------------------------------------------------------

export function rowToDict(colNames: string[], row: unknown[]): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (let i = 0; i < colNames.length; i++) {
    const val = row[i] ?? null;
    if (isDuckDbTimestamp(val)) {
      result[colNames[i]] = tsToIso(val);
    } else if (typeof val === "bigint") {
      result[colNames[i]] = bigIntToExact(val);
    } else {
      result[colNames[i]] = val;
    }
  }
  return result;
}

/**
 * Run a query and return rows as an array of column-name → value dicts.
 * Timestamps and bigints are converted at this boundary.
 *
 * @param h       Open DuckDbHandle (caller owns lifetime)
 * @param sql     SQL query string
 * @param params  Bound parameters (varchar only for prepared statements)
 */
export async function queryDicts(
  h: DuckDbHandle,
  sql: string,
  params: string[] = []
): Promise<Record<string, unknown>[]> {
  let result;
  if (params.length > 0) {
    const stmt = await h.conn.prepare(sql);
    try {
      for (let i = 0; i < params.length; i++) {
        stmt.bindVarchar(i + 1, params[i]);
      }
      result = await stmt.runAndReadAll();
    } finally {
      try { stmt.destroySync(); } catch { /* ignore */ }
    }
  } else {
    result = await h.conn.runAndReadAll(sql);
  }
  const rows = result.getRows() as unknown[][];
  const columnNames = result.columnNames() as string[];
  return rows.map(row => rowToDict(columnNames, row));
}
