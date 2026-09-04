/**
 * POST /budget/init — TypeScript port of backend/routers/ops_budget.py.
 *
 * Source fidelity (D#1437 Spec P4a — parity bug fix):
 * Writes three blackboard keys to the FILE-BASED blackboard directory:
 *   - budget/session_ceiling  → <root>/budget/session_ceiling.json
 *   - budget/session_spent    → <root>/budget/session_spent.json
 *   - budget/per_agent_ceiling → <root>/budget/per_agent_ceiling.json
 *
 * Each file contains the exact JSON envelope Python's Blackboard.write() writes:
 *   { "value": <N>, "version": <int>, "updated_at": "<ISO+00:00>", "updated_by": "budget-tracker" }
 * Written atomically via tmp→rename (mirrors Blackboard._atomic_write()).
 *
 * Bug fixed (confirmed during #1449 investigation):
 *   The original P4a implementation wrote to the SQLite `blackboard` table in
 *   state.db.  But Python's BudgetTracker uses the FILE-based Blackboard
 *   (backend/blackboard.py class Blackboard), writing to files under
 *   <STATE_DIR>/blackboard/.  Python's budget code never reads the SQLite keys,
 *   so P4a writes went to a store nobody reads.  This fix changes the write
 *   target to the file-based blackboard to match Python's real store.
 *
 * Route: POST /budget/init
 * Auth:  bearer auth + RBAC("POST", "/budget/init")
 * Body:  { "ceiling"?: number }   (optional; null/absent → default from config)
 * Response 200: { "ok": true, "status": BudgetStatus }
 *   Always 200 — mirrors Python's no-validation behavior (accepts any numeric value,
 *   including negative and zero). No 400 for non-positive ceiling.
 *
 * Coexistence model (D#1437 Spec P4 coexistence):
 *   - Python remains writer of record on the shared production blackboard dir.
 *   - This TS route is loopback-only and parity-gated.
 *   - During parity testing, BOTH Python and TS handlers are pointed at a
 *     TEMP COPY of the blackboard directory (never the production directory).
 *   - In production the TS route is not invoked unless explicitly opted in.
 *
 * Safety:
 *   - Effect is bounded to 3 .json files in <root>/budget/.
 *   - No subprocess spawns, no external system calls.
 *   - The audit-trail emit in Python's init_session() is best-effort and
 *     silently suppressed on error; the TS port omits it (safe: it is advisory
 *     only and the Python reference itself swallows failures).
 */

import type { Context } from "hono";
import { join } from "node:path";
import {
  existsSync,
  readFileSync,
  readdirSync,
  writeFileSync,
  mkdirSync,
  renameSync,
} from "node:fs";
import { checkRbac } from "../middleware/rbac-check.js";
import { blackboardDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Config defaults — mirrors _DEFAULT_BUDGET in backend/budget.py
// ---------------------------------------------------------------------------
const DEFAULT_SESSION_CEILING = 5_000_000;
const DEFAULT_PER_AGENT_CEILING = 500_000;
const DEFAULT_WARN_THRESHOLD_PCT = 80;

// ---------------------------------------------------------------------------
// Blackboard root resolution — mirrors _resolve_default_root() in backend/blackboard.py
//
// Priority order (same as Python):
//   1. AUTONOMOUS_TEAM_BLACKBOARD_ROOT env var (for parity harness to redirect)
//   2. AUTONOMOUS_TEAM_STATE_DIR env var → <STATE_DIR>/blackboard
//   3. Fall back to config/state-paths.ts blackboardDir() default
// ---------------------------------------------------------------------------

// ts-backend/src/routes/ -> ts-backend/src/ -> ts-backend/ -> repo root
const REPO_ROOT = join(import.meta.dir, "..", "..", "..");

function resolveBlackboardRoot(): string {
  // Explicit override for parity harness to point at a temp directory.
  if (process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT) {
    return process.env.AUTONOMOUS_TEAM_BLACKBOARD_ROOT;
  }
  return blackboardDir();
}

// ---------------------------------------------------------------------------
// Config loader — mirrors _load_config() in backend/budget.py
// Reads budget defaults from .autonomous-team/config.json if available.
// Falls back to _DEFAULT_BUDGET constants.
// ---------------------------------------------------------------------------

interface BudgetConfig {
  session_ceiling: number;
  per_agent_ceiling: number;
  warn_threshold_pct: number;
}

function loadBudgetConfig(): BudgetConfig {
  const configPath =
    process.env.AUTONOMOUS_TEAM_CONFIG ??
    join(REPO_ROOT, ".autonomous-team", "config.json");

  if (!existsSync(configPath)) {
    return {
      session_ceiling: DEFAULT_SESSION_CEILING,
      per_agent_ceiling: DEFAULT_PER_AGENT_CEILING,
      warn_threshold_pct: DEFAULT_WARN_THRESHOLD_PCT,
    };
  }
  try {
    const raw = readFileSync(configPath, "utf-8");
    const cfg = JSON.parse(raw) as Record<string, unknown>;
    const budgetCfg = (cfg["budget"] as Record<string, unknown> | undefined) ?? {};
    return {
      session_ceiling:
        typeof budgetCfg["session_ceiling"] === "number"
          ? budgetCfg["session_ceiling"]
          : DEFAULT_SESSION_CEILING,
      per_agent_ceiling:
        typeof budgetCfg["per_agent_ceiling"] === "number"
          ? budgetCfg["per_agent_ceiling"]
          : DEFAULT_PER_AGENT_CEILING,
      warn_threshold_pct:
        typeof budgetCfg["warn_threshold_pct"] === "number"
          ? budgetCfg["warn_threshold_pct"]
          : DEFAULT_WARN_THRESHOLD_PCT,
    };
  } catch {
    return {
      session_ceiling: DEFAULT_SESSION_CEILING,
      per_agent_ceiling: DEFAULT_PER_AGENT_CEILING,
      warn_threshold_pct: DEFAULT_WARN_THRESHOLD_PCT,
    };
  }
}

// ---------------------------------------------------------------------------
// ISO timestamp helper — mirrors _now_iso() in backend/budget.py
// Produces "YYYY-MM-DDTHH:MM:SS+00:00" (Python's isoformat(timespec="seconds"))
// ---------------------------------------------------------------------------

function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "+00:00");
}

// ---------------------------------------------------------------------------
// FileBlackboardWriter — writes blackboard entries as JSON files.
// Mirrors the file-based Blackboard class in backend/blackboard.py.
//
// Key naming: forward-slash separators, e.g. "budget/session_ceiling"
// File path:  <root>/<key>.json  (e.g. <root>/budget/session_ceiling.json)
//
// JSON envelope (matches Python's Blackboard._atomic_write exactly):
//   {
//     "value": <any>,
//     "version": <int>,          // starts at 1, increments on each write
//     "updated_at": "<ISO+00:00>",
//     "updated_by": "<string>"
//   }
//   followed by a trailing newline (Python: fh.write("\n"))
//
// Atomic write: write to <key>.tmp then os.rename(tmp, dest)
// Mirrors Blackboard._atomic_write().
// ---------------------------------------------------------------------------

interface BlackboardEntry {
  value: unknown;
  version: number;
  updated_at: string;
  updated_by: string;
}

class FileBlackboardWriter {
  private root: string;

  constructor(root: string) {
    this.root = root;
  }

  /** Map a key to its JSON file path. Mirrors Blackboard._key_path(). */
  private keyPath(key: string): string {
    return join(this.root, key + ".json");
  }

  /** Read the existing entry for a key, or null if absent. */
  getEntry(key: string): BlackboardEntry | null {
    const path = this.keyPath(key);
    if (!existsSync(path)) return null;
    try {
      const raw = readFileSync(path, "utf-8");
      const parsed = JSON.parse(raw) as unknown;
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as BlackboardEntry;
      }
      return null;
    } catch {
      return null;
    }
  }

  /** Read just the value for a key, or null. */
  readValue(key: string): unknown {
    const entry = this.getEntry(key);
    return entry !== null ? entry.value : null;
  }

  /**
   * Atomically write value under key.
   * Mirrors Blackboard.write() + Blackboard._atomic_write().
   * Increments version; writes via tmp→rename.
   */
  write(key: string, value: unknown, updatedBy: string): void {
    const dest = this.keyPath(key);
    const existing = this.getEntry(key);
    const version = existing !== null ? (existing.version ?? 0) + 1 : 1;
    const entry: BlackboardEntry = {
      value,
      version,
      updated_at: nowIso(),
      updated_by: updatedBy,
    };
    // Ensure parent directory exists (mirrors dest.parent.mkdir(parents=True)).
    const parentDir = join(dest, "..");
    mkdirSync(parentDir, { recursive: true });
    // Write to .tmp then rename (mirrors Python's tmp-then-rename dance).
    const tmp = dest.replace(/\.json$/, ".tmp");
    // json.dump with indent=2 + trailing newline (mirrors Python exactly).
    writeFileSync(tmp, JSON.stringify(entry, null, 2) + "\n", "utf-8");
    renameSync(tmp, dest);
  }

  /**
   * List .json files directly under <root>/budget/ (non-recursive).
   * Returns keys like "budget/session_ceiling" (no agents/ subdir).
   * Used only to enumerate the 3 session keys for buildStatus.
   */
  listBudgetSessionKeys(): string[] {
    const budgetDir = join(this.root, "budget");
    if (!existsSync(budgetDir)) return [];
    try {
      const files = readdirSync(budgetDir, { withFileTypes: true });
      const keys: string[] = [];
      for (const ent of files) {
        if (ent.isFile() && ent.name.endsWith(".json")) {
          keys.push("budget/" + ent.name.slice(0, -5));
        }
      }
      return keys.sort();
    } catch {
      return [];
    }
  }

  /**
   * List all agent keys under budget/agents/.
   * Returns "budget/agents/<id>" keys.
   */
  listAgentKeys(): string[] {
    const agentsDir = join(this.root, "budget", "agents");
    if (!existsSync(agentsDir)) return [];
    try {
      const files = readdirSync(agentsDir, { withFileTypes: true });
      const keys: string[] = [];
      for (const ent of files) {
        if (ent.isFile() && ent.name.endsWith(".json")) {
          keys.push("budget/agents/" + ent.name.slice(0, -5));
        }
      }
      return keys.sort();
    } catch {
      return [];
    }
  }
}

// ---------------------------------------------------------------------------
// BudgetStatus — mirrors BudgetTracker.get_status() output shape
// ---------------------------------------------------------------------------

interface BudgetStatus {
  ceiling: number;
  spent: number;
  remaining: number;
  per_agent_ceiling: number;
  warn_threshold_pct: number;
  agents: unknown[];
}

/** Build status by reading budget/* keys from the file-based blackboard. */
function buildStatus(
  writer: FileBlackboardWriter,
  config: BudgetConfig,
): BudgetStatus {
  const ceiling =
    typeof writer.readValue("budget/session_ceiling") === "number"
      ? (writer.readValue("budget/session_ceiling") as number)
      : config.session_ceiling;

  const perAgentCeiling =
    typeof writer.readValue("budget/per_agent_ceiling") === "number"
      ? (writer.readValue("budget/per_agent_ceiling") as number)
      : config.per_agent_ceiling;

  const agentKeys = writer.listAgentKeys();
  const agents: unknown[] = agentKeys
    .map((k) => writer.readValue(k))
    .filter((v) => v !== null);

  const spent = (agents as Array<Record<string, number>>).reduce(
    (sum, a) => sum + ((a["input"] ?? 0) || 0) + ((a["output"] ?? 0) || 0),
    0,
  );
  const remaining = Math.max(0, ceiling - spent);

  return {
    ceiling,
    spent,
    remaining,
    per_agent_ceiling: perAgentCeiling,
    warn_threshold_pct: config.warn_threshold_pct,
    agents,
  };
}

// ---------------------------------------------------------------------------
// initBudgetSession — core mutation, testable without HTTP layer.
//
// Mirrors BudgetTracker.init_session():
//   self._bb.write("budget/session_ceiling",  effective_ceiling, "budget-tracker")
//   self._bb.write("budget/session_spent",    0,                 "budget-tracker")
//   self._bb.write("budget/per_agent_ceiling", per_agent_ceiling, "budget-tracker")
//
// Returns the status dict after the write (mirrors the POST response body).
// Takes an explicit bbRoot so the parity harness can redirect to a temp copy
// of the file-based blackboard directory.
// ---------------------------------------------------------------------------

export interface BudgetInitResult {
  ok: boolean;
  status: BudgetStatus;
}

export function initBudgetSession(
  ceiling: number | null,
  bbRoot?: string,
): BudgetInitResult {
  const resolvedRoot = bbRoot ?? resolveBlackboardRoot();
  const config = loadBudgetConfig();
  const effectiveCeiling = ceiling !== null ? ceiling : config.session_ceiling;

  const writer = new FileBlackboardWriter(resolvedRoot);
  writer.write("budget/session_ceiling", effectiveCeiling, "budget-tracker");
  writer.write("budget/session_spent", 0, "budget-tracker");
  writer.write("budget/per_agent_ceiling", config.per_agent_ceiling, "budget-tracker");

  const status = buildStatus(writer, config);
  return { ok: true, status };
}

// ---------------------------------------------------------------------------
// HTTP handler
// ---------------------------------------------------------------------------

export async function budgetInitHandler(c: Context): Promise<Response> {
  // RBAC gate — mirrors make_require_rbac("POST", "/budget/init")
  const deny = checkRbac(c, "POST", "/budget/init");
  if (deny !== null) return deny;

  // Parse optional body — mirrors ops_budget_init in Python.
  // Python: ceiling = body.get("ceiling") then passes it straight to
  // init_session() with NO validation.  Any JSON value (including negative,
  // zero, float, or null) is accepted and written.  TS must match exactly.
  let ceiling: number | null = null;
  const contentType = c.req.header("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      const body = (await c.req.json()) as Record<string, unknown>;
      const raw = body["ceiling"];
      if (raw !== undefined && raw !== null) {
        // Python passes the JSON value directly — no coercion, no validation.
        // Accept any numeric value (positive, zero, negative, float).
        ceiling = Number(raw);
      }
    } catch {
      // Malformed JSON — mirrors Python's bare except; treat as no body.
    }
  }

  const result = initBudgetSession(ceiling);
  return c.json(result, 200);
}
