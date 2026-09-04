/**
 * spawn/pre-spawn-check.ts — Pre-spawn gate: concurrency caps, spec-readiness,
 * general-purpose guard, dial check, and budget check.
 *
 * Mirrors scripts/pre-spawn-check.sh decision logic.
 *
 * The bash script shells out to Python for every sub-check; this native TS gate
 * calls the already-ported TS modules directly:
 *   - control-plane.ts  → caps, policies, dial levels
 *   - agent-run-tracker.ts → active-run counts (via DuckDB)
 *   - discussion-status.ts → missing-sections check
 *
 * CLI usage (mirrors bash):
 *   bun run ts-backend/src/spawn/pre-spawn-check.ts \
 *       --role executor \
 *       --discussion 1506 \
 *       [--subagent-type executor] \
 *       [--dry-run] \
 *       [--no-register] \
 *       [--operation-class agent.spawn]
 *
 * Exit codes:
 *   0  → spawn allowed (prints JSON with allowed:true)
 *   1  → spawn blocked (prints reason to stderr, JSON with allowed:false to stdout)
 *   2  → missing required arg or general-purpose guard tripped
 *
 * Outputs: JSON to stdout with at minimum { allowed, role, reason?, warnings[] }
 */

import { ControlPlane } from "./control-plane.js";
import { missingSections } from "./discussion-status.js";
import { readFreshBody } from "./fresh-body-read.js";
import { DuckDBInstance } from "@duckdb/node-api";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { statsDb } from "../config/state-paths.js";
import { repoRoot as resolveCheckoutRoot } from "../config/repo-root.js";

// ---------------------------------------------------------------------------
// Role → dial-class mapping (mirrors _ROLE_TO_DIAL_CLASS in dial_registry.py)
// ---------------------------------------------------------------------------

const ROLE_TO_DIAL_CLASS: Record<string, string> = {
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

// Forbidden subagent types (mirrors CLAUDE.md HARD STOPS)
const FORBIDDEN_SUBAGENT_TYPES = new Set(["general-purpose"]);

// Fleet-wide hard cap (mirrors CLAUDE.md "max 4 executors + max 4 other = 8 total")
const FLEET_CAP_DEFAULT = 8;

// ---------------------------------------------------------------------------
// DB path resolution (mirrors agent-run-tracker.ts)
// ---------------------------------------------------------------------------

function resolveDbPath(override?: string | null): string {
  if (override) return override;
  const env = process.env["STATS_DB_PATH"];
  if (env) return env;
  return statsDb();
}

// ---------------------------------------------------------------------------
// Count active runs via DuckDB (open rows: end_ts IS NULL)
// ---------------------------------------------------------------------------

async function countActiveRunsByRole(
  role: string,
  dbPathOverride?: string | null
): Promise<number> {
  const path = resolveDbPath(dbPathOverride);
  if (!existsSync(path)) return 0;
  try {
    const inst = await DuckDBInstance.create(path);
    const conn = await inst.connect();
    try {
      const stmt = await conn.prepare(
        "SELECT COUNT(*) FROM agent_run WHERE role = ? AND end_ts IS NULL"
      );
      stmt.bindVarchar(1, role);
      const result = await stmt.runAndReadAll();
      const rows = result.getRows() as unknown[][];
      return rows.length > 0 ? Number(rows[0]![0] ?? 0) : 0;
    } finally {
      try { conn.closeSync(); } catch { /* ignore */ }
      try { inst.closeSync(); } catch { /* ignore */ }
    }
  } catch {
    return 0;
  }
}

async function countAllActiveRuns(dbPathOverride?: string | null): Promise<number> {
  const path = resolveDbPath(dbPathOverride);
  if (!existsSync(path)) return 0;
  try {
    const inst = await DuckDBInstance.create(path);
    const conn = await inst.connect();
    try {
      const result = await conn.runAndReadAll(
        "SELECT COUNT(*) FROM agent_run WHERE end_ts IS NULL"
      );
      const rows = result.getRows() as unknown[][];
      return rows.length > 0 ? Number(rows[0]![0] ?? 0) : 0;
    } finally {
      try { conn.closeSync(); } catch { /* ignore */ }
      try { inst.closeSync(); } catch { /* ignore */ }
    }
  } catch {
    return 0;
  }
}

// ---------------------------------------------------------------------------
// Dial check — mirrors bash §2.6 using dial_registry.check() semantics
// ---------------------------------------------------------------------------

interface DialCheckResult {
  allowed: boolean;
  reason: string;
}

/**
 * Check the dial for dialClass at requestedLevel using the ControlPlane data.
 *
 * Mirrors dial_registry.check() logic:
 *  - Unknown class → allow at level 1, deny above.
 *  - requested_level > ceiling → deny.
 *  - current >= requested_level → allow.
 *  - current < requested_level → deny.
 */
function checkDial(
  cp: ControlPlane,
  dialClass: string,
  requestedLevel = 2
): DialCheckResult {
  const dial = cp.getDial(dialClass);
  if (!dial) {
    if (requestedLevel <= 1) {
      return {
        allowed: true,
        reason: `unknown class '${dialClass}' — default allow at level 1`,
      };
    }
    return {
      allowed: false,
      reason: `unknown class '${dialClass}' — requested level ${requestedLevel} > default 1`,
    };
  }

  const current = Number(dial["level"] ?? 0);
  const ceiling = Number(dial["ceiling"] ?? 5);

  if (requestedLevel < 1) {
    return {
      allowed: false,
      reason: `requested_level must be >= 1, got ${requestedLevel}`,
    };
  }
  if (requestedLevel > ceiling) {
    return {
      allowed: false,
      reason: `requested level ${requestedLevel} exceeds ceiling ${ceiling} for '${dialClass}'`,
    };
  }
  if (current >= requestedLevel) {
    return {
      allowed: true,
      reason: `dial '${dialClass}' at ${current} >= requested ${requestedLevel}`,
    };
  }
  return {
    allowed: false,
    reason: `dial '${dialClass}' at ${current} < requested ${requestedLevel}`,
  };
}

// ---------------------------------------------------------------------------
// Spec-readiness check (mirrors bash §4 discussion_status missing-sections)
// ---------------------------------------------------------------------------

async function checkSpecReadiness(
  discussion: number,
  injectedBody?: string
): Promise<string[]> {
  // Test injection: body provided directly — avoids all external calls.
  if (injectedBody !== undefined) {
    return missingSections(injectedBody);
  }
  // Production: fresh-read via fresh-body-read.ts (D#1794). Advisory / fail-open
  // site — a fresh read is attempted, but the disposition on any non-live outcome
  // (unavailable OR stale) stays "no missing sections found", matching this site's
  // pre-existing fail-open behavior. Only the freshness of the read changed here.
  // Delegates to config/repo-root.ts (D#1825) — this call site wants
  // repoRoot() (the checkout this process is running in), the same answer
  // the hand-rolled walk it replaces used to compute (when unbroken).
  const repoRoot = resolveCheckoutRoot();
  const read = readFreshBody(repoRoot, discussion);
  if (read.status === "unavailable") return [];
  return missingSections(read.body);
}

// ---------------------------------------------------------------------------
// Budget check — shells to budget.py (mirrors bash §1)
// ---------------------------------------------------------------------------

interface BudgetCheckResult {
  allowed: boolean;
  remaining: number;
}

function checkBudget(repoRoot: string): BudgetCheckResult {
  try {
    const budgetScript = join(repoRoot, "backend", "budget.py");
    if (!existsSync(budgetScript)) {
      return { allowed: true, remaining: 0 };
    }
    const raw = execFileSync("python3", [budgetScript, "check"], {
      timeout: 10_000,
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
    });
    const parsed = JSON.parse(raw) as { allowed?: boolean; remaining?: number };
    return {
      allowed: parsed.allowed !== false,
      remaining: Number(parsed.remaining ?? 0),
    };
  } catch {
    return { allowed: true, remaining: 0 };
  }
}

// ---------------------------------------------------------------------------
// Result types
// ---------------------------------------------------------------------------

export interface PreSpawnResult {
  allowed: boolean;
  role: string;
  discussion?: number;
  reason?: string;
  blocked_reason?: string;
  exit_code: number;
  warnings: string[];
  dial_class?: string;
  dial_level?: number;
  active_runs?: number;
  active_runs_for_role?: number;
  max_concurrent?: number;
  missing_sections?: string[];
  budget_remaining?: number;
}

// ---------------------------------------------------------------------------
// Options for runPreSpawnCheck
// ---------------------------------------------------------------------------

export interface PreSpawnOptions {
  role: string;
  discussion?: number | null;
  subagentType?: string | null;
  dryRun?: boolean;
  noRegister?: boolean;
  operationClass?: string | null;
  /**
   * When true, skip only the concurrency-cap check (Step 3: per-role + fleet-wide).
   * All other checks (budget, dial, spec-readiness) still run.
   * Mirrors bash §164-248: OVERRIDE_CAP=1 skips the §0a concurrency block entirely.
   */
  overrideCap?: boolean;
  /** Inject a Discussion body directly (for tests — avoids gh/cache calls). */
  discussionBody?: string;
  /** Override DB path for tests. */
  dbPathOverride?: string | null;
  /** Override control-plane config path for tests. */
  configPathOverride?: string | null;
  /** Override repo root for tests (for budget.py resolution). */
  repoRootOverride?: string | null;
}

// ---------------------------------------------------------------------------
// Core decision function — testable without CLI plumbing
// ---------------------------------------------------------------------------

/**
 * Run the full pre-spawn decision pipeline and return a structured result.
 *
 * Decision tree (mirrors scripts/pre-spawn-check.sh):
 *
 *  Step 0:   general-purpose subagent_type guard (exit 2)
 *  Step 0a:  role required guard (exit 1)
 *  Step 1:   budget check (gates.budget_check; shells to budget.py)
 *  Step 1.2: per-role token_cap vs remaining (informational warning if unavailable)
 *  Step 2.6: dial check (role → class → cp.getDial() → current >= requested(2))
 *  Step 3:   concurrency caps (fleet-wide + per-role from policies)
 *  Step 4:   spec-readiness (discussion_status.missingSections if discussion given)
 *  → allowed
 */
export async function runPreSpawnCheck(opts: PreSpawnOptions): Promise<PreSpawnResult> {
  const {
    role,
    discussion,
    subagentType,
    dryRun = false,
    noRegister = false,
    operationClass,
    overrideCap = false,
    discussionBody,
    dbPathOverride,
    configPathOverride,
    repoRootOverride,
  } = opts;

  const warnings: string[] = [];

  // Resolve repo root (needed for budget.py, discussion_cache.py)
  let repoRoot: string;
  if (repoRootOverride) {
    repoRoot = repoRootOverride;
  } else {
    // Delegates to config/repo-root.ts (D#1825). repoRootOverride, when
    // supplied, still wins unconditionally above — this branch is the
    // resolver's default, not a replacement for the override.
    repoRoot = resolveCheckoutRoot();
  }

  // ── Step 0: general-purpose subagent_type guard ────────────────────────────
  // Mirrors bash §107-116: subagent_type=general-purpose is forbidden.
  if (subagentType && FORBIDDEN_SUBAGENT_TYPES.has(subagentType)) {
    const result: PreSpawnResult = {
      allowed: false,
      role,
      discussion: discussion ?? undefined,
      reason: `subagent_type=${subagentType} is forbidden — use a named role (see CLAUDE.md HARD STOPS)`,
      blocked_reason: "general_purpose_forbidden",
      exit_code: 2,
      warnings,
    };
    process.stderr.write(
      `ERROR: subagent_type=general-purpose is forbidden — use a named role\n`
    );
    return result;
  }

  // ── Step 0a: role required ─────────────────────────────────────────────────
  if (!role) {
    return {
      allowed: false,
      role: "",
      reason: "--role is required",
      blocked_reason: "missing_role",
      exit_code: 1,
      warnings,
    };
  }

  // ── Load control plane ────────────────────────────────────────────────────
  const cp = new ControlPlane(configPathOverride ?? undefined);
  cp.load();

  // ── Step 1: budget check ───────────────────────────────────────────────────
  // Mirrors bash §251-269: reads budget.py check; blocked if allowed=false.
  // Gate: gates.budget_check (default true). Dry-run skips the block.
  const budgetGateOn = cp.gateEnabled("budget_check");
  let budgetRemaining = 0;
  if (budgetGateOn && !dryRun) {
    const budgetResult = checkBudget(repoRoot);
    budgetRemaining = budgetResult.remaining;
    if (!budgetResult.allowed) {
      process.stderr.write(`ERROR: budget exceeded. Spawn of ${role} blocked.\n`);
      return {
        allowed: false,
        role,
        discussion: discussion ?? undefined,
        reason: `budget exceeded (remaining: ${budgetResult.remaining})`,
        blocked_reason: "budget_exceeded",
        exit_code: 1,
        warnings,
        budget_remaining: budgetResult.remaining,
      };
    }
  }

  // ── Step 1.2: per-role token_cap check ────────────────────────────────────
  // Mirrors bash §271-291: policies.<role>.token_cap vs budget remaining.
  // Requires budget remaining which requires budget.py — document as caveat when skip.
  const rolePolicy = cp.getPolicy(role);
  const roleTokenCap = rolePolicy["token_cap"];
  if (typeof roleTokenCap === "number" && roleTokenCap > 0 && !dryRun) {
    if (budgetGateOn && budgetRemaining > 0 && budgetRemaining < roleTokenCap) {
      process.stderr.write(
        `ERROR: per-role token_cap (${roleTokenCap}) for ${role} exceeds budget remaining (${budgetRemaining}). Spawn blocked.\n`
      );
      return {
        allowed: false,
        role,
        discussion: discussion ?? undefined,
        reason: `per-role token_cap (${roleTokenCap}) exceeds budget remaining (${budgetRemaining})`,
        blocked_reason: "per_role_token_cap_exceeded",
        exit_code: 1,
        warnings,
        budget_remaining: budgetRemaining,
      };
    }
  }

  // ── Step 2.6: dial check ───────────────────────────────────────────────────
  // Mirrors bash §488-544: role → dial class → check at requested_level=2.
  const dialClass = operationClass ?? ROLE_TO_DIAL_CLASS[role] ?? "agent.spawn";
  const dialResult = checkDial(cp, dialClass, 2);

  if (!dialResult.allowed) {
    const blockedReason = `dial_denied ${dialClass} ${dialResult.reason}`;
    process.stderr.write(`blocked_reason=${blockedReason}\n`);
    process.stderr.write(
      `ERROR: dial check denied spawn of ${role} (class=${dialClass}): ${dialResult.reason}\n`
    );
    return {
      allowed: false,
      role,
      discussion: discussion ?? undefined,
      reason: dialResult.reason,
      blocked_reason: blockedReason,
      exit_code: 1,
      warnings,
      dial_class: dialClass,
    };
  }

  // ── Step 3: concurrency caps ───────────────────────────────────────────────
  // Mirrors bash §596-634:
  //   - Per-project/per-role cap: policies.<role>.max_concurrent
  //   - Fleet-wide cap: FLEET_CAP_DEFAULT (8) from backend.fleet.concurrency
  // We use agent_run DuckDB rows with end_ts IS NULL as the live count,
  // which is the same underlying source as the Python fleet concurrency module.
  let activeRuns = 0;
  let activeRunsForRole = 0;

  // overrideCap=true bypasses only the concurrency-cap block (mirrors bash OVERRIDE_CAP=1)
  if (!dryRun && !noRegister && !overrideCap) {
    try {
      activeRuns = await countAllActiveRuns(dbPathOverride);
      activeRunsForRole = await countActiveRunsByRole(role, dbPathOverride);
    } catch {
      warnings.push("agent_run DB unavailable — skipping concurrency cap check");
    }

    // Per-role cap from policies.<role>.max_concurrent
    const perRoleCap =
      typeof rolePolicy["max_concurrent"] === "number"
        ? (rolePolicy["max_concurrent"] as number)
        : null;

    if (perRoleCap !== null && activeRunsForRole >= perRoleCap) {
      process.stderr.write(
        `ERROR: blocked_reason=per_role_cap_exceeded — per-role agent cap (${perRoleCap}) reached (${activeRunsForRole} active). Spawn of ${role} blocked.\n`
      );
      return {
        allowed: false,
        role,
        discussion: discussion ?? undefined,
        reason: `per-role concurrency cap (${perRoleCap}) reached: ${activeRunsForRole} active ${role} runs`,
        blocked_reason: "per_role_cap_exceeded",
        exit_code: 1,
        warnings,
        active_runs: activeRuns,
        active_runs_for_role: activeRunsForRole,
        max_concurrent: perRoleCap,
      };
    }

    // Fleet-wide hard cap (default 8)
    if (activeRuns >= FLEET_CAP_DEFAULT) {
      process.stderr.write(
        `ERROR: blocked_reason=fleet_cap_exceeded — fleet agent cap reached, spawn of ${role} blocked.\n`
      );
      return {
        allowed: false,
        role,
        discussion: discussion ?? undefined,
        reason: `fleet cap (${FLEET_CAP_DEFAULT}) reached: ${activeRuns} active runs`,
        blocked_reason: "fleet_cap_exceeded",
        exit_code: 1,
        warnings,
        active_runs: activeRuns,
        active_runs_for_role: activeRunsForRole,
        max_concurrent: FLEET_CAP_DEFAULT,
      };
    }
  }

  // ── Step 4: spec-readiness ─────────────────────────────────────────────────
  // Mirrors bash §671-705 (lessons injection uses discussion body):
  // If a discussion number is given, ensure all three sections are present.
  let missing: string[] = [];
  if (discussion != null) {
    try {
      missing = await checkSpecReadiness(discussion, discussionBody);
      if (missing.length > 0) {
        process.stderr.write(
          `ERROR: spec not ready for D#${discussion} — missing sections: ${missing.join(", ")}\n`
        );
        return {
          allowed: false,
          role,
          discussion,
          reason: `spec not ready — missing sections: ${missing.join(", ")}`,
          blocked_reason: "spec_not_ready",
          exit_code: 1,
          warnings,
          missing_sections: missing,
        };
      }
    } catch {
      warnings.push("discussion spec check failed (non-fatal)");
    }
  }

  // ── Allowed ────────────────────────────────────────────────────────────────
  return {
    allowed: true,
    role,
    discussion: discussion ?? undefined,
    exit_code: 0,
    warnings,
    dial_class: dialClass,
    active_runs: activeRuns,
    active_runs_for_role: activeRunsForRole,
    budget_remaining: budgetRemaining,
  };
}

// ---------------------------------------------------------------------------
// CLI entry point — mirrors bash argument parsing
// ---------------------------------------------------------------------------

function parseArgs(argv: string[]): {
  role: string;
  discussion: number | null;
  subagentType: string | null;
  dryRun: boolean;
  noRegister: boolean;
  operationClass: string | null;
} {
  let role = "";
  let discussion: number | null = null;
  let subagentType: string | null = null;
  let dryRun = false;
  let noRegister = false;
  let operationClass: string | null = null;
  let i = 0;
  const argv2 = argv;

  while (i < argv2.length) {
    const arg = argv2[i]!;
    switch (arg) {
      case "--role":
        role = argv2[++i] ?? "";
        break;
      case "--discussion": {
        const raw = argv2[++i] ?? "";
        const n = parseInt(raw, 10);
        discussion = isNaN(n) ? null : n;
        break;
      }
      case "--isolation":
      case "--event-id":
        // Accepted but not used in TS gate (bash-only plumbing)
        i++;
        break;
      case "--resume":
        // No-op in TS gate
        break;
      case "--dry-run":
        dryRun = true;
        break;
      case "--subagent-type":
        subagentType = argv2[++i] ?? null;
        break;
      case "--no-register":
      case "--dry-run-fleet":
        noRegister = true;
        break;
      case "--operation-class":
        operationClass = argv2[++i] ?? null;
        break;
      default:
        process.stderr.write(`Unknown argument: ${arg}\n`);
        process.stderr.write(
          `Usage: pre-spawn-check.ts --role <role> [--discussion <N>] [--dry-run] [--no-register] [--operation-class <class>]\n`
        );
        process.exit(1);
    }
    i++;
  }

  return { role, discussion, subagentType, dryRun, noRegister, operationClass };
}

if (import.meta.main) {
  const args = parseArgs(process.argv.slice(2));

  const result = await runPreSpawnCheck({
    role: args.role,
    discussion: args.discussion,
    subagentType: args.subagentType,
    dryRun: args.dryRun,
    noRegister: args.noRegister,
    operationClass: args.operationClass,
  });

  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  process.exit(result.exit_code);
}
