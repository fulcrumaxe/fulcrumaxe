/**
 * spawn/claude-spawn-tracker.ts — Mirrors backend/claude_spawn_tracker.py 1:1.
 *
 * Repo-wide Claude Code subprocess spawn budget + circuit breaker.
 *
 * Tracks Claude Code subprocess spawn count and estimated USD spend over rolling
 * 1h / 24h windows in the blackboard. Trips when any of three configurable
 * thresholds are crossed and refuses further spawns until manual or auto reset.
 *
 * Integration contract
 * --------------------
 * Every call site that exec's `claude -p` (or equivalent subprocess) MUST call
 * record() BEFORE starting the process. If SpawnBlocked is thrown, abort the
 * spawn and surface the error to the caller.
 *
 * CLI usage:
 *   bun run src/spawn/claude-spawn-tracker.ts status
 *   bun run src/spawn/claude-spawn-tracker.ts summary --json
 *   bun run src/spawn/claude-spawn-tracker.ts reset
 *   bun run src/spawn/claude-spawn-tracker.ts record <source> [--est-tokens N] [--est-cost-usd F]
 */

import {
  existsSync,
  readFileSync,
  writeFileSync,
  mkdirSync,
  renameSync,
  unlinkSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Path helpers (mirrors blackboard._resolve_default_root logic)
// ---------------------------------------------------------------------------

function stateDir(): string {
  return sharedStateDir();
}

function repoRoot(): string {
  if (process.env["AF_REPO_ROOT"]) return process.env["AF_REPO_ROOT"]!;
  // This file lives at ts-backend/src/spawn/claude-spawn-tracker.ts
  // → ts-backend/src/spawn/ → ts-backend/src/ → ts-backend/ → repo root
  const thisFile = new URL(import.meta.url).pathname;
  return join(thisFile, "..", "..", "..", "..");
}

function autonomousTeamDir(): string {
  return process.env["AUTONOMOUS_TEAM_DIR"] ?? join(repoRoot(), ".autonomous-team");
}

function blackboardRoot(): string {
  return join(stateDir(), "blackboard");
}

// ---------------------------------------------------------------------------
// Constants (mirrors Python _DEFAULT_CONFIG)
// ---------------------------------------------------------------------------

const _DEFAULT_CONFIG = {
  spawns_per_hour_max: 50,
  spend_per_hour_usd_max: 5.00,
  spawns_24h_max: 200,
  auto_reset_idle_seconds: 3600,
  cost_per_spawn_usd_default: 0.05,
};

const _EVENTS_KEY = "spawn/claude_events";
const _TRIPPED_KEY = "spawn_breaker/tripped";
const _META_KEY = "spawn_breaker/tripped_meta";
const _BANNER_KEY = "dashboard/banner/spawn_breaker";

// Sources whose real cost is tracked in agent_runs / audit.jsonl rather than
// at spawn-time. For these sources, est_tokens and est_cost_usd are stored as
// null instead of the config default (0.05) so the spawn tracker stays a
// reliable counter without injecting fake spend into the budget dashboard.
const _UNMETERED_SOURCES = new Set(["innovate_tick_internal"]);

// ---------------------------------------------------------------------------
// Blackboard helpers (file-backed key-value store)
// ---------------------------------------------------------------------------

/** Convert a blackboard key like "spawn/claude_events" to a file path. */
function bbKeyToPath(key: string): string {
  return join(blackboardRoot(), ...key.split("/")) + ".json";
}

/** Read a value from the blackboard. Returns null on missing/error. */
function bbRead(key: string): unknown {
  const filePath = bbKeyToPath(key);
  if (!existsSync(filePath)) return null;
  try {
    const raw = readFileSync(filePath, "utf-8");
    const entry = JSON.parse(raw) as Record<string, unknown>;
    // BB file format: { "value": <actual value>, "version": int, ... }
    if ("value" in entry) return entry["value"];
    return entry;
  } catch {
    return null;
  }
}

/** Write a value to the blackboard atomically. */
function bbWrite(key: string, value: unknown, updatedBy = "claude_spawn_tracker"): void {
  const filePath = bbKeyToPath(key);
  mkdirSync(dirname(filePath), { recursive: true });
  const entry: Record<string, unknown> = {
    value,
    version: 1,
    updated_at: nowIso(),
    updated_by: updatedBy,
  };
  // Atomic write via temp file + rename
  const tmpPath = filePath + ".tmp";
  try {
    writeFileSync(tmpPath, JSON.stringify(entry, null, 2));
    renameSync(tmpPath, filePath);
  } catch {
    // best-effort
  }
}

/** Delete a blackboard key. Best-effort. */
function bbDelete(key: string): void {
  const filePath = bbKeyToPath(key);
  if (!existsSync(filePath)) return;
  try {
    unlinkSync(filePath);
  } catch {
    // best-effort — write null as fallback
    try {
      bbWrite(key, null, "claude_spawn_tracker/delete");
    } catch {
      // ignore
    }
  }
}

// ---------------------------------------------------------------------------
// Timestamp helpers
// ---------------------------------------------------------------------------

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function tsToMs(tsStr: string): number {
  return new Date(tsStr.replace(/Z$/, "+00:00")).getTime();
}

function ageStr(tsStr: string | null | undefined): string {
  if (!tsStr) return "";
  try {
    const secs = Math.floor((Date.now() - tsToMs(tsStr)) / 1000);
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    return `${Math.floor(secs / 3600)}h ago`;
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Config loading (mirrors Python _load_config)
// ---------------------------------------------------------------------------

interface SpawnConfig {
  spawns_per_hour_max: number;
  spend_per_hour_usd_max: number;
  spawns_24h_max: number;
  auto_reset_idle_seconds: number;
  cost_per_spawn_usd_default: number;
}

function loadConfig(): SpawnConfig {
  const configPath = join(autonomousTeamDir(), "config.json");
  try {
    if (!existsSync(configPath)) return { ..._DEFAULT_CONFIG };
    const raw = JSON.parse(readFileSync(configPath, "utf-8")) as Record<string, unknown>;
    const cfg = (raw["spawn_breaker"] ?? {}) as Partial<SpawnConfig>;
    return {
      spawns_per_hour_max: cfg.spawns_per_hour_max ?? _DEFAULT_CONFIG.spawns_per_hour_max,
      spend_per_hour_usd_max: cfg.spend_per_hour_usd_max ?? _DEFAULT_CONFIG.spend_per_hour_usd_max,
      spawns_24h_max: cfg.spawns_24h_max ?? _DEFAULT_CONFIG.spawns_24h_max,
      auto_reset_idle_seconds: cfg.auto_reset_idle_seconds ?? _DEFAULT_CONFIG.auto_reset_idle_seconds,
      cost_per_spawn_usd_default: cfg.cost_per_spawn_usd_default ?? _DEFAULT_CONFIG.cost_per_spawn_usd_default,
    };
  } catch {
    return { ..._DEFAULT_CONFIG };
  }
}

// ---------------------------------------------------------------------------
// Spawn event type
// ---------------------------------------------------------------------------

interface SpawnEvent {
  ts: string;
  source: string;
  est_tokens: number | null;
  est_cost_usd: number | null;
}

// ---------------------------------------------------------------------------
// Internal helpers (mirror Python equivalents)
// ---------------------------------------------------------------------------

function loadEvents(): SpawnEvent[] {
  const raw = bbRead(_EVENTS_KEY);
  if (Array.isArray(raw)) return raw as SpawnEvent[];
  return [];
}

function trimEvents(events: SpawnEvent[], windowHours = 24.0): SpawnEvent[] {
  const cutoffMs = Date.now() - windowHours * 3600 * 1000;
  return events.filter((e) => tsToMs(e.ts) >= cutoffMs);
}

function windowEvents(events: SpawnEvent[], windowHours: number): SpawnEvent[] {
  const cutoffMs = Date.now() - windowHours * 3600 * 1000;
  return events.filter((e) => tsToMs(e.ts) >= cutoffMs);
}

function postTeamLog(message: string): void {
  const root = repoRoot();
  try {
    spawnSync(
      "bash",
      ["scripts/rotate-team-log.sh", "comment", message],
      { cwd: root, timeout: 10_000, stdio: "pipe" }
    );
  } catch {
    // non-fatal
  }
}

function doReset(): void {
  bbWrite(_TRIPPED_KEY, false, "claude_spawn_tracker/reset");
  bbDelete(_META_KEY);
  bbDelete(_BANNER_KEY);
}

function updateLastAttempt(nowStr: string): void {
  const meta = bbRead(_META_KEY);
  if (meta !== null && typeof meta === "object" && !Array.isArray(meta)) {
    const m = meta as Record<string, unknown>;
    m["last_attempt_at"] = nowStr;
    bbWrite(_META_KEY, m, "claude_spawn_tracker");
  }
}

function maybeAutoReset(cfg: SpawnConfig): void {
  if (!bbRead(_TRIPPED_KEY)) return;
  const meta = bbRead(_META_KEY);
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) return;
  const m = meta as Record<string, unknown>;
  const lastAttempt = m["last_attempt_at"] as string | undefined;
  if (!lastAttempt) return;
  try {
    const elapsedMs = Date.now() - tsToMs(lastAttempt);
    if (elapsedMs / 1000 >= cfg.auto_reset_idle_seconds) {
      doReset();
    }
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Public exception
// ---------------------------------------------------------------------------

export class SpawnBlocked extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SpawnBlocked";
  }
}

// ---------------------------------------------------------------------------
// Core API (mirrors Python record, is_tripped, get_state, reset, check_auto_reset)
// ---------------------------------------------------------------------------

/**
 * Record a Claude Code subprocess spawn.
 *
 * Increments rolling-window counters and checks thresholds.
 * Throws SpawnBlocked if the breaker is currently tripped.
 *
 * Mirrors backend/claude_spawn_tracker.record() exactly.
 */
export function record(
  source: string,
  estTokens = 0,
  estCostUsd: number | null = null,
): void {
  const cfg = loadConfig();

  const unmetered = _UNMETERED_SOURCES.has(source);
  let storedTokens: number | null;
  let storedCost: number | null;

  if (unmetered) {
    storedTokens = null;
    storedCost = null;
  } else {
    storedTokens = Math.trunc(estTokens);
    if (estCostUsd === null) {
      storedCost = cfg.cost_per_spawn_usd_default;
    } else {
      storedCost = estCostUsd;
    }
  }

  const nowStr = nowIso();

  // Check auto-reset before doing anything else
  maybeAutoReset(cfg);

  // Update last_attempt_at in meta regardless of tripped state
  updateLastAttempt(nowStr);

  // If already tripped, refuse
  if (bbRead(_TRIPPED_KEY)) {
    throw new SpawnBlocked("Claude spawn breaker is tripped — call reset() or wait for auto-reset");
  }

  // Append event
  let events = loadEvents();
  events.push({
    ts: nowStr,
    source,
    est_tokens: storedTokens,
    est_cost_usd: storedCost,
  });
  // Trim to last 24h before persisting
  events = trimEvents(events, 24.0);
  bbWrite(_EVENTS_KEY, events, "claude_spawn_tracker");

  // Check thresholds
  const events1h = windowEvents(events, 1.0);
  const events24h = events; // already trimmed to 24h

  const spawns1h = events1h.length;
  const spend1h = events1h.reduce(
    (s, e) => s + (e.est_cost_usd !== null ? e.est_cost_usd : 0),
    0
  );
  const spawns24h = events24h.length;

  const trip = (thresholdName: string, value: number): never => {
    const meta = {
      tripped_at: nowStr,
      reason: `${thresholdName} exceeded: ${value}`,
      threshold_name: thresholdName,
      value,
      last_attempt_at: nowStr,
    };
    bbWrite(_TRIPPED_KEY, true, "claude_spawn_tracker");
    bbWrite(_META_KEY, meta, "claude_spawn_tracker");
    bbWrite(_BANNER_KEY, {
      level: "error",
      message: `Spawn breaker tripped: ${thresholdName}=${value}`,
      dismissable: false,
      set_at: nowStr,
    }, "claude_spawn_tracker");
    postTeamLog(
      `[${nowStr.slice(0, 16)}] spawn-breaker: TRIPPED — ` +
      `${thresholdName}=${value} (source=${source})`
    );
    throw new SpawnBlocked(`Claude spawn breaker tripped: ${thresholdName}=${value}`);
  };

  if (spawns1h > cfg.spawns_per_hour_max) trip("spawns_per_hour_max", spawns1h);
  if (spend1h > cfg.spend_per_hour_usd_max) trip("spend_per_hour_usd_max", parseFloat(spend1h.toFixed(4)));
  if (spawns24h > cfg.spawns_24h_max) trip("spawns_24h_max", spawns24h);
}

/**
 * Return true if the spawn breaker is currently tripped.
 * Mirrors backend/claude_spawn_tracker.is_tripped() exactly.
 */
export function isTripped(): boolean {
  const cfg = loadConfig();
  maybeAutoReset(cfg);
  return Boolean(bbRead(_TRIPPED_KEY));
}

/**
 * Return current state dict: counts, spend, tripped flag, trip metadata.
 * Mirrors backend/claude_spawn_tracker.get_state() exactly.
 */
export interface SpawnState {
  tripped: boolean;
  spawns_1h: number;
  spawns_24h: number;
  spend_1h_usd: number;
  spend_24h_usd: number;
  per_source: Record<string, number>;
  thresholds: {
    spawns_per_hour_max: number;
    spend_per_hour_usd_max: number;
    spawns_24h_max: number;
  };
  tripped_meta: Record<string, unknown> | null;
}

export function getState(): SpawnState {
  const cfg = loadConfig();
  maybeAutoReset(cfg);

  const events = loadEvents();
  const events1h = windowEvents(events, 1.0);
  const events24h = trimEvents(events, 24.0);

  const perSource: Record<string, number> = {};
  for (const e of events24h) {
    const src = e.source ?? "unknown";
    perSource[src] = (perSource[src] ?? 0) + 1;
  }

  const meta = bbRead(_META_KEY);
  const trippedMeta =
    meta !== null && typeof meta === "object" && !Array.isArray(meta)
      ? (meta as Record<string, unknown>)
      : null;

  return {
    tripped: Boolean(bbRead(_TRIPPED_KEY)),
    spawns_1h: events1h.length,
    spawns_24h: events24h.length,
    spend_1h_usd: parseFloat(
      events1h
        .reduce((s, e) => s + (e.est_cost_usd !== null ? e.est_cost_usd : 0), 0)
        .toFixed(4)
    ),
    spend_24h_usd: parseFloat(
      events24h
        .reduce((s, e) => s + (e.est_cost_usd !== null ? e.est_cost_usd : 0), 0)
        .toFixed(4)
    ),
    per_source: perSource,
    thresholds: {
      spawns_per_hour_max: cfg.spawns_per_hour_max,
      spend_per_hour_usd_max: cfg.spend_per_hour_usd_max,
      spawns_24h_max: cfg.spawns_24h_max,
    },
    tripped_meta: trippedMeta,
  };
}

/**
 * Manual reset — clears tripped state, meta, and dashboard banner.
 * Mirrors backend/claude_spawn_tracker.reset() exactly.
 */
export function reset(): void {
  doReset();
}

/**
 * Check and apply auto-reset if idle. Returns true if reset was applied.
 * Mirrors backend/claude_spawn_tracker.check_auto_reset() exactly.
 */
export function checkAutoReset(): boolean {
  const before = Boolean(bbRead(_TRIPPED_KEY));
  const cfg = loadConfig();
  maybeAutoReset(cfg);
  const after = Boolean(bbRead(_TRIPPED_KEY));
  return before && !after;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

interface ParsedArgs {
  command: string | null;
  flags: Record<string, string | boolean>;
  positionals: string[];
}

function parseCliArgs(argv: string[]): ParsedArgs {
  const flags: Record<string, string | boolean> = {};
  let command: string | null = null;
  const positionals: string[] = [];
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i]!;
    if (command === null && !arg.startsWith("--")) {
      command = arg;
      i++;
    } else if (arg.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        flags[key] = next;
        i += 2;
      } else {
        flags[key] = true;
        i++;
      }
    } else {
      positionals.push(arg);
      i++;
    }
  }
  return { command, flags, positionals };
}

async function main(argv?: string[]): Promise<number> {
  const rawArgs = argv ?? process.argv.slice(2);
  const { command, flags, positionals } = parseCliArgs(rawArgs);

  if (command === "status") {
    const state = getState();
    const tripped = state.tripped;
    const meta = state.tripped_meta ?? {};
    console.log(`Spawn breaker: ${tripped ? "TRIPPED" : "closed"}`);
    if (tripped && Object.keys(meta).length > 0) {
      console.log(`  Reason:        ${meta["reason"] ?? "unknown"}`);
      const trippedAt = meta["tripped_at"] as string | undefined;
      const lastAttempt = meta["last_attempt_at"] as string | undefined;
      console.log(`  Tripped at:    ${trippedAt ?? "?"} (${ageStr(trippedAt)})`);
      console.log(`  Last attempt:  ${lastAttempt ?? "?"} (${ageStr(lastAttempt)})`);
    }
    console.log(`Spawns  1h:     ${state.spawns_1h}  / ${state.thresholds.spawns_per_hour_max}`);
    console.log(`Spawns 24h:     ${state.spawns_24h}  / ${state.thresholds.spawns_24h_max}`);
    console.log(
      `Spend   1h:     $${state.spend_1h_usd.toFixed(4)}  / $${state.thresholds.spend_per_hour_usd_max.toFixed(2)}`
    );
    console.log(`Spend  24h:     $${state.spend_24h_usd.toFixed(4)}`);
    const perSource = state.per_source;
    if (Object.keys(perSource).length > 0) {
      console.log("Per-source (24h):");
      const sorted = Object.entries(perSource).sort(([, a], [, b]) => b - a);
      for (const [src, count] of sorted) {
        console.log(`  ${src.padEnd(30)} ${count}`);
      }
    }
    return 0;
  }

  if (command === "summary") {
    const state = getState();
    // Python prints JSON regardless of --json flag (both branches do the same)
    console.log(JSON.stringify(state, null, 2));
    return 0;
  }

  if (command === "reset") {
    reset();
    console.log("Spawn breaker reset. Tripped state cleared.");
    return 0;
  }

  if (command === "record") {
    // source is the first positional (after command was consumed)
    const source = positionals[0] ?? (flags["source"] as string | undefined);
    if (!source) {
      process.stderr.write("record: source argument required\n");
      return 1;
    }
    const estTokens = flags["est-tokens"]
      ? parseInt(String(flags["est-tokens"]), 10)
      : 0;
    const estCostUsdRaw = flags["est-cost-usd"];
    const estCostUsd: number | null =
      estCostUsdRaw !== undefined && estCostUsdRaw !== true
        ? parseFloat(String(estCostUsdRaw))
        : null;

    try {
      record(source, estTokens, estCostUsd);
      console.log(`Recorded spawn from source=${JSON.stringify(source)}`);
      return 0;
    } catch (e) {
      if (e instanceof SpawnBlocked) {
        process.stderr.write(`SpawnBlocked: ${e.message}\n`);
        return 1;
      }
      throw e;
    }
  }

  process.stderr.write(
    "claude_spawn_tracker — Global Claude Code spawn budget + circuit breaker.\n\n" +
    "  status                        Human-readable: counts, spend, tripped state\n" +
    "  summary [--json]              Structured summary (JSON)\n" +
    "  reset                         Manual reset: clear tripped state and banner\n" +
    "  record <source> [--est-tokens N] [--est-cost-usd F]\n" +
    "                                Record a spawn\n"
  );
  return 1;
}

// Run as CLI when invoked directly
if (import.meta.main) {
  main().then((code) => process.exit(code));
}
