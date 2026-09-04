/**
 * spawn/dial-registry.ts — Runtime autonomy dial system.
 *
 * Mirrors backend/dial_registry.py 1:1.
 *
 * Each "dial class" has a numeric level 1–5. check() allows/denies an action
 * based on whether the current dial level meets the requested threshold.
 * set_dial() mutates the level and records a hash-chained audit row.
 * Directives can carry TTLs and expire automatically on the next check().
 *
 * Hardcoded ceilings (cannot be raised even by an allowlisted source):
 *   sandbox.modify      → 1
 *   methodology.change  → 2
 *   external.system     → 2
 *
 * All other classes ceiling = 5.
 *
 * Default dial state lives in <STATE_DIR>/dial-registry.json (written on
 * first use if absent). Mutations are recorded in <STATE_DIR>/audit.jsonl
 * as hash-chained rows with kind="dial_change" (accepted) or
 * kind="dial_directive_rejected" (rejected calls).
 *
 * CLI usage:
 *   bun run ts-backend/src/spawn/dial-registry.ts list
 *   bun run ts-backend/src/spawn/dial-registry.ts check agent.spawn 1
 *   bun run ts-backend/src/spawn/dial-registry.ts set agent.spawn 3
 *   bun run ts-backend/src/spawn/dial-registry.ts revert-expired
 *
 * Programmatic exports:
 *   import { check, setDial, listDirectives, DialCeilingExceeded } from "./dial-registry.js";
 */

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------

export class DialCeilingExceeded extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DialCeilingExceeded";
  }
}

// ---------------------------------------------------------------------------
// Hardcoded ceilings — mirrors _CEILINGS in dial_registry.py
// ---------------------------------------------------------------------------

const _CEILINGS: Record<string, number> = {
  "sandbox.modify": 1,
  "methodology.change": 2,
  "external.system": 2,
};
const _DEFAULT_CEILING = 5;

// ---------------------------------------------------------------------------
// Default dial state — mirrors _DEFAULT_DIALS in dial_registry.py exactly
// ---------------------------------------------------------------------------

interface DefaultDial {
  class: string;
  level: number;
  ceiling: number;
}

const _DEFAULT_DIALS: DefaultDial[] = [
  { class: "docs.write",         level: 5, ceiling: 5 },
  { class: "tests.add",          level: 4, ceiling: 5 },
  { class: "deps.bump",          level: 3, ceiling: 5 },
  { class: "agent.spawn",        level: 4, ceiling: 5 },
  { class: "merge.standard",     level: 4, ceiling: 5 },
  { class: "merge.fast-path",    level: 2, ceiling: 5 },
  { class: "intent.generate",    level: 1, ceiling: 5 },
  { class: "methodology.change", level: 1, ceiling: 2 },
  { class: "external.system",    level: 1, ceiling: 2 },
  { class: "sandbox.modify",     level: 1, ceiling: 1 },
  { class: "cost.spend",         level: 2, ceiling: 5 },
  { class: "memory.write",       level: 3, ceiling: 5 },
  { class: "archive.move",       level: 4, ceiling: 5 },
];

// ---------------------------------------------------------------------------
// Role-to-dial-class mapping — mirrors _ROLE_TO_DIAL_CLASS in dial_registry.py
// ---------------------------------------------------------------------------

export const ROLE_TO_DIAL_CLASS: Record<string, string> = {
  "executor":             "agent.spawn",
  "code-reviewer":        "agent.spawn",
  "security-reviewer":    "agent.spawn",
  "acceptance-tester":    "agent.spawn",
  "project-manager":      "agent.spawn",
  "technical-architect":  "agent.spawn",
  "security-expert":      "agent.spawn",
  "cost-analyst":         "agent.spawn",
  "product-owner":        "agent.spawn",
  "performance-expert":   "agent.spawn",
  "run-analyst":          "agent.spawn",
  "feedback-scanner":     "agent.spawn",
  "quality-sweep":        "agent.spawn",
  "docs-writer":          "agent.spawn",
  "browser-tester":       "agent.spawn",
  "researcher":           "agent.spawn",
  "mission-analyst":      "agent.spawn",
  "visual-verifier":      "agent.spawn",
  "incident-commander":   "agent.spawn",
  "release-manager":      "agent.spawn",
};

// ---------------------------------------------------------------------------
// State directory resolution — mirrors _state_dir() in dial_registry.py
// ---------------------------------------------------------------------------

function _stateDir(): string {
  return sharedStateDir();
}

function _registryPath(): string {
  return join(_stateDir(), "dial-registry.json");
}

function _allowlistPath(): string {
  return join(_stateDir(), "dial-directive-allowlist.json");
}

function _auditPath(): string {
  return join(_stateDir(), "audit.jsonl");
}

// ---------------------------------------------------------------------------
// Helpers — mirrors dial_registry.py helpers exactly
// ---------------------------------------------------------------------------

function _nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

/**
 * Return tomorrow's local midnight expressed as a UTC ISO-8601 timestamp.
 * Mirrors _local_midnight_utc() in dial_registry.py.
 */
function _localMidnightUtc(): string {
  const now = new Date();
  // Construct tomorrow's midnight in local time
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  tomorrow.setHours(0, 0, 0, 0);
  return tomorrow.toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

/**
 * Convert ttl argument to an ISO-8601 UTC expiry string or null.
 * Mirrors _parse_ttl() in dial_registry.py.
 */
function _parseTtl(ttl: string | null | undefined): string | null {
  if (ttl === null || ttl === undefined) return null;
  if (ttl === "for-today") return _localMidnightUtc();
  // Already an ISO string — normalise
  try {
    const dt = new Date(ttl);
    if (isNaN(dt.getTime())) throw new Error("invalid date");
    return dt.toISOString().replace(/\.\d{3}Z$/, "+00:00");
  } catch {
    throw new Error(`Invalid ttl format ${JSON.stringify(ttl)}`);
  }
}

/**
 * Return true if ttl_until is set and has passed.
 * Mirrors _is_expired() in dial_registry.py.
 */
function _isExpired(ttl_until: string | null | undefined): boolean {
  if (!ttl_until) return false;
  try {
    const expiry = new Date(ttl_until);
    if (isNaN(expiry.getTime())) return false;
    return Date.now() >= expiry.getTime();
  } catch {
    return false;
  }
}

/**
 * Return the ceiling that actually applies — hardcoded wins.
 * Mirrors _effective_ceiling() in dial_registry.py.
 */
function _effectiveCeiling(className: string, storedCeiling: number): number {
  const hardcoded = _CEILINGS[className];
  if (hardcoded !== undefined) return hardcoded;
  return storedCeiling;
}

// ---------------------------------------------------------------------------
// Registry state type
// ---------------------------------------------------------------------------

interface DialDirective {
  level: number;
  source: Record<string, unknown> | null;
  set_at: string;
  ttl_until: string | null;
}

interface DialState {
  level: number;
  ceiling: number;
  directives: DialDirective[];
}

type Registry = Record<string, DialState>;

// ---------------------------------------------------------------------------
// Registry I/O — mirrors _load_registry() and _save_registry() in dial_registry.py
// ---------------------------------------------------------------------------

function _initDefaults(): Registry {
  const registry: Registry = {};
  for (const entry of _DEFAULT_DIALS) {
    registry[entry.class] = {
      level: entry.level,
      ceiling: _effectiveCeiling(entry.class, entry.ceiling),
      directives: [],
    };
  }
  _saveRegistry(registry);
  return registry;
}

function _loadRegistry(): Registry {
  const path = _registryPath();
  mkdirSync(join(path, ".."), { recursive: true });

  if (!existsSync(path)) return _initDefaults();

  let data: unknown;
  try {
    data = JSON.parse(readFileSync(path, "utf-8"));
  } catch {
    return _initDefaults();
  }

  // data may be a list (legacy) or dict keyed by class name
  if (Array.isArray(data)) {
    const registry: Registry = {};
    for (const entry of data as Record<string, unknown>[]) {
      const cls = (entry["class"] ?? entry["class_name"]) as string | undefined;
      if (cls) {
        registry[cls] = {
          level: parseInt(String(entry["level"] ?? 1), 10),
          ceiling: _effectiveCeiling(cls, parseInt(String(entry["ceiling"] ?? _DEFAULT_CEILING), 10)),
          directives: (entry["directives"] as DialDirective[] | undefined) ?? [],
        };
      }
    }
    return registry;
  }

  if (typeof data === "object" && data !== null) {
    const registry: Registry = {};
    const raw = data as Record<string, unknown>;
    for (const [cls, val] of Object.entries(raw)) {
      if (typeof val === "object" && val !== null) {
        const v = val as Record<string, unknown>;
        registry[cls] = {
          level: parseInt(String(v["level"] ?? 1), 10),
          ceiling: _effectiveCeiling(cls, parseInt(String(v["ceiling"] ?? _DEFAULT_CEILING), 10)),
          directives: (v["directives"] as DialDirective[] | undefined) ?? [],
        };
      }
    }

    // Migrate legacy key executor.spawn → agent.spawn (mirrors Python migration)
    if ("executor.spawn" in registry) {
      const legacyDirectives = registry["executor.spawn"].directives;
      delete registry["executor.spawn"];

      if (!("agent.spawn" in registry)) {
        const defaultEntry = _DEFAULT_DIALS.find((e) => e.class === "agent.spawn");
        registry["agent.spawn"] = {
          level: defaultEntry?.level ?? 4,
          ceiling: _effectiveCeiling("agent.spawn", defaultEntry?.ceiling ?? _DEFAULT_CEILING),
          directives: [],
        };
      }
      // Concat legacy directives first, then existing ones
      const existing = registry["agent.spawn"].directives;
      registry["agent.spawn"].directives = [...legacyDirectives, ...existing];

      // Atomically persist migrated state
      _saveRegistry(registry);

      // Emit one audit row for the migration
      const prevHash = _readLastAuditHash();
      const row = {
        kind: "dial_state_migration",
        prev_hash: prevHash,
        legacy_class: "executor.spawn",
        target_class: "agent.spawn",
        directives_moved: legacyDirectives.length,
        timestamp: _nowIso(),
      };
      _appendAudit(row);
    }

    return registry;
  }

  return _initDefaults();
}

function _saveRegistry(registry: Registry): void {
  const path = _registryPath();
  mkdirSync(join(path, ".."), { recursive: true });
  const tmp = path + ".tmp";
  try {
    writeFileSync(tmp, JSON.stringify(registry, null, 2) + "\n", "utf-8");
    renameSync(tmp, path);
  } catch {
    try { /* unlinkSync(tmp) */ } catch { /* ignore */ }
  }
}

// ---------------------------------------------------------------------------
// Allowlist auth — mirrors _load_allowlist() and _authenticate_source()
// ---------------------------------------------------------------------------

function _loadAllowlist(): Record<string, unknown>[] {
  const path = _allowlistPath();
  mkdirSync(join(path, ".."), { recursive: true });

  if (!existsSync(path)) {
    try {
      writeFileSync(path, JSON.stringify([], null, 2) + "\n", "utf-8");
    } catch { /* ignore */ }
    return [];
  }

  try {
    const data = JSON.parse(readFileSync(path, "utf-8"));
    if (Array.isArray(data)) return data as Record<string, unknown>[];
  } catch { /* ignore */ }
  return [];
}

/**
 * Return true if source is in the allowlist.
 * Mirrors _authenticate_source() in dial_registry.py.
 */
function _authenticateSource(source: Record<string, unknown> | null | undefined): boolean {
  if (!source || typeof source !== "object") return false;

  const allowlist = _loadAllowlist();
  if (allowlist.length === 0) return false;

  const kind = source["kind"];
  for (const entry of allowlist) {
    if (typeof entry !== "object" || entry === null) continue;
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

// ---------------------------------------------------------------------------
// Audit log (hash-chained) — mirrors audit helpers in dial_registry.py
// ---------------------------------------------------------------------------

function _readLastAuditHash(): string {
  const path = _auditPath();
  if (!existsSync(path)) return "genesis";

  try {
    const content = readFileSync(path);
    const lines = content
      .toString()
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l.length > 0);
    if (lines.length === 0) return "genesis";
    const lastLine = lines[lines.length - 1];
    return createHash("sha256").update(lastLine).digest("hex");
  } catch {
    return "genesis";
  }
}

function _appendAudit(row: Record<string, unknown>): void {
  const path = _auditPath();
  mkdirSync(join(path, ".."), { recursive: true });
  try {
    appendFileSync(path, JSON.stringify(row) + "\n", "utf-8");
  } catch { /* best-effort */ }
}

function _emitDialChange(
  className: string,
  prevLevel: number,
  newLevel: number,
  source: Record<string, unknown> | null | undefined,
  ttl_until: string | null
): void {
  const prevHash = _readLastAuditHash();
  _appendAudit({
    kind: "dial_change",
    prev_hash: prevHash,
    class: className,
    prev_level: prevLevel,
    new_level: newLevel,
    source: source ?? null,
    ttl_until,
    timestamp: _nowIso(),
  });
}

function _emitDialRejection(
  className: string,
  attemptedLevel: number,
  source: Record<string, unknown> | null | undefined,
  reason: string
): void {
  const prevHash = _readLastAuditHash();
  _appendAudit({
    kind: "dial_directive_rejected",
    prev_hash: prevHash,
    class: className,
    level: attemptedLevel,
    source: source ?? null,
    reason,
    timestamp: _nowIso(),
  });
}

// ---------------------------------------------------------------------------
// Public API — mirrors public functions in dial_registry.py
// ---------------------------------------------------------------------------

/**
 * Check all directives and revert any that have passed their TTL.
 * Returns the count of classes that were reverted.
 * Mirrors revert_expired() in dial_registry.py.
 */
export function revertExpired(): number {
  const registry = _loadRegistry();
  let reverted = 0;

  for (const [className, state] of Object.entries(registry)) {
    const directives = state.directives ?? [];
    if (directives.length === 0) continue;

    const live = directives.filter((d) => !_isExpired(d.ttl_until));
    const expired = directives.filter((d) => _isExpired(d.ttl_until));

    if (expired.length === 0) continue;

    // Recompute level: use highest non-expired directive or fall back to default
    let newLevel: number;
    if (live.length > 0) {
      newLevel = Math.max(...live.map((d) => d.level));
    } else {
      const defaultEntry = _DEFAULT_DIALS.find((e) => e.class === className);
      newLevel = defaultEntry?.level ?? 1;
    }

    const oldLevel = state.level;
    state.directives = live;
    state.level = newLevel;

    if (newLevel !== oldLevel) {
      _emitDialChange(className, oldLevel, newLevel, null, null);
      reverted += 1;
    }
  }

  _saveRegistry(registry);
  return reverted;
}

/**
 * Check whether an action at requested_level is permitted.
 * Calls revertExpired() first (lazy TTL cleanup).
 * Returns [allowed, reason].
 * Mirrors check() in dial_registry.py.
 */
export function check(className: string, requestedLevel = 1): [boolean, string] {
  revertExpired();
  const registry = _loadRegistry();

  if (!(className in registry)) {
    // Unknown class: default to allow at level 1, deny above
    if (requestedLevel <= 1) {
      return [true, `unknown class ${JSON.stringify(className)} — default allow at level 1`];
    }
    return [
      false,
      `unknown class ${JSON.stringify(className)} — requested level ${requestedLevel} > default 1`,
    ];
  }

  const state = registry[className];
  const current = state.level;
  const ceiling = state.ceiling;

  if (requestedLevel < 1) {
    return [false, `requested_level must be >= 1, got ${requestedLevel}`];
  }

  if (requestedLevel > ceiling) {
    return [
      false,
      `requested level ${requestedLevel} exceeds ceiling ${ceiling} for ${JSON.stringify(className)}`,
    ];
  }

  if (current >= requestedLevel) {
    return [true, `dial ${JSON.stringify(className)} at ${current} >= requested ${requestedLevel}`];
  }

  return [false, `dial ${JSON.stringify(className)} at ${current} < requested ${requestedLevel}`];
}

/**
 * Set the dial for class_name to level.
 * Returns the updated state dict.
 * Raises ValueError on validation failure, DialCeilingExceeded on ceiling violation.
 * Mirrors set_dial() in dial_registry.py.
 */
export function setDial(
  className: string,
  level: number,
  opts: {
    ttl?: string | null;
    source?: Record<string, unknown> | null;
  } = {}
): DialState {
  const { ttl, source } = opts;

  // Ceiling/validity checks run BEFORE auth (mirrors Python error precedence)
  if (level < 1) {
    _emitDialRejection(className, level, source, "invalid_level");
    throw new Error(`level must be >= 1, got ${level}`);
  }

  const ceiling = _CEILINGS[className] ?? _DEFAULT_CEILING;
  if (level > ceiling) {
    _emitDialRejection(className, level, source, "ceiling_violation");
    throw new DialCeilingExceeded(
      `level ${level} exceeds ceiling ${ceiling} for class ${JSON.stringify(className)}`
    );
  }

  if (!_authenticateSource(source)) {
    _emitDialRejection(className, level, source, "unauthenticated_source");
    // SEC-1 (D#1883 security review round 2): worded at the operator, not
    // the caller — a rejected call should not be told to go authorize
    // itself. See the mirror in backend/dial_registry.py for the full note.
    throw new Error(
      `source ${JSON.stringify(source)} is not in the directive allowlist. ` +
      "A caller cannot authorize itself — ask an operator to run " +
      "`bash scripts/provision-dial-allowlist.sh`, or to add an entry to " +
      "<STATE_DIR>/dial-directive-allowlist.json by hand. Ceilings stay " +
      "enforced either way."
    );
  }

  // Reject unknown class names
  const knownClasses = new Set(_DEFAULT_DIALS.map((e) => e.class));
  if (!knownClasses.has(className)) {
    _emitDialRejection(className, level, source, "unknown_class");
    throw new Error(
      `unknown dial class ${JSON.stringify(className)} — ` +
      `registered classes: ${JSON.stringify([...knownClasses].sort())}`
    );
  }

  const ttlUntil = _parseTtl(ttl);
  const registry = _loadRegistry();

  if (!(className in registry)) {
    registry[className] = {
      level: 1,
      ceiling,
      directives: [],
    };
  }

  const state = registry[className];
  const prevLevel = state.level;

  const directive: DialDirective = {
    level,
    source: source ?? null,
    set_at: _nowIso(),
    ttl_until: ttlUntil,
  };
  state.directives.push(directive);
  state.level = level;

  _saveRegistry(registry);
  _emitDialChange(className, prevLevel, level, source, ttlUntil);

  return { ...state };
}

/**
 * Return current dial state for all registered classes.
 * Each entry has keys: class, level, ceiling, directives.
 * Mirrors list_directives() in dial_registry.py.
 */
export function listDirectives(): Array<{
  class: string;
  level: number;
  ceiling: number;
  directives: DialDirective[];
}> {
  revertExpired();
  const registry = _loadRegistry();
  const result: Array<{
    class: string;
    level: number;
    ceiling: number;
    directives: DialDirective[];
  }> = [];

  for (const [className, state] of Object.entries(registry).sort(([a], [b]) =>
    a.localeCompare(b)
  )) {
    result.push({
      class: className,
      level: state.level,
      ceiling: state.ceiling,
      directives: state.directives ?? [],
    });
  }
  return result;
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

// ---------------------------------------------------------------------------
// CLI entry point — mirrors main() in dial_registry.py
// ---------------------------------------------------------------------------

export function main(argv?: string[]): number {
  const { command, flags, positional } = _parseArgs(argv ?? process.argv.slice(2));

  if (command === "list") {
    const directives = listDirectives();
    for (const d of directives) {
      const cls = d.class.padEnd(25);
      const n = d.directives.length;
      process.stdout.write(`  ${cls}  level=${d.level}  ceiling=${d.ceiling}  directives=${n}\n`);
    }
    return 0;
  }

  if (command === "check") {
    const className = positional[0];
    const requestedLevel = positional[1] !== undefined ? parseInt(positional[1], 10) : 1;
    if (!className) {
      process.stderr.write("check: class_name required\n");
      return 1;
    }
    const [allowed, reason] = check(className, requestedLevel);
    const status = allowed ? "ALLOW" : "DENY";
    process.stdout.write(`${status}: ${reason}\n`);
    return allowed ? 0 : 1;
  }

  if (command === "set") {
    const className = positional[0];
    const level = positional[1] !== undefined ? parseInt(positional[1], 10) : NaN;
    if (!className || isNaN(level)) {
      process.stderr.write("set: class_name and level required\n");
      return 1;
    }

    let source: Record<string, unknown> | null = null;
    if (typeof flags["source"] === "string") {
      try {
        source = JSON.parse(flags["source"]) as Record<string, unknown>;
      } catch {
        process.stderr.write("error: --source must be valid JSON\n");
        return 1;
      }
    }

    const ttl = typeof flags["ttl"] === "string" ? flags["ttl"] : null;

    try {
      const result = setDial(className, level, { ttl, source });
      process.stdout.write(
        `set ${className} level=${result.level} ceiling=${result.ceiling}\n`
      );
    } catch (err) {
      if (err instanceof DialCeilingExceeded) {
        process.stderr.write(`DialCeilingExceeded: ${err.message}\n`);
      } else {
        process.stderr.write(`error: ${String(err instanceof Error ? err.message : err)}\n`);
      }
      return 1;
    }
    return 0;
  }

  if (command === "revert-expired") {
    const count = revertExpired();
    process.stdout.write(`reverted ${count} class(es)\n`);
    return 0;
  }

  process.stderr.write(
    "usage: dial-registry.ts <list|check|set|revert-expired> ...\n"
  );
  return 1;
}

// Run as CLI when this is the main module
if (import.meta.main) {
  process.exit(main());
}
