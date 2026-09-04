/**
 * spawn/budget.ts — Token budget tracker per-session and per-agent.
 *
 * Mirrors backend/budget.py 1:1.
 *
 * Tracks token usage in the blackboard under the `budget/` namespace.
 * Used by the Team Lead to gate agent spawns and log spend after completion.
 *
 * CLI usage:
 *   bun run ts-backend/src/spawn/budget.ts init [--ceiling N]
 *   bun run ts-backend/src/spawn/budget.ts check
 *   bun run ts-backend/src/spawn/budget.ts spend <agent_id> <role> <input_tokens> <output_tokens> [--discussion N]
 *   bun run ts-backend/src/spawn/budget.ts status
 *   bun run ts-backend/src/spawn/budget.ts reset
 *   bun run ts-backend/src/spawn/budget.ts record --input-tokens N --output-tokens N [--role R] [--discussion N] [--model M] [--event-id ID]
 *
 * Programmatic exports:
 *   import { BudgetTracker } from "./budget.js";
 *
 * Store: blackboard under `budget/` namespace (per-key JSON files).
 * Dedup: .autonomous-team/hook-events/seen.sqlite for event_id idempotency.
 */

import {
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { blackboardDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Constants — mirrors budget.py top-level
// ---------------------------------------------------------------------------

const _DEFAULT_BUDGET = {
  session_ceiling: 5_000_000,
  per_agent_ceiling: 500_000,
  warn_threshold_pct: 80,
};

const _KEY_SESSION_CEILING = "budget/session_ceiling";
const _KEY_SESSION_SPENT = "budget/session_spent";
const _KEY_PER_AGENT_CEILING = "budget/per_agent_ceiling";
const _AGENTS_PREFIX = "budget/agents/";

// ---------------------------------------------------------------------------
// Blackboard root path resolution
// Mirrors Blackboard._resolve_default_root() priority:
//  1. AUTONOMOUS_TEAM_STATE_DIR/blackboard
//  2. In-repo .autonomous-team/blackboard (if real dir, not symlink)
//  3. config/state-paths.ts blackboardDir() default
// ---------------------------------------------------------------------------

let _bbRootCache: string | null = null;

function _bbRoot(): string {
  if (_bbRootCache !== null) return _bbRootCache;

  const stateDir = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  if (stateDir) {
    _bbRootCache = join(stateDir, "blackboard");
    return _bbRootCache;
  }

  // Check legacy in-repo dir — skip if it's a symlink (mirrors Python logic)
  const here = new URL(import.meta.url).pathname;
  const repoRoot = join(here, "..", "..", "..", "..");
  const legacy = join(repoRoot, ".autonomous-team", "blackboard");

  if (existsSync(legacy)) {
    try {
      const st = lstatSync(legacy);
      if (!st.isSymbolicLink()) {
        _bbRootCache = legacy;
        return _bbRootCache;
      }
    } catch {
      _bbRootCache = legacy;
      return _bbRootCache;
    }
  }

  _bbRootCache = blackboardDir();
  return _bbRootCache;
}

// ---------------------------------------------------------------------------
// Lightweight Blackboard client
// Implements the subset of Blackboard API used by BudgetTracker.
// ---------------------------------------------------------------------------

interface BlackboardEntry {
  value: unknown;
  version: number;
  updated_at: string;
  updated_by: string;
}

function _nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

function _keyPath(key: string, root: string): string {
  return join(root, ...key.split("/")) + ".json";
}

class Blackboard {
  private _root: string;

  constructor(root?: string) {
    this._root = root ?? _bbRoot();
  }

  read(key: string): unknown {
    const path = _keyPath(key, this._root);
    if (!existsSync(path)) return null;
    try {
      const data = JSON.parse(readFileSync(path, "utf-8")) as BlackboardEntry;
      return data.value ?? null;
    } catch {
      return null;
    }
  }

  readEntry(key: string): BlackboardEntry | null {
    const path = _keyPath(key, this._root);
    if (!existsSync(path)) return null;
    try {
      return JSON.parse(readFileSync(path, "utf-8")) as BlackboardEntry;
    } catch {
      return null;
    }
  }

  write(key: string, value: unknown, updated_by = "unknown"): boolean {
    const path = _keyPath(key, this._root);
    mkdirSync(dirname(path), { recursive: true });

    let version = 1;
    if (existsSync(path)) {
      try {
        const current = JSON.parse(readFileSync(path, "utf-8")) as BlackboardEntry;
        version = (current.version ?? 0) + 1;
      } catch { /* ignore */ }
    }

    const entry: BlackboardEntry = {
      value,
      version,
      updated_at: _nowIso(),
      updated_by,
    };

    const tmp = path + ".tmp";
    try {
      writeFileSync(tmp, JSON.stringify(entry, null, 2) + "\n", "utf-8");
      renameSync(tmp, path);
    } catch {
      try { unlinkSync(tmp); } catch { /* ignore */ }
      return false;
    }
    return true;
  }

  cas(key: string, value: unknown, expected_version: number, updated_by = "unknown"): boolean {
    const path = _keyPath(key, this._root);
    if (!existsSync(path)) return false;

    let current: BlackboardEntry;
    try {
      current = JSON.parse(readFileSync(path, "utf-8")) as BlackboardEntry;
    } catch {
      return false;
    }

    if (current.version !== expected_version) return false;

    const entry: BlackboardEntry = {
      value,
      version: expected_version + 1,
      updated_at: _nowIso(),
      updated_by,
    };

    const tmp = path + ".tmp";
    try {
      writeFileSync(tmp, JSON.stringify(entry, null, 2) + "\n", "utf-8");
      renameSync(tmp, path);
    } catch {
      try { unlinkSync(tmp); } catch { /* ignore */ }
      return false;
    }
    return true;
  }

  listKeys(prefix = ""): string[] {
    if (!existsSync(this._root)) return [];

    const keys: string[] = [];
    const lockDir = join(this._root, ".locks");

    const walk = (dir: string): void => {
      try {
        for (const entry of readdirSync(dir, { withFileTypes: true })) {
          const fullPath = join(dir, entry.name);
          if (entry.isDirectory()) {
            // Skip the .locks directory
            if (fullPath === lockDir || fullPath.startsWith(lockDir + sep)) continue;
            walk(fullPath);
          } else if (
            entry.isFile() &&
            entry.name.endsWith(".json") &&
            !entry.name.endsWith(".tmp")
          ) {
            const rel = relative(this._root, fullPath);
            // Strip .json suffix and normalize separators to /
            const key = rel.replace(/\.json$/, "").split(sep).join("/");
            if (!prefix || key.startsWith(prefix)) {
              keys.push(key);
            }
          }
        }
      } catch { /* ignore unreadable dirs */ }
    };

    walk(this._root);
    return keys.sort();
  }

  delete(key: string): boolean {
    const path = _keyPath(key, this._root);
    if (!existsSync(path)) return false;
    try {
      unlinkSync(path);
      // Prune empty parent directories up to root (mirrors Python _prune_empty_dirs)
      let dir = dirname(path);
      while (dir !== this._root && dir.startsWith(this._root)) {
        try {
          const entries = readdirSync(dir);
          if (entries.length === 0) {
            try {
              rmdirSync(dir);
            } catch { /* ignore */ }
          } else {
            break;
          }
        } catch {
          break;
        }
        dir = dirname(dir);
      }
      return true;
    } catch {
      return false;
    }
  }
}

// ---------------------------------------------------------------------------
// Dedup helper — seen.sqlite via Bun's built-in SQLite
// Mirrors budget._check_seen()
// ---------------------------------------------------------------------------

function _resolveSeenDbPath(): string {
  const here = new URL(import.meta.url).pathname;
  const repoRoot = join(here, "..", "..", "..", "..");
  return join(repoRoot, ".autonomous-team", "hook-events", "seen.sqlite");
}

/**
 * Return true if event_id has already been recorded (i.e. it is a duplicate).
 * Return false if it is new (and register it atomically).
 * Prunes records older than 7 days on every call to bound table size.
 * Mirrors budget._check_seen().
 */
function _checkSeen(eventId: string, hook: string): boolean {
  if (!eventId) return false;
  const dbPath = _resolveSeenDbPath();
  mkdirSync(dirname(dbPath), { recursive: true });

  try {
    // Bun ships with bun:sqlite built-in. Loaded via require() (not a static
    // import) so a missing/broken sqlite binding only fails this function's
    // try/catch instead of crashing the whole module at import time.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { Database } = require("bun:sqlite") as typeof import("bun:sqlite");
    const db = new Database(dbPath, { create: true });
    db.run(
      "CREATE TABLE IF NOT EXISTS seen_events " +
      "(event_id TEXT PRIMARY KEY, hook TEXT, ts TEXT)"
    );
    db.run("DELETE FROM seen_events WHERE ts < datetime('now','-7 days')");
    const stmt = db.prepare(
      "INSERT OR IGNORE INTO seen_events VALUES (?,?,datetime('now'))"
    );
    const result = stmt.run(eventId, hook) as { changes: number };
    db.close();
    return result.changes === 0; // 0 changes = INSERT OR IGNORE was a no-op = already seen
  } catch {
    // If sqlite fails, don't block budget recording — treat as unseen
    return false;
  }
}

// ---------------------------------------------------------------------------
// Config loading — mirrors budget._load_config()
// ---------------------------------------------------------------------------

interface BudgetConfig {
  session_ceiling: number;
  per_agent_ceiling: number;
  warn_threshold_pct: number;
}

function _loadConfig(configPathOverride?: string): BudgetConfig {
  let configPath = configPathOverride;
  if (!configPath) {
    const here = new URL(import.meta.url).pathname;
    const repoRoot = join(here, "..", "..", "..", "..");
    configPath = join(repoRoot, ".autonomous-team", "config.json");
  }

  try {
    const raw = readFileSync(configPath, "utf-8");
    const cfg = JSON.parse(raw) as Record<string, unknown>;
    const budgetCfg = (cfg["budget"] ?? {}) as Record<string, unknown>;
    return {
      session_ceiling:
        typeof budgetCfg["session_ceiling"] === "number"
          ? budgetCfg["session_ceiling"]
          : _DEFAULT_BUDGET.session_ceiling,
      per_agent_ceiling:
        typeof budgetCfg["per_agent_ceiling"] === "number"
          ? budgetCfg["per_agent_ceiling"]
          : _DEFAULT_BUDGET.per_agent_ceiling,
      warn_threshold_pct:
        typeof budgetCfg["warn_threshold_pct"] === "number"
          ? budgetCfg["warn_threshold_pct"]
          : _DEFAULT_BUDGET.warn_threshold_pct,
    };
  } catch {
    return { ..._DEFAULT_BUDGET };
  }
}

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface AgentSpendRecord {
  agent: string;
  agent_id: string;
  input: number;
  output: number;
  total: number;
  model: string;
  finished: string;
  discussion?: number;
  pr?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

export interface BudgetStatus {
  ceiling: number;
  spent: number;
  remaining: number;
  per_agent_ceiling: number;
  warn_threshold_pct: number;
  agents: AgentSpendRecord[];
}

export interface CheckResult {
  allowed: boolean;
  remaining: number;
  ceiling: number;
  spent: number;
  warn: boolean;
  agent_role: string;
}

// ---------------------------------------------------------------------------
// BudgetTracker — mirrors class BudgetTracker in budget.py
// ---------------------------------------------------------------------------

export class BudgetTracker {
  private _bb: Blackboard;
  private _config: BudgetConfig;

  constructor(bbRoot?: string) {
    this._bb = new Blackboard(bbRoot);
    this._config = _loadConfig();
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /**
   * Initialize budget blackboard keys for a new session.
   * Mirrors BudgetTracker.init_session().
   */
  initSession(ceiling?: number): void {
    const effectiveCeiling = ceiling ?? this._config.session_ceiling;
    const perAgentCeiling = this._config.per_agent_ceiling;
    this._bb.write(_KEY_SESSION_CEILING, effectiveCeiling, "budget-tracker");
    this._bb.write(_KEY_SESSION_SPENT, 0, "budget-tracker");
    this._bb.write(_KEY_PER_AGENT_CEILING, perAgentCeiling, "budget-tracker");
  }

  /**
   * Check whether there is enough budget remaining to spawn an agent.
   * Mirrors BudgetTracker.check_budget().
   */
  checkBudget(agentRole: string): CheckResult {
    const ceiling = this._readInt(_KEY_SESSION_CEILING, this._config.session_ceiling);
    const perAgentCeiling = this._readInt(
      _KEY_PER_AGENT_CEILING,
      this._config.per_agent_ceiling
    );
    const warnPct = this._config.warn_threshold_pct;

    // Derive spent from agents[] — mirrors Python comment about CAS races
    const spent = this._sumAgentTokens();
    const remaining = Math.max(0, ceiling - spent);
    const allowed = remaining >= perAgentCeiling;
    const warn = spent > (warnPct / 100) * ceiling;

    return { allowed, remaining, ceiling, spent, warn, agent_role: agentRole };
  }

  /**
   * Atomically increment session_spent and write the per-agent spend record.
   * Mirrors BudgetTracker.record_spend().
   */
  recordSpend(params: {
    agentId: string;
    agentRole: string;
    inputTokens: number;
    outputTokens: number;
    discussion?: number | null;
    model?: string;
    eventId?: string | null;
    pr?: number | null;
    cacheReadTokens?: number;
    cacheWriteTokens?: number;
  }): void {
    const {
      agentId,
      agentRole,
      inputTokens,
      outputTokens,
      discussion,
      model = "default",
      eventId,
      pr,
      cacheReadTokens = 0,
      cacheWriteTokens = 0,
    } = params;

    // Idempotency check — skip if this event_id has already been recorded
    if (eventId && _checkSeen(eventId, "budget")) return;

    const total = inputTokens + outputTokens;

    // Write per-agent record FIRST so _sum_agent_tokens() is always accurate
    // (mirrors Python comment about CAS failure leaving spent=0)
    const record: Record<string, unknown> = {
      agent: agentRole,
      agent_id: agentId,
      input: inputTokens,
      output: outputTokens,
      total,
      model,
      finished: _nowIso(),
    };
    if (discussion !== null && discussion !== undefined) record["discussion"] = discussion;
    if (pr !== null && pr !== undefined) record["pr"] = pr;
    if (cacheReadTokens) record["cache_read_tokens"] = cacheReadTokens;
    if (cacheWriteTokens) record["cache_write_tokens"] = cacheWriteTokens;

    const agentKey = `${_AGENTS_PREFIX}${agentId}`;
    this._bb.write(agentKey, record, "budget-tracker");

    // Increment session_spent — best-effort; agents[] is always accurate
    try {
      this._casIncrement(_KEY_SESSION_SPENT, total);
    } catch {
      // agents[] already written above; spent sum remains correct
    }
  }

  /**
   * Return current budget state as a dict.
   * Mirrors BudgetTracker.get_status().
   */
  getStatus(): BudgetStatus {
    const ceiling = this._readInt(_KEY_SESSION_CEILING, this._config.session_ceiling);
    const perAgentCeiling = this._readInt(
      _KEY_PER_AGENT_CEILING,
      this._config.per_agent_ceiling
    );

    const agentKeys = this._bb.listKeys(_AGENTS_PREFIX);
    const agents: AgentSpendRecord[] = [];
    for (const key of agentKeys) {
      const val = this._bb.read(key);
      if (val !== null && val !== undefined) {
        agents.push(val as AgentSpendRecord);
      }
    }

    // Derive spent by summing tokens across agent entries (mirrors Python comment)
    const spent = agents.reduce((sum, a) => {
      const inp = typeof a.input === "number" ? a.input : 0;
      const out = typeof a.output === "number" ? a.output : 0;
      return sum + inp + out;
    }, 0);
    const remaining = Math.max(0, ceiling - spent);

    return {
      ceiling,
      spent,
      remaining,
      per_agent_ceiling: perAgentCeiling,
      warn_threshold_pct: this._config.warn_threshold_pct,
      agents,
    };
  }

  /**
   * Delete all budget/ keys from the blackboard.
   * Mirrors BudgetTracker.reset().
   */
  reset(): void {
    const keys = this._bb.listKeys("budget/");
    for (const key of keys) {
      this._bb.delete(key);
    }
  }

  // -------------------------------------------------------------------------
  // Internal helpers
  // -------------------------------------------------------------------------

  private _sumAgentTokens(): number {
    const agentKeys = this._bb.listKeys(_AGENTS_PREFIX);
    let total = 0;
    for (const key of agentKeys) {
      const val = this._bb.read(key) as Record<string, unknown> | null;
      if (val !== null && val !== undefined) {
        const inp = typeof val["input"] === "number" ? val["input"] : 0;
        const out = typeof val["output"] === "number" ? val["output"] : 0;
        total += inp + out;
      }
    }
    return total;
  }

  private _readInt(key: string, defaultVal: number): number {
    const val = this._bb.read(key);
    if (typeof val === "number" && Number.isInteger(val)) return val;
    return defaultVal;
  }

  /**
   * Atomically increment the int at key by delta.
   * Mirrors BudgetTracker._cas_increment() with max_retries=1.
   */
  private _casIncrement(key: string, delta: number, maxRetries = 1): void {
    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      const entry = this._bb.readEntry(key);
      if (entry === null) {
        // Key doesn't exist — write initial value
        this._bb.write(key, delta, "budget-tracker");
        return;
      }

      const currentValue = typeof entry.value === "number" ? entry.value : 0;
      const newValue = currentValue + delta;
      const currentVersion = entry.version ?? 1;

      const ok = this._bb.cas(key, newValue, currentVersion, "budget-tracker");
      if (ok) return;

      if (attempt < maxRetries) {
        // Brief spin — mirrors Python time.sleep(0.05)
        const start = Date.now();
        while (Date.now() - start < 50) { /* spin */ }
      }
    }

    throw new Error(
      `CAS conflict on '${key}' after ${maxRetries + 1} attempts — could not increment`
    );
  }
}

// ---------------------------------------------------------------------------
// CLI arg parser
// ---------------------------------------------------------------------------

interface ParsedArgs {
  command: string | null;
  flags: Record<string, string | boolean>;
  positional: string[];
}

function _parseArgs(argv: string[]): ParsedArgs {
  const flags: Record<string, string | boolean> = {};
  let command: string | null = null;
  const positional: string[] = [];
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i];
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
      positional.push(arg);
      i++;
    }
  }
  return { command, flags, positional };
}

function _toInt(v: string | boolean | undefined): number | null {
  if (v === undefined || typeof v === "boolean") return null;
  const n = parseInt(String(v), 10);
  return isNaN(n) ? null : n;
}

function _toStr(v: string | boolean | undefined): string | null {
  if (v === undefined || typeof v === "boolean") return null;
  return String(v);
}

// ---------------------------------------------------------------------------
// CLI entry point — mirrors budget.main()
// ---------------------------------------------------------------------------

export function main(argv?: string[]): number {
  const { command, flags, positional } = _parseArgs(argv ?? process.argv.slice(2));
  const bt = new BudgetTracker();

  if (command === "init") {
    const ceiling = _toInt(flags["ceiling"]);
    bt.initSession(ceiling ?? undefined);
    const status = bt.getStatus();
    process.stdout.write(
      `initialized: ceiling=${status.ceiling}, spent=0, per_agent_ceiling=${status.per_agent_ceiling}\n`
    );
    return 0;
  }

  if (command === "check") {
    const result = bt.checkBudget("cli");
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
    if (result.warn) {
      const pct = Math.floor((result.spent * 100) / Math.max(result.ceiling, 1));
      process.stderr.write(
        `WARNING: spent ${result.spent.toLocaleString()} of ${result.ceiling.toLocaleString()} tokens (${pct}%)\n`
      );
    }
    if (!result.allowed) {
      process.stderr.write(
        `BUDGET EXCEEDED: remaining ${result.remaining.toLocaleString()} < per_agent_ceiling\n`
      );
      return 1;
    }
    return 0;
  }

  if (command === "spend") {
    // positional args: agent_id role input_tokens output_tokens
    const agentId = positional[0] ?? _toStr(flags["agent-id"]);
    const role = positional[1] ?? _toStr(flags["role"]);
    const inpRaw = positional[2] !== undefined ? positional[2] : flags["input-tokens"];
    const outRaw = positional[3] !== undefined ? positional[3] : flags["output-tokens"];
    const inputTokens = _toInt(inpRaw as string | boolean | undefined);
    const outputTokens = _toInt(outRaw as string | boolean | undefined);

    if (!agentId || !role || inputTokens === null || outputTokens === null) {
      process.stderr.write("spend: required: agent_id role input_tokens output_tokens\n");
      return 1;
    }

    bt.recordSpend({
      agentId,
      agentRole: role,
      inputTokens,
      outputTokens,
      discussion: _toInt(flags["discussion"]),
      model: _toStr(flags["model"]) ?? "default",
      eventId: _toStr(flags["event-id"]),
      pr: _toInt(flags["pr"]),
    });

    const total = inputTokens + outputTokens;
    process.stdout.write(
      `recorded: ${agentId} spent ${total.toLocaleString()} tokens (${inputTokens.toLocaleString()} in + ${outputTokens.toLocaleString()} out)\n`
    );
    return 0;
  }

  if (command === "record") {
    const inputTokens = _toInt(flags["input-tokens"]) ?? 0;
    const outputTokens = _toInt(flags["output-tokens"]) ?? 0;
    const role = _toStr(flags["role"]) ?? "unknown";
    const eventId = _toStr(flags["event-id"]);
    const model = _toStr(flags["model"]) ?? "default";
    const discussion = _toInt(flags["discussion"]);

    // Pre-check dedup (mirrors Python record command logic)
    if (eventId) {
      const wasSeen = _checkSeen(eventId, "budget");
      if (wasSeen) {
        process.stdout.write(`skipped (duplicate event_id=${eventId})\n`);
        return 0;
      }
    }

    const agentId = `record-${role}-${Math.floor(Date.now() / 1000)}`;
    bt.recordSpend({
      agentId,
      agentRole: role,
      inputTokens,
      outputTokens,
      discussion,
      model,
      // Don't pass eventId here — dedup check already done above (mirrors Python comment)
    });

    const total = inputTokens + outputTokens;
    process.stdout.write(
      `recorded: ${agentId} spent ${total.toLocaleString()} tokens (${inputTokens.toLocaleString()} in + ${outputTokens.toLocaleString()} out)\n`
    );
    return 0;
  }

  if (command === "status") {
    const status = bt.getStatus();
    process.stdout.write(JSON.stringify(status, null, 2) + "\n");
    return 0;
  }

  if (command === "reset") {
    bt.reset();
    process.stdout.write("reset: all budget/ keys removed\n");
    return 0;
  }

  process.stderr.write(
    "usage: budget.ts <init|check|spend|record|status|reset> ...\n"
  );
  return 1;
}

// Run as CLI when this is the main module
if (import.meta.main) {
  process.exit(main());
}
