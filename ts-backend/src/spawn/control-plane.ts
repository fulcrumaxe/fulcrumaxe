/**
 * spawn/control-plane.ts — Runtime configuration, feature gates, and agent policies.
 *
 * Mirrors backend/control_plane.py 1:1.
 *
 * Reads/writes .autonomous-team/config.json under the `gates`, `policies`,
 * and `audit_log` keys. All changes are recorded in the audit log so every
 * behavioral modification is traceable.
 *
 * CLI entry point mirrors the Python subcommands exactly:
 *   bun run ts-backend/src/spawn/control-plane.ts show
 *   bun run ts-backend/src/spawn/control-plane.ts get gates.auto_merge
 *   bun run ts-backend/src/spawn/control-plane.ts set gates.auto_merge false
 *   bun run ts-backend/src/spawn/control-plane.ts gates
 *   bun run ts-backend/src/spawn/control-plane.ts audit
 *   bun run ts-backend/src/spawn/control-plane.ts mode show
 *   bun run ts-backend/src/spawn/control-plane.ts mode set strict
 *   bun run ts-backend/src/spawn/control-plane.ts mode list
 *
 * Programmatic exports:
 *   import { ControlPlane, checkGate, checkPolicy } from "./control-plane.js";
 *
 * Path resolution: AF_CONTROL_PLANE_CONFIG env var overrides the default
 * .autonomous-team/config.json (relative to repo root, resolved from this file).
 *
 * All handlers are ADDITIVE — Python runtime is never modified.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, openSync, closeSync, renameSync } from "node:fs";
import { join, dirname, resolve } from "node:path";

// ---------------------------------------------------------------------------
// Per-gate type metadata — mirrors _GATE_TYPES in control_plane.py
// ---------------------------------------------------------------------------

const _GATE_TYPES: Record<string, string> = {
  self_observe_enforcement: "enum[shadow|advisory|enforced]",
};

// ---------------------------------------------------------------------------
// Default gates — mirrors _DEFAULT_GATES in control_plane.py exactly
// ---------------------------------------------------------------------------

const _DEFAULT_GATES: Record<string, boolean | string> = {
  auto_merge: true,
  security_review: true,
  budget_check: true,
  idea_generation: true,
  stall_detection: true,
  wiki_sync: true,
  human_verification: false,
  // Self-observe gates
  self_observe_executor: false,
  self_observe_impl_coord: false,
  // Self-observe enforcement mode (string gate)
  self_observe_enforcement: "shadow",
  // Docs-writer gate
  docs_writer: true,
  // Incident-commander gate
  incident_commander: false,
  // Release-manager gate
  release_manager: true,
  // Runbook-writer gate
  runbook_writer: true,
  // Analytics-engineer gate
  analytics_engineer: true,
  // Phased orchestration gates (D#559)
  phased_orchestration: false,
  phased_code_review: true,
  // Cost-aware Discussion router (D#836)
  cost_aware_router: false,
  // Debater pass (D#841)
  debater_pass: false,
  // TUI tester pilot sweep (D#855)
  tui_tester_pilot_sweep: false,
  // Execve fence (D#887)
  execve_fence: true,
  // Loop-start gate (D#505)
  loop_start: false,
  // Dial-state-summary scheduled job (D#1188)
  dial_state_summary: false,
};

// ---------------------------------------------------------------------------
// Default settings — mirrors _DEFAULT_SETTINGS in control_plane.py
// ---------------------------------------------------------------------------

const _DEFAULT_SETTINGS: Record<string, Record<string, unknown>> = {
  "team-lead": {
    max_parallel_impl: 3,
  },
};

// ---------------------------------------------------------------------------
// Default policies — mirrors _DEFAULT_POLICIES in control_plane.py exactly
// ---------------------------------------------------------------------------

const _DEFAULT_POLICIES: Record<string, Record<string, unknown>> = {
  executor: {
    timeout_minutes: 45,
    max_retries: 2,
    token_ceiling: 500_000,
  },
  "code-reviewer": {
    timeout_minutes: 20,
    max_retries: 1,
    token_ceiling: 200_000,
    max_concurrent: 4,
  },
  "security-reviewer": {
    timeout_minutes: 20,
    max_retries: 1,
    token_ceiling: 200_000,
  },
  "project-manager": {
    timeout_minutes: 30,
    max_retries: 1,
    token_ceiling: 300_000,
  },
  incident_commander: {
    timeout_minutes: 30,
    max_retries: 1,
    token_ceiling: 80_000,
    max_spawns_per_hour: 1,
  },
  // Debater (D#841)
  debater: {
    token_cap: 5_000,
    timeout_seconds: 90,
    min_precision_30d: 0.30,
  },
  // Loop-run log retention (D#412)
  loop_runs: {
    retention_days: 30,
  },
};

// ---------------------------------------------------------------------------
// Mode presets — mirrors _MODE_PRESETS in control_plane.py exactly
// ---------------------------------------------------------------------------

type ModePreset = {
  gates: Record<string, boolean | string>;
  policies: Record<string, Record<string, unknown>>;
  settings: Record<string, Record<string, unknown>>;
};

const _MODE_PRESETS: Record<string, ModePreset> = {
  strict: {
    gates: {
      auto_merge: false,
      security_review: true,
      budget_check: true,
      idea_generation: true,
      stall_detection: true,
      wiki_sync: true,
    },
    policies: {
      executor: { max_retries: 1, token_ceiling: 300_000 },
      "code-reviewer": { timeout_minutes: 30 },
    },
    settings: {
      "team-lead": { max_parallel_impl: 1 },
    },
  },
  standard: {
    gates: { ..._DEFAULT_GATES },
    policies: {},
    settings: {},
  },
  fast: {
    gates: {
      auto_merge: true,
      security_review: false,
      budget_check: false,
      idea_generation: true,
      stall_detection: true,
      wiki_sync: false,
    },
    policies: {
      executor: { max_retries: 3, token_ceiling: 800_000, timeout_minutes: 60 },
      "code-reviewer": { timeout_minutes: 10 },
    },
    settings: {
      "team-lead": { max_parallel_impl: 5 },
    },
  },
  readonly: {
    gates: {
      auto_merge: false,
      security_review: true,
      budget_check: true,
      idea_generation: false,
      stall_detection: false,
      wiki_sync: false,
    },
    policies: {
      executor: { max_retries: 0, token_ceiling: 0 },
    },
    settings: {
      "team-lead": { max_parallel_impl: 0 },
    },
  },
};

// ---------------------------------------------------------------------------
// Dials schema — mirrors _DIAL_CEILINGS and _DEFAULT_DIALS in control_plane.py
// ---------------------------------------------------------------------------

const _DIAL_CEILINGS: Record<string, number> = {
  "sandbox.modify": 1,
  "methodology.change": 2,
  "external.system": 2,
};
const _DIAL_DEFAULT_CEILING = 5;

const _DEFAULT_DIALS: Array<{ class_name: string; level: number; ceiling: number }> = [
  { class_name: "docs.write",         level: 5, ceiling: 5 },
  { class_name: "tests.add",          level: 4, ceiling: 5 },
  { class_name: "deps.bump",          level: 3, ceiling: 5 },
  { class_name: "agent.spawn",        level: 4, ceiling: 5 },
  { class_name: "merge.standard",     level: 4, ceiling: 5 },
  { class_name: "merge.fast-path",    level: 2, ceiling: 5 },
  { class_name: "intent.generate",    level: 1, ceiling: 5 },
  { class_name: "methodology.change", level: 1, ceiling: 2 },
  { class_name: "external.system",    level: 1, ceiling: 2 },
  { class_name: "sandbox.modify",     level: 1, ceiling: 1 },
  { class_name: "cost.spend",         level: 2, ceiling: 5 },
  { class_name: "memory.write",       level: 3, ceiling: 5 },
  { class_name: "archive.move",       level: 4, ceiling: 5 },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

/**
 * Resolve config file path.
 * Mirrors _resolve_config_path() in control_plane.py.
 * Test override: AF_CONTROL_PLANE_CONFIG env var.
 */
function _resolveConfigPath(): string {
  const override = process.env.AF_CONTROL_PLANE_CONFIG;
  if (override) return resolve(override);
  // src/spawn/ → up 4 levels → repo_root
  const here = dirname(new URL(import.meta.url).pathname);
  const repoRoot = resolve(here, "..", "..", "..", "..");
  return join(repoRoot, ".autonomous-team", "config.json");
}

/**
 * Deep clone a value using JSON round-trip (mirrors copy.deepcopy).
 */
function _deepClone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

// ---------------------------------------------------------------------------
// ControlPlane class — mirrors class ControlPlane in control_plane.py
// ---------------------------------------------------------------------------

export class ControlPlane {
  private _path: string;
  private _data: Record<string, unknown>;

  constructor(configPath?: string) {
    this._path = configPath ?? _resolveConfigPath();
    this._data = {};
  }

  // -------------------------------------------------------------------------
  // Load / Save
  // -------------------------------------------------------------------------

  /**
   * Read config.json and populate internal state with defaults for missing keys.
   * Mirrors ControlPlane.load() in control_plane.py exactly.
   */
  load(): void {
    try {
      const raw = readFileSync(this._path, "utf-8");
      this._data = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      this._data = {};
    }

    // Inject defaults for gates section
    if (!("gates" in this._data)) {
      this._data["gates"] = { ..._DEFAULT_GATES };
    } else {
      const gates = this._data["gates"] as Record<string, unknown>;
      for (const [k, v] of Object.entries(_DEFAULT_GATES)) {
        if (!(k in gates)) gates[k] = v;
      }
    }

    // Inject defaults for policies section
    if (!("policies" in this._data)) {
      const policies: Record<string, Record<string, unknown>> = {};
      for (const [role, defaults] of Object.entries(_DEFAULT_POLICIES)) {
        policies[role] = { ...defaults };
      }
      this._data["policies"] = policies;
    } else {
      const policies = this._data["policies"] as Record<string, Record<string, unknown>>;
      for (const [role, defaults] of Object.entries(_DEFAULT_POLICIES)) {
        if (!(role in policies)) policies[role] = {};
        for (const [k, v] of Object.entries(defaults)) {
          if (!(k in policies[role])) policies[role][k] = v;
        }
      }
    }

    // Inject defaults for settings section
    if (!("settings" in this._data)) {
      const settings: Record<string, Record<string, unknown>> = {};
      for (const [section, defaults] of Object.entries(_DEFAULT_SETTINGS)) {
        settings[section] = { ...defaults };
      }
      this._data["settings"] = settings;
    } else {
      const settings = this._data["settings"] as Record<string, Record<string, unknown>>;
      for (const [section, defaults] of Object.entries(_DEFAULT_SETTINGS)) {
        if (!(section in settings)) settings[section] = {};
        for (const [k, v] of Object.entries(defaults)) {
          if (!(k in settings[section])) settings[section][k] = v;
        }
      }
    }

    // Inject audit_log if missing
    if (!("audit_log" in this._data)) {
      this._data["audit_log"] = [];
    }

    // Inject dials section with defaults for missing classes
    if (!("dials" in this._data)) {
      const dials: Record<string, unknown> = {};
      for (const d of _DEFAULT_DIALS) {
        dials[d.class_name] = {
          level: d.level,
          ceiling: _DIAL_CEILINGS[d.class_name] ?? d.ceiling,
          directives: [],
        };
      }
      this._data["dials"] = dials;
    } else {
      const dials = this._data["dials"] as Record<string, Record<string, unknown>>;
      for (const d of _DEFAULT_DIALS) {
        const cls = d.class_name;
        if (!(cls in dials)) {
          dials[cls] = {
            level: d.level,
            ceiling: _DIAL_CEILINGS[cls] ?? d.ceiling,
            directives: [],
          };
        }
        // Always enforce hardcoded ceiling
        if (cls in _DIAL_CEILINGS) {
          dials[cls]["ceiling"] = _DIAL_CEILINGS[cls];
        }
      }
    }
  }

  /**
   * Atomic write: write to a temp file, then rename.
   * Mirrors ControlPlane.save() in control_plane.py.
   */
  save(): void {
    const dir = dirname(this._path);
    mkdirSync(dir, { recursive: true });

    const tmpPath = this._path + ".tmp";
    const content = JSON.stringify(this._data, null, 2) + "\n";

    // Ensure the target file exists (mirrors fcntl open("a") for the lock)
    if (!existsSync(this._path)) {
      try {
        const fd = openSync(this._path, "a");
        closeSync(fd);
      } catch { /* ignore */ }
    }

    writeFileSync(tmpPath, content, "utf-8");
    renameSync(tmpPath, this._path);
  }

  // -------------------------------------------------------------------------
  // Get / Set with dot-notation
  // -------------------------------------------------------------------------

  /**
   * Retrieve a value using dot-notation (e.g. 'gates.auto_merge').
   *
   * For keys under the 'dials' section, dial class names may contain dots
   * (e.g. 'agent.spawn', 'merge.fast-path'). Naive splitting on '.' would
   * break these names. When the first segment is 'dials', this method uses
   * longest-prefix matching against the registered class names so that a key
   * like 'dials.agent.spawn.level' resolves correctly.
   *
   * Mirrors ControlPlane.get() in control_plane.py exactly.
   * Returns undefined (equivalent to Python None) if any segment is missing.
   */
  get(key: string): unknown {
    const parts = key.split(".");

    // Fast path for non-dials keys
    if (!parts.length || parts[0] !== "dials") {
      let node: unknown = this._data;
      for (const part of parts) {
        if (!node || typeof node !== "object" || !(part in (node as Record<string, unknown>))) {
          return undefined;
        }
        node = (node as Record<string, unknown>)[part];
      }
      return node;
    }

    // 'dials' section: longest-prefix matching for dotted class names
    const dialsNode = this._data["dials"];
    if (!dialsNode || typeof dialsNode !== "object") return undefined;

    const remainder = parts.slice(1).join(".");
    if (!remainder) return dialsNode;

    const knownClasses = Object.keys(dialsNode as Record<string, unknown>)
      .sort((a, b) => b.length - a.length); // longest first

    let matchedClass: string | null = null;
    for (const cls of knownClasses) {
      if (remainder === cls) {
        matchedClass = cls;
        break;
      }
      if (remainder.startsWith(cls + ".")) {
        matchedClass = cls;
        break;
      }
    }

    if (matchedClass === null) return undefined;

    const dialsMap = dialsNode as Record<string, unknown>;
    let node: unknown = dialsMap[matchedClass];
    const suffix = remainder.slice(matchedClass.length);
    if (!suffix) return node;

    // suffix starts with '.', strip it
    for (const part of suffix.replace(/^\./, "").split(".")) {
      if (!node || typeof node !== "object" || !(part in (node as Record<string, unknown>))) {
        return undefined;
      }
      node = (node as Record<string, unknown>)[part];
    }
    return node;
  }

  /**
   * Set a value using dot-notation and record an audit log entry.
   * Creates intermediate dicts as needed.
   * Mirrors ControlPlane.set() in control_plane.py.
   */
  set(key: string, value: unknown): void {
    const oldValue = this.get(key);
    const parts = key.split(".");
    let node = this._data;
    for (const part of parts.slice(0, -1)) {
      if (!(part in node) || typeof node[part] !== "object" || node[part] === null) {
        node[part] = {};
      }
      node = node[part] as Record<string, unknown>;
    }
    node[parts[parts.length - 1]] = value;

    // Append audit entry (keep last 200 entries)
    const entry = {
      timestamp: _nowIso(),
      key,
      old_value: oldValue ?? null,
      new_value: value,
    };
    if (!Array.isArray(this._data["audit_log"])) {
      this._data["audit_log"] = [];
    }
    (this._data["audit_log"] as unknown[]).push(entry);
    this._data["audit_log"] = (this._data["audit_log"] as unknown[]).slice(-200);

    this.save();
  }

  // -------------------------------------------------------------------------
  // Feature gates
  // -------------------------------------------------------------------------

  /**
   * Return true if the named gate is on (defaults to true if not configured).
   * Mirrors ControlPlane.gate_enabled() in control_plane.py.
   */
  gateEnabled(gateName: string): boolean {
    return Boolean(this.get(`gates.${gateName}`) ?? false);
  }

  /**
   * Return all gates as a {name: bool | string} dict.
   * Most gates are booleans, but string-valued gates are returned as-is.
   * Mirrors ControlPlane.list_gates() in control_plane.py exactly.
   */
  listGates(): Record<string, boolean | string> {
    const gates = (this._data["gates"] as Record<string, unknown> | undefined) ?? {};
    // Merge: start with defaults, overlay stored values
    const result: Record<string, boolean | string> = { ..._DEFAULT_GATES };
    for (const [k, v] of Object.entries(gates)) {
      result[k] = typeof v === "string" ? v : Boolean(v);
    }
    // Coerce all non-string values to bool (mirrors Python comprehension)
    for (const k of Object.keys(result)) {
      const v = result[k];
      if (typeof v !== "string") {
        result[k] = Boolean(v);
      }
    }
    return result;
  }

  // -------------------------------------------------------------------------
  // Agent policies
  // -------------------------------------------------------------------------

  /**
   * Return the policy dict for the given agent role.
   * Falls back to hardcoded defaults if the role is not in config.
   * Mirrors ControlPlane.get_policy() in control_plane.py.
   */
  getPolicy(role: string): Record<string, unknown> {
    const policies = (this._data["policies"] as Record<string, Record<string, unknown>> | undefined) ?? {};
    const policy = policies[role] ?? {};
    const defaults = _DEFAULT_POLICIES[role] ?? {};
    const merged: Record<string, unknown> = { ...defaults };
    for (const [k, v] of Object.entries(policy)) {
      merged[k] = v;
    }
    return merged;
  }

  // -------------------------------------------------------------------------
  // Dial registry integration
  // -------------------------------------------------------------------------

  /**
   * Return the dial state dict for class_name, or undefined if unknown.
   * Mirrors ControlPlane.get_dial() in control_plane.py.
   */
  getDial(className: string): Record<string, unknown> | undefined {
    const dials = this._data["dials"] as Record<string, Record<string, unknown>> | undefined;
    return dials?.[className];
  }

  /**
   * Return all dial states keyed by class name.
   * Mirrors ControlPlane.list_dials() in control_plane.py.
   */
  listDials(): Record<string, Record<string, unknown>> {
    return { ...(this._data["dials"] as Record<string, Record<string, unknown>> | undefined ?? {}) };
  }

  /**
   * Return the effective ceiling for class_name (hardcoded wins over stored).
   * Mirrors ControlPlane.get_dial_ceiling() in control_plane.py.
   */
  getDialCeiling(className: string): number {
    return _DIAL_CEILINGS[className] ?? _DIAL_DEFAULT_CEILING;
  }

  // -------------------------------------------------------------------------
  // Settings
  // -------------------------------------------------------------------------

  /**
   * Return a setting value from the settings section, falling back to defaults.
   * Mirrors ControlPlane.get_setting() in control_plane.py.
   */
  getSetting(section: string, key: string): unknown {
    const val = this.get(`settings.${section}.${key}`);
    if (val === undefined) {
      return _DEFAULT_SETTINGS[section]?.[key];
    }
    return val;
  }

  /**
   * Return all settings merged with defaults.
   * Mirrors ControlPlane.list_settings() in control_plane.py.
   */
  listSettings(): Record<string, Record<string, unknown>> {
    const result: Record<string, Record<string, unknown>> = {};
    for (const [section, defaults] of Object.entries(_DEFAULT_SETTINGS)) {
      const merged: Record<string, unknown> = { ...defaults };
      const stored = (this._data["settings"] as Record<string, Record<string, unknown>> | undefined)?.[section] ?? {};
      for (const [k, v] of Object.entries(stored)) {
        merged[k] = v;
      }
      result[section] = merged;
    }
    return result;
  }

  // -------------------------------------------------------------------------
  // Audit log
  // -------------------------------------------------------------------------

  /**
   * Return the most recent `limit` audit entries, newest first.
   * Mirrors ControlPlane.get_audit_log() in control_plane.py.
   */
  getAuditLog(limit = 20): unknown[] {
    const entries = (this._data["audit_log"] as unknown[] | undefined) ?? [];
    return [...entries.slice(-limit)].reverse();
  }

  // -------------------------------------------------------------------------
  // Mode presets
  // -------------------------------------------------------------------------

  /**
   * Apply a named mode preset.
   * Deep-merges the preset values into the current config.
   * Mirrors ControlPlane.apply_mode() in control_plane.py.
   * Throws Error for unknown mode names.
   */
  applyMode(modeName: string): void {
    if (!(modeName in _MODE_PRESETS)) {
      throw new Error(
        `Unknown mode ${JSON.stringify(modeName)}. Valid modes: ${JSON.stringify(Object.keys(_MODE_PRESETS).sort())}`
      );
    }

    const preset = _deepClone(_MODE_PRESETS[modeName]);
    const oldMode = this._data["active_mode"] as string | undefined;

    // Deep-merge: update only the keys listed in the preset
    for (const section of ["gates", "policies", "settings"] as const) {
      const presetSection = preset[section] as Record<string, unknown> | undefined;
      if (!presetSection || Object.keys(presetSection).length === 0) continue;
      if (!this._data[section] || typeof this._data[section] !== "object") {
        this._data[section] = {};
      }
      const currentSection = this._data[section] as Record<string, unknown>;
      for (const [key, value] of Object.entries(presetSection)) {
        if (value && typeof value === "object" && !Array.isArray(value)) {
          if (!currentSection[key] || typeof currentSection[key] !== "object") {
            currentSection[key] = {};
          }
          // Shallow update (mirrors dict.update for nested dicts)
          for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
            (currentSection[key] as Record<string, unknown>)[k] = v;
          }
        } else {
          currentSection[key] = value;
        }
      }
    }

    this._data["active_mode"] = modeName;

    // Audit log entry
    const entry = {
      timestamp: _nowIso(),
      key: "mode",
      old_value: oldMode ?? null,
      new_value: modeName,
    };
    if (!Array.isArray(this._data["audit_log"])) {
      this._data["audit_log"] = [];
    }
    (this._data["audit_log"] as unknown[]).push(entry);
    this._data["audit_log"] = (this._data["audit_log"] as unknown[]).slice(-200);

    this.save();
  }

  /**
   * Return the currently active mode name, or undefined if no mode has been applied.
   * Mirrors ControlPlane.get_mode() in control_plane.py.
   */
  getMode(): string | undefined {
    return this._data["active_mode"] as string | undefined;
  }

  /**
   * Return all preset definitions keyed by mode name (deep copy — safe to mutate).
   * Mirrors ControlPlane.list_modes() in control_plane.py.
   */
  listModes(): Record<string, ModePreset> {
    return _deepClone(_MODE_PRESETS);
  }

  /** Expose internal data for CLI display (mirrors _cmd_show access to cp._data). */
  getData(): Record<string, unknown> {
    return this._data;
  }
}

// ---------------------------------------------------------------------------
// Module-level convenience functions — mirrors check_gate / check_policy
// ---------------------------------------------------------------------------

/**
 * Quick check: load control plane, return whether gate is enabled.
 * Mirrors check_gate() in control_plane.py.
 */
export function checkGate(gateName: string): boolean {
  const cp = new ControlPlane();
  cp.load();
  return cp.gateEnabled(gateName);
}

/**
 * Quick check: load control plane, return policy value for role.
 * Mirrors check_policy() in control_plane.py.
 */
export function checkPolicy(role: string, key: string): unknown {
  const cp = new ControlPlane();
  cp.load();
  return cp.getPolicy(role)[key];
}

// ---------------------------------------------------------------------------
// CLI helpers — mirror Python _cmd_* functions
// ---------------------------------------------------------------------------

/**
 * Try to parse raw as JSON (handles true/false/null/numbers), else return as string.
 * Mirrors _coerce_value() in control_plane.py.
 */
function _coerceValue(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/**
 * Return an error message when value is invalid for gateName, else null.
 * Mirrors _validate_gate_value() in control_plane.py.
 */
function _validateGateValue(gateName: string, value: unknown): string | null {
  const gateType = _GATE_TYPES[gateName] ?? "bool";
  if (gateType === "bool") {
    if (typeof value !== "boolean") {
      return `gate '${gateName}' expects a bool (true/false), got ${typeof value} ${JSON.stringify(value)}`;
    }
  } else if (gateType.startsWith("enum[") && gateType.endsWith("]")) {
    const allowed = gateType.slice(5, -1).split("|");
    if (!allowed.includes(value as string)) {
      return `gate '${gateName}' expects one of ${JSON.stringify(allowed)}, got ${JSON.stringify(value)}`;
    }
  }
  return null;
}

function _cmdShow(cp: ControlPlane, _args: string[]): number {
  const data = cp.getData();
  const display: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    if (k !== "audit_log") display[k] = v;
  }
  process.stdout.write(JSON.stringify(display, null, 2) + "\n");
  return 0;
}

function _cmdGet(cp: ControlPlane, args: string[]): number {
  const key = args[0];
  if (!key) {
    process.stderr.write("get requires a key argument\n");
    return 1;
  }
  const value = cp.get(key);
  if (value === undefined || value === null) {
    process.stderr.write("(not set)\n");
    return 1;
  }
  if (typeof value === "object") {
    process.stdout.write(JSON.stringify(value, null, 2) + "\n");
  } else {
    process.stdout.write(JSON.stringify(value) + "\n");
  }
  return 0;
}

function _cmdSet(cp: ControlPlane, args: string[]): number {
  const [key, rawValue] = args;
  if (!key || rawValue === undefined) {
    process.stderr.write("set requires key and value arguments\n");
    return 1;
  }
  const value = _coerceValue(rawValue);
  // Validate gate values before writing
  const parts = key.split(".");
  if (parts.length === 2 && parts[0] === "gates") {
    const err = _validateGateValue(parts[1], value);
    if (err !== null) {
      process.stderr.write(`error: ${err}\n`);
      return 1;
    }
  }
  cp.set(key, value);
  process.stdout.write(`set ${key} = ${JSON.stringify(value)}\n`);
  return 0;
}

function _cmdGates(cp: ControlPlane, _args: string[]): number {
  const gates = cp.listGates();
  const names = Object.keys(gates);
  const maxName = names.reduce((m, n) => Math.max(m, n.length), 10);
  for (const name of names.sort()) {
    const enabled = gates[name];
    // String gates: show actual value; bool gates: show "on " / "off"
    let status: string;
    if (typeof enabled === "string") {
      status = enabled;
    } else {
      status = enabled ? "on " : "off";
    }
    process.stdout.write(`  ${name.padEnd(maxName)}  ${status}\n`);
  }
  return 0;
}

function _cmdSettings(cp: ControlPlane, _args: string[]): number {
  const settings = cp.listSettings();
  for (const section of Object.keys(settings).sort()) {
    process.stdout.write(`  [${section}]\n`);
    const values = settings[section];
    for (const key of Object.keys(values).sort()) {
      process.stdout.write(`    ${key} = ${JSON.stringify(values[key])}\n`);
    }
  }
  return 0;
}

function _cmdAudit(cp: ControlPlane, _args: string[]): number {
  const entries = cp.getAuditLog(20);
  if (entries.length === 0) {
    process.stdout.write("(no audit entries)\n");
    return 0;
  }
  for (const entry of entries) {
    const e = entry as Record<string, unknown>;
    const ts = (e["timestamp"] as string | undefined) ?? "?";
    const key = (e["key"] as string | undefined) ?? "?";
    const oldStr = JSON.stringify(e["old_value"] ?? null);
    const newStr = JSON.stringify(e["new_value"] ?? null);
    process.stdout.write(`  ${ts}  ${key}: ${oldStr} → ${newStr}\n`);
  }
  return 0;
}

function _cmdDials(cp: ControlPlane, _args: string[]): number {
  const dials = cp.listDials();
  const entries = Object.entries(dials);
  if (entries.length === 0) {
    process.stdout.write("(no dials configured)\n");
    return 0;
  }
  const maxName = entries.reduce((m, [k]) => Math.max(m, k.length), 10);
  for (const [name, state] of entries) {
    const s = state as Record<string, unknown>;
    const lvl = s["level"] ?? "?";
    const ceil = s["ceiling"] ?? "?";
    const ndirs = Array.isArray(s["directives"]) ? s["directives"].length : 0;
    process.stdout.write(`  ${name.padEnd(maxName)}  level=${lvl}  ceiling=${ceil}  directives=${ndirs}\n`);
  }
  return 0;
}

function _cmdMode(cp: ControlPlane, args: string[]): number {
  const sub = args[0];
  if (sub === "show") {
    const active = cp.getMode();
    if (!active) {
      process.stdout.write("No active mode set (using defaults)\n");
      return 0;
    }
    process.stdout.write(`Active mode: ${active}\n`);
    const preset = _MODE_PRESETS[active] ?? {};
    process.stdout.write(JSON.stringify(preset, null, 2) + "\n");
    return 0;
  } else if (sub === "set") {
    const modeName = args[1];
    if (!modeName) {
      process.stderr.write("mode set requires a mode name argument\n");
      return 1;
    }
    try {
      cp.applyMode(modeName);
      process.stdout.write(`Applied mode: ${modeName}\n`);
    } catch (err) {
      process.stderr.write(String(err instanceof Error ? err.message : err) + "\n");
      return 1;
    }
    return 0;
  } else if (sub === "list") {
    for (const name of Object.keys(_MODE_PRESETS).sort()) {
      const preset = _MODE_PRESETS[name];
      const gatesEntries = Object.entries(preset.gates ?? {});
      const gatesSummary = gatesEntries
        .map(([k, v]) => `${k}=${typeof v === "boolean" ? (v ? "on" : "off") : v}`)
        .join(", ");
      process.stdout.write(`  ${name}\n`);
      if (gatesSummary) {
        process.stdout.write(`    gates: ${gatesSummary}\n`);
      }
      const policies = preset.policies ?? {};
      if (Object.keys(policies).length > 0) {
        process.stdout.write(`    policies: ${JSON.stringify(policies)}\n`);
      }
      const settings = preset.settings ?? {};
      if (Object.keys(settings).length > 0) {
        process.stdout.write(`    settings: ${JSON.stringify(settings)}\n`);
      }
    }
    return 0;
  } else {
    process.stderr.write(`Unknown mode subcommand: ${sub}\n`);
    return 1;
  }
}

// ---------------------------------------------------------------------------
// CLI entry point — mirrors main() in control_plane.py
// ---------------------------------------------------------------------------

/**
 * CLI entry point. Parses argv and dispatches to the appropriate command.
 * Mirrors main() in control_plane.py.
 */
export function main(argv?: string[]): number {
  const args = argv ?? process.argv.slice(2);
  const command = args[0];

  if (!command) {
    process.stderr.write(
      "usage: control-plane.ts <show|get|set|gates|dials|settings|audit|mode> ...\n"
    );
    return 1;
  }

  const cp = new ControlPlane();
  cp.load();

  const rest = args.slice(1);

  switch (command) {
    case "show":
      return _cmdShow(cp, rest);
    case "get":
      return _cmdGet(cp, rest);
    case "set":
      return _cmdSet(cp, rest);
    case "gates":
      return _cmdGates(cp, rest);
    case "dials":
      return _cmdDials(cp, rest);
    case "settings":
      return _cmdSettings(cp, rest);
    case "audit":
      return _cmdAudit(cp, rest);
    case "mode":
      return _cmdMode(cp, rest);
    default:
      process.stderr.write(`Unknown command: ${command}\n`);
      process.stderr.write(
        "usage: control-plane.ts <show|get|set|gates|dials|settings|audit|mode> ...\n"
      );
      return 1;
  }
}

// Run as CLI when this is the main module
if (import.meta.main) {
  process.exit(main());
}
