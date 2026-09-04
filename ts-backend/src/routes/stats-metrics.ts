/**
 * stats-metrics.ts — DuckDB-backed stats metric GET routes.
 *
 * P3: converts the stats.duckdb read paths exposed by Python's stats_reader.py
 * (summary() and series() functions, consumed by the dashboard via JSON-RPC
 * stats.summary / stats.series methods).
 *
 * These routes provide a REST GET equivalent of the JSON-RPC methods so the
 * TS backend can be parity-tested without standing up a full RPC session:
 *
 *   GET /stats/metrics/summary
 *     → {"metrics": [{name, value, unit, updated_at_iso}, ...]}
 *     Mirrors Python stats_reader.summary() + RPC wrapper {"metrics": ...}
 *
 *   GET /stats/metrics/series/:name?since_hours=168
 *     → {"name": str, "points": [{ts_iso, value}, ...]}
 *     Mirrors Python stats_reader.series(name, since_hours)
 *
 * Both routes:
 *   - Require bearer auth + RBAC (same gates as all auth-gated routes)
 *   - Open a read-only DuckDB connection per-call (mirrors Python per-call pattern)
 *   - Return {} / [] on DB-absent rather than 500 (matches Python graceful fallback)
 *   - Use the duckdb-helpers timestamp + bigint conversions for numeric fidelity
 *
 * Unit correction table mirrors stats_reader._UNIT_CORRECTIONS (same values).
 */

import type { Context } from "hono";
import { openReadConn, closeConn, queryDicts } from "../duckdb-helpers.js";
import { bigIntToExact } from "../normalizer.js";
import { checkRbac } from "../middleware/rbac-check.js";

// ---------------------------------------------------------------------------
// Unit corrections — mirrors stats_reader._UNIT_CORRECTIONS (Python parity)
// Justification: PR #1040 fixed the producer to write unit='count' for
// orphan_worktree_rate; older rows may still have unit='ratio'. Correct at
// the API boundary so the display layer never sees the stale value.
// ---------------------------------------------------------------------------
const UNIT_CORRECTIONS: Record<string, Record<string, string>> = {
  orphan_worktree_rate: { ratio: "count" },
};

function correctUnit(metric: string, unit: string): string {
  return UNIT_CORRECTIONS[metric]?.[unit] ?? unit;
}

// ---------------------------------------------------------------------------
// GET /stats/metrics/summary
// Mirrors Python stats_reader.summary() + RPC handler {"metrics": list}
// ---------------------------------------------------------------------------

export async function statsMetricsSummaryHandler(c: Context): Promise<Response> {
  const rbacDeny = checkRbac(c, "GET", "/stats/metrics/summary");
  if (rbacDeny !== null) return rbacDeny;

  let h;
  try {
    h = await openReadConn();
  } catch {
    // DB absent — return empty list matching Python graceful fallback
    return c.json({ metrics: [] });
  }
  try {
    const rows = await queryDicts(h, `
      SELECT metric, value, unit, ts
      FROM (
        SELECT metric, value, unit, ts,
               ROW_NUMBER() OVER (PARTITION BY metric ORDER BY ts DESC) AS rn
        FROM metric_event
      ) t
      WHERE rn = 1
      ORDER BY metric
    `);

    const metrics = rows.map(row => {
      const metric = row["metric"] as string;
      const unit = correctUnit(metric, (row["unit"] as string | null) ?? "");
      // ts is already converted to ISO string by rowToDict via tsToIso
      const ts = row["ts"] as string | null;
      // value is a float from DuckDB DOUBLE column — stays as number;
      // but coerce bigint just in case (integer metrics)
      const rawVal = row["value"];
      const value = typeof rawVal === "bigint"
        ? bigIntToExact(rawVal)
        : rawVal ?? null;
      return {
        name: metric,
        value,
        unit,
        updated_at_iso: ts ?? null,
      };
    });

    return c.json({ metrics });
  } finally {
    closeConn(h);
  }
}

// ---------------------------------------------------------------------------
// GET /stats/metrics/series/:name?since_hours=168
// Mirrors Python stats_reader.series(name, since_hours)
// ---------------------------------------------------------------------------

export async function statsMetricsSeriesHandler(c: Context): Promise<Response> {
  const rbacDeny = checkRbac(c, "GET", "/stats/metrics/series/:name");
  if (rbacDeny !== null) return rbacDeny;

  const name = c.req.param("name");
  if (!name) {
    return c.json({ error: "name parameter required" }, 400);
  }

  const sinceHoursRaw = c.req.query("since_hours") ?? "168";
  const sinceHours = Math.max(1, Math.min(8760, parseInt(sinceHoursRaw, 10) || 168));

  // Format cutoff as "YYYY-MM-DD HH:MM:SS" — matches Python's
  // cutoff.strftime("%Y-%m-%d %H:%M:%S") used in stats_reader.series()
  const cutoff = new Date(Date.now() - sinceHours * 3600 * 1000);
  const cutoffStr = cutoff.toISOString().replace("T", " ").slice(0, 19);

  let h;
  try {
    h = await openReadConn();
  } catch {
    return c.json({ name, points: [] });
  }
  try {
    const rows = await queryDicts(
      h,
      `
      SELECT ts, value
      FROM metric_event
      WHERE metric = ?
        AND ts >= CAST(? AS TIMESTAMP)
      ORDER BY ts
      `,
      [name, cutoffStr]
    );

    const points = rows.map(row => {
      // ts is already a string (converted by rowToDict)
      const ts = row["ts"] as string | null;
      const rawVal = row["value"];
      const value = typeof rawVal === "bigint"
        ? bigIntToExact(rawVal)
        : rawVal ?? null;
      return { ts_iso: ts ?? "", value };
    });

    return c.json({ name, points });
  } finally {
    closeConn(h);
  }
}
