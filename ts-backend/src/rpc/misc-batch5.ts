/**
 * rpc/misc-batch5.ts — Native TS implementations of miscellaneous RPC methods (batch 5).
 *
 * Mirrors the following Python RPC handlers exactly (1:1 parity):
 *   - dial.list               → handleDialList()
 *   - auth_retry.summary      → handleAuthRetrySummary()
 *   - circuit_breaker.summary → handleCircuitBreakerSummary()
 *   - circuitBreaker.history  → handleCircuitBreakerHistory()
 *   - kpi.history             → handleKpiHistory()
 *   - kpi.cycle_time          → handleKpiCycleTime()
 *   - cost.per_discussion     → handleCostPerDiscussion()
 *   - cost.by_discussion      → handleCostByDiscussion()
 *
 * All handlers are additive — Python runtime code is not modified.
 *
 * Data sources per method:
 *   dial.list               — <STATE_DIR>/dial-registry.json
 *   auth_retry.summary      — SQLite state.db blackboard table (auth_retry_count, auth_retry_timestamps)
 *   circuit_breaker.summary — file-based blackboard: failures/ + failures_meta/ dirs
 *   circuitBreaker.history  — <STATE_DIR>/circuit-breaker-history.jsonl
 *   kpi.history             — git log subprocess (fixed argv, same as Python kpi_engine.history)
 *   kpi.cycle_time          — .autonomous-team/registry.json + file-based blackboard discussions/
 *   cost.per_discussion     — file-based blackboard budget/agents/ + config.json pricing
 *   cost.by_discussion      — same + days-window aggregation
 *
 * Deferred (remain proxied — see rpc.ts PROXY_METHODS comments):
 *   dashboard.pr_detail     — gh pr view + gh api graphql (complex gh-GraphQL chain)
 *   dashboard.pr_list       — gh pr list + gh api graphql + blackboard quality/
 *   team_status.snapshot    — imports backend.team_status (Python-only module)
 *   claude_spawn_tracker.summary — shells to python3 backend/claude_spawn_tracker.py
 *   discussions.list        — gh api graphql (rate-limit-prone)
 *   discussions.get         — gh api graphql (rate-limit-prone)
 *   stats.sdk_lane          — depends on CreditTracker (sdk_credit.json) + billing_regime env combo
 *   stats.cost_per_outcome  — depends on CostTracker + DuckDB project scoping
 *   fleet.projects          — backend.fleet.discovery (Python-specific state scanning)
 *   fleet.cost              — depends on fleet.discovery + cost_summary Python modules
 *   fleet.concurrency       — backend.fleet.concurrency (fleet.db SQLite + reap logic)
 *
 * Design notes:
 *   - dial.list: mirrors Python dial_control.handle_list() → list_directives() → _load_registry()
 *     Output is sorted by class name (same as Python's sorted(registry.items())).
 *   - auth_retry.summary: reads SQLite state.db directly via bun:sqlite. Python's get_blackboard()
 *     prefers SQLite when state.db exists (always true in production). The blackboard value
 *     column stores a JSON object {"value": <inner>, "version": int, ...}.
 *   - circuit_breaker.summary: replicates _collect_tripped() by reading file-based blackboard
 *     directories (failures/ + failures_meta/) directly — avoids spawning python3 circuit_breaker.py.
 *     Python's _bb = Blackboard() uses the file-based store. The file blackboard root is
 *     STATE_DIR/blackboard.
 *   - circuitBreaker.history: reads circuit-breaker-history.jsonl, filters by role, returns last N.
 *     Python's _HISTORY_FILE is in repo root .autonomous-team/ but production puts it in STATE_DIR.
 *     We check STATE_DIR first, then fall back to autonomousTeamDir().
 *   - kpi.history: shells `git log` with fixed argv, same pattern as batch-3 weekly_velocity.
 *     Squash-merge pattern: subject matches /^#\d+:/ or /\(#\d+\)\s*$/ (same regex as Python).
 *   - kpi.cycle_time: reads registry.json, reads file-based blackboard discussions/<N>.json
 *     for spawn times. Bucket boundaries: 0-2h, 2-6h, 6-24h, 24h+ (same as Python).
 *   - cost.*: file-based blackboard budget/agents/ read, pricing from config.json.
 *     cost.per_discussion returns a single entry or null.
 *     cost.by_discussion aggregates by_agent within a days window, sorts by usd desc, tops N.
 *   - Math.trunc for int() truncation (NOT Math.round) — per batch invariant.
 *   - revert_expired() (called before list_directives in Python): TS skips the expired-directive
 *     revert step as a WRITE operation; we faithfully READ the registry as-is and report
 *     TTL fields for display only — same data, no write side-effect.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { Database } from "bun:sqlite";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Shared path helpers
// ---------------------------------------------------------------------------

function stateDir(): string {
  return sharedStateDir();
}

/**
 * Repo root.
 * Priority: AF_REPO_ROOT → AUTONOMOUS_TEAM_DIR/.. → 5 levels up from this file.
 * This file lives at ts-backend/src/rpc/misc-batch5.ts (5 levels below repo root).
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
// dial.list
// Mirrors: backend/rpc/dial_control.py handle_list() → dial_registry.list_directives()
// Response: {"dials": [{name, level, ceiling, active_directives, ttl_revert_at}, ...]}
// ---------------------------------------------------------------------------

interface DialEntry {
  level: number;
  ceiling: number;
  directives: Array<{ ttl_until?: string }>;
}

/** Read dial-registry.json exactly as Python's _load_registry() does. */
function loadDialRegistry(state: string): Map<string, DialEntry> {
  const registryPath = join(state, "dial-registry.json");
  if (!existsSync(registryPath)) return new Map();

  let data: unknown;
  try {
    data = JSON.parse(readFileSync(registryPath, "utf-8"));
  } catch {
    return new Map();
  }

  const registry = new Map<string, DialEntry>();

  if (Array.isArray(data)) {
    // Legacy list format
    for (const entry of data as Array<Record<string, unknown>>) {
      const cls =
        (entry["class"] as string | undefined) ??
        (entry["class_name"] as string | undefined);
      if (cls) {
        registry.set(cls, {
          level: parseInt(String(entry["level"] ?? 1), 10) || 1,
          ceiling: parseInt(String(entry["ceiling"] ?? 5), 10) || 5,
          directives:
            (entry["directives"] as Array<{ ttl_until?: string }>) ?? [],
        });
      }
    }
  } else if (data !== null && data !== undefined && typeof data === "object") {
    for (const [cls, val] of Object.entries(
      data as Record<string, unknown>
    )) {
      const v = val as Record<string, unknown>;
      if (v && typeof v === "object") {
        registry.set(cls, {
          level: parseInt(String(v["level"] ?? 1), 10) || 1,
          ceiling: parseInt(String(v["ceiling"] ?? 5), 10) || 5,
          directives:
            (v["directives"] as Array<{ ttl_until?: string }>) ?? [],
        });
      }
    }
  }

  return registry;
}

/**
 * dial.list — return current dial state for all registered classes.
 *
 * Mirrors Python: dial_control.handle_list() → list_directives()
 * Output is sorted by class name (same as Python's sorted(registry.items())).
 */
export function handleDialList(
  _params: Record<string, unknown>
): unknown {
  const state = stateDir();
  const registry = loadDialRegistry(state);

  const dials: unknown[] = [];
  for (const [className, entry] of [...registry.entries()].sort()) {
    // Find the earliest TTL from active directives (any that have ttl_until).
    const ttlDates: Date[] = [];
    for (const d of entry.directives ?? []) {
      if (d.ttl_until) {
        try {
          const t = new Date(d.ttl_until);
          if (!isNaN(t.getTime())) ttlDates.push(t);
        } catch {
          /* skip */
        }
      }
    }

    let ttlRevertAt: string | null = null;
    if (ttlDates.length > 0) {
      const earliest = ttlDates.reduce((a, b) => (a < b ? a : b));
      // Python: min(d["ttl_until"] for d in active) — string min comparison.
      // Python stores ttl_until as ISO string; min() on strings is lexicographic = chronological.
      // We rebuild from Date for type safety; output matches Python's min isoformat.
      ttlRevertAt = earliest.toISOString().replace(/\.\d{3}Z$/, "+00:00");
    }

    dials.push({
      name: className,
      level: entry.level,
      ceiling: entry.ceiling,
      active_directives: (entry.directives ?? []).length,
      ttl_revert_at: ttlRevertAt,
    });
  }

  return { dials };
}

// ---------------------------------------------------------------------------
// auth_retry.summary
// Mirrors: backend/rpc/auth_retry_counter.py handle_summary()
// Data source: SQLite state.db blackboard table (auth_retry_count, auth_retry_timestamps)
// Response: {count_24h: int, count_total: int, last_seen: iso8601 | null}
// ---------------------------------------------------------------------------

/**
 * Read a blackboard value from SQLite state.db.
 * The value column stores a JSON string encoding {"value": <inner>, "version": int, ...}.
 * Returns the inner value (not the full entry).
 */
function readBlackboardFromSqlite(
  dbPath: string,
  key: string
): unknown {
  try {
    const db = new Database(dbPath, { readonly: true });
    try {
      const row = db
        .query<{ value: string }, [string]>(
          "SELECT value FROM blackboard WHERE key = ?"
        )
        .get(key);
      if (!row) return null;
      const entry = JSON.parse(row.value) as Record<string, unknown>;
      // entry is {"value": <inner>, "version": int, ...}
      return "value" in entry ? entry["value"] : entry;
    } finally {
      db.close();
    }
  } catch {
    return null;
  }
}

/**
 * auth_retry.summary — read auth-retry telemetry from SQLite blackboard.
 *
 * Mirrors Python: auth_retry_counter.handle_summary()
 * Uses get_blackboard() which prefers SQLite when state.db exists.
 */
export function handleAuthRetrySummary(
  _params: Record<string, unknown>
): unknown {
  const emptyResult = { count_24h: 0, count_total: 0, last_seen: null };

  try {
    const dbPath = join(stateDir(), "state.db");
    if (!existsSync(dbPath)) {
      // No SQLite — return empty (file-based blackboard not yet implemented here)
      return emptyResult;
    }

    const totalRaw = readBlackboardFromSqlite(dbPath, "auth_retry_count");
    const countTotal =
      typeof totalRaw === "number"
        ? Math.trunc(totalRaw)
        : totalRaw !== null && totalRaw !== undefined
        ? Math.trunc(parseInt(String(totalRaw), 10)) || 0
        : 0;

    const tsRaw = readBlackboardFromSqlite(dbPath, "auth_retry_timestamps");

    let timestamps: string[] = [];
    if (Array.isArray(tsRaw)) {
      timestamps = tsRaw.filter((t): t is string => typeof t === "string");
    } else if (typeof tsRaw === "string") {
      try {
        const parsed = JSON.parse(tsRaw) as unknown;
        if (Array.isArray(parsed)) {
          timestamps = (parsed as unknown[]).filter(
            (t): t is string => typeof t === "string"
          );
        }
      } catch {
        /* ignore */
      }
    }

    // cutoff = now - 24h (ISO string, same string comparison as Python: t >= cutoff)
    const cutoff = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
    const count24h = timestamps.filter((t) => t >= cutoff).length;
    const lastSeen =
      timestamps.length > 0 ? timestamps[timestamps.length - 1] : null;

    return { count_24h: count24h, count_total: countTotal, last_seen: lastSeen };
  } catch {
    return emptyResult;
  }
}

// ---------------------------------------------------------------------------
// circuit_breaker.summary
// Mirrors: backend/circuit_breaker.py _collect_tripped() + summary CLI output
// Data source: file-based blackboard failures/ + failures_meta/ directories
// Response: {tripped: [...], warnings: [...], threshold: 3}
// ---------------------------------------------------------------------------

const CB_DEFAULT_THRESHOLD = 3;

/**
 * Read all keys under a blackboard prefix from the file-based blackboard.
 * The file blackboard stores each key as <bbRoot>/<prefix>/<key>.json.
 * Returns {key, value} pairs where value is the inner blackboard value.
 */
function readFileBlackboardPrefix(
  bbRoot: string,
  prefix: string
): Array<{ key: string; value: unknown }> {
  const dirName = prefix.replace(/\/$/, "");
  const dir = join(bbRoot, dirName);
  if (!existsSync(dir)) return [];

  let files: string[] = [];
  try {
    files = readdirSync(dir).filter((f) => f.endsWith(".json"));
  } catch {
    return [];
  }

  const result: Array<{ key: string; value: unknown }> = [];
  for (const f of files) {
    const key = `${dirName}/${f.replace(/\.json$/, "")}`;
    try {
      const raw = readFileSync(join(dir, f), "utf-8");
      const entry = JSON.parse(raw) as Record<string, unknown>;
      // entry = {"value": <inner>, "version": int, ...}
      const inner = "value" in entry ? entry["value"] : entry;
      result.push({ key, value: inner });
    } catch {
      /* skip malformed files */
    }
  }
  return result;
}

/**
 * Read a single key from the file-based blackboard.
 */
function readFileBlackboardKey(bbRoot: string, key: string): unknown {
  const filePath = join(bbRoot, `${key}.json`);
  if (!existsSync(filePath)) return null;
  try {
    const raw = readFileSync(filePath, "utf-8");
    const entry = JSON.parse(raw) as Record<string, unknown>;
    return "value" in entry ? entry["value"] : entry;
  } catch {
    return null;
  }
}

/**
 * circuit_breaker.summary — replicate _collect_tripped() from Python.
 *
 * Mirrors Python: circuit_breaker.py _collect_tripped() + summary output
 * The Python circuit_breaker uses Blackboard() (file-based, not SQLite).
 * Blackboard root: STATE_DIR/blackboard/
 */
export function handleCircuitBreakerSummary(
  _params: Record<string, unknown>
): unknown {
  const empty = { tripped: [], warnings: [], threshold: CB_DEFAULT_THRESHOLD };

  try {
    const bbRoot = join(stateDir(), "blackboard");
    const failureEntries = readFileBlackboardPrefix(bbRoot, "failures/");

    const collected: Array<{
      discussion: number;
      count: number;
      agent: string | null;
      reason: string | null;
      updated_at: string | null;
      blocked: boolean;
    }> = [];

    for (const { key, value } of failureEntries) {
      // key like "failures/10"
      const discStr = key.split("/")[1];
      if (!discStr) continue;
      const discNum = parseInt(discStr, 10);
      if (isNaN(discNum)) continue;

      const count =
        typeof value === "number" ? Math.trunc(value) : 0;

      // Read meta sidecar: failures_meta/<disc_num>
      const meta = readFileBlackboardKey(bbRoot, `failures_meta/${discNum}`);
      let agent: string | null = null;
      let reason: string | null = null;
      let updatedAt: string | null = null;
      if (meta && typeof meta === "object") {
        const m = meta as Record<string, unknown>;
        agent = typeof m["agent"] === "string" ? m["agent"] : null;
        reason = typeof m["reason"] === "string" ? m["reason"] : null;
        updatedAt =
          typeof m["updated_at"] === "string" ? m["updated_at"] : null;
      }

      collected.push({
        discussion: discNum,
        count,
        agent,
        reason,
        updated_at: updatedAt,
        blocked: count >= CB_DEFAULT_THRESHOLD,
      });
    }

    // Python summary output:
    //   tripped = [e for e in tripped if e["blocked"]]
    //   warnings = [e for e in tripped if not e["blocked"] and e["count"] > 0]
    const tripped = collected
      .filter((e) => e.blocked)
      .map((e) => ({
        discussion: e.discussion,
        count: e.count,
        agent: e.agent,
        reason: e.reason,
        updated_at: e.updated_at,
      }));

    const warnings = collected
      .filter((e) => !e.blocked && e.count > 0)
      .map((e) => ({
        discussion: e.discussion,
        count: e.count,
        agent: e.agent,
        reason: e.reason,
        updated_at: e.updated_at,
      }));

    return { tripped, warnings, threshold: CB_DEFAULT_THRESHOLD };
  } catch {
    return empty;
  }
}

// ---------------------------------------------------------------------------
// circuitBreaker.history
// Mirrors: backend/circuit_breaker.py history(role, limit)
// Data source: STATE_DIR/circuit-breaker-history.jsonl (or autonomousTeamDir)
// Response: list of transition objects for the given role, newest last, up to limit
// ---------------------------------------------------------------------------

/**
 * circuitBreaker.history — read transition history for a role.
 *
 * Params: {role: str, limit: int (default 20)}
 * Mirrors Python: circuit_breaker.history(role=role, limit=limit)
 *
 * Python's _HISTORY_FILE = _REPO_ROOT/.autonomous-team/circuit-breaker-history.jsonl
 * but production typically places the file in STATE_DIR.
 * We check STATE_DIR first, then fall back to autonomousTeamDir().
 */
export function handleCircuitBreakerHistory(
  params: Record<string, unknown>
): unknown {
  const role =
    typeof params["role"] === "string" ? params["role"].trim() : "";
  if (!role) {
    const err = new Error("role is required") as Error & {
      rpc_code?: number;
    };
    err.rpc_code = -32602;
    throw err;
  }
  const limitRaw = params["limit"];
  const limit =
    limitRaw !== undefined
      ? Math.trunc(parseInt(String(limitRaw), 10)) || 20
      : 20;

  // Resolve history file — check STATE_DIR first, then autonomousTeamDir
  const state = stateDir();
  const historyPathState = join(state, "circuit-breaker-history.jsonl");
  const historyPathRepo = join(
    autonomousTeamDir(),
    "circuit-breaker-history.jsonl"
  );

  const historyPath = existsSync(historyPathState)
    ? historyPathState
    : historyPathRepo;

  if (!existsSync(historyPath)) return [];

  let raw: string;
  try {
    raw = readFileSync(historyPath, "utf-8");
  } catch {
    return [];
  }

  const matches: unknown[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let entry: Record<string, unknown>;
    try {
      entry = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      continue;
    }
    if (entry["role"] === role) {
      matches.push(entry);
    }
  }

  // Return the last `limit` entries (chronological order — newest last)
  return matches.slice(-limit);
}

// ---------------------------------------------------------------------------
// kpi.history
// Mirrors: backend/kpi_engine.py history(days, repo_root)
// Data source: git log with fixed argv
// Response: [{"date": "YYYY-MM-DD", "count": int}, ...] sorted by date asc
// ---------------------------------------------------------------------------

// Squash-merge pattern: mirrors Python _PR_SUBJECT = re.compile(r"^#\d+:|\(#\d+\)\s*$")
const _PR_SUBJECT_RE = /^#\d+:|\(#\d+\)\s*$/;

/**
 * kpi.history — return merged-PRs-per-day for the last `days` days.
 *
 * Params: {days: int (default 30), project: str}
 * Mirrors Python: kpi_engine.history(days, repo_root)
 * Per-project scoping: when project param is set and a local checkout exists,
 * Python runs git log in that checkout. TS batch 5 serves AF default only
 * (project param accepted but non-AF projects return []).
 */
export function handleKpiHistory(
  params: Record<string, unknown>
): unknown {
  const _project = params["project"] ?? null;
  void _project; // per-project scoping is P6b scope

  const daysRaw = params["days"] ?? 30;
  let days: number;
  try {
    days = Math.trunc(parseInt(String(daysRaw), 10));
  } catch {
    const err = new Error(
      `days must be an integer, got ${String(daysRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (isNaN(days)) {
    const err = new Error(
      `days must be an integer, got ${String(daysRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (days < 1) {
    const err = new Error("days must be >= 1") as Error & {
      rpc_code?: number;
    };
    err.rpc_code = -32602;
    throw err;
  }

  const cutoff = new Date(Date.now() - days * 86400 * 1000);
  const sinceStr = cutoff.toISOString().slice(0, 10); // YYYY-MM-DD

  let stdout: string;
  try {
    stdout = execFileSync(
      "git",
      [
        "log",
        `--since=${sinceStr}`,
        "--pretty=format:%cd\t%s",
        "--date=format:%Y-%m-%d",
      ],
      {
        encoding: "utf-8",
        timeout: 30_000,
        cwd: repoRoot(),
      }
    );
  } catch {
    return [];
  }

  const counts: Map<string, number> = new Map();
  for (const rawLine of stdout.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const tabIdx = line.indexOf("\t");
    if (tabIdx < 0) continue;
    const dateStr = line.slice(0, tabIdx);
    const subject = line.slice(tabIdx + 1);
    if (_PR_SUBJECT_RE.test(subject)) {
      counts.set(dateStr, (counts.get(dateStr) ?? 0) + 1);
    }
  }

  // Sort by date ascending (matches Python dict + sorted())
  return [...counts.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([date, count]) => ({ date, count }));
}

// ---------------------------------------------------------------------------
// kpi.cycle_time
// Mirrors: backend/kpi_engine.py cycle_time_histogram(days, repo_root)
// Data source: .autonomous-team/registry.json + file-based blackboard discussions/
// Response: [{"bucket": str, "count": int}, ...] — 4 fixed buckets
// ---------------------------------------------------------------------------

const _CYCLE_BUCKETS = ["0-2h", "2-6h", "6-24h", "24h+"] as const;

function parseIso(s: string | undefined | null): Date | null {
  if (!s) return null;
  try {
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  } catch {
    return null;
  }
}

/**
 * kpi.cycle_time — return cycle-time histogram for merged PRs.
 *
 * Params: {days: int (default 90), project: str}
 * Mirrors Python: kpi_engine.cycle_time_histogram(days, repo_root)
 */
export function handleKpiCycleTime(
  params: Record<string, unknown>
): unknown {
  const zeroBuckets = _CYCLE_BUCKETS.map((b) => ({ bucket: b, count: 0 }));

  const daysRaw = params["days"] ?? 90;
  let days: number;
  try {
    days = Math.trunc(parseInt(String(daysRaw), 10));
  } catch {
    const err = new Error(
      `days must be an integer, got ${String(daysRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (isNaN(days)) {
    const err = new Error(
      `days must be an integer, got ${String(daysRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (days < 1) {
    const err = new Error("days must be >= 1") as Error & {
      rpc_code?: number;
    };
    err.rpc_code = -32602;
    throw err;
  }

  // Load registry.json
  const registryPath = join(autonomousTeamDir(), "registry.json");
  if (!existsSync(registryPath)) return zeroBuckets;

  let discussions: unknown[];
  try {
    const data = JSON.parse(
      readFileSync(registryPath, "utf-8")
    ) as Record<string, unknown>;
    const raw = data["discussions"];
    discussions = Array.isArray(raw) ? raw : [];
  } catch {
    return zeroBuckets;
  }

  const cutoff = new Date(Date.now() - days * 86400 * 1000);
  const counts: Record<string, number> = {
    "0-2h": 0,
    "2-6h": 0,
    "6-24h": 0,
    "24h+": 0,
  };
  const bbRoot = join(stateDir(), "blackboard");

  for (const d of discussions) {
    if (typeof d !== "object" || d === null) continue;
    const disc = d as Record<string, unknown>;

    if (disc["status"] !== "DONE") continue;

    const closed = parseIso(disc["closed_at"] as string | undefined);
    if (closed === null || closed < cutoff) continue;

    // Prefer blackboard spawn time — mirrors Python's bb_path logic
    let spawnTime: Date | null = null;
    const discNum = disc["number"] ?? disc["id"];
    if (discNum !== undefined && discNum !== null) {
      const bbDiscPath = join(bbRoot, "discussions", `${discNum}.json`);
      if (existsSync(bbDiscPath)) {
        try {
          const bbEntry = JSON.parse(
            readFileSync(bbDiscPath, "utf-8")
          ) as Record<string, unknown>;
          // File blackboard: {"value": {...}, ...}
          const inner =
            "value" in bbEntry && typeof bbEntry["value"] === "object"
              ? (bbEntry["value"] as Record<string, unknown>)
              : bbEntry;
          spawnTime =
            parseIso(inner["spawned_at"] as string | undefined) ??
            parseIso(inner["created_at"] as string | undefined);
        } catch {
          /* skip */
        }
      }
    }

    if (spawnTime === null) {
      spawnTime = parseIso(disc["created_at"] as string | undefined);
    }
    if (spawnTime === null || closed <= spawnTime) continue;

    const hours =
      (closed.getTime() - spawnTime.getTime()) / 3_600_000;
    if (hours < 2) counts["0-2h"]++;
    else if (hours < 6) counts["2-6h"]++;
    else if (hours < 24) counts["6-24h"]++;
    else counts["24h+"]++;
  }

  return _CYCLE_BUCKETS.map((b) => ({ bucket: b, count: counts[b] }));
}

// ---------------------------------------------------------------------------
// CostTracker helpers (shared by cost.per_discussion and cost.by_discussion)
// Mirrors: backend/cost_tracker.py _compute_cost() + CostTracker.get_session_cost()
// Data source: file-based blackboard budget/agents/ + config.json pricing
// ---------------------------------------------------------------------------

interface PricingRate {
  input_per_1k: number;
  output_per_1k: number;
  cache_read_per_1k?: number;
  cache_write_5m_per_1k?: number;
}

type PricingTable = Record<string, PricingRate>;

/** Default pricing (mirrors Python's _DEFAULT_PRICING exactly). */
const _DEFAULT_PRICING: PricingTable = {
  default: { input_per_1k: 0.003, output_per_1k: 0.015 },
  "claude-opus-4-7": {
    input_per_1k: 0.015,
    output_per_1k: 0.075,
    cache_read_per_1k: 0.0015,
    cache_write_5m_per_1k: 0.01875,
  },
  "claude-opus-4-7[1m]": {
    input_per_1k: 0.030,
    output_per_1k: 0.150,
    cache_read_per_1k: 0.003,
    cache_write_5m_per_1k: 0.0375,
  },
  "claude-sonnet-4-6": {
    input_per_1k: 0.003,
    output_per_1k: 0.015,
    cache_read_per_1k: 0.0003,
    cache_write_5m_per_1k: 0.00375,
  },
  "claude-sonnet-4-5-20250929": {
    input_per_1k: 0.003,
    output_per_1k: 0.015,
  },
  "claude-haiku-4-5-20251001": {
    input_per_1k: 0.0008,
    output_per_1k: 0.004,
  },
  "claude-sonnet-4-20250514": { input_per_1k: 0.003, output_per_1k: 0.015 },
  "claude-opus-4-20250514": { input_per_1k: 0.015, output_per_1k: 0.075 },
  "kimi-k2-0711": { input_per_1k: 0.0006, output_per_1k: 0.002 },
};

/** Load pricing from config.json, falling back to _DEFAULT_PRICING. */
function loadPricing(): PricingTable {
  try {
    const configPath = join(autonomousTeamDir(), "config.json");
    if (!existsSync(configPath)) return { ..._DEFAULT_PRICING };
    const cfg = JSON.parse(readFileSync(configPath, "utf-8")) as Record<
      string,
      unknown
    >;
    const pricing = cfg["pricing"] as PricingTable | undefined;
    if (
      !pricing ||
      typeof pricing !== "object" ||
      Object.keys(pricing).length === 0
    ) {
      return { ..._DEFAULT_PRICING };
    }
    if (!pricing["default"]) pricing["default"] = _DEFAULT_PRICING["default"];
    return pricing;
  } catch {
    return { ..._DEFAULT_PRICING };
  }
}

/**
 * Compute USD cost for a single agent record.
 * Mirrors Python: _compute_cost(input_tokens, output_tokens, model, pricing, ...)
 */
function computeCost(
  inputTokens: number,
  outputTokens: number,
  model: string,
  pricing: PricingTable,
  cacheReadTokens = 0,
  cacheWriteTokens = 0
): number {
  const rates =
    pricing[model] ?? pricing["default"] ?? _DEFAULT_PRICING["default"];
  const inputRate = rates.input_per_1k ?? 0.003;
  const outputRate = rates.output_per_1k ?? 0.015;
  let cost =
    (inputTokens / 1000) * inputRate + (outputTokens / 1000) * outputRate;
  if (cacheReadTokens > 0) {
    cost += (cacheReadTokens / 1000) * (rates.cache_read_per_1k ?? 0);
  }
  if (cacheWriteTokens > 0) {
    cost += (cacheWriteTokens / 1000) * (rates.cache_write_5m_per_1k ?? 0);
  }
  return cost;
}

interface AgentRecord {
  agent: string;
  agent_id: string;
  input: number;
  output: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  model: string;
  finished: string | null;
  discussion: number | null;
  pr: number | null;
  cost_usd: number;
}

/**
 * Read all agent records from the file-based blackboard budget/agents/ directory.
 * Mirrors Python: CostTracker.get_session_cost() agent_keys loop.
 */
function readAgentRecords(
  bbRoot: string,
  pricing: PricingTable
): AgentRecord[] {
  const agentsDir = join(bbRoot, "budget", "agents");
  if (!existsSync(agentsDir)) return [];

  let files: string[];
  try {
    files = readdirSync(agentsDir).filter((f) => f.endsWith(".json"));
  } catch {
    return [];
  }

  const records: AgentRecord[] = [];
  for (const f of files) {
    try {
      const raw = readFileSync(join(agentsDir, f), "utf-8");
      const entry = JSON.parse(raw) as Record<string, unknown>;
      // File blackboard entry: {"value": {...}, "version": int, ...}
      const record =
        entry["value"] && typeof entry["value"] === "object"
          ? (entry["value"] as Record<string, unknown>)
          : entry;

      const inputTokens =
        Math.trunc(parseInt(String(record["input"] ?? 0), 10)) || 0;
      const outputTokens =
        Math.trunc(parseInt(String(record["output"] ?? 0), 10)) || 0;
      const cacheRead =
        Math.trunc(parseInt(String(record["cache_read_tokens"] ?? 0), 10)) ||
        0;
      const cacheWrite =
        Math.trunc(
          parseInt(String(record["cache_write_tokens"] ?? 0), 10)
        ) || 0;
      const model = (record["model"] as string) || "default";
      const agentId =
        (record["agent_id"] as string) || f.replace(/\.json$/, "");
      const role = (record["agent"] as string) || "unknown";
      const finished = (record["finished"] as string) || null;

      const discussionRaw = record["discussion"];
      const discussionParsed =
        discussionRaw !== null && discussionRaw !== undefined
          ? Math.trunc(parseInt(String(discussionRaw), 10))
          : NaN;
      const discussion = isNaN(discussionParsed) ? null : discussionParsed;

      const prRaw = record["pr"];
      const prParsed =
        prRaw !== null && prRaw !== undefined
          ? Math.trunc(parseInt(String(prRaw), 10))
          : NaN;
      const pr = isNaN(prParsed) ? null : prParsed;

      const cost = computeCost(
        inputTokens,
        outputTokens,
        model,
        pricing,
        cacheRead,
        cacheWrite
      );

      records.push({
        agent: role,
        agent_id: agentId,
        input: inputTokens,
        output: outputTokens,
        cache_read_tokens: cacheRead,
        cache_write_tokens: cacheWrite,
        model,
        finished,
        discussion,
        pr,
        cost_usd: parseFloat(cost.toFixed(6)),
      });
    } catch {
      /* skip malformed files */
    }
  }
  return records;
}

// ---------------------------------------------------------------------------
// cost.per_discussion
// Mirrors: backend/server.py _rpc_cost_per_discussion()
// → CostTracker().get_session_cost() → filter by_discussion[disc_num]
// Response: single cost entry dict or null
// ---------------------------------------------------------------------------

/**
 * cost.per_discussion — return cost breakdown for a single Discussion.
 *
 * Params: {discussion: int}
 * Mirrors Python: _rpc_cost_per_discussion() → CostTracker().get_session_cost()
 *   then returns next((e for e in full["by_discussion"] if e["discussion"]==disc_num), None)
 */
export function handleCostPerDiscussion(
  params: Record<string, unknown>
): unknown {
  const discRaw = params["discussion"];
  if (discRaw === null || discRaw === undefined) {
    const err = new Error("discussion parameter required") as Error & {
      rpc_code?: number;
    };
    err.rpc_code = -32602;
    throw err;
  }
  const discNum = Math.trunc(parseInt(String(discRaw), 10));
  if (isNaN(discNum)) {
    const err = new Error(
      `discussion must be an integer, got ${String(discRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }

  try {
    const bbRoot = join(stateDir(), "blackboard");
    const pricing = loadPricing();
    const agents = readAgentRecords(bbRoot, pricing);

    // Build by_discussion totals — mirrors Python's discussion_totals accumulation
    const discTotals = new Map<
      number,
      {
        discussion: number;
        cost_usd: number;
        agents: string[];
        input_tokens: number;
        output_tokens: number;
        agent_count: number;
        _agent_breakdown: Record<string, number>;
        _pr_breakdown: Record<string, number>;
      }
    >();

    for (const rec of agents) {
      if (rec.discussion === null) continue;
      const discKey = rec.discussion;
      if (!discTotals.has(discKey)) {
        discTotals.set(discKey, {
          discussion: discKey,
          cost_usd: 0,
          agents: [],
          input_tokens: 0,
          output_tokens: 0,
          agent_count: 0,
          _agent_breakdown: {},
          _pr_breakdown: {},
        });
      }
      const entry = discTotals.get(discKey)!;
      entry.cost_usd += rec.cost_usd;
      entry.agents.push(rec.agent_id);
      entry.input_tokens += rec.input;
      entry.output_tokens += rec.output;
      entry.agent_count++;
      entry._agent_breakdown[rec.agent] =
        (entry._agent_breakdown[rec.agent] ?? 0) + rec.cost_usd;
      if (rec.pr !== null) {
        const prStr = String(rec.pr);
        entry._pr_breakdown[prStr] =
          (entry._pr_breakdown[prStr] ?? 0) + rec.cost_usd;
      }
    }

    // Find the matching discussion entry
    const raw = discTotals.get(discNum);
    if (!raw) return null;

    // Convert to Python's by_discussion shape
    const costUsd = parseFloat(raw.cost_usd.toFixed(6));
    return {
      discussion: raw.discussion,
      cost_usd: costUsd,
      total_cost_usd: costUsd,
      total_input_tokens: raw.input_tokens,
      total_output_tokens: raw.output_tokens,
      agent_count: raw.agent_count,
      agents: raw.agents,
      agent_breakdown: Object.fromEntries(
        Object.entries(raw._agent_breakdown).map(([k, v]) => [
          k,
          parseFloat(v.toFixed(6)),
        ])
      ),
      pr_breakdown: Object.fromEntries(
        Object.entries(raw._pr_breakdown).map(([k, v]) => [
          k,
          parseFloat(v.toFixed(6)),
        ])
      ),
    };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// cost.by_discussion
// Mirrors: backend/server.py _rpc_cost_by_discussion()
// Response: [{discussion: int, tokens: int, usd: float}, ...] sorted by usd desc, top N
// ---------------------------------------------------------------------------

/**
 * cost.by_discussion — return top-N discussions by token spend within `days` days.
 *
 * Params: {top: int (default 10, min 1), days: int (default 90, min 1)}
 * Mirrors Python: _rpc_cost_by_discussion() which re-aggregates by_agent within days window.
 */
export function handleCostByDiscussion(
  params: Record<string, unknown>
): unknown {
  const topRaw = params["top"] ?? 10;
  let top: number;
  try {
    top = Math.trunc(parseInt(String(topRaw), 10));
  } catch {
    const err = new Error(
      `top must be an integer, got ${String(topRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (isNaN(top)) {
    const err = new Error(
      `top must be an integer, got ${String(topRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (top < 1) {
    const err = new Error("top must be >= 1") as Error & {
      rpc_code?: number;
    };
    err.rpc_code = -32602;
    throw err;
  }

  const daysRaw = params["days"] ?? 90;
  let days: number;
  try {
    days = Math.trunc(parseInt(String(daysRaw), 10));
  } catch {
    const err = new Error(
      `days must be an integer, got ${String(daysRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (isNaN(days)) {
    const err = new Error(
      `days must be an integer, got ${String(daysRaw)}`
    ) as Error & { rpc_code?: number };
    err.rpc_code = -32602;
    throw err;
  }
  if (days < 1) {
    const err = new Error("days must be >= 1") as Error & {
      rpc_code?: number;
    };
    err.rpc_code = -32602;
    throw err;
  }

  try {
    const cutoff = new Date(Date.now() - days * 86400 * 1000);
    const bbRoot = join(stateDir(), "blackboard");
    const pricing = loadPricing();
    const agents = readAgentRecords(bbRoot, pricing);

    // Re-aggregate by_discussion within the days window using by_agent entries.
    // Mirrors Python: each by_agent entry has 'finished' ISO timestamp + 'discussion' number.
    const discTotals = new Map<number, { tokens: number; usd: number }>();

    for (const rec of agents) {
      if (rec.discussion === null) continue;
      const discInt = rec.discussion;

      // Apply days window filter on finished timestamp
      if (rec.finished) {
        try {
          const finishedDt = new Date(rec.finished);
          if (!isNaN(finishedDt.getTime()) && finishedDt < cutoff) {
            continue;
          }
        } catch {
          // Unparseable timestamp — include the entry (fail open, mirrors Python)
        }
      }

      const tokens = rec.input + rec.output;
      const usd = rec.cost_usd;

      if (!discTotals.has(discInt)) {
        discTotals.set(discInt, { tokens: 0, usd: 0 });
      }
      const t = discTotals.get(discInt)!;
      t.tokens += tokens;
      t.usd += usd;
    }

    // Sort by usd descending, then take top N
    const out = [...discTotals.entries()]
      .map(([disc, t]) => ({
        discussion: disc,
        tokens: Math.trunc(t.tokens),
        usd: parseFloat(t.usd.toFixed(6)),
      }))
      .sort((a, b) => b.usd - a.usd);

    return out.slice(0, top);
  } catch {
    return [];
  }
}
