/**
 * Unit tests for the normalizer module.
 *
 * Run: bun test tests/normalizer.test.ts
 *
 * Covers all rules from src/normalizer.ts:
 * 1. Recursive key sort
 * 2. Timestamp canonicalization
 * 3. Float normalization
 * 4. int64/large int as exact
 * 5. null passthrough
 * 6. NaN/Infinity → null
 * 7. Non-deterministic field masking
 */

import { describe, it, expect } from "bun:test";
import { normalize, normalizeJson, compareNormalized } from "../src/normalizer.js";
import type { JsonValue } from "../src/normalizer.js";

// ---------------------------------------------------------------------------
// Rule 1: Recursive key sort
// ---------------------------------------------------------------------------

describe("rule 1: recursive key sort", () => {
  it("sorts top-level keys", () => {
    const input: JsonValue = { z: 1, a: 2, m: 3 };
    const result = normalize(input);
    expect(JSON.stringify(result)).toBe('{"a":2,"m":3,"z":1}');
  });

  it("sorts nested object keys", () => {
    const input: JsonValue = { z: { b: 1, a: 2 }, a: { d: 3, c: 4 } };
    const result = normalize(input);
    expect(JSON.stringify(result)).toBe('{"a":{"c":4,"d":3},"z":{"a":2,"b":1}}');
  });

  it("does not sort array elements", () => {
    const input: JsonValue = [3, 1, 2];
    const result = normalize(input);
    expect(JSON.stringify(result)).toBe("[3,1,2]");
  });

  it("sorts keys inside objects within arrays", () => {
    const input: JsonValue = [{ z: 1, a: 2 }, { b: 3, a: 4 }];
    const result = normalize(input);
    expect(JSON.stringify(result)).toBe('[{"a":2,"z":1},{"a":4,"b":3}]');
  });
});

// ---------------------------------------------------------------------------
// Rule 2: Timestamp normalization
// ---------------------------------------------------------------------------

describe("rule 2: timestamp normalization", () => {
  it("normalizes UTC Z timestamp", () => {
    const input: JsonValue = { ts: "2026-05-23T17:39:14Z" };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["ts"]).toBe("2026-05-23T17:39:14Z");
  });

  it("normalizes timestamp with milliseconds", () => {
    const input: JsonValue = { ts: "2026-05-23T17:39:14.123Z" };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["ts"]).toBe("2026-05-23T17:39:14Z");
  });

  it("normalizes timestamp with +00:00 offset", () => {
    const input: JsonValue = { ts: "2026-05-23T17:39:14+00:00" };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["ts"]).toBe("2026-05-23T17:39:14Z");
  });

  it("leaves non-timestamp strings unchanged", () => {
    const input: JsonValue = { s: "hello world" };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["s"]).toBe("hello world");
  });
});

// ---------------------------------------------------------------------------
// Rule 3: Float normalization
// ---------------------------------------------------------------------------

describe("rule 3: float normalization", () => {
  it("rounds float to 6 significant digits", () => {
    const input: JsonValue = { f: 0.3333333333333333 };
    const result = normalize(input);
    // 0.333333 (6 sig figs)
    expect((result as Record<string, JsonValue>)["f"]).toBeCloseTo(0.333333, 5);
  });

  it("preserves simple floats", () => {
    const input: JsonValue = { f: 0.5 };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["f"]).toBe(0.5);
  });

  it("preserves loop_idle_rate 4-decimal precision", () => {
    // Python's round(x, 4) → e.g. 0.1234; must survive normalization
    const input: JsonValue = { loop_idle_rate: 0.1234 };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["loop_idle_rate"]).toBeCloseTo(0.1234, 4);
  });

  it("does not alter integers", () => {
    const input: JsonValue = { i: 42 };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["i"]).toBe(42);
  });
});

// ---------------------------------------------------------------------------
// Rule 4: int64 / large integers
// ---------------------------------------------------------------------------

describe("rule 4: large integers", () => {
  it("preserves integers up to MAX_SAFE_INTEGER exactly", () => {
    const input: JsonValue = { n: Number.MAX_SAFE_INTEGER };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["n"]).toBe(Number.MAX_SAFE_INTEGER);
  });

  it("preserves zero", () => {
    const input: JsonValue = { n: 0 };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["n"]).toBe(0);
  });

  it("preserves negative integers", () => {
    const input: JsonValue = { n: -12345 };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["n"]).toBe(-12345);
  });
});

// ---------------------------------------------------------------------------
// Rule 5: null passthrough
// ---------------------------------------------------------------------------

describe("rule 5: null handling", () => {
  it("passes null through unchanged", () => {
    const input: JsonValue = { n: null };
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["n"]).toBeNull();
  });

  it("handles top-level null", () => {
    expect(normalize(null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Rule 6: NaN / Infinity → null
// ---------------------------------------------------------------------------

describe("rule 6: NaN/Infinity → null", () => {
  it("normalizes NaN to null", () => {
    const input = { n: NaN } as unknown as JsonValue;
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["n"]).toBeNull();
  });

  it("normalizes +Infinity to null", () => {
    const input = { n: Infinity } as unknown as JsonValue;
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["n"]).toBeNull();
  });

  it("normalizes -Infinity to null", () => {
    const input = { n: -Infinity } as unknown as JsonValue;
    const result = normalize(input);
    expect((result as Record<string, JsonValue>)["n"]).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Rule 7: Non-deterministic field masking
// ---------------------------------------------------------------------------

describe("rule 7: field masking", () => {
  it("masks /health dynamic fields", () => {
    const input: JsonValue = {
      ok: true,
      loop_last_run: "2026-05-23T17:39:14Z",
      loop_duration_s: 300,
      loop_idle_rate: 0.0,
      malformed_lines: 0,
    };
    const result = normalize(input, { route: "/health" });
    const obj = result as Record<string, JsonValue>;
    expect(obj["loop_last_run"]).toBe("<masked>");
    expect(obj["loop_duration_s"]).toBe("<masked>");
    expect(obj["loop_idle_rate"]).toBe("<masked>");
    // Structural fields must NOT be masked
    expect(obj["ok"]).toBe(true);
    expect(obj["malformed_lines"]).toBe(0);
  });

  it("does not mask when no route specified", () => {
    const input: JsonValue = { loop_last_run: "2026-05-23T17:39:14Z" };
    const result = normalize(input);
    const obj = result as Record<string, JsonValue>;
    // timestamp normalization applied but not masked
    expect(obj["loop_last_run"]).toBe("2026-05-23T17:39:14Z");
  });

  it("applies override masked fields", () => {
    const input: JsonValue = { a: 1, b: 2, c: 3 };
    const result = normalize(input, { maskedFields: ["b"] });
    const obj = result as Record<string, JsonValue>;
    expect(obj["b"]).toBe("<masked>");
    expect(obj["a"]).toBe(1);
    expect(obj["c"]).toBe(3);
  });
});

// ---------------------------------------------------------------------------
// compareNormalized: end-to-end equality comparison
// ---------------------------------------------------------------------------

describe("compareNormalized", () => {
  it("returns equal for same JSON regardless of key order", () => {
    const a = '{"z":1,"a":2}';
    const b = '{"a":2,"z":1}';
    const { equal } = compareNormalized(a, b);
    expect(equal).toBe(true);
  });

  it("returns not-equal for different values", () => {
    const a = '{"ok":true}';
    const b = '{"ok":false}';
    const { equal } = compareNormalized(a, b);
    expect(equal).toBe(false);
  });

  it("/health: two calls with different live values match after masking", () => {
    const pyResponse = JSON.stringify({
      _api_version: 1,
      ok: true,
      loop_last_run: "2026-05-23T17:39:14Z",
      loop_duration_s: 300,
      loop_idle_rate: 0.0,
      malformed_lines: 0,
    });
    const tsResponse = JSON.stringify({
      _api_version: 1,
      ok: true,
      loop_last_run: "2026-05-23T17:41:02Z",  // different timestamp
      loop_duration_s: 275,                     // different duration
      loop_idle_rate: 0.1,                      // different rate
      malformed_lines: 0,
    });
    const { equal, normA, normB } = compareNormalized(pyResponse, tsResponse, {
      route: "/health",
    });
    expect(equal).toBe(true);
    // Both normalized forms should have masked fields
    expect(normA).toContain('"<masked>"');
    expect(normB).toContain('"<masked>"');
  });

  it("/health: structural mismatch (wrong ok) is caught even with masking", () => {
    const pyResponse = JSON.stringify({
      _api_version: 1, ok: true, loop_last_run: "2026-05-23T17:39:14Z",
      loop_duration_s: 300, loop_idle_rate: 0.0, malformed_lines: 0,
    });
    const tsResponse = JSON.stringify({
      _api_version: 1, ok: false,  // wrong!
      loop_last_run: "2026-05-23T17:39:14Z",
      loop_duration_s: 300, loop_idle_rate: 0.0, malformed_lines: 0,
    });
    const { equal } = compareNormalized(pyResponse, tsResponse, { route: "/health" });
    expect(equal).toBe(false);
  });

  it("normalizeJson is stable (idempotent)", () => {
    const json = '{"z":1,"a":{"c":3,"b":2}}';
    const once = normalizeJson(json);
    const twice = normalizeJson(once);
    expect(once).toBe(twice);
  });
});
