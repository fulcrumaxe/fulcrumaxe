/**
 * Unit tests for duckdb-helpers.ts (prerequisite B, D#1437).
 *
 * Tests the timestamp conversion helper and rowToDict behavior.
 * These tests use the live stats.duckdb for integration smoke tests.
 *
 * Run: bun test tests/duckdb-helpers.test.ts
 */

import { describe, it, expect } from "bun:test";
import {
  isDuckDbTimestamp,
  tsToIso,
  rowToDict,
  openReadConn,
  closeConn,
  queryDicts,
} from "../src/duckdb-helpers.js";
import type { DuckDbTimestampValue } from "../src/duckdb-helpers.js";

// ---------------------------------------------------------------------------
// isDuckDbTimestamp — type guard
// ---------------------------------------------------------------------------

describe("isDuckDbTimestamp", () => {
  it("identifies an object with .micros: bigint as DuckDBTimestampValue", () => {
    const mock = { micros: 1778518106022000n, toString: () => "2026-05-11 16:48:26.022" };
    expect(isDuckDbTimestamp(mock)).toBe(true);
  });

  it("rejects plain object without micros", () => {
    expect(isDuckDbTimestamp({ time: "2026-05-11" })).toBe(false);
  });

  it("rejects null", () => {
    expect(isDuckDbTimestamp(null)).toBe(false);
  });

  it("rejects string", () => {
    expect(isDuckDbTimestamp("2026-05-11T16:48:26Z")).toBe(false);
  });

  it("rejects object with micros: number (not bigint)", () => {
    expect(isDuckDbTimestamp({ micros: 123456 })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// tsToIso — DuckDBTimestampValue → ISO-8601 UTC string
// ---------------------------------------------------------------------------

describe("tsToIso", () => {
  it("converts a known DuckDB timestamp correctly", () => {
    // Timestamp from SPIKE-1 harness: 2026-05-11T16:48:26Z
    // DuckDB .toString() shows "2026-05-11 16:48:26.022"
    // .micros = 1778518106022000n  (microseconds since epoch)
    const mock: DuckDbTimestampValue = {
      micros: 1778518106022000n,
      toString: () => "2026-05-11 16:48:26.022",
    };
    expect(tsToIso(mock)).toBe("2026-05-11T16:48:26Z");
  });

  it("produces ISO-8601 with Z suffix (no milliseconds)", () => {
    // 2026-01-01T00:00:00Z = 1767225600 seconds = 1767225600000000 microseconds
    const epochMs = new Date("2026-01-01T00:00:00Z").getTime();
    const mock: DuckDbTimestampValue = {
      micros: BigInt(epochMs) * 1000n,
      toString: () => "2026-01-01 00:00:00.000",
    };
    const result = tsToIso(mock);
    expect(result).toBe("2026-01-01T00:00:00Z");
    // Must end with Z and have no milliseconds
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it("matches Python's strftime output format: YYYY-MM-DDTHH:MM:SSZ", () => {
    // Python: datetime(2026, 5, 23, 17, 39, 14, tzinfo=utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    // → "2026-05-23T17:39:14Z"
    const epochMs = new Date("2026-05-23T17:39:14Z").getTime();
    const mock: DuckDbTimestampValue = {
      micros: BigInt(epochMs) * 1000n,
      toString: () => "2026-05-23 17:39:14.000",
    };
    expect(tsToIso(mock)).toBe("2026-05-23T17:39:14Z");
  });
});

// ---------------------------------------------------------------------------
// rowToDict — column names + raw values → dict with type conversions
// ---------------------------------------------------------------------------

describe("rowToDict", () => {
  it("converts DuckDBTimestampValue to ISO string", () => {
    const ts: DuckDbTimestampValue = {
      micros: 1778518106022000n,
      toString: () => "2026-05-11 16:48:26.022",
    };
    const result = rowToDict(["ts", "val"], [ts, 1.5]);
    expect(result["ts"]).toBe("2026-05-11T16:48:26Z");
    expect(result["val"]).toBe(1.5);
  });

  it("converts bigint to number for safe values", () => {
    const result = rowToDict(["count"], [3549n]);
    expect(result["count"]).toBe(3549);
  });

  it("converts bigint to string for values > MAX_SAFE_INTEGER", () => {
    const large = BigInt(Number.MAX_SAFE_INTEGER) + 1n;
    const result = rowToDict(["big"], [large]);
    expect(typeof result["big"]).toBe("string");
  });

  it("passes through null", () => {
    const result = rowToDict(["x"], [null]);
    expect(result["x"]).toBeNull();
  });

  it("passes through string and number unchanged", () => {
    const result = rowToDict(["s", "n"], ["hello", 3.14]);
    expect(result["s"]).toBe("hello");
    expect(result["n"]).toBe(3.14);
  });
});

// ---------------------------------------------------------------------------
// Integration: live DuckDB queries (smoke tests against real stats.duckdb)
// ---------------------------------------------------------------------------

describe("DuckDB integration: live stats.duckdb reads", () => {
  it("queryDicts returns rows with converted timestamps from metric_event", async () => {
    let h;
    try {
      h = await openReadConn();
    } catch {
      console.warn("stats.duckdb not found — skipping live integration test");
      return;
    }
    try {
      const rows = await queryDicts(h, `
        SELECT metric, value, unit, ts
        FROM metric_event
        ORDER BY ts DESC
        LIMIT 3
      `);
      // Should have at least 1 row
      expect(rows.length).toBeGreaterThan(0);

      for (const row of rows) {
        // ts must be a string in ISO-8601 format (not a DuckDBTimestampValue object)
        const ts = row["ts"] as string;
        expect(typeof ts).toBe("string");
        expect(ts).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
        // value must be a number (DOUBLE column)
        expect(typeof row["value"]).toBe("number");
      }
    } finally {
      closeConn(h);
    }
  });

  it("queryDicts converts COUNT(*) bigint to number", async () => {
    let h;
    try {
      h = await openReadConn();
    } catch {
      console.warn("stats.duckdb not found — skipping live integration test");
      return;
    }
    try {
      const rows = await queryDicts(h, "SELECT COUNT(*) AS cnt FROM agent_run");
      expect(rows.length).toBe(1);
      const cnt = rows[0]["cnt"];
      // After conversion, COUNT(*) should be a number (not bigint — not JSON-serializable)
      expect(typeof cnt).toBe("number");
      // Must be JSON-serializable
      expect(() => JSON.stringify(rows)).not.toThrow();
    } finally {
      closeConn(h);
    }
  });

  it("prepared statement query works with varchar params", async () => {
    let h;
    try {
      h = await openReadConn();
    } catch {
      console.warn("stats.duckdb not found — skipping live integration test");
      return;
    }
    try {
      // Use a 30-day cutoff like stats_reader.series() does
      const cutoff = new Date(Date.now() - 30 * 24 * 3600 * 1000)
        .toISOString()
        .replace("T", " ")
        .slice(0, 19);
      const rows = await queryDicts(
        h,
        `SELECT ts, value FROM metric_event WHERE metric = ? AND ts >= CAST(? AS TIMESTAMP) ORDER BY ts DESC LIMIT 5`,
        ["scan_to_spawn_ratio", cutoff]
      );
      // Should have at least some rows or empty array — both are fine
      expect(Array.isArray(rows)).toBe(true);
      for (const row of rows) {
        const ts = row["ts"] as string;
        expect(ts).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
      }
    } finally {
      closeConn(h);
    }
  });
});
