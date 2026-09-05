/**
 * rpc/stats-dora.ts — Native TS implementation of the stats.dora RPC method.
 *
 * Mirrors Python's call chain exactly:
 *   backend/rpc/stats_dora.py:handle()
 *     → backend/analytics_engineer.compute_snapshot()
 *       → backend/release_manager.compute_dora_snapshot()   (deploy_freq + lead_time)
 *       → backend/analytics_engineer._compute_cfr()         (change_failure_rate_pct)
 *       → backend/kpi_engine.compute_velocity()             (velocity_all_time_per_day)
 *       → backend/kpi_engine.compute_pr_cycle_time()        (cycle_time_median_hours)
 *
 * Response shape (verbatim from stats_dora.py):
 * {
 *   applicable:                bool,
 *   deploy_frequency_per_day:  float (rounded 4 dp),
 *   lead_time_minutes_p50:     float (rounded 2 dp) | -1.0,
 *   change_failure_rate_pct:   string ("n/a" | "0.0" | numeric — verbatim),
 *   velocity_all_time_per_day: float (rounded 2 dp),
 *   cycle_time_median_hours:   float (rounded 2 dp) | null,
 *   window_start:              string ("YYYY-MM-DD" UTC today),
 * }
 *
 * Data sources:
 *   deploy_frequency_per_day  — .autonomous-team/releases/*.json (7-day trailing window)
 *   lead_time_minutes_p50     — `gh pr list` (7-day window, p50 via sort+median)
 *   change_failure_rate_pct   — releases above + GitHub bug discussions via gh api graphql
 *   velocity_all_time_per_day — .autonomous-team/registry.json discussions[].{status,closed_at}
 *   cycle_time_median_hours   — same registry.json (DONE items, created_at→closed_at)
 *
 * Read-only: no writes, no spawns.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { resolveCodeRepo, resolveRepo } from "../config/repo.js";

// ---------------------------------------------------------------------------
// Shared path helpers (mirrors misc-batch5.ts / stats-batch3.ts convention)
// ---------------------------------------------------------------------------

/**
 * Repo root.
 * Priority: AF_REPO_ROOT → AUTONOMOUS_TEAM_DIR/.. → 5 levels up from this file.
 */
function repoRoot(): string {
  if (process.env.AF_REPO_ROOT) return process.env.AF_REPO_ROOT;
  if (process.env.AUTONOMOUS_TEAM_DIR)
    return join(process.env.AUTONOMOUS_TEAM_DIR, "..");
  const thisFile = new URL(import.meta.url).pathname;
  return join(thisFile, "..", "..", "..", "..", "..");
}

/** Autonomous team dir — mirrors Python _REPO_ROOT / ".autonomous-team". */
function autonomousTeamDir(): string {
  return process.env.AUTONOMOUS_TEAM_DIR ?? join(repoRoot(), ".autonomous-team");
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const _7D_SECONDS = 7 * 24 * 3600;
// Two planes from one file: merged PRs are code, bug Discussions are not.
const AF_CODE_REPO = resolveCodeRepo();
const AF_REPO = resolveRepo();

// ---------------------------------------------------------------------------
// Rounding helpers — Math.round(x * 10^n) / 10^n to mirror Python's round(x, n)
// ---------------------------------------------------------------------------

function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}

function round2(x: number): number {
  return Math.round(x * 100) / 100;
}

function round1(x: number): number {
  return Math.round(x * 10) / 10;
}

// ---------------------------------------------------------------------------
// Median helper — mean of two middle elements for even count (Python statistics.median)
// ---------------------------------------------------------------------------

function median(sorted: number[]): number {
  const n = sorted.length;
  if (n === 0) throw new Error("median of empty array");
  const mid = Math.floor(n / 2);
  if (n % 2 === 1) {
    return sorted[mid];
  }
  return (sorted[mid - 1] + sorted[mid]) / 2;
}

// ---------------------------------------------------------------------------
// ISO date parse helper
// ---------------------------------------------------------------------------

function parseIso(s: string | undefined | null): Date | null {
  if (!s) return null;
  try {
    const d = new Date(s.replace("Z", "+00:00"));
    return isNaN(d.getTime()) ? null : d;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// 1. Deploy frequency — mirrors release_manager.compute_dora_snapshot() lines ~103-123
// ---------------------------------------------------------------------------

/**
 * Load release records whose merged_at is within the trailing 7-day window.
 * Skips records with null/missing merged_at.
 * Returns [] if releases dir is absent (not an error — freq becomes 0.0).
 */
function loadRecentReleases(
  releasesDir: string,
  cutoffTs: number
): Array<Record<string, unknown>> {
  if (!existsSync(releasesDir)) return [];

  let files: string[];
  try {
    files = readdirSync(releasesDir).filter((f) => f.endsWith(".json"));
  } catch {
    return [];
  }

  const releases: Array<Record<string, unknown>> = [];
  for (const f of files) {
    try {
      const data = JSON.parse(
        readFileSync(join(releasesDir, f), "utf-8")
      ) as Record<string, unknown>;
      const mergedAt = data["merged_at"] as string | undefined | null;
      if (!mergedAt) continue;
      const mergedAtTs = parseIso(mergedAt)?.getTime();
      if (mergedAtTs === undefined || mergedAtTs === null) continue;
      if (mergedAtTs / 1000 >= cutoffTs) {
        releases.push(data);
      }
    } catch {
      /* skip malformed files */
    }
  }
  return releases;
}

// ---------------------------------------------------------------------------
// 2. Lead time p50 — mirrors release_manager.compute_dora_snapshot() lines ~128-164
// ---------------------------------------------------------------------------

/**
 * Compute p50 lead time in minutes from merged PRs in the 7-day window.
 * Shells `gh pr list` (same argv as handleWeeklyVelocity in stats-batch3.ts).
 * Returns -1.0 on gh failure or no samples.
 */
function computeLeadTimeP50(cutoffTs: number): number {
  let stdout: string;
  try {
    stdout = execFileSync(
      "gh",
      [
        "pr",
        "list",
        "--repo",
        AF_CODE_REPO,
        "--state",
        "merged",
        "--json",
        "number,createdAt,mergedAt",
        "--limit",
        "50",
      ],
      { encoding: "utf-8", timeout: 30_000 }
    );
  } catch {
    return -1.0;
  }

  let prs: Array<{ createdAt: string; mergedAt: string }>;
  try {
    const parsed = JSON.parse(stdout.trim());
    prs = Array.isArray(parsed) ? parsed : [];
  } catch {
    return -1.0;
  }

  const leadTimes: number[] = [];
  for (const pr of prs) {
    try {
      const created = parseIso(pr.createdAt);
      const merged = parseIso(pr.mergedAt);
      if (!created || !merged) continue;
      if (merged.getTime() / 1000 >= cutoffTs) {
        leadTimes.push((merged.getTime() - created.getTime()) / 60_000);
      }
    } catch {
      /* skip */
    }
  }

  if (leadTimes.length === 0) return -1.0;

  leadTimes.sort((a, b) => a - b);
  return round2(median(leadTimes));
}

// ---------------------------------------------------------------------------
// 3. Change failure rate — mirrors analytics_engineer._compute_cfr()
// ---------------------------------------------------------------------------

/**
 * Compute change_failure_rate_pct as a string.
 * Returns "n/a" on no releases, gh error, or exception.
 * Returns "0.0" when no bug discussions found.
 * Returns str(round(failed/len(releases)*100, 1)) otherwise.
 */
function computeCfr(releases: Array<Record<string, unknown>>): string {
  if (releases.length === 0) return "n/a";

  // Fetch bug discussions via gh api graphql
  let stdout: string;
  try {
    const queryArg = `query=query{repository(owner:"${AF_REPO.split("/")[0]}",name:"${AF_REPO.split("/")[1]}"){discussions(first:100,categoryId:null,filterBy:{labels:[]}){nodes{title createdAt}}}}`;
    stdout = execFileSync(
      "gh",
      ["api", "graphql", "-f", queryArg],
      { encoding: "utf-8", timeout: 20_000 }
    );
  } catch {
    return "n/a";
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(stdout) as Record<string, unknown>;
  } catch {
    return "n/a";
  }

  const data = payload["data"] as Record<string, unknown> | undefined;
  if (!data) return "n/a";
  const repo = data["repository"] as Record<string, unknown> | undefined;
  if (!repo) return "n/a";
  const discussions = (
    (repo["discussions"] as Record<string, unknown> | undefined)?.["nodes"] ?? []
  ) as Array<Record<string, unknown>>;

  // Filter to [Bug]-prefixed discussions and collect their timestamps
  const bugTimestamps: Date[] = [];
  for (const d of discussions) {
    const title = (d["title"] as string) ?? "";
    if (!title.startsWith("[Bug]")) continue;
    const createdAt = parseIso(d["createdAt"] as string | undefined);
    if (createdAt) bugTimestamps.push(createdAt);
  }

  if (bugTimestamps.length === 0) return "0.0";

  // Count releases that had a [Bug] filed within 24h of merged_at
  const _24H_MS = 24 * 3600 * 1000;
  let failed = 0;

  for (const rel of releases) {
    const mergedAt = parseIso(rel["merged_at"] as string | undefined);
    if (!mergedAt) continue;
    const mergedMs = mergedAt.getTime();

    for (const bugDt of bugTimestamps) {
      const diff = bugDt.getTime() - mergedMs;
      if (diff >= 0 && diff <= _24H_MS) {
        failed++;
        break; // count each release at most once
      }
    }
  }

  const pct = round1((failed / releases.length) * 100);
  return String(pct);
}

// ---------------------------------------------------------------------------
// 4. Velocity — mirrors kpi_engine.compute_velocity() lines 103-121
// ---------------------------------------------------------------------------

interface VelocityResult {
  allTimePerDay: number;
}

function computeVelocity(
  discussions: Array<Record<string, unknown>>
): VelocityResult {
  const done = discussions.filter((d) => d["status"] === "DONE");

  let earliest: Date | null = null;
  for (const d of done) {
    const closed = parseIso(d["closed_at"] as string | undefined);
    if (closed && (!earliest || closed < earliest)) {
      earliest = closed;
    }
  }

  const totalDone = done.length;
  if (earliest && totalDone > 0) {
    const nowMs = Date.now();
    const spanDays = Math.max(
      (nowMs - earliest.getTime()) / (86400 * 1000),
      1.0
    );
    return { allTimePerDay: round2(totalDone / spanDays) };
  }
  return { allTimePerDay: 0.0 };
}

// ---------------------------------------------------------------------------
// 5. Cycle time median — mirrors kpi_engine.compute_pr_cycle_time() lines 183-195
// ---------------------------------------------------------------------------

function computeCycleTimeMedianHours(
  discussions: Array<Record<string, unknown>>
): number | null {
  const hours: number[] = [];
  for (const d of discussions) {
    if (d["status"] !== "DONE") continue;
    const created = parseIso(d["created_at"] as string | undefined);
    const closed = parseIso(d["closed_at"] as string | undefined);
    if (created && closed && closed > created) {
      hours.push((closed.getTime() - created.getTime()) / 3_600_000);
    }
  }
  if (hours.length === 0) return null;
  hours.sort((a, b) => a - b);
  return round2(median(hours));
}

// ---------------------------------------------------------------------------
// 6. Load registry.json — mirrors kpi_engine.load_registry()
// ---------------------------------------------------------------------------

function loadRegistry(): Array<Record<string, unknown>> {
  const registryPath = join(autonomousTeamDir(), "registry.json");
  if (!existsSync(registryPath)) return [];
  try {
    const data = JSON.parse(readFileSync(registryPath, "utf-8")) as Record<
      string,
      unknown
    >;
    const discussions = data["discussions"];
    return Array.isArray(discussions)
      ? (discussions as Array<Record<string, unknown>>)
      : [];
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Main handler — mirrors stats_dora.py:handle()
// ---------------------------------------------------------------------------

/**
 * stats.dora — return DORA + KPI snapshot for the dashboard.
 *
 * params is accepted and ignored (reserved for future project-scoping, same as Python).
 */
export async function handleDora(
  _params: Record<string, unknown>
): Promise<unknown> {
  try {
    const nowMs = Date.now();
    const nowTs = nowMs / 1000;
    const cutoffTs = nowTs - _7D_SECONDS;

    // UTC today as "YYYY-MM-DD"
    const windowStart = new Date(nowMs).toISOString().slice(0, 10);

    // Releases dir
    const releasesDir = join(autonomousTeamDir(), "releases");

    // --- 1. Deploy frequency ---
    let deployFreq: number;
    try {
      const recentReleases = loadRecentReleases(releasesDir, cutoffTs);
      deployFreq = round4(recentReleases.length / 7.0);
    } catch {
      deployFreq = -1.0;
    }

    // --- 2. Lead time p50 (async-compatible: gh blocks but we run it sync) ---
    let leadTime: number;
    try {
      leadTime = computeLeadTimeP50(cutoffTs);
    } catch {
      leadTime = -1.0;
    }

    // --- 3. CFR (string, verbatim) ---
    let cfr: string;
    try {
      const recentReleases = loadRecentReleases(releasesDir, cutoffTs);
      cfr = computeCfr(recentReleases);
    } catch {
      cfr = "n/a";
    }

    // --- 4 + 5. Velocity + cycle time from registry ---
    const discussions = loadRegistry();
    const velocity = computeVelocity(discussions);
    const cycleTimeMedian = computeCycleTimeMedianHours(discussions);

    // --- 7. applicable (mirrors stats_dora.py lines 40-42) ---
    const hasData =
      deployFreq > 0 ||
      (typeof leadTime === "number" && leadTime >= 0);
    const applicable = Boolean(hasData);

    return {
      applicable,
      deploy_frequency_per_day: deployFreq,
      lead_time_minutes_p50: leadTime,
      change_failure_rate_pct: cfr,
      velocity_all_time_per_day: velocity.allTimePerDay,
      cycle_time_median_hours: cycleTimeMedian,
      window_start: windowStart,
    };
  } catch {
    // Outer catch mirrors Python: try/except Exception: return {"applicable": False}
    return { applicable: false };
  }
}
