/**
 * rpc/mutating-p6b.ts — Native TS implementations of safe mutating RPC methods (P6b).
 *
 * Mirrors the following Python RPC handlers exactly (1:1 parity):
 *   - dial.set              → handleDialSet()
 *   - auth_retry.record     → handleAuthRetryRecord()
 *   - fleet.discovery_ack   → handleFleetDiscoveryAck()
 *
 * NOT converted (remain in DEFERRED_METHODS — spawning/killing real loops):
 *   - loop.start
 *   - loop.stop
 *
 * All handlers are additive — Python runtime code is not modified.
 *
 * SAFETY INVARIANTS:
 *   - Handlers never touch production state during tests. Mutation tests run against
 *     a TEMP COPY of the relevant store (see tests/rpc-mutating-p6b.test.ts).
 *   - State directories are injected via env vars (AUTONOMOUS_TEAM_STATE_DIR,
 *     FLEET_STATE_DIR_OVERRIDE) so tests can redirect without touching production.
 *
 * Auth model:
 *   All three methods use the same RPC token gate as all other /rpc methods.
 *   dial.set additionally requires source={"kind":"system","reason":"dashboard_rpc"}
 *   to appear in the dial-directive-allowlist.json file — this mirrors Python's
 *   dial_control.handle_set() → dial_registry.set_dial() → _authenticate_source().
 *   The allowlist check is part of Python's business logic, not HTTP-level RBAC.
 *   We mirror it faithfully: set_dial fails with ValueError if allowlist does not
 *   contain the dashboard source — which maps to an RPC error with code -32000.
 *
 * Data sources per method:
 *   dial.set            — <STATE_DIR>/dial-registry.json (R/W), audit.jsonl (append)
 *                         + <STATE_DIR>/dial-directive-allowlist.json (read auth)
 *   auth_retry.record   — SQLite state.db blackboard table (R/W, same as summary)
 *   fleet.discovery_ack — ~/.autonomous-fleet-state/known.json (R/W)
 *
 * Python reference implementations:
 *   dial.set            → backend/rpc/dial_control.py handle_set()
 *                         → backend/dial_registry.py set_dial()
 *   auth_retry.record   → backend/rpc/auth_retry_counter.py handle_record()
 *   fleet.discovery_ack → backend/rpc/fleet_discovery_ack.py handle()
 */

import {
  existsSync,
  readFileSync,
  writeFileSync,
  mkdirSync,
  renameSync,
  appendFileSync,
} from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { createHash } from "node:crypto";
import { Database } from "bun:sqlite";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

function stateDir(): string {
  return sharedStateDir();
}

/** Fleet state dir — injectable for tests via FLEET_STATE_DIR_OVERRIDE. */
function fleetStateDir(): string {
  return (
    process.env.FLEET_STATE_DIR_OVERRIDE ??
    join(homedir(), ".autonomous-fleet-state")
  );
}

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

// ---------------------------------------------------------------------------
// dial.set
//
// Mirrors backend/rpc/dial_control.py handle_set() →
//          backend/dial_registry.py set_dial()
//
// Validation order (must match Python EXACTLY):
//   1. missing/invalid params.name → ValueError
//   2. missing/invalid params.level → ValueError
//   3. invalid params.ttl type → ValueError
//   4. level < 1 → ValueError (invalid_level) + audit row
//   5. level > ceiling → ValueError (ceiling_violation) + audit row
//   6. source not in allowlist → ValueError (unauthenticated_source) + audit row
//   7. unknown class → ValueError (unknown_class) + audit row
//   8. success: write registry + audit change row
//
// Source is hardcoded to {"kind":"system","reason":"dashboard_rpc"} exactly
// as Python does in dial_control.py _DASHBOARD_SOURCE.
// ---------------------------------------------------------------------------

/** Hardcoded ceilings — cannot be raised regardless of allowlist. */
const DIAL_CEILINGS: Record<string, number> = {
  "sandbox.modify": 1,
  "methodology.change": 2,
  "external.system": 2,
};
const DIAL_DEFAULT_CEILING = 5;

/** The 13 registered dial classes — unknown classes are rejected. */
const KNOWN_DIAL_CLASSES = new Set([
  "docs.write",
  "tests.add",
  "deps.bump",
  "agent.spawn",
  "merge.standard",
  "merge.fast-path",
  "intent.generate",
  "methodology.change",
  "external.system",
  "sandbox.modify",
  "cost.spend",
  "memory.write",
  "archive.move",
]);

const DASHBOARD_SOURCE = { kind: "system", reason: "dashboard_rpc" };

interface DialEntry {
  level: number;
  ceiling: number;
  directives: Array<{
    level: number;
    source: Record<string, string> | null;
    set_at: string;
    ttl_until: string | null;
  }>;
}

type DialRegistry = Record<string, DialEntry>;

function effectiveCeiling(className: string, storedCeiling: number): number {
  const hardcoded = DIAL_CEILINGS[className];
  if (hardcoded !== undefined) return hardcoded;
  return storedCeiling;
}

function loadDialRegistry(registryPath: string): DialRegistry {
  if (!existsSync(registryPath)) return {};
  try {
    const raw = readFileSync(registryPath, "utf-8");
    const data = JSON.parse(raw) as unknown;
    if (typeof data !== "object" || data === null || Array.isArray(data)) return {};
    const obj = data as Record<string, unknown>;
    const registry: DialRegistry = {};
    for (const [cls, val] of Object.entries(obj)) {
      if (typeof val === "object" && val !== null && !Array.isArray(val)) {
        const entry = val as Record<string, unknown>;
        registry[cls] = {
          level: Number(entry["level"] ?? 1),
          ceiling: effectiveCeiling(cls, Number(entry["ceiling"] ?? DIAL_DEFAULT_CEILING)),
          directives: Array.isArray(entry["directives"])
            ? (entry["directives"] as DialEntry["directives"])
            : [],
        };
      }
    }
    return registry;
  } catch {
    return {};
  }
}

/**
 * Atomic write: write to .tmp then rename — mirrors Python _save_registry().
 * Note: Python uses fcntl flock; we use a tmp+rename which is atomic on Linux.
 */
function saveDialRegistry(registryPath: string, registry: DialRegistry): void {
  const dir = registryPath.replace(/\/[^/]+$/, "");
  mkdirSync(dir, { recursive: true });
  const tmp = registryPath + ".tmp";
  writeFileSync(tmp, JSON.stringify(registry, null, 2) + "\n", "utf-8");
  renameSync(tmp, registryPath);
}

function loadDialAllowlist(allowlistPath: string): Array<Record<string, string>> {
  if (!existsSync(allowlistPath)) return [];
  try {
    const data = JSON.parse(readFileSync(allowlistPath, "utf-8")) as unknown;
    if (Array.isArray(data)) {
      return data.filter(
        (e): e is Record<string, string> =>
          typeof e === "object" && e !== null && !Array.isArray(e)
      );
    }
    return [];
  } catch {
    return [];
  }
}

/** Mirrors Python _authenticate_source() exactly. */
function authenticateSource(
  source: Record<string, string> | null,
  allowlist: Array<Record<string, string>>
): boolean {
  if (!source || typeof source !== "object") return false;
  if (allowlist.length === 0) return false;
  const kind = source["kind"];
  for (const entry of allowlist) {
    if (entry["kind"] !== kind) continue;
    // The entry is the constraint and the source must satisfy it — every key
    // the entry requires must be present and equal on the source. An entry
    // can never be more specific than the source it authorizes (D#1883
    // Decision 2). This single check subsumes the old kind-specific
    // shortcuts (github_user/login, system/reason) — those matched on
    // kind+one-key and ignored any other key the entry carried, the exact
    // inverse of this invariant (SEC-2, D#1883 security review round 2).
    if (Object.entries(entry).every(([k, v]) => source[k] === v)) return true;
  }
  return false;
}

/** Read last line of audit.jsonl for hash chaining — mirrors Python _read_last_audit_hash(). */
function readLastAuditHash(auditPath: string): string {
  if (!existsSync(auditPath)) return "genesis";
  try {
    const content = readFileSync(auditPath);
    const lines = content.toString("utf-8").split("\n").filter((l) => l.trim());
    if (lines.length === 0) return "genesis";
    return createHash("sha256")
      .update(Buffer.from(lines[lines.length - 1], "utf-8"))
      .digest("hex");
  } catch {
    return "genesis";
  }
}

function appendAuditRow(auditPath: string, row: Record<string, unknown>): void {
  mkdirSync(auditPath.replace(/\/[^/]+$/, ""), { recursive: true });
  appendFileSync(auditPath, JSON.stringify(row) + "\n", "utf-8");
}

function emitDialRejection(
  auditPath: string,
  className: string,
  attemptedLevel: number,
  source: Record<string, string> | null,
  reason: string
): void {
  const prevHash = readLastAuditHash(auditPath);
  appendAuditRow(auditPath, {
    kind: "dial_directive_rejected",
    prev_hash: prevHash,
    class: className,
    level: attemptedLevel,
    source,
    reason,
    timestamp: nowIso(),
  });
}

function emitDialChange(
  auditPath: string,
  className: string,
  prevLevel: number,
  newLevel: number,
  source: Record<string, string> | null,
  ttlUntil: string | null
): void {
  const prevHash = readLastAuditHash(auditPath);
  appendAuditRow(auditPath, {
    kind: "dial_change",
    prev_hash: prevHash,
    class: className,
    prev_level: prevLevel,
    new_level: newLevel,
    source,
    ttl_until: ttlUntil,
    timestamp: nowIso(),
  });
}

/** Parse TTL string — mirrors Python _parse_ttl(). */
function parseTtl(ttl: string | null | undefined): string | null {
  if (ttl == null) return null;
  if (ttl === "for-today") {
    // Tomorrow's local midnight as UTC ISO string — mirrors Python _local_midnight_utc()
    const now = new Date();
    const tomorrowMidnightLocal = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() + 1,
      0,
      0,
      0,
      0
    );
    return tomorrowMidnightLocal.toISOString().replace(/\.\d{3}Z$/, "+00:00");
  }
  // Already an ISO string — validate and normalise
  const dt = new Date(ttl);
  if (isNaN(dt.getTime())) {
    throw new Error(`Invalid ttl format ${JSON.stringify(ttl)}`);
  }
  return dt.toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

/**
 * handleDialSet — mirrors backend/rpc/dial_control.py handle_set()
 *
 * Params: { name: string, level: int, ttl?: string | null }
 * Returns: { name: string, level: int, ceiling: int }
 * Raises: Error on invalid params, ceiling violation, unauthenticated source, unknown class
 *
 * The source is always DASHBOARD_SOURCE ({"kind":"system","reason":"dashboard_rpc"}).
 * This must be present in <STATE_DIR>/dial-directive-allowlist.json for the call to succeed.
 */
export function handleDialSet(
  params: Record<string, unknown>,
  /** Optional override for state dir — used by tests to redirect to temp copies. */
  stateDirOverride?: string
): unknown {
  const sd = stateDirOverride ?? stateDir();
  const registryPath = join(sd, "dial-registry.json");
  const allowlistPath = join(sd, "dial-directive-allowlist.json");
  const auditPath = join(sd, "audit.jsonl");

  // Param validation (mirrors Python handle_set())
  const name = params["name"];
  if (!name || typeof name !== "string") {
    throw new Error("params.name is required and must be a string");
  }

  const level = params["level"];
  if (level === null || level === undefined || typeof level !== "number" || !Number.isInteger(level)) {
    throw new Error("params.level is required and must be an integer");
  }

  const ttlRaw = params["ttl"] !== undefined ? params["ttl"] : null;
  if (ttlRaw !== null && ttlRaw !== undefined && typeof ttlRaw !== "string") {
    throw new Error("params.ttl must be a string or null");
  }
  const ttl = ttlRaw as string | null | undefined;

  // Ceiling check (level < 1 first, then ceiling violation)
  const ceiling = DIAL_CEILINGS[name] ?? DIAL_DEFAULT_CEILING;

  if (level < 1) {
    emitDialRejection(auditPath, name, level, DASHBOARD_SOURCE, "invalid_level");
    throw new Error("level must be >= 1, got " + level);
  }

  if (level > ceiling) {
    emitDialRejection(auditPath, name, level, DASHBOARD_SOURCE, "ceiling_violation");
    throw new Error(
      `ceiling_exceeded: level ${level} exceeds ceiling ${ceiling} for class ${JSON.stringify(name)}`
    );
  }

  // Auth: source must be in allowlist
  const allowlist = loadDialAllowlist(allowlistPath);
  if (!authenticateSource(DASHBOARD_SOURCE, allowlist)) {
    emitDialRejection(auditPath, name, level, DASHBOARD_SOURCE, "unauthenticated_source");
    // SEC-1 (D#1883 security review round 2): worded at the operator, not
    // the caller — a rejected call should not be told to go authorize
    // itself. See the mirror in backend/dial_registry.py for the full note.
    throw new Error(
      `source ${JSON.stringify(DASHBOARD_SOURCE)} is not in the directive allowlist. ` +
        "A caller cannot authorize itself — ask an operator to run " +
        "`bash scripts/provision-dial-allowlist.sh`, or to add an entry to " +
        "<STATE_DIR>/dial-directive-allowlist.json by hand. Ceilings stay " +
        "enforced either way."
    );
  }

  // Unknown class check
  if (!KNOWN_DIAL_CLASSES.has(name)) {
    emitDialRejection(auditPath, name, level, DASHBOARD_SOURCE, "unknown_class");
    throw new Error(
      `unknown dial class ${JSON.stringify(name)} — ` +
        `registered classes: ${JSON.stringify([...KNOWN_DIAL_CLASSES].sort())}`
    );
  }

  // Parse TTL
  let ttlUntil: string | null = null;
  try {
    ttlUntil = parseTtl(ttl);
  } catch (err) {
    throw new Error(err instanceof Error ? err.message : String(err));
  }

  // Load registry, update, save
  const registry = loadDialRegistry(registryPath);
  if (!(name in registry)) {
    registry[name] = { level: 1, ceiling, directives: [] };
  }

  const state = registry[name];
  const prevLevel = state.level;

  const directive = {
    level,
    source: DASHBOARD_SOURCE,
    set_at: nowIso(),
    ttl_until: ttlUntil,
  };
  state.directives.push(directive);
  state.level = level;

  saveDialRegistry(registryPath, registry);
  emitDialChange(auditPath, name, prevLevel, level, DASHBOARD_SOURCE, ttlUntil);

  return {
    name,
    level: state.level,
    ceiling: state.ceiling,
  };
}

// ---------------------------------------------------------------------------
// auth_retry.record
//
// Mirrors backend/rpc/auth_retry_counter.py handle_record()
//
// Writes to SQLite state.db blackboard table (two keys):
//   auth_retry_count      — integer total
//   auth_retry_timestamps — JSON list of ISO8601 strings
//
// Python's get_blackboard() prefers SQLite when state.db exists (production).
// For the TS mirror we also write directly to SQLite.
//
// Best-effort: never raises to the caller (returns {recorded: false} on error).
// ---------------------------------------------------------------------------

/** Write a blackboard entry to SQLite state.db — mirrors SqliteBlackboard.write(). */
function bbWrite(
  db: InstanceType<typeof Database>,
  key: string,
  value: unknown,
  updatedBy: string
): void {
  // Read current version
  const existing = db
    .query<{ value: string }, [string]>("SELECT value FROM blackboard WHERE key = ?")
    .get(key);
  let version = 1;
  if (existing) {
    try {
      const entry = JSON.parse(existing.value) as Record<string, unknown>;
      version = (typeof entry["version"] === "number" ? entry["version"] : 0) + 1;
    } catch {
      version = 1;
    }
  }

  const entry = {
    value,
    version,
    updated_at: new Date().toISOString(),
    updated_by: updatedBy,
  };

  const now = new Date().toISOString();
  db.run(
    `INSERT OR REPLACE INTO blackboard (key, value, updated_at, locked_by, locked_at)
     VALUES (?, ?, ?, ?, ?)`,
    [key, JSON.stringify(entry), now, null, null]
  );
}

/** Read a blackboard value from SQLite — mirrors readBlackboardFromSqlite() in misc-batch5. */
function bbRead(
  db: InstanceType<typeof Database>,
  key: string
): unknown {
  try {
    const row = db
      .query<{ value: string }, [string]>("SELECT value FROM blackboard WHERE key = ?")
      .get(key);
    if (!row) return null;
    const entry = JSON.parse(row.value) as Record<string, unknown>;
    return "value" in entry ? entry["value"] : entry;
  } catch {
    return null;
  }
}

/**
 * handleAuthRetryRecord — mirrors backend/rpc/auth_retry_counter.py handle_record()
 *
 * Params: {} (no params required)
 * Returns: {recorded: boolean, count_total?: int}
 *
 * Best-effort: returns {recorded: false} on any error.
 *
 * Accepts optional dbPathOverride for test isolation — points to a temp state.db copy.
 */
export function handleAuthRetryRecord(
  _params: Record<string, unknown>,
  dbPathOverride?: string
): unknown {
  const dbPath = dbPathOverride ?? join(stateDir(), "state.db");

  try {
    // Open read-write (default mode in bun:sqlite)
    const db = new Database(dbPath);

    try {
      // Ensure blackboard table exists (mirrors Python db.py CREATE TABLE)
      db.run(`
        CREATE TABLE IF NOT EXISTS blackboard (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT,
          locked_by TEXT,
          locked_at TEXT
        )
      `);

      // Increment total counter
      const totalRaw = bbRead(db, "auth_retry_count");
      const newTotal =
        typeof totalRaw === "number"
          ? Math.trunc(totalRaw) + 1
          : totalRaw !== null
          ? (parseInt(String(totalRaw), 10) || 0) + 1
          : 1;
      bbWrite(db, "auth_retry_count", newTotal, "auth_retry_counter");

      // Append timestamp
      const tsRaw = bbRead(db, "auth_retry_timestamps");
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

      timestamps.push(new Date().toISOString());

      // Prune entries older than 48 hours — mirrors Python handler
      const cutoff48h = new Date(Date.now() - 48 * 3600 * 1000).toISOString();
      timestamps = timestamps.filter((t) => t >= cutoff48h);

      bbWrite(db, "auth_retry_timestamps", timestamps, "auth_retry_counter");

      return { recorded: true, count_total: newTotal };
    } finally {
      db.close();
    }
  } catch {
    // Best-effort: swallow all errors so the 401 retry still proceeds
    return { recorded: false };
  }
}

// ---------------------------------------------------------------------------
// fleet.discovery_ack
//
// Mirrors backend/rpc/fleet_discovery_ack.py handle()
//
// Writes to ~/.autonomous-fleet-state/known.json (or FLEET_STATE_DIR_OVERRIDE).
// Atomically: write to .tmp then replace (mirrors Python's tmp.replace()).
//
// Params: { project_name: string }
// Returns: { ok: boolean, known?: string[], error?: string }
// ---------------------------------------------------------------------------

function readKnown(knownPath: string): string[] {
  if (!existsSync(knownPath)) return [];
  try {
    const data = JSON.parse(readFileSync(knownPath, "utf-8")) as unknown;
    if (Array.isArray(data)) {
      return data.map(String);
    }
    return [];
  } catch {
    return [];
  }
}

function writeKnown(knownPath: string, known: string[]): void {
  const dir = knownPath.replace(/\/[^/]+$/, "");
  mkdirSync(dir, { recursive: true });
  const sorted = [...new Set(known)].sort();
  const tmp = knownPath + ".tmp";
  writeFileSync(tmp, JSON.stringify(sorted, null, 2), "utf-8");
  renameSync(tmp, knownPath);
}

/**
 * handleFleetDiscoveryAck — mirrors backend/rpc/fleet_discovery_ack.py handle()
 *
 * Params: { project_name: string }
 * Returns: { ok: true, known: string[] } or { ok: false, error: string }
 */
export function handleFleetDiscoveryAck(
  params: Record<string, unknown>,
  fleetStateDirOverride?: string
): unknown {
  const fsd = fleetStateDirOverride ?? fleetStateDir();
  const knownPath = join(fsd, "known.json");

  const projectName =
    typeof params["project_name"] === "string"
      ? params["project_name"].trim()
      : "";

  if (!projectName) {
    return { ok: false, error: "project_name is required" };
  }

  const known = readKnown(knownPath);
  if (!known.includes(projectName)) {
    known.push(projectName);
  }
  writeKnown(knownPath, known);

  return { ok: true, known: [...new Set(known)].sort() };
}
