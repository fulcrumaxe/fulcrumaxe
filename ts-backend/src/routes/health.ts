/**
 * /health route — TypeScript port of backend/routers/health.py GET /health.
 *
 * Source fidelity (D#1437 Spec, P0 DoD #6):
 * Reads the same loop-metrics.jsonl the Python handler reads via get_loop_metrics().
 * Dynamic fields (loop_last_run, loop_duration_s, loop_idle_rate) are masked in
 * the normalizer when doing parity comparison — they change on every loop run and
 * cannot be byte-matched between two independent reads.
 *
 * _api_version field: the Python backend returns {"_api_version": 1, "ok": true, ...}
 * because it is injected by the legacy api.py wrapper. The TS backend mirrors this.
 *
 * Masking strategy documented in src/normalizer.ts ROUTE_MASKED_FIELDS["/health"].
 */

import type { Context } from "hono";
import { join } from "node:path";
import { readFileSync, existsSync } from "node:fs";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface HealthResponse {
  _api_version: number;
  ok: boolean;
  loop_last_run: string | null;
  loop_duration_s: number | null;
  loop_idle_rate: number | null;
  malformed_lines: number;
}

interface LoopMetricsEntry {
  timestamp?: string;
  ts?: string;
  duration_seconds?: number;
  duration_s?: number;
  idle?: boolean;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Loop metrics reader — mirrors backend/health_monitor.py get_loop_metrics()
// ---------------------------------------------------------------------------

const REPO_ROOT = join(import.meta.dir, "..", "..", "..");
const DEFAULT_METRICS_PATH = join(
  REPO_ROOT,
  ".autonomous-team",
  "loop-metrics.jsonl"
);
const N_ENTRIES = 10;

export function getLoopMetrics(metricsPath?: string): Omit<HealthResponse, "_api_version" | "ok"> {
  const path = metricsPath ?? DEFAULT_METRICS_PATH;

  if (!existsSync(path)) {
    return {
      loop_last_run: null,
      loop_duration_s: null,
      loop_idle_rate: null,
      malformed_lines: 0,
    };
  }

  const content = readFileSync(path, "utf-8");
  const rawLines = content
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (rawLines.length === 0) {
    return {
      loop_last_run: null,
      loop_duration_s: null,
      loop_idle_rate: null,
      malformed_lines: 0,
    };
  }

  const parsed: LoopMetricsEntry[] = [];
  let malformedLines = 0;

  for (const raw of rawLines) {
    try {
      const entry = JSON.parse(raw) as unknown;
      if (entry !== null && typeof entry === "object" && !Array.isArray(entry)) {
        parsed.push(entry as LoopMetricsEntry);
      } else {
        malformedLines++;
      }
    } catch {
      malformedLines++;
    }
  }

  if (parsed.length === 0) {
    return {
      loop_last_run: null,
      loop_duration_s: null,
      loop_idle_rate: null,
      malformed_lines: malformedLines,
    };
  }

  const lastEntry = parsed[parsed.length - 1];

  // Support both field names: "timestamp" (old cron) and "ts" (interactive /loop)
  const loopLastRun: string | null =
    (lastEntry.timestamp as string | undefined) ||
    (lastEntry.ts as string | undefined) ||
    null;

  // Support both "duration_seconds" (old) and "duration_s" (interactive)
  const durRaw =
    lastEntry.duration_seconds !== undefined
      ? lastEntry.duration_seconds
      : lastEntry.duration_s !== undefined
        ? lastEntry.duration_s
        : null;
  const loopDurationS: number | null =
    durRaw !== null && durRaw !== undefined ? Math.trunc(Number(durRaw)) : null;

  // Compute idle rate from last N_ENTRIES of valid parsed entries
  const recent = parsed.slice(-N_ENTRIES);
  const idleCount = recent.filter((e) => e.idle === true).length;
  const validCount = recent.length;

  const loopIdleRate: number | null =
    validCount > 0
      ? Math.round((idleCount / validCount) * 10000) / 10000
      : null;

  return {
    loop_last_run: loopLastRun,
    loop_duration_s: loopDurationS,
    loop_idle_rate: loopIdleRate,
    malformed_lines: malformedLines,
  };
}

// ---------------------------------------------------------------------------
// Route handler
// ---------------------------------------------------------------------------

export async function healthHandler(c: Context): Promise<Response> {
  const metrics = getLoopMetrics();
  const body: HealthResponse = {
    _api_version: 1,
    ok: true,
    ...metrics,
  };
  return c.json(body);
}
