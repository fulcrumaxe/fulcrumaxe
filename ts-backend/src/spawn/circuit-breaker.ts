/**
 * spawn/circuit-breaker.ts — Agent failure circuit breaker.
 *
 * Mirrors backend/circuit_breaker.py 1:1.
 *
 * Tracks consecutive agent failures per Discussion in the blackboard under the
 * `failures/` namespace. After 3 consecutive failures the circuit opens and
 * the Team Lead skips spawning agents for that Discussion until manually reset.
 *
 * State transitions are persisted to `.autonomous-team/circuit-breaker-history.jsonl`
 * so operators can see the timeline of trips and resets over time.
 *
 * CLI usage:
 *   bun run ts-backend/src/spawn/circuit-breaker.ts status [discussion_number]
 *   bun run ts-backend/src/spawn/circuit-breaker.ts reset <discussion_number>
 *   bun run ts-backend/src/spawn/circuit-breaker.ts record <discussion_number> <agent> <reason>
 *   bun run ts-backend/src/spawn/circuit-breaker.ts list
 *   bun run ts-backend/src/spawn/circuit-breaker.ts summary [--json]
 *   bun run ts-backend/src/spawn/circuit-breaker.ts history --role <role> [--limit 20]
 *   bun run ts-backend/src/spawn/circuit-breaker.ts expire [--dry-run]
 *
 * Programmatic exports:
 *   import { recordFailure, recordSuccess, isBlocked } from "./circuit-breaker.js";
 *
 * Store: blackboard under `failures/` and `failures_meta/` namespaces.
 */

import {
  appendFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { spawnSync } from "node:child_process";
import { repoName, repoOwner } from "../config/repo.js";
import { blackboardDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const DEFAULT_THRESHOLD = 3;

/**
 * Number of days after which a tripped breaker is considered age-stale
 * and eligible for auto-expiry.
 */
export const STALE_BREAKER_DAYS = 7;

// ---------------------------------------------------------------------------
// Blackboard root path resolution (mirrors budget.ts)
// ---------------------------------------------------------------------------

let _bbRootCache: string | null = null;

function _bbRoot(): string {
  if (_bbRootCache !== null) return _bbRootCache;

  const stateDir = process.env["AUTONOMOUS_TEAM_STATE_DIR"];
  if (stateDir) {
    _bbRootCache = join(stateDir, "blackboard");
    return _bbRootCache;
  }

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
// History file path
// ---------------------------------------------------------------------------

function _historyFilePath(): string {
  const here = new URL(import.meta.url).pathname;
  const repoRoot = join(here, "..", "..", "..", "..");
  return join(repoRoot, ".autonomous-team", "circuit-breaker-history.jsonl");
}

// ---------------------------------------------------------------------------
// Lightweight Blackboard client (same as budget.ts)
// ---------------------------------------------------------------------------

interface BlackboardEntry {
  value: unknown;
  version: number;
  updated_at: string;
  updated_by: string;
}

function _nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function _keyFilePath(key: string, root: string): string {
  return join(root, ...key.split("/")) + ".json";
}

class Blackboard {
  private _root: string;

  constructor(root?: string) {
    this._root = root ?? _bbRoot();
  }

  read(key: string): unknown {
    const path = _keyFilePath(key, this._root);
    if (!existsSync(path)) return null;
    try {
      const data = JSON.parse(readFileSync(path, "utf-8")) as BlackboardEntry;
      return data.value ?? null;
    } catch {
      return null;
    }
  }

  write(key: string, value: unknown, updated_by = "unknown"): boolean {
    const path = _keyFilePath(key, this._root);
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

  listKeys(prefix = ""): string[] {
    if (!existsSync(this._root)) return [];

    const keys: string[] = [];
    const lockDir = join(this._root, ".locks");

    const walk = (dir: string): void => {
      try {
        for (const entry of readdirSync(dir, { withFileTypes: true })) {
          const fullPath = join(dir, entry.name);
          if (entry.isDirectory()) {
            if (fullPath === lockDir || fullPath.startsWith(lockDir + sep)) continue;
            walk(fullPath);
          } else if (
            entry.isFile() &&
            entry.name.endsWith(".json") &&
            !entry.name.endsWith(".tmp")
          ) {
            const rel = relative(this._root, fullPath);
            const key = rel.replace(/\.json$/, "").split(sep).join("/");
            if (!prefix || key.startsWith(prefix)) {
              keys.push(key);
            }
          }
        }
      } catch { /* ignore */ }
    };

    walk(this._root);
    return keys.sort();
  }

  delete(key: string): boolean {
    const path = _keyFilePath(key, this._root);
    if (!existsSync(path)) return false;
    try {
      unlinkSync(path);
      return true;
    } catch {
      return false;
    }
  }
}

// Blackboard accessor — re-evaluates env on each call so tests can override AUTONOMOUS_TEAM_STATE_DIR.
// Mirrors Python module-level _bb = Blackboard() but with lazy env resolution.
function _getBb(): Blackboard {
  // Clear root cache so env overrides take effect (test isolation)
  _bbRootCache = null;
  return new Blackboard();
}

// Convenience alias used by all public API functions
const _bb = {
  read: (key: string) => _getBb().read(key),
  write: (key: string, value: unknown, updated_by?: string) => _getBb().write(key, value, updated_by),
  listKeys: (prefix?: string) => _getBb().listKeys(prefix),
  delete: (key: string) => _getBb().delete(key),
};

// ---------------------------------------------------------------------------
// Key helpers
// ---------------------------------------------------------------------------

function _key(discussion: number): string {
  return `failures/${discussion}`;
}

function _metaKey(discussion: number): string {
  return `failures_meta/${discussion}`;
}

// ---------------------------------------------------------------------------
// History log
// Mirrors _append_history() — atomic append via appendFileSync (O_APPEND on Linux)
// ---------------------------------------------------------------------------

interface HistoryLine {
  role: string;
  from_state: string;
  to_state: string;
  timestamp: string;
  reason: string;
  context: Record<string, unknown>;
  last_pr: number | null;
}

function _appendHistory(
  role: string,
  from_state: string,
  to_state: string,
  reason: string,
  context: Record<string, unknown>,
  last_pr: number | null = null
): void {
  const line: HistoryLine = {
    role,
    from_state,
    to_state,
    timestamp: _nowIso(),
    reason,
    context,
    last_pr,
  };
  const filePath = _historyFilePath();
  mkdirSync(dirname(filePath), { recursive: true });
  try {
    appendFileSync(filePath, JSON.stringify(line) + "\n", "utf-8");
  } catch { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// Public API — mirrors module-level functions in circuit_breaker.py
// ---------------------------------------------------------------------------

/**
 * Return recent history transitions for role (newest last, up to limit).
 * Mirrors circuit_breaker.history().
 */
export function history(role: string, limit = 20): HistoryLine[] {
  const filePath = _historyFilePath();
  if (!existsSync(filePath)) return [];

  const matches: HistoryLine[] = [];
  try {
    const lines = readFileSync(filePath, "utf-8").split("\n");
    for (const raw of lines) {
      const trimmed = raw.trim();
      if (!trimmed) continue;
      try {
        const entry = JSON.parse(trimmed) as HistoryLine;
        if (entry.role === role) matches.push(entry);
      } catch { /* skip malformed */ }
    }
  } catch { /* file unreadable */ }

  return matches.slice(-limit);
}

/**
 * Increment the consecutive failure counter for discussion.
 * Returns the new failure count.
 * Mirrors circuit_breaker.record_failure().
 */
export function recordFailure(
  discussion: number,
  agent: string,
  reason: string,
  last_pr: number | null = null
): number {
  const key = _key(discussion);
  const current = ((_bb.read(key) as number) || 0);
  const newCount = current + 1;
  _bb.write(key, newCount, `circuit-breaker/${agent}`);

  const meta = {
    count: newCount,
    agent,
    reason,
    updated_at: _nowIso(),
  };
  _bb.write(_metaKey(discussion), meta, `circuit-breaker/${agent}`);

  // Emit history transition on threshold crossing (healthy → tripped)
  if (current < DEFAULT_THRESHOLD && newCount >= DEFAULT_THRESHOLD) {
    _appendHistory(
      agent,
      "healthy",
      "tripped",
      reason,
      { recent_errors: [reason], trip_count_24h: newCount },
      last_pr
    );
  }

  return newCount;
}

/**
 * Reset the failure counter for discussion (delete both keys).
 * Emits history only if the circuit was previously tripped.
 * Mirrors circuit_breaker.record_success().
 */
export function recordSuccess(
  discussion: number,
  agent = "unknown",
  last_pr: number | null = null
): void {
  const wasTripped = isBlocked(discussion);
  _bb.delete(_key(discussion));
  _bb.delete(_metaKey(discussion));
  if (wasTripped) {
    _appendHistory(
      agent,
      "tripped",
      "healthy",
      "reset after success",
      { recent_errors: [] },
      last_pr
    );
  }
}

/**
 * Return true if the failure count for discussion >= threshold.
 * Mirrors circuit_breaker.is_blocked().
 */
export function isBlocked(discussion: number, threshold = DEFAULT_THRESHOLD): boolean {
  const count = ((_bb.read(_key(discussion)) as number) || 0);
  return count >= threshold;
}

/**
 * Return the most recent failure record for discussion, or null if none.
 * Mirrors circuit_breaker.get_latest_failure().
 */
export function getLatestFailure(discussion: number): {
  count: number;
  agent: string | null;
  reason: string | null;
  updated_at: string | null;
} | null {
  const count = ((_bb.read(_key(discussion)) as number) || 0);
  if (count === 0) return null;

  const meta = _bb.read(_metaKey(discussion)) as Record<string, unknown> | null;
  if (!meta || typeof meta !== "object") {
    return { count, agent: null, reason: null, updated_at: null };
  }
  return {
    count: typeof meta["count"] === "number" ? meta["count"] : count,
    agent: (meta["agent"] as string | null) ?? null,
    reason: (meta["reason"] as string | null) ?? null,
    updated_at: (meta["updated_at"] as string | null) ?? null,
  };
}

// ---------------------------------------------------------------------------
// Internal: collect all tripped entries
// ---------------------------------------------------------------------------

interface TrippedEntry {
  discussion: number;
  count: number;
  agent: string | null;
  reason: string | null;
  updated_at: string | null;
  blocked: boolean;
}

function _collectTripped(threshold = DEFAULT_THRESHOLD): TrippedEntry[] {
  const keys = _bb.listKeys("failures/");
  const result: TrippedEntry[] = [];

  for (const key of keys) {
    const count = ((_bb.read(key) as number) || 0);
    const discStr = key.split("/").slice(1).join("/");
    const discNum = parseInt(discStr, 10);
    if (isNaN(discNum)) continue;

    const meta = (_bb.read(_metaKey(discNum)) || {}) as Record<string, unknown>;
    const agent = typeof meta["agent"] === "string" ? meta["agent"] : null;
    const reason = typeof meta["reason"] === "string" ? meta["reason"] : null;
    const updated_at = typeof meta["updated_at"] === "string" ? meta["updated_at"] : null;

    result.push({
      discussion: discNum,
      count,
      agent,
      reason,
      updated_at,
      blocked: count >= threshold,
    });
  }
  return result;
}

// ---------------------------------------------------------------------------
// Internal: human-readable age string
// Mirrors circuit_breaker._age_str()
// ---------------------------------------------------------------------------

function _ageStr(updated_at: string | null): string {
  if (!updated_at) return "";
  try {
    const ts = new Date(updated_at.replace("Z", "+00:00"));
    if (isNaN(ts.getTime())) return "";
    const secs = Math.floor((Date.now() - ts.getTime()) / 1000);
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    return `${Math.floor(secs / 3600)}h ago`;
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Discussion state lookup (for auto-expire)
// Mirrors circuit_breaker._discussion_state()
// ---------------------------------------------------------------------------

type DiscussionState = "open" | "closed" | "absent" | "unknown";

function _discussionState(discussion: number): DiscussionState {
  const query = `
    query($owner:String!, $repo:String!, $num:Int!) {
      repository(owner:$owner, name:$repo) {
        discussion(number:$num) { closed }
      }
    }
  `;

  try {
    const result = spawnSync(
      "gh",
      [
        "api", "graphql",
        "-f", `query=${query}`,
        "-f", `owner=${repoOwner()}`,
        "-f", `repo=${repoName()}`,
        "-F", `num=${discussion}`,
      ],
      { encoding: "utf-8", timeout: 10_000 }
    );

    const stdout = (result.stdout ?? "").trim();
    if (!stdout) return "unknown";

    let data: Record<string, unknown>;
    try {
      data = JSON.parse(stdout) as Record<string, unknown>;
    } catch {
      return "unknown";
    }

    const errors = (data["errors"] as unknown[] | undefined) ?? [];
    const notFoundErrors = errors.filter((e) => {
      const err = e as Record<string, unknown>;
      return (
        String(err["type"] ?? "").includes("NOT_FOUND") ||
        String(err["message"] ?? "").includes("Could not resolve")
      );
    });
    const otherErrors = errors.filter((e) => !notFoundErrors.includes(e));

    if (notFoundErrors.length > 0) return "absent";
    if (otherErrors.length > 0) return "unknown";

    const disc = ((data["data"] as Record<string, unknown> | undefined)
      ?.["repository"] as Record<string, unknown> | undefined)
      ?.["discussion"] as Record<string, unknown> | null | undefined;

    if (!disc) return "absent";
    return disc["closed"] ? "closed" : "open";
  } catch {
    return "unknown";
  }
}

// ---------------------------------------------------------------------------
// Auto-expiry
// Mirrors circuit_breaker.expire_stale()
// ---------------------------------------------------------------------------

export function expireStale(opts: {
  now?: Date;
  dryRun?: boolean;
} = {}): { discussion: number; reason: string }[] {
  const now = opts.now ?? new Date();
  const dryRun = opts.dryRun ?? false;

  const cutoffMs = now.getTime() - STALE_BREAKER_DAYS * 24 * 60 * 60 * 1000;
  const expired: { discussion: number; reason: string }[] = [];

  const stateCache: Map<number, DiscussionState> = new Map();

  for (const entry of _collectTripped()) {
    if (!entry.blocked) continue;

    const disc = entry.discussion;
    const updatedAtRaw = entry.updated_at;

    // Age check — mirrors Python fail-safe: missing/unparseable ts is NOT age-eligible
    let ageEligible = false;
    if (updatedAtRaw !== null) {
      try {
        const ts = new Date(updatedAtRaw.replace("Z", "+00:00"));
        if (!isNaN(ts.getTime())) {
          ageEligible = ts.getTime() < cutoffMs;
        }
      } catch { /* unparseable → not age-eligible */ }
    }

    // Discussion state check
    if (!stateCache.has(disc)) {
      stateCache.set(disc, _discussionState(disc));
    }
    const state = stateCache.get(disc)!;

    // Determine expiry reason (matches Python logic exactly)
    let reason: string;
    if (state === "closed") {
      reason = "closed";
    } else if (state === "absent") {
      reason = "absent";
    } else if (ageEligible) {
      reason = "age";
    } else {
      // Hold — not age-eligible + open/unknown
      continue;
    }

    expired.push({ discussion: disc, reason });

    if (!dryRun) {
      _bb.delete(_key(disc));
      _bb.delete(_metaKey(disc));
      _appendHistory(
        "circuit-breaker/auto-expire",
        "tripped",
        "expired",
        `auto-expired: ${reason}`,
        { expiry_reason: reason, updated_at: updatedAtRaw }
      );
    }
  }

  return expired;
}

// ---------------------------------------------------------------------------
// CLI helpers
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
// CLI entry point — mirrors circuit_breaker.main()
// ---------------------------------------------------------------------------

export function main(argv?: string[]): number {
  const { command, flags, positional } = _parseArgs(argv ?? process.argv.slice(2));

  if (command === "status") {
    const disc = positional[0] !== undefined ? parseInt(positional[0], 10) : null;

    if (disc !== null && !isNaN(disc)) {
      // Single-counter mode
      const count = ((_bb.read(_key(disc)) as number) || 0);
      const blocked = count >= DEFAULT_THRESHOLD;
      process.stdout.write(String(count) + "\n");
      if (blocked) {
        process.stderr.write(
          `circuit open: Discussion #${disc} has ${count} consecutive failures\n`
        );
      }
      return 0;
    }

    // No-arg mode: list all active counters
    const entries = _collectTripped();
    if (entries.length === 0) {
      process.stdout.write("no active failure counters\n");
      return 0;
    }
    for (const e of entries) {
      const age = _ageStr(e.updated_at);
      const agePart = age ? ` (${age})` : "";
      const agentPart =
        e.agent ? `${e.agent}: ${e.reason}` : "unknown";
      const blockedPart = e.blocked ? " [BLOCKED]" : "";
      process.stdout.write(
        `#${e.discussion}: ${e.count} failures — ${agentPart}${agePart}${blockedPart}\n`
      );
    }
    return 0;
  }

  if (command === "reset") {
    const disc = positional[0] !== undefined ? parseInt(positional[0], 10) : null;
    if (disc === null || isNaN(disc)) {
      process.stderr.write("reset: discussion number required\n");
      return 1;
    }
    const removed = _bb.delete(_key(disc));
    _bb.delete(_metaKey(disc));
    if (removed) {
      process.stdout.write(`reset: Discussion #${disc} failure counter cleared\n`);
    } else {
      process.stdout.write(`ok: Discussion #${disc} had no active failure counter\n`);
    }
    return 0;
  }

  if (command === "record") {
    // positional: discussion agent reason
    const disc = positional[0] !== undefined ? parseInt(positional[0], 10) : null;
    const agent = positional[1] ?? null;
    const reason = positional[2] ?? null;

    if (disc === null || isNaN(disc) || !agent || !reason) {
      process.stderr.write("record: required: discussion agent reason\n");
      return 1;
    }

    const newCount = recordFailure(disc, agent, reason);
    process.stdout.write(`failures/${disc} = ${newCount}\n`);
    if (newCount >= DEFAULT_THRESHOLD) {
      process.stderr.write(
        `circuit open: Discussion #${disc} now has ${newCount} consecutive failures\n`
      );
    }
    return 0;
  }

  if (command === "list") {
    const keys = _bb.listKeys("failures/");
    if (keys.length === 0) {
      process.stdout.write("no active failure counters\n");
      return 0;
    }
    for (const key of keys) {
      const count = ((_bb.read(key) as number) || 0);
      const discNum = key.split("/").slice(1).join("/");
      const blockedPart = count >= DEFAULT_THRESHOLD ? " [BLOCKED]" : "";
      process.stdout.write(
        `Discussion #${discNum}: ${count} consecutive failure(s)${blockedPart}\n`
      );
    }
    return 0;
  }

  if (command === "summary") {
    const _jsonOutput = flags["json"] === true || flags["json-output"] === true;
    void _jsonOutput; // mirrors Python --json flag; reserved for future use
    const tripped = _collectTripped();
    const trippedList = tripped.filter((e) => e.blocked);
    const warningsList = tripped.filter((e) => !e.blocked && e.count > 0);

    const output = {
      tripped: trippedList.map((e) => ({
        discussion: e.discussion,
        count: e.count,
        agent: e.agent,
        reason: e.reason,
        updated_at: e.updated_at,
      })),
      warnings: warningsList.map((e) => ({
        discussion: e.discussion,
        count: e.count,
        agent: e.agent,
        reason: e.reason,
        updated_at: e.updated_at,
      })),
      threshold: DEFAULT_THRESHOLD,
    };
    process.stdout.write(JSON.stringify(output) + "\n");
    return 0;
  }

  if (command === "history") {
    const role = _toStr(flags["role"]);
    if (!role) {
      process.stderr.write("history: --role is required\n");
      return 1;
    }
    const limit = _toInt(flags["limit"]) ?? 20;
    const entries = history(role, limit);

    if (entries.length === 0) {
      process.stdout.write(`no transitions recorded for role ${role}\n`);
      return 0;
    }

    // Pretty table: time | from → to | reason | last_pr
    const header = `${"time".padEnd(25)} ${"transition".padEnd(22)} ${"reason".padEnd(45)} last_pr`;
    process.stdout.write(header + "\n");
    process.stdout.write("-".repeat(header.length) + "\n");
    for (const e of entries) {
      const ts = (e.timestamp ?? "").slice(0, 19);
      const transition = `${e.from_state ?? "?"} → ${e.to_state ?? "?"}`;
      const reason = (e.reason ?? "").slice(0, 44);
      const pr = String(e.last_pr ?? "");
      process.stdout.write(
        `${ts.padEnd(25)} ${transition.padEnd(22)} ${reason.padEnd(45)} ${pr}\n`
      );
    }
    return 0;
  }

  if (command === "expire") {
    const dryRun = flags["dry-run"] === true;
    const results = expireStale({ dryRun });
    if (results.length === 0) {
      process.stdout.write("no stale breakers found\n");
      return 0;
    }
    const mode = dryRun ? "[dry-run] " : "";
    for (const item of results) {
      process.stdout.write(`${mode}expired: #${item.discussion} (${item.reason})\n`);
    }
    process.stdout.write(`${mode}${results.length} breaker(s) expired\n`);
    return 0;
  }

  process.stderr.write(
    "usage: circuit-breaker.ts <status|reset|record|list|summary|history|expire> ...\n"
  );
  return 1;
}

// Run as CLI when this is the main module
if (import.meta.main) {
  process.exit(main());
}
