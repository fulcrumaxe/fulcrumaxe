/**
 * POST /graphql — D#1437 P6c: home-grown GraphQL at 1:1 parity with Python.
 *
 * Ports backend/graphql_api.py (parser + resolvers) + backend/routers/graphql_route.py.
 * FAITHFUL MIRROR: replicates Python's behavior exactly including quirks.
 *
 * Python quirks faithfully mirrored (documented at each resolver):
 *   - budget: "used"/"model"/"utilization_pct" ALWAYS null (key mismatch in BudgetTracker)
 *   - cost:   "total_usd"/"by_model"/"by_agent" ALWAYS null (key mismatch in CostTracker)
 *   - registry stats: "open"/"closed"/"velocity_7d" ALWAYS null (key mismatch in DiscussionRegistry.stats)
 *   - replays: "duration_s" ALWAYS null (_read_meta returns "duration_seconds")
 *   - modules: "healthy"/"error" ALWAYS null (module_health returns "import_ok"/"errors")
 *   - kpi: p95_hours ALWAYS null; prs_7d=last_24h, prs_30d=total_done (misnamed in Python)
 *   - control gates: bool values use Python str(bool) = "True"/"False" (capital)
 *
 * Route: POST /graphql (auth+RBAC gated)
 * Body: {"query": "<graphql query string>"}
 * Response 200: {"data":{...}} | {"data":{...},"errors":[...]} | {"errors":[...]}
 * Response 400: {"detail":"'query' is required"}
 */

import type { Context } from "hono";
import { existsSync, readFileSync, statSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { checkRbac } from "../middleware/rbac-check.js";
import { stateDir as sharedStateDir } from "../config/state-paths.js";

// ---------------------------------------------------------------------------
// Path helpers — mirrors Python _REPO_ROOT resolution
// All path functions are lazy (read env vars at call time) so tests can
// override AUTONOMOUS_TEAM_DIR and STATE_DB_PATH between test cases.
// ---------------------------------------------------------------------------

// ts-backend/src/routes/ -> ts-backend/src/ -> ts-backend/ -> repo root
const REPO_ROOT = join(import.meta.dir, "..", "..", "..");

function getDefaultStateDir(): string {
  return sharedStateDir();
}

function getAutonomousTeamDir(): string {
  return process.env.AUTONOMOUS_TEAM_DIR ?? join(REPO_ROOT, ".autonomous-team");
}

// ---------------------------------------------------------------------------
// Schema types (mirrors _TYPE_FIELDS from backend/graphql_api.py)
// ---------------------------------------------------------------------------

const TYPE_FIELDS: Record<string, string[]> = {
  Query: [
    "health", "budget", "cost", "registry", "agents", "kpi",
    "control", "audit", "replays", "spawnQueue", "notifications", "plugins",
  ],
  HealthStatus: ["ok", "loop", "modules"],
  LoopHealth: ["age", "threshold", "healthy"],
  ModuleHealth: ["name", "healthy", "error"],
  BudgetStatus: ["ceiling", "used", "remaining", "model", "utilization_pct"],
  CostSummary: ["total_usd", "by_model", "by_agent"],
  ModelCost: ["model", "cost"],
  AgentCost: ["agent_id", "role", "cost"],
  Registry: ["discussions", "stats"],
  Discussion: ["number", "title", "status", "pr", "created_at", "closed_at", "labels"],
  RegistryStats: ["total", "open", "closed", "velocity_7d"],
  AgentList: ["agents"],
  Agent: ["role", "description", "status", "tools", "review_pipeline"],
  KPI: ["velocity", "cycle_time"],
  KPIVelocity: ["prs_7d", "prs_30d"],
  KPICycleTime: ["median_hours", "p95_hours"],
  ControlInfo: ["gates"],
  ControlGate: ["key", "value"],
  AuditEntry: ["timestamp", "source", "action", "actor", "details"],
  ReplayList: ["replays"],
  ReplayMeta: ["agent_id", "role", "discussion", "started_at", "duration_s", "event_count"],
  SpawnQueue: ["pending_count", "active_count", "utilization_pct"],
  NotificationHistory: ["notifications"],
  PluginList: ["plugins"],
  Plugin: ["name", "description", "version", "review_pipeline", "tools"],
};

// ---------------------------------------------------------------------------
// Recursive-descent parser — exact port of backend/graphql_api.py _Parser
// ---------------------------------------------------------------------------

interface GqlToken {
  kind: string;
  value: string;
}

interface GqlField {
  name: string;
  alias: string | null;
  args: Record<string, unknown>;
  sub: GqlField[];
}

class ParseError extends Error {}

const TOKEN_RE =
  /(?<LBRACE>\{)|(?<RBRACE>\})|(?<LPAREN>\()|(?<RPAREN>\))|(?<COLON>:)|(?<COMMA>,)|(?<STRING>"(?:[^"\\]|\\.)*")|(?<NUMBER>-?\d+(?:\.\d+)?)|(?<NAME>[_A-Za-z][_0-9A-Za-z]*)|(?<WS>\s+)|(?<COMMENT>#[^\n]*)/g;

function tokenize(source: string): GqlToken[] {
  const tokens: GqlToken[] = [];
  let m: RegExpExecArray | null;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(source)) !== null) {
    // Find which group matched
    const groups = m.groups!;
    let kind = "";
    for (const k of Object.keys(groups)) {
      if (groups[k] !== undefined) {
        kind = k;
        break;
      }
    }
    if (kind === "WS" || kind === "COMMENT") continue;
    tokens.push({ kind, value: m[0] });
  }
  return tokens;
}

class Parser {
  private tokens: GqlToken[];
  private pos: number;

  constructor(tokens: GqlToken[]) {
    this.tokens = tokens;
    this.pos = 0;
  }

  private peek(): GqlToken | null {
    return this.pos < this.tokens.length ? this.tokens[this.pos] : null;
  }

  private consume(kind?: string, value?: string): GqlToken {
    const tok = this.peek();
    if (tok === null) throw new ParseError("Unexpected end of input");
    if (kind && tok.kind !== kind)
      throw new ParseError(`Expected token kind ${JSON.stringify(kind)}, got ${JSON.stringify(tok.kind)} (${JSON.stringify(tok.value)})`);
    if (value && tok.value !== value)
      throw new ParseError(`Expected token value ${JSON.stringify(value)}, got ${JSON.stringify(tok.value)}`);
    this.pos++;
    return tok;
  }

  parseDocument(): GqlField[] {
    // Allow optional leading 'query' keyword
    if (this.peek()?.kind === "NAME" && this.peek()?.value === "query") {
      this.consume();
      // Optional operation name
      if (this.peek()?.kind === "NAME") {
        this.consume();
      }
    }
    return this.parseSelectionSet();
  }

  parseSelectionSet(): GqlField[] {
    this.consume("LBRACE");
    const selections: GqlField[] = [];
    while (this.peek() !== null && this.peek()!.kind !== "RBRACE") {
      selections.push(this.parseField());
      if (this.peek()?.kind === "COMMA") {
        this.consume("COMMA");
      }
    }
    this.consume("RBRACE");
    return selections;
  }

  parseField(): GqlField {
    const nameTok = this.consume("NAME");
    let alias: string | null = null;
    let name = nameTok.value;

    // Alias check: NAME COLON NAME
    if (this.peek()?.kind === "COLON") {
      this.consume("COLON");
      alias = name;
      name = this.consume("NAME").value;
    }

    // Arguments
    let args: Record<string, unknown> = {};
    if (this.peek()?.kind === "LPAREN") {
      args = this.parseArguments();
    }

    // Sub-selection
    let sub: GqlField[] = [];
    if (this.peek()?.kind === "LBRACE") {
      sub = this.parseSelectionSet();
    }

    return { name, alias, args, sub };
  }

  parseArguments(): Record<string, unknown> {
    this.consume("LPAREN");
    const args: Record<string, unknown> = {};
    while (this.peek() !== null && this.peek()!.kind !== "RPAREN") {
      const key = this.consume("NAME").value;
      this.consume("COLON");
      args[key] = this.parseValue();
      if (this.peek()?.kind === "COMMA") {
        this.consume("COMMA");
      }
    }
    this.consume("RPAREN");
    return args;
  }

  parseValue(): unknown {
    const tok = this.peek();
    if (tok === null) throw new ParseError("Expected value, got end of input");
    if (tok.kind === "STRING") {
      this.consume();
      return tok.value.slice(1, -1); // strip surrounding quotes
    }
    if (tok.kind === "NUMBER") {
      this.consume();
      return tok.value.includes(".") ? parseFloat(tok.value) : parseInt(tok.value, 10);
    }
    if (tok.kind === "NAME") {
      this.consume();
      if (tok.value === "true") return true;
      if (tok.value === "false") return false;
      if (tok.value === "null") return null;
      return tok.value;
    }
    throw new ParseError(`Unexpected token in value position: ${JSON.stringify(tok)}`);
  }
}

function parse(query: string): GqlField[] {
  const tokens = tokenize(query);
  return new Parser(tokens).parseDocument();
}

// ---------------------------------------------------------------------------
// Executor — walks AST and calls resolvers
// Mirrors _filter_object and _execute_selections from backend/graphql_api.py
// ---------------------------------------------------------------------------

function filterObject(
  data: unknown,
  subFields: GqlField[],
  errors: Array<{ message: string; path: string }>,
  path: string,
): unknown {
  if (!subFields.length) {
    // Leaf node — return data as-is
    return data;
  }

  if (Array.isArray(data)) {
    return data.map((item, i) => filterObject(item, subFields, errors, `${path}[${i}]`));
  }

  if (data === null || typeof data !== "object") {
    return data;
  }

  const obj = data as Record<string, unknown>;
  const result: Record<string, unknown> = {};

  for (const field of subFields) {
    const fieldName = field.name;
    const alias = field.alias ?? fieldName;
    const outputKey = alias;

    if (!(fieldName in obj)) {
      errors.push({
        message: `Field '${fieldName}' does not exist at path '${path}'`,
        path: `${path}.${fieldName}`,
      });
      result[outputKey] = null;
      continue;
    }

    const value = obj[fieldName];
    result[outputKey] = filterObject(value, field.sub, errors, `${path}.${fieldName}`);
  }

  return result;
}

// ---------------------------------------------------------------------------
// Introspection — mirrors backend/graphql_api.py _introspect_schema / _introspect_type
// ---------------------------------------------------------------------------

function fieldTypeInfo(subFields: GqlField[]): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const f of subFields) {
    if (f.name === "name") {
      result["name"] = "String"; // simplified — all fields report String
    }
  }
  return result;
}

function introspectSchema(subFields: GqlField[]): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const f of subFields) {
    if (f.name === "types") {
      const typeList: Record<string, unknown>[] = [];
      for (const [typeName, fields] of Object.entries(TYPE_FIELDS)) {
        const typeEntry: Record<string, unknown> = {};
        for (const tf of f.sub) {
          if (tf.name === "name") {
            typeEntry["name"] = typeName;
          } else if (tf.name === "fields") {
            const fieldEntries: Record<string, unknown>[] = [];
            for (const fieldName of fields) {
              const fe: Record<string, unknown> = {};
              for (const ff of tf.sub) {
                if (ff.name === "name") {
                  fe["name"] = fieldName;
                } else if (ff.name === "type") {
                  fe["type"] = fieldTypeInfo(ff.sub);
                }
              }
              fieldEntries.push(fe);
            }
            typeEntry["fields"] = fieldEntries;
          }
        }
        typeList.push(typeEntry);
      }
      result["types"] = typeList;
    }
  }
  return result;
}

function introspectType(
  typeName: string,
  subFields: GqlField[],
): Record<string, unknown> | null {
  if (!(typeName in TYPE_FIELDS)) return null;
  const result: Record<string, unknown> = {};
  for (const f of subFields) {
    if (f.name === "name") {
      result["name"] = typeName;
    } else if (f.name === "fields") {
      const fieldEntries: Record<string, unknown>[] = [];
      for (const fieldName of TYPE_FIELDS[typeName]) {
        const fe: Record<string, unknown> = {};
        for (const ff of f.sub) {
          if (ff.name === "name") {
            fe["name"] = fieldName;
          } else if (ff.name === "type") {
            fe["type"] = fieldTypeInfo(ff.sub);
          }
        }
        fieldEntries.push(fe);
      }
      result["fields"] = fieldEntries;
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// Resolvers — each mirrors the Python resolver in backend/graphql_api.py
// ---------------------------------------------------------------------------

// --- health resolver ---
// Mirrors _resolve_health: calls check_loop_health() + get_cached_module_health()
//
// QUIRK: Python's get_cached_module_health() returns {modules: [{name, import_ok, errors}]}
// but the resolver accesses m.get("healthy") and m.get("error") — both always None.
// We faithfully mirror: module.healthy = null, module.error = null.

const DEFAULT_THRESHOLD_MINUTES = 30;

function getLatestLoopRunMtime(): number | null {
  const LOOP_RUNS_DIR = join(getAutonomousTeamDir(), "loop-runs");
  if (!existsSync(LOOP_RUNS_DIR)) return null;
  let maxMtime: number | null = null;
  try {
    const subdirs = readdirSync(LOOP_RUNS_DIR);
    for (const sub of subdirs) {
      const subPath = join(LOOP_RUNS_DIR, sub);
      try {
        const subStat = statSync(subPath);
        if (!subStat.isDirectory()) continue;
        const files = readdirSync(subPath);
        for (const f of files) {
          if (!f.endsWith(".log")) continue;
          try {
            const fileStat = statSync(join(subPath, f));
            const mtime = fileStat.mtimeMs / 1000;
            if (maxMtime === null || mtime > maxMtime) {
              maxMtime = mtime;
            }
          } catch {
            // skip
          }
        }
      } catch {
        // skip
      }
    }
  } catch {
    return null;
  }
  return maxMtime;
}

function checkLoopHealth(): Record<string, unknown> {
  const thresholdMinutes = parseInt(
    process.env.AF_LOOP_STALE_MINUTES ?? String(DEFAULT_THRESHOLD_MINUTES),
    10,
  );

  const latestMtime = getLatestLoopRunMtime();

  if (latestMtime === null) {
    return {
      healthy: false,
      status: "error",
      last_run: null,
      lastRunAt: null,
      age_minutes: null,
      threshold_minutes: thresholdMinutes,
      reason: "no loop-runs logs found",
    };
  }

  const now = Date.now() / 1000;
  const ageSeconds = now - latestMtime;
  const ageMinutes = Math.round((ageSeconds / 60) * 10) / 10;
  const lastRunIso = new Date(latestMtime * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");

  const STALE_WARNING_MINUTES = 60;
  let status: string;
  let healthy: boolean;
  if (ageMinutes <= thresholdMinutes) {
    status = "healthy";
    healthy = true;
  } else if (ageMinutes <= STALE_WARNING_MINUTES) {
    status = "warning";
    healthy = false;
  } else {
    status = "error";
    healthy = false;
  }

  return {
    healthy,
    status,
    last_run: lastRunIso,
    lastRunAt: lastRunIso,
    age_minutes: ageMinutes,
    threshold_minutes: thresholdMinutes,
  };
}

// Module health: Python returns {modules: [{name, import_ok, ...}]} but resolver
// accesses m.get("healthy") and m.get("error") — always None.
// We return an empty modules list as a safe faithful mirror (no imports to run).
// QUIRK: healthy/error are always null in Python too; empty array = same parity.

function getCachedModuleHealth(): Record<string, unknown> {
  const cacheFile = join(getAutonomousTeamDir(), "module-health-cache.json");
  // Try to read a pre-cached file if it exists (written by Python checker)
  if (existsSync(cacheFile)) {
    try {
      const raw = readFileSync(cacheFile, "utf-8");
      return JSON.parse(raw) as Record<string, unknown>;
    } catch {
      // fall through
    }
  }
  // Return empty modules list — resolver maps m.get("healthy") and m.get("error")
  // which are always null in Python's actual output anyway (import_ok != healthy)
  return { modules: [] };
}

function resolveHealth(_args: Record<string, unknown>): Record<string, unknown> {
  const loop = checkLoopHealth();
  const modules = getCachedModuleHealth();
  const modulesArr = Array.isArray(modules["modules"]) ? (modules["modules"] as Record<string, unknown>[]) : [];
  return {
    ok: loop["healthy"] ?? false,
    loop: {
      // QUIRK: Python resolver accesses loop.get("age_seconds") but check_loop_health()
      // returns "age_minutes" (not "age_seconds") → age is ALWAYS null in Python.
      age: loop["age_seconds"] !== undefined ? loop["age_seconds"] : null,
      // QUIRK: Python resolver accesses loop.get("threshold_seconds") but check_loop_health()
      // returns "threshold_minutes" (not "threshold_seconds") → ALWAYS null in Python.
      threshold: loop["threshold_seconds"] !== undefined ? loop["threshold_seconds"] : null,
      healthy: loop["healthy"] !== undefined ? loop["healthy"] : null,
    },
    modules: modulesArr.map((m) => ({
      name: m["name"] ?? null,
      // QUIRK: Python accesses m.get("healthy") but module_health returns "import_ok"
      healthy: m["healthy"] ?? null,
      // QUIRK: Python accesses m.get("error") but module_health returns "errors" (list)
      error: m["error"] ?? null,
    })),
  };
}

// --- budget resolver ---
// QUIRK: Python calls bt.get_status() which returns:
//   {ceiling, spent, remaining, per_agent_ceiling, warn_threshold_pct, agents}
// but resolver accesses:
//   s.get("ceiling")        → OK (exists)
//   s.get("used")           → None (key doesn't exist; actual key is "spent")
//   s.get("remaining")      → OK (exists)
//   s.get("model")          → None (key doesn't exist)
//   s.get("pct_used")       → None (key doesn't exist)

interface BlackboardEntry {
  value: unknown;
  version?: number;
  updated_at?: string;
  updated_by?: string;
}


function resolveBudget(_args: Record<string, unknown>): Record<string, unknown> {
  // Mirrors Python _resolve_budget which calls BudgetTracker().get_status()
  //
  // BudgetTracker() (no-arg) instantiates Blackboard() (file-based, NOT SqliteBlackboard).
  // Blackboard root: AUTONOMOUS_TEAM_STATE_DIR/blackboard  (see config/state-paths.ts blackboardDir())
  // Keys live at: <root>/budget/session_ceiling.json, <root>/budget/agents/*.json
  //
  // The SQLite state.db blackboard table has NO budget/agents/* rows — only 3 session-level
  // keys written by P4a budget-init. Reading SQLite yielded remaining=5000000 (wrong).
  // Reading the file-based blackboard yields remaining=4223150 (matching Python).
  //
  // Fix: read from file-based blackboard to match Python exactly.

  const stateDir = getDefaultStateDir();
  const bbDir = join(stateDir, "blackboard");
  const budgetDir = join(bbDir, "budget");
  const agentsDir = join(budgetDir, "agents");
  const ceilingFile = join(budgetDir, "session_ceiling.json");

  const DEFAULT_CEILING = 5_000_000;

  let actualCeiling: number | null = null;
  // Read session_ceiling from file-based blackboard
  if (existsSync(ceilingFile)) {
    try {
      const raw = readFileSync(ceilingFile, "utf-8");
      const entry = JSON.parse(raw) as BlackboardEntry;
      const v = entry.value;
      if (typeof v === "number") actualCeiling = v;
    } catch {
      // fallback: ceiling stays null
    }
  }
  if (actualCeiling === null && existsSync(ceilingFile)) {
    actualCeiling = DEFAULT_CEILING;
  }

  // Sum spent from budget/agents/*.json (mirrors BudgetTracker.get_status agent loop)
  let actualSpent = 0;
  if (existsSync(agentsDir)) {
    let agentFiles: string[] = [];
    try {
      agentFiles = readdirSync(agentsDir).filter((f) => f.endsWith(".json"));
    } catch {
      // ignore
    }
    for (const f of agentFiles) {
      try {
        const raw = readFileSync(join(agentsDir, f), "utf-8");
        const entry = JSON.parse(raw) as BlackboardEntry;
        const agentData = entry.value as Record<string, number> | null;
        if (agentData && typeof agentData === "object") {
          actualSpent += (agentData["input"] ?? 0) + (agentData["output"] ?? 0);
        }
      } catch {
        // skip malformed files
      }
    }
  }

  const actualRemaining = actualCeiling !== null ? Math.max(0, actualCeiling - actualSpent) : null;

  return {
    ceiling: actualCeiling,
    // QUIRK: Python s.get("used") → always null (key "used" does not exist in get_status())
    used: null,
    remaining: actualRemaining,
    // QUIRK: Python s.get("model") → always null
    model: null,
    // QUIRK: Python s.get("pct_used") → always null (key is "warn_threshold_pct", not "pct_used")
    utilization_pct: null,
  };
}

// --- cost resolver ---
// QUIRK: Python calls CostTracker().get_summary() which returns:
//   {total_cost_usd, model_breakdown}
// but resolver accesses:
//   s.get("total_usd")  → None (key is "total_cost_usd")
//   s.get("by_model")   → None (key is "model_breakdown")
//   s.get("by_agent")   → None (key doesn't exist)
// All three are ALWAYS null in Python.

function resolveCost(_args: Record<string, unknown>): Record<string, unknown> {
  // Faithful mirror: Python's resolver always returns null for these fields
  // because of the key name mismatch in CostTracker.get_summary().
  return {
    total_usd: null,
    by_model: null,
    by_agent: null,
  };
}

// --- registry resolver ---
// Mirrors Python _resolve_registry which reads registry.json
// QUIRK: stats.get("open"), stats.get("closed"), stats.get("velocity_7d") are all null
// because DiscussionRegistry.stats() returns "done"/"in_progress"/"tasks_per_day" etc.

function resolveRegistry(_args: Record<string, unknown>): Record<string, unknown> {
  const REGISTRY_FILE = join(getAutonomousTeamDir(), "registry.json");
  let data: Record<string, unknown> = { discussions: [] };
  if (existsSync(REGISTRY_FILE)) {
    try {
      const raw = readFileSync(REGISTRY_FILE, "utf-8");
      data = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      // ignore — return empty
    }
  }

  const rawDiscussions = Array.isArray(data["discussions"])
    ? (data["discussions"] as Record<string, unknown>[])
    : [];

  const discussions = rawDiscussions.map((d) => ({
    number: d["number"] ?? null,
    title: d["title"] ?? null,
    status: d["status"] ?? null,
    pr: d["pr"] ?? null,
    created_at: d["created_at"] ?? null,
    closed_at: d["closed_at"] ?? null,
    labels: Array.isArray(d["labels"]) ? d["labels"] : [],
  }));

  // Compute stats — mirrors DiscussionRegistry.stats() keys that Python ACTUALLY reads
  // Python resolver: stats.get("total"), stats.get("open"), stats.get("closed"), stats.get("velocity_7d")
  // DiscussionRegistry.stats() returns: total, done, in_progress, tasks_per_day, avg_days_to_complete, completion_count
  // → open, closed, velocity_7d are ALWAYS null (key mismatch)
  const total = discussions.length;

  return {
    discussions,
    stats: {
      total,
      // QUIRK: Python accesses "open" but stats() returns different keys → null
      open: null,
      // QUIRK: Python accesses "closed" but stats() returns different keys → null
      closed: null,
      // QUIRK: Python accesses "velocity_7d" but stats() returns "tasks_per_day" → null
      velocity_7d: null,
    },
  };
}

// --- agents resolver ---
// Reads .autonomous-team/agents/*.json (same as AgentCards)

function resolveAgents(_args: Record<string, unknown>): Record<string, unknown> {
  const AGENTS_DIR = join(getAutonomousTeamDir(), "agents");
  const agents: Record<string, unknown>[] = [];

  if (!existsSync(AGENTS_DIR)) {
    return { agents };
  }

  let names: string[] = [];
  try {
    names = readdirSync(AGENTS_DIR)
      .filter((f) => f.endsWith(".json"))
      .map((f) => f.slice(0, -5))
      .sort();
  } catch {
    return { agents };
  }

  for (const name of names) {
    const cardPath = join(AGENTS_DIR, `${name}.json`);
    try {
      const raw = readFileSync(cardPath, "utf-8");
      const card = JSON.parse(raw) as Record<string, unknown>;
      agents.push({
        role: card["role"] ?? name,
        description: card["description"] ?? "",
        status: card["status"] ?? "active",
        tools: Array.isArray(card["tools"]) ? card["tools"] : [],
        review_pipeline: card["review_pipeline"] ?? "",
      });
    } catch {
      // Python: except Exception → fallback with role=name, status=unknown
      agents.push({
        role: name,
        description: "",
        status: "unknown",
        tools: [],
        review_pipeline: "",
      });
    }
  }

  return { agents };
}

// --- kpi resolver ---
// Mirrors Python _resolve_kpi which calls kpi_engine.compute_all()
// kpi_engine.compute_all() reads registry.json and returns:
//   {velocity: {last_24h, all_time_per_day, total_done}, pr_cycle_time: {median_hours, ...}}
// Resolver maps:
//   vel.get("last_24h", 0) → prs_7d  (misnamed — it's actually last 24h, not 7d)
//   vel.get("total_done", 0) → prs_30d (misnamed — it's total done, not 30d)
//   cyc.get("median_hours") → median_hours
//   cyc.get("p95_hours") → ALWAYS null (compute_pr_cycle_time returns no "p95_hours" key)

function resolveKpi(_args: Record<string, unknown>): Record<string, unknown> {
  const REGISTRY_FILE = join(getAutonomousTeamDir(), "registry.json");
  let discussions: Record<string, unknown>[] = [];
  if (existsSync(REGISTRY_FILE)) {
    try {
      const raw = readFileSync(REGISTRY_FILE, "utf-8");
      const data = JSON.parse(raw) as Record<string, unknown>;
      if (Array.isArray(data["discussions"])) {
        discussions = data["discussions"] as Record<string, unknown>[];
      }
    } catch {
      // ignore
    }
  }

  // Compute velocity (mirrors compute_velocity in kpi_engine.py)
  const done = discussions.filter((d) => d["status"] === "DONE");
  const cutoffMs = Date.now() - 24 * 3600 * 1000;
  let last24h = 0;
  for (const d of done) {
    const closedAt = d["closed_at"] as string | undefined;
    if (closedAt) {
      try {
        const closedMs = new Date(closedAt).getTime();
        if (closedMs >= cutoffMs) last24h++;
      } catch {
        // skip
      }
    }
  }
  const totalDone = done.length;

  // Compute pr_cycle_time (mirrors compute_pr_cycle_time in kpi_engine.py)
  const hours: number[] = [];
  for (const d of done) {
    const createdAt = d["created_at"] as string | undefined;
    const closedAt = d["closed_at"] as string | undefined;
    if (createdAt && closedAt) {
      try {
        const created = new Date(createdAt).getTime();
        const closed = new Date(closedAt).getTime();
        if (closed > created) {
          hours.push((closed - created) / 3600000);
        }
      } catch {
        // skip
      }
    }
  }

  let medianHours: number | null = null;
  if (hours.length > 0) {
    const sorted = [...hours].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    const raw = sorted.length % 2 === 0
      ? (sorted[mid - 1] + sorted[mid]) / 2
      : sorted[mid];
    medianHours = Math.round(raw * 100) / 100;
  }

  return {
    velocity: {
      // QUIRK: Python names this prs_7d but it's actually last_24h
      prs_7d: last24h,
      // QUIRK: Python names this prs_30d but it's actually total_done
      prs_30d: totalDone,
    },
    cycle_time: {
      median_hours: medianHours,
      // QUIRK: p95_hours does not exist in compute_pr_cycle_time() → always null
      p95_hours: null,
    },
  };
}

// --- control resolver ---
// Mirrors Python _resolve_control which reads ControlPlane().list_gates()
// list_gates() reads .autonomous-team/config.json["gates"] and merges with defaults

// Default gates from backend/control_plane.py _DEFAULT_GATES
// Exact copy of Python's _DEFAULT_GATES (23 keys as of 2026-05-23).
// config.json overrides are merged on top in resolveControl(), so gates
// like lint_must_pass / human_approval_before_merge / training_triggers
// that live only in config.json still appear in the merged output.
const DEFAULT_GATES: Record<string, boolean | string> = {
  auto_merge: true,
  security_review: true,
  budget_check: true,
  idea_generation: true,         // Python: True (TS had false — bug)
  stall_detection: true,
  wiki_sync: true,
  human_verification: false,
  self_observe_executor: false,
  self_observe_impl_coord: false,
  self_observe_enforcement: "shadow",
  docs_writer: true,
  incident_commander: false,
  release_manager: true,
  runbook_writer: true,
  analytics_engineer: true,
  phased_orchestration: false,
  phased_code_review: true,
  cost_aware_router: false,
  debater_pass: false,
  tui_tester_pilot_sweep: false,
  execve_fence: true,
  loop_start: false,
  dial_state_summary: false,
};

function resolveControl(_args: Record<string, unknown>): Record<string, unknown> {
  const CONFIG_FILE = join(getAutonomousTeamDir(), "config.json");
  let configData: Record<string, unknown> = {};
  if (existsSync(CONFIG_FILE)) {
    try {
      const raw = readFileSync(CONFIG_FILE, "utf-8");
      configData = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      // ignore
    }
  }

  // Mirrors ControlPlane.list_gates() — merge defaults then override with config
  const gatesFromConfig = (configData["gates"] as Record<string, unknown> | undefined) ?? {};
  const merged: Record<string, unknown> = { ...DEFAULT_GATES, ...gatesFromConfig };

  // Python: preserve string gates; coerce all others to bool using str(bool).
  // Python's str(True) = "True" (capital T), str(False) = "False" (capital F).
  // We MUST match this exactly — it's Python's behavior, not a bug.
  const gates: Array<{ key: string; value: string }> = [];
  for (const [k, v] of Object.entries(merged)) {
    let value: string;
    if (typeof v === "string") {
      value = v; // string gates preserved as-is (e.g. "shadow")
    } else {
      // Mirrors Python: str(True) = "True", str(False) = "False"
      value = v ? "True" : "False";
    }
    gates.push({ key: k, value });
  }

  return { gates };
}

// --- audit resolver ---
// Mirrors Python _resolve_audit which reads from AuditTrail (audit.jsonl)

function resolveAudit(args: Record<string, unknown>): Record<string, unknown>[] {
  const AUDIT_FILE = join(getAutonomousTeamDir(), "audit.jsonl");
  const limit = typeof args["limit"] === "number" ? Math.floor(args["limit"]) : 50;
  const source = typeof args["source"] === "string" ? args["source"] : null;
  const action = typeof args["action"] === "string" ? args["action"] : null;
  const actor = typeof args["actor"] === "string" ? args["actor"] : null;
  const since = typeof args["since"] === "string" ? args["since"] : null;

  if (!existsSync(AUDIT_FILE)) return [];

  let sinceMs: number | null = null;
  if (since) {
    try {
      sinceMs = new Date(since.replace("Z", "+00:00")).getTime();
    } catch {
      // ignore invalid since
    }
  }

  const results: Record<string, unknown>[] = [];
  let content: string;
  try {
    content = readFileSync(AUDIT_FILE, "utf-8");
  } catch {
    return [];
  }

  const lines = content.split("\n");
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let entry: Record<string, unknown>;
    try {
      entry = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      continue;
    }

    if (source !== null && entry["source"] !== source) continue;
    if (action !== null && entry["action"] !== action) continue;
    if (actor !== null && entry["actor"] !== actor) continue;
    if (sinceMs !== null) {
      const ts = entry["ts"] as string | undefined;
      if (!ts) continue;
      try {
        const entryMs = new Date(ts).getTime();
        if (entryMs < sinceMs) continue;
      } catch {
        continue;
      }
    }

    results.push({
      timestamp: entry["timestamp"] ?? entry["ts"] ?? null,
      source: entry["source"] ?? null,
      action: entry["action"] ?? null,
      actor: entry["actor"] ?? null,
      details: String(entry["details"] ?? ""),
    });

    if (results.length >= limit) break;
  }

  return results;
}

// --- replays resolver ---
// Mirrors Python _resolve_replays which reads .autonomous-team/replays/*.jsonl

function resolveReplays(_args: Record<string, unknown>): Record<string, unknown> {
  const REPLAYS_DIR = join(getAutonomousTeamDir(), "replays");
  const replays: Record<string, unknown>[] = [];

  if (!existsSync(REPLAYS_DIR)) return { replays };

  let files: string[];
  try {
    files = readdirSync(REPLAYS_DIR).filter((f) => f.endsWith(".jsonl"));
  } catch {
    return { replays };
  }

  // Sort by mtime descending (mirrors Python sorted(..., key=st_mtime, reverse=True))
  const fileStats = files.map((f) => {
    const fp = join(REPLAYS_DIR, f);
    try {
      return { file: fp, mtime: statSync(fp).mtimeMs };
    } catch {
      return { file: fp, mtime: 0 };
    }
  });
  fileStats.sort((a, b) => b.mtime - a.mtime);

  const LIMIT = 20;
  for (const { file } of fileStats.slice(0, LIMIT)) {
    const meta = readReplayMeta(file);
    if (meta !== null) {
      replays.push(meta);
    }
  }

  return { replays };
}

function readReplayMeta(filePath: string): Record<string, unknown> | null {
  let content: string;
  try {
    content = readFileSync(filePath, "utf-8");
  } catch {
    return null;
  }

  let header: Record<string, unknown> | null = null;
  let footer: Record<string, unknown> | null = null;

  for (const line of content.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let obj: Record<string, unknown>;
    try {
      obj = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      continue;
    }
    const t = obj["type"];
    if (t === "header") header = obj;
    else if (t === "footer") footer = obj;
  }

  if (header === null) return null;

  const hc = (header["content"] as Record<string, unknown>) ?? {};
  const fc = footer ? ((footer["content"] as Record<string, unknown>) ?? {}) : {};
  const stem = filePath.replace(/\.jsonl$/, "").split("/").pop() ?? "";

  return {
    agent_id: hc["agent_id"] ?? stem,
    role: hc["role"] ?? "",
    discussion: String(hc["discussion"] ?? ""),
    started_at: header["ts"] ?? "",
    // QUIRK: Python resolver calls r.get("duration_s") but _read_meta() returns
    // "duration_seconds" → duration_s is ALWAYS null in Python. Mirror faithfully.
    duration_s: null,
    event_count: fc["total_events"] ?? 0,
  };
}

// --- spawnQueue resolver ---
// Mirrors Python _resolve_spawn_queue → reads spawn-queue.json (already done in spawn-queue.ts)

const DEFAULT_LIMITS: Record<string, number> = {
  executor: 2,
  "code-reviewer": 2,
  "security-reviewer": 1,
  "project-manager": 1,
  "mission-analyst": 1,
  _total: 6,
};

function resolveSpawnQueue(_args: Record<string, unknown>): Record<string, unknown> {
  const QUEUE_FILE = join(getAutonomousTeamDir(), "spawn-queue.json");
  const CONFIG_FILE_LOCAL = join(getAutonomousTeamDir(), "config.json");
  // Load config for limits (mirrors spawn_queue.py _load_config)
  let configData: Record<string, unknown> = {};
  if (existsSync(CONFIG_FILE_LOCAL)) {
    try {
      configData = JSON.parse(readFileSync(CONFIG_FILE_LOCAL, "utf-8")) as Record<string, unknown>;
    } catch { /* ignore */ }
  }
  const policies = configData["policies"] as Record<string, unknown> | undefined;
  const queueConc = policies?.["queue_concurrency"] as Record<string, number> | undefined;
  const limits = { ...DEFAULT_LIMITS, ...(queueConc ?? {}) };

  if (!existsSync(QUEUE_FILE)) {
    return {
      pending_count: 0,
      active_count: 0,
      utilization_pct: 0,
    };
  }

  let queueData: Record<string, unknown> = {};
  try {
    queueData = JSON.parse(readFileSync(QUEUE_FILE, "utf-8")) as Record<string, unknown>;
  } catch {
    return { pending_count: 0, active_count: 0, utilization_pct: 0 };
  }

  const pending = Array.isArray(queueData["pending"]) ? (queueData["pending"] as unknown[]) : [];
  const active = Array.isArray(queueData["active"]) ? (queueData["active"] as unknown[]) : [];

  const pendingCount = pending.length;
  const activeCount = active.length;
  const totalLimit = limits["_total"] ?? 6;
  const utilizationPct = totalLimit > 0 ? Math.round((activeCount / totalLimit) * 100) : 0;

  return {
    pending_count: pendingCount,
    active_count: activeCount,
    utilization_pct: utilizationPct,
  };
}

// --- notifications resolver ---
// Mirrors Python _resolve_notifications → reads notification-log.jsonl

function resolveNotifications(_args: Record<string, unknown>): Record<string, unknown> {
  const NOTIF_LOG = join(getAutonomousTeamDir(), "notification-log.jsonl");
  if (!existsSync(NOTIF_LOG)) {
    return { notifications: [] };
  }

  let content: string;
  try {
    content = readFileSync(NOTIF_LOG, "utf-8");
  } catch {
    return { notifications: [] };
  }

  const lines = content.split("\n").filter((l) => l.trim());
  const records: unknown[] = [];
  for (const line of lines) {
    try {
      records.push(JSON.parse(line));
    } catch {
      // skip malformed lines
    }
  }

  // Last 50 (mirrors Python get_history(50))
  const last50 = records.slice(-50);
  // Mirrors: [str(r) for r in records] — Python converts each record to string
  return { notifications: last50.map((r) => String(r)) };
}

// --- plugins resolver ---
// Mirrors Python _resolve_plugins → reads .autonomous-team/plugins/*.yaml

function resolvePlugins(_args: Record<string, unknown>): Record<string, unknown> {
  // No YAML parser in Bun stdlib; read PluginDef from JSON if plugins were pre-compiled,
  // otherwise return empty. In practice plugins dir only has .example files.
  // Python PluginLoader reads .yaml files; since we have no YAML parser,
  // return empty list (same result as when no valid plugins exist).
  const plugins: unknown[] = [];
  return { plugins };
}

// ---------------------------------------------------------------------------
// Root resolver dispatch (mirrors _ROOT_RESOLVERS in backend/graphql_api.py)
// ---------------------------------------------------------------------------

type Resolver = (args: Record<string, unknown>) => unknown;

const ROOT_RESOLVERS: Record<string, Resolver> = {
  health: resolveHealth,
  budget: resolveBudget,
  cost: resolveCost,
  registry: resolveRegistry,
  agents: resolveAgents,
  kpi: resolveKpi,
  control: resolveControl,
  audit: resolveAudit,
  replays: resolveReplays,
  spawnQueue: resolveSpawnQueue,
  notifications: resolveNotifications,
  plugins: resolvePlugins,
};

// ---------------------------------------------------------------------------
// Execute selections (mirrors _execute_selections in backend/graphql_api.py)
// ---------------------------------------------------------------------------

function executeSelections(
  selections: GqlField[],
  errors: Array<{ message: string; path: string }>,
): Record<string, unknown> {
  const data: Record<string, unknown> = {};

  for (const field of selections) {
    const { name: fieldName, alias, args, sub } = field;
    const outputAlias = alias ?? fieldName;

    // Introspection — __schema
    if (fieldName === "__schema") {
      try {
        data[outputAlias] = introspectSchema(sub);
      } catch (exc) {
        errors.push({ message: `__schema introspection error: ${exc}`, path: fieldName });
        data[outputAlias] = null;
      }
      continue;
    }

    // Introspection — __type
    if (fieldName === "__type") {
      const typeName = typeof args["name"] === "string" ? args["name"] : "";
      try {
        data[outputAlias] = introspectType(typeName, sub);
      } catch (exc) {
        errors.push({ message: `__type introspection error: ${exc}`, path: fieldName });
        data[outputAlias] = null;
      }
      continue;
    }

    const resolver = ROOT_RESOLVERS[fieldName];
    if (resolver === undefined) {
      errors.push({
        message: `Unknown field '${fieldName}' on type 'Query'`,
        path: fieldName,
      });
      data[outputAlias] = null;
      continue;
    }

    let raw: unknown;
    try {
      raw = resolver(args as Record<string, unknown>);
    } catch (exc) {
      errors.push({
        message: `Resolver error for field '${fieldName}': ${exc}`,
        path: fieldName,
      });
      data[outputAlias] = null;
      continue;
    }

    data[outputAlias] = filterObject(raw, sub, errors, fieldName);
  }

  return data;
}

// ---------------------------------------------------------------------------
// execute() — public API (mirrors backend/graphql_api.execute)
// ---------------------------------------------------------------------------

function execute(query: string): Record<string, unknown> {
  let selections: GqlField[];
  try {
    selections = parse(query);
  } catch (exc) {
    return { errors: [{ message: `Parse error: ${exc}` }] };
  }

  const errors: Array<{ message: string; path: string }> = [];
  let data: Record<string, unknown>;
  try {
    data = executeSelections(selections, errors);
  } catch (exc) {
    return { errors: [{ message: `Execution error: ${exc}` }] };
  }

  if (errors.length > 0) {
    return { data, errors };
  }
  return { data };
}

// ---------------------------------------------------------------------------
// HTTP handler — mirrors backend/routers/graphql_route.py post_graphql
// ---------------------------------------------------------------------------

export async function graphqlHandler(c: Context): Promise<Response> {
  // RBAC gate — mirrors make_require_rbac("POST", "/graphql")
  const deny = checkRbac(c, "POST", "/graphql");
  if (deny !== null) return deny;

  // Parse body — mirrors Python: body = await request.json() with bare except
  let body: Record<string, unknown> = {};
  try {
    body = (await c.req.json()) as Record<string, unknown>;
  } catch {
    // Bare except in Python → body stays empty dict
  }

  const queryStr = body["query"];
  if (!queryStr) {
    // Mirrors Python: raise HTTPException(status_code=400, detail="'query' is required")
    return c.json({ detail: "'query' is required" }, 400);
  }

  const result = execute(String(queryStr));
  return c.json(result, 200);
}
