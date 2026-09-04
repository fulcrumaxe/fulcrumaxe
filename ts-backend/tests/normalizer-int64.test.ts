/**
 * Unit tests for the normalizer int64/BigInt exact path (prerequisite A, D#1437).
 *
 * Per the task requirement:
 * "Add unit tests proving a value > Number.MAX_SAFE_INTEGER round-trips
 *  exactly. Keep the change additive to the existing normalizer behavior
 *  (don't break P0/P1/P2 fixtures)."
 *
 * Run: bun test tests/normalizer-int64.test.ts
 */

import { describe, it, expect } from "bun:test";
import { bigIntToExact, coerceBigInt } from "../src/normalizer.js";
import type { ExtendedValue } from "../src/normalizer.js";

// ---------------------------------------------------------------------------
// bigIntToExact — the canonical BigInt → JSON-safe conversion
// ---------------------------------------------------------------------------

describe("bigIntToExact: safe-integer range", () => {
  it("converts 0n to 0", () => {
    expect(bigIntToExact(0n)).toBe(0);
  });

  it("converts positive safe integer", () => {
    expect(bigIntToExact(42n)).toBe(42);
  });

  it("converts Number.MAX_SAFE_INTEGER exactly", () => {
    const n = BigInt(Number.MAX_SAFE_INTEGER);
    expect(bigIntToExact(n)).toBe(Number.MAX_SAFE_INTEGER);
  });

  it("converts Number.MIN_SAFE_INTEGER exactly", () => {
    const n = BigInt(Number.MIN_SAFE_INTEGER);
    expect(bigIntToExact(n)).toBe(Number.MIN_SAFE_INTEGER);
  });

  it("returns a number (not string) for safe integers", () => {
    expect(typeof bigIntToExact(12345n)).toBe("number");
  });
});

describe("bigIntToExact: beyond safe-integer range — exact string preservation", () => {
  // Values > Number.MAX_SAFE_INTEGER (2^53 - 1 = 9007199254740991)
  // These would lose precision if coerced through JS Number.

  it("converts MAX_SAFE_INTEGER + 1 to string (cannot be exact as number)", () => {
    const val = BigInt(Number.MAX_SAFE_INTEGER) + 1n;  // 9007199254740992n
    const result = bigIntToExact(val);
    expect(typeof result).toBe("string");
    expect(result).toBe("9007199254740992");
  });

  it("a value > MAX_SAFE_INTEGER round-trips exactly (the load-bearing test)", () => {
    // This is the test the D#1437 spec requires: "a value > Number.MAX_SAFE_INTEGER
    // round-trips exactly".  We prove that:
    //   1. Converting via Number() loses precision (lossy path)
    //   2. bigIntToExact() preserves the exact value as a string
    const large = 9007199254740993n;  // MAX_SAFE_INTEGER + 2

    // Lossy path — demonstrates the problem bigIntToExact solves:
    const lossy = Number(large);
    expect(lossy).toBe(9007199254740992); // off by 1 — precision lost!

    // Exact path:
    const exact = bigIntToExact(large);
    expect(typeof exact).toBe("string");
    expect(exact).toBe("9007199254740993");
    // Prove the string preserves the exact value BigInt can parse back:
    expect(BigInt(exact as string)).toBe(large);
  });

  it("large token SUM (realistic DuckDB value) round-trips exactly", () => {
    // Simulate a realistic large SUM(input_tok) from agent_run.
    // Current production value is ~527056, but we test a hypothetical
    // very large value that would lose precision as JS Number.
    const largeTokenSum = 9100000000000001n;  // > MAX_SAFE_INTEGER
    const result = bigIntToExact(largeTokenSum);
    expect(typeof result).toBe("string");
    expect(BigInt(result as string)).toBe(largeTokenSum);
  });

  it("converts negative value > MIN_SAFE_INTEGER to string", () => {
    const val = BigInt(Number.MIN_SAFE_INTEGER) - 1n;
    const result = bigIntToExact(val);
    expect(typeof result).toBe("string");
    expect(BigInt(result as string)).toBe(val);
  });
});

// ---------------------------------------------------------------------------
// coerceBigInt — recursive BigInt coercion for DuckDB result objects
// ---------------------------------------------------------------------------

describe("coerceBigInt: recursive coercion", () => {
  it("passes through null", () => {
    expect(coerceBigInt(null)).toBeNull();
  });

  it("passes through string", () => {
    expect(coerceBigInt("hello")).toBe("hello");
  });

  it("passes through number", () => {
    expect(coerceBigInt(42)).toBe(42);
  });

  it("passes through boolean", () => {
    expect(coerceBigInt(true)).toBe(true);
  });

  it("converts top-level bigint (safe range) to number", () => {
    expect(coerceBigInt(3549n)).toBe(3549);
  });

  it("converts top-level bigint (unsafe range) to string", () => {
    const large = 9007199254740993n;
    const result = coerceBigInt(large);
    expect(typeof result).toBe("string");
    expect(result).toBe("9007199254740993");
  });

  it("converts bigint inside object", () => {
    const input: ExtendedValue = { count: 3549n, name: "executor" };
    const result = coerceBigInt(input) as Record<string, unknown>;
    expect(result["count"]).toBe(3549);
    expect(result["name"]).toBe("executor");
  });

  it("converts bigint inside array", () => {
    const input: ExtendedValue = [1n, 2n, 3n];
    const result = coerceBigInt(input) as number[];
    expect(result).toEqual([1, 2, 3]);
  });

  it("converts bigint in nested structure (realistic DuckDB row)", () => {
    // Simulate a queryDicts row with mixed types from agent_run aggregates
    const row: ExtendedValue = {
      n: 3549n,           // COUNT(*) → bigint
      sum_input: 527056n, // SUM(input_tok) → bigint
      p50: 0.0639,        // APPROX_QUANTILE → number
      role: "executor",   // VARCHAR → string
      start_ts: "2026-05-23T17:39:14Z",  // already converted timestamp → string
    };
    const result = coerceBigInt(row) as Record<string, unknown>;
    expect(result["n"]).toBe(3549);          // safe range → number
    expect(result["sum_input"]).toBe(527056); // safe range → number
    expect(result["p50"]).toBe(0.0639);
    expect(result["role"]).toBe("executor");
    expect(result["start_ts"]).toBe("2026-05-23T17:39:14Z");
  });

  it("result of coerceBigInt can be JSON.stringified (no BigInt error)", () => {
    const input: ExtendedValue = { n: 9007199254740993n, small: 42n };
    const result = coerceBigInt(input);
    // This MUST NOT throw — BigInt would cause JSON.stringify to throw TypeError
    expect(() => JSON.stringify(result)).not.toThrow();
    const json = JSON.stringify(result);
    // The large value is a string in JSON, small is a number
    expect(json).toContain('"9007199254740993"');
    expect(json).toContain('42');
  });
});

// ---------------------------------------------------------------------------
// Integration: bigIntToExact + normalizer rule 4 end-to-end
// ---------------------------------------------------------------------------

describe("int64 integration with normalizer pipeline", () => {
  it("safe bigint becomes a plain number in JSON output", () => {
    // Simulate the full pipeline: DuckDB bigint → coerceBigInt → normalize → JSON
    // For safe values, the result should be a plain number (same as Python int)
    const rawCount = 3549n;
    const coerced = coerceBigInt(rawCount);
    expect(JSON.stringify(coerced)).toBe("3549");
  });

  it("unsafe bigint becomes a decimal string in JSON output", () => {
    const rawLarge = 9007199254740993n;
    const coerced = coerceBigInt(rawLarge);
    expect(JSON.stringify(coerced)).toBe('"9007199254740993"');
  });

  it("Python int vs TS bigint are comparable after coercion", () => {
    // Python emits {count: 3549} in JSON; DuckDB returns bigint 3549n.
    // After coerceBigInt they should produce identical JSON.
    const pythonJson = JSON.stringify({ count: 3549 });
    const tsJson = JSON.stringify({ count: coerceBigInt(3549n) });
    expect(pythonJson).toBe(tsJson);
  });
});
