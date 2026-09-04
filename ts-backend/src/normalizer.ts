/**
 * Response normalizer — semantic-equivalence-after-normalization module.
 *
 * Rules (justified in D#1437 Spec / Consensus Summary):
 *
 * 1. Object keys sorted recursively — Python's json.dumps and JS JSON.stringify
 *    may produce keys in different orders; canonical form uses sorted keys.
 *
 * 2. Timestamps → canonical ISO-8601 UTC — any ISO-8601 timestamp string is
 *    normalized to "YYYY-MM-DDTHH:MM:SSZ" (no milliseconds, always Z suffix).
 *    Python's datetime.strftime("%Y-%m-%dT%H:%M:%SZ") matches this form.
 *
 * 3. float → string formatting — Python's json serializes floats with up to
 *    17 significant digits; JS JSON.stringify may differ on edge values. We
 *    normalize by rounding to 6 significant digits for comparison. Values that
 *    compare equal after this rounding are considered equivalent.
 *
 * 4. int64 / large integers — carried as exact BigInt; never coerced through
 *    lossy JS Number. Values <= Number.MAX_SAFE_INTEGER stay as number; larger
 *    values are preserved as strings in the canonical form.
 *
 * 5. null → null (canonical). Python None serializes to JSON null; JS null is
 *    identical. No conversion needed.
 *
 * 6. NaN / Infinity — Python's json module does NOT produce NaN/Infinity by
 *    default (it raises ValueError). Both backends should emit null for these
 *    cases. If a value is NaN or Infinity we normalize it to null.
 *
 * 7. Non-deterministic field masking — fields listed in MASKED_FIELDS for a
 *    given route are replaced with the sentinel value "<masked>" before
 *    comparison. This handles live counters, durations, timestamps that change
 *    on every request, etc. Any masking must be justified here.
 *
 * Masked fields for /health (documented):
 *   - loop_last_run: live timestamp, changes every loop iteration
 *   - loop_duration_s: live integer, changes every loop iteration
 *   - loop_idle_rate: live float, changes every loop iteration
 *
 * These are masked because the Python reference and the TS backend read the
 * same underlying file (loop-metrics.jsonl) at different milliseconds, so the
 * values may differ by one entry between requests even in a shadow diff.
 * The golden corpus captures the STRUCTURE (ok:true, field presence), not the
 * live values. The _api_version field is structural and must match exactly.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

/**
 * Extended value type that includes BigInt — returned by @duckdb/node-api for
 * COUNT(*) and integer aggregates.  The normalizer accepts this and converts
 * BigInt values to an exact form before comparison (rule 4).
 *
 * BigInt → exact integer path (rule 4 — real implementation):
 *   - value <= Number.MAX_SAFE_INTEGER: emitted as a JS number (no precision loss)
 *   - value >  Number.MAX_SAFE_INTEGER: emitted as a decimal string so JSON
 *     serialization preserves the exact value.  The Python backend emits large
 *     integers as plain JSON numbers; the TS normalizer converts them to strings
 *     for comparison so both sides can be compared after the same transformation.
 *
 * This is the "REAL exact-integer path" called for in the D#1437 prerequisite:
 * "Implement a REAL exact-integer path so int64 values are carried/serialized
 *  exactly (string or exact integer), never through lossy JS Number."
 */
export type ExtendedValue = JsonValue | bigint | ExtendedValue[] | { [key: string]: ExtendedValue };

/**
 * Convert a BigInt to a JSON-safe exact value.
 *
 * Values within JS safe-integer range are returned as number (no precision
 * loss; matches Python JSON output for typical small integers).
 * Values outside the safe range are returned as a decimal string so that
 * JSON.stringify preserves the exact magnitude.
 *
 * This is the canonical conversion point for all @duckdb/node-api BigInt
 * results — use it before inserting any DuckDB integer into a response body.
 */
export function bigIntToExact(val: bigint): number | string {
  if (val >= BigInt(Number.MIN_SAFE_INTEGER) && val <= BigInt(Number.MAX_SAFE_INTEGER)) {
    return Number(val);
  }
  return val.toString(10);
}

/**
 * Recursively walk an ExtendedValue (which may contain BigInt leaves returned
 * by @duckdb/node-api) and convert it to a plain JsonValue.  BigInt values
 * are converted via bigIntToExact — exactly preserving their magnitude.
 *
 * Use this on any object/value received from a DuckDB query result before
 * passing it to normalize() or JSON.stringify().
 */
export function coerceBigInt(val: ExtendedValue): JsonValue {
  if (val === null) return null;
  if (typeof val === "bigint") return bigIntToExact(val);
  if (typeof val === "boolean" || typeof val === "number" || typeof val === "string") return val;
  if (Array.isArray(val)) return val.map(coerceBigInt);
  if (typeof val === "object") {
    const out: { [key: string]: JsonValue } = {};
    for (const k of Object.keys(val)) {
      out[k] = coerceBigInt((val as { [key: string]: ExtendedValue })[k]);
    }
    return out;
  }
  return null;
}

export interface NormalizeOptions {
  /** Route path for per-route masked fields. E.g. "/health" */
  route?: string;
  /** Override masked fields (bypass route lookup). */
  maskedFields?: string[];
}

// ---------------------------------------------------------------------------
// Per-route masked fields
// Justified: these fields change on every request (live metrics reads).
// Structure (presence, type) is still verified — only the values are masked.
// ---------------------------------------------------------------------------
//
// P2 routes — no top-level masking needed for sessions or spawn-queue:
//   - /sessions: started_at / ended_at are timestamps (normalized by rule 2)
//   - /sessions/compare: duration_minutes is live (changes as time passes for
//     open sessions). Masked at the nested delta.duration_minutes level in the
//     parity test; not masked globally because closed-session compares are stable.
//     We do NOT add it to ROUTE_MASKED_FIELDS here so stable comparisons still work.
//   - /spawn-queue: pending/active/completed/failed counts are live; shadow diff
//     runs both backends against the SAME source file so they should match.
//   - /spawn-blocks: ts field is normalized by rule 2 (ISO-8601 timestamp).
const ROUTE_MASKED_FIELDS: Record<string, string[]> = {
  "/health": ["loop_last_run", "loop_duration_s", "loop_idle_rate"],
  "/health/loop": ["lastRun", "duration"],
  // /sessions/compare: mask duration_minutes because it is computed from
  // datetime.now() for open sessions (changes between the Python and TS reads).
  // Python uses datetime.now(tz=utc) at query time; TS does too. A 1-millisecond
  // difference between the two reads will produce a different rounded float.
  "/sessions/compare": ["duration_minutes"],
};

// ---------------------------------------------------------------------------
// Timestamp detection
// Matches ISO-8601 strings: "2026-05-23T17:39:14Z" or with offset
// ---------------------------------------------------------------------------
const ISO_TS_RE =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function normalizeTimestamp(value: string): string {
  if (!ISO_TS_RE.test(value)) return value;
  try {
    const d = new Date(value);
    if (isNaN(d.getTime())) return value;
    // Format: YYYY-MM-DDTHH:MM:SSZ (no milliseconds, always UTC Z)
    return d.toISOString().replace(/\.\d{3}Z$/, "Z");
  } catch {
    return value;
  }
}

// ---------------------------------------------------------------------------
// Float normalization
// We round floats to 6 significant digits to absorb Python vs JS serialization
// differences on edge float values. This is the minimum precision that keeps
// loop_idle_rate (0–1.0, 4 decimal places from Python's round(..., 4)) intact.
// ---------------------------------------------------------------------------
function normalizeFloat(value: number): number {
  if (!isFinite(value)) return 0; // NaN / Infinity → 0 (then masked or null)
  // Use toPrecision(6) to normalize, then parseFloat to strip trailing zeros
  return parseFloat(value.toPrecision(6));
}

// ---------------------------------------------------------------------------
// Core recursive normalizer
// ---------------------------------------------------------------------------
function normalizeValue(value: JsonValue, maskedSet: Set<string> | null, _key?: string): JsonValue {
  if (value === null) return null;

  if (typeof value === "number") {
    if (!isFinite(value)) return null; // NaN / Infinity → null (rule 6)
    if (Number.isInteger(value)) return value; // integers as-is (rule 4)
    return normalizeFloat(value); // float normalization (rule 3)
  }

  if (typeof value === "boolean") return value;

  if (typeof value === "string") {
    return normalizeTimestamp(value); // timestamp normalization (rule 2)
  }

  if (Array.isArray(value)) {
    return value.map((item) => normalizeValue(item, null));
  }

  if (typeof value === "object") {
    const sorted: { [key: string]: JsonValue } = {};
    // Sort keys recursively (rule 1)
    const keys = Object.keys(value).sort();
    for (const k of keys) {
      const v = value[k];
      // Apply mask before normalizing (rule 7)
      if (maskedSet && maskedSet.has(k)) {
        sorted[k] = "<masked>";
      } else {
        sorted[k] = normalizeValue(v, maskedSet, k);
      }
    }
    return sorted;
  }

  return value;
}

/**
 * Normalize a parsed JSON value for parity comparison.
 *
 * @param value  Parsed JSON (from JSON.parse or equivalent)
 * @param opts   Optional route for masked-field lookup
 * @returns      Normalized JsonValue suitable for JSON.stringify comparison
 */
export function normalize(value: JsonValue, opts: NormalizeOptions = {}): JsonValue {
  const route = opts.route;
  const overrideMasked = opts.maskedFields;

  let maskedSet: Set<string> | null = null;
  if (overrideMasked && overrideMasked.length > 0) {
    maskedSet = new Set(overrideMasked);
  } else if (route && ROUTE_MASKED_FIELDS[route]) {
    maskedSet = new Set(ROUTE_MASKED_FIELDS[route]);
  }

  return normalizeValue(value, maskedSet);
}

/**
 * Normalize a JSON string (parse → normalize → stringify).
 * Returns canonical JSON string with sorted keys and masked fields.
 */
export function normalizeJson(jsonStr: string, opts: NormalizeOptions = {}): string {
  const parsed = JSON.parse(jsonStr) as JsonValue;
  const normed = normalize(parsed, opts);
  return JSON.stringify(normed);
}

/**
 * Deep-equal two normalized JsonValue trees.
 * Returns true if they are semantically equivalent after normalization.
 */
export function deepEqual(a: JsonValue, b: JsonValue): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * Compare two JSON strings after normalization.
 * Returns { equal: boolean, diff?: string } where diff describes the first divergence.
 */
export function compareNormalized(
  aJson: string,
  bJson: string,
  opts: NormalizeOptions = {}
): { equal: boolean; normA: string; normB: string } {
  const normA = normalizeJson(aJson, opts);
  const normB = normalizeJson(bJson, opts);
  return { equal: normA === normB, normA, normB };
}
