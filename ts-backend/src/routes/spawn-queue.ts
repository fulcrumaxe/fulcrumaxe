/**
 * /spawn-queue* and /spawn-blocks GET routes — TypeScript port of
 * backend/routers/spawn_queue_get.py.
 *
 * Source fidelity (D#1437 Spec P2):
 * Reads spawn-queue.json and agent-feed.jsonl directly, exactly as the
 * Python router delegates to get_spawn_queue().status/list_pending/list_active
 * and the inline _spawn_blocks_response() handler.
 *
 * Routes:
 *   GET /spawn-queue          → status dict
 *   GET /spawn-queue/pending  → { pending: [...] }
 *   GET /spawn-queue/active   → { active: [...] }
 *   GET /spawn-blocks         → [...] (array of spawn_blocked events)
 *   GET /spawn-blocks/*       → same (legacy startswith match)
 *
 * All routes are RBAC-gated (same method+path strings as Python make_require_rbac).
 *
 * Data path:
 *   /spawn-queue*  → .autonomous-team/spawn-queue.json
 *   /spawn-blocks  → .autonomous-team/agent-feed.jsonl
 */

import type { Context } from "hono";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { checkRbac } from "../middleware/rbac-check.js";

// ---------------------------------------------------------------------------
// Paths — mirrors Python spawn_queue.py _REPO_ROOT / _QUEUE_FILE
//
// AUTONOMOUS_TEAM_DIR env var overrides the .autonomous-team directory path.
// This is useful for parity testing from a git worktree where the worktree's
// .autonomous-team/ is sparse (does not contain spawn-queue.json).
// In production (running from main repo), the env var is not needed.
// ---------------------------------------------------------------------------

// ts-backend/src/routes/ -> ts-backend/src/ -> ts-backend/ -> repo root
const REPO_ROOT = join(import.meta.dir, "..", "..", "..");
const AUTONOMOUS_TEAM_DIR =
  process.env.AUTONOMOUS_TEAM_DIR ??
  join(REPO_ROOT, ".autonomous-team");
const QUEUE_FILE = join(AUTONOMOUS_TEAM_DIR, "spawn-queue.json");
const FEED_FILE = join(AUTONOMOUS_TEAM_DIR, "agent-feed.jsonl");
const CONFIG_FILE = join(AUTONOMOUS_TEAM_DIR, "config.json");

// ---------------------------------------------------------------------------
// Defaults — mirrors Python DEFAULT_LIMITS and DEFAULT_PRIORITIES
// ---------------------------------------------------------------------------

const DEFAULT_LIMITS: Record<string, number> = {
  executor: 2,
  "code-reviewer": 2,
  "security-reviewer": 1,
  "project-manager": 1,
  "mission-analyst": 1,
  _total: 6,
};

// ---------------------------------------------------------------------------
// Config loader — mirrors Python _load_config()
// ---------------------------------------------------------------------------

function loadConfig(): Record<string, unknown> {
  try {
    const raw = readFileSync(CONFIG_FILE, "utf-8");
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function effectiveLimits(): Record<string, number> {
  const config = loadConfig();
  const policies = config["policies"] as Record<string, unknown> | undefined;
  const queueConc = policies?.["queue_concurrency"] as
    | Record<string, number>
    | undefined;
  if (!queueConc) return { ...DEFAULT_LIMITS };
  return { ...DEFAULT_LIMITS, ...queueConc };
}

// ---------------------------------------------------------------------------
// Queue state reader — mirrors Python SpawnQueue._load()
// ---------------------------------------------------------------------------

interface QueueState {
  pending: Record<string, unknown>[];
  active: Record<string, unknown>[];
  completed: Record<string, unknown>[];
  failed: Record<string, unknown>[];
}

function loadQueueState(): QueueState {
  const empty: QueueState = {
    pending: [],
    active: [],
    completed: [],
    failed: [],
  };
  if (!existsSync(QUEUE_FILE)) return empty;
  try {
    const raw = readFileSync(QUEUE_FILE, "utf-8");
    const data = JSON.parse(raw) as Record<string, unknown>;
    return {
      pending: (data["pending"] as Record<string, unknown>[] | undefined) ?? [],
      active: (data["active"] as Record<string, unknown>[] | undefined) ?? [],
      completed:
        (data["completed"] as Record<string, unknown>[] | undefined) ?? [],
      failed: (data["failed"] as Record<string, unknown>[] | undefined) ?? [],
    };
  } catch {
    return empty;
  }
}

// ---------------------------------------------------------------------------
// Status builder — mirrors Python SpawnQueue.status()
// Note: We do NOT call _cleanup_stale() (that mutates the file; this is
// read-only). The Python route DOES call status() which internally calls
// _cleanup_stale() and saves — but since this is a read-only mirror we
// report the raw state, consistent with the parity goal of reading the same
// source data.
// ---------------------------------------------------------------------------

function spawnQueueStatus(): Record<string, unknown> {
  const state = loadQueueState();
  const limits = effectiveLimits();

  const activeByRole: Record<string, number> = {};
  for (const a of state.active) {
    const role = (a["role"] as string) ?? "";
    activeByRole[role] = (activeByRole[role] ?? 0) + 1;
  }

  const totalActive = state.active.length;
  const totalLimit = limits["_total"] ?? DEFAULT_LIMITS["_total"];
  const utilizationPct =
    totalLimit > 0 ? Math.floor((totalActive / totalLimit) * 100) : 0;

  const roleUtilization: Record<string, { active: number; limit: number }> = {};
  for (const [role, limit] of Object.entries(limits)) {
    if (role === "_total") continue;
    roleUtilization[role] = {
      active: activeByRole[role] ?? 0,
      limit,
    };
  }

  return {
    pending: state.pending.length,
    active_total: totalActive,
    total_limit: totalLimit,
    utilization_pct: utilizationPct,
    by_role: roleUtilization,
    completed: state.completed.length,
    failed: state.failed.length,
  };
}

// ---------------------------------------------------------------------------
// Spawn-blocks reader — mirrors Python _spawn_blocks_response()
// Reads agent-feed.jsonl, filters for event_type == "spawn_blocked",
// iterates in reverse order, returns up to limit entries.
// ---------------------------------------------------------------------------

interface SpawnBlockEvent {
  role: string;
  reason: string;
  ts: string;
  discussion: unknown;
}

function spawnBlocksResponse(limit = 10): SpawnBlockEvent[] {
  const blocks: SpawnBlockEvent[] = [];
  if (!existsSync(FEED_FILE)) return blocks;

  let content: string;
  try {
    content = readFileSync(FEED_FILE, "utf-8");
  } catch {
    return blocks;
  }

  const lines = content.split("\n");
  // Iterate in reverse — mirrors Python reversed(lines)
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line) continue;
    try {
      const ev = JSON.parse(line) as Record<string, unknown>;
      if (ev["event_type"] === "spawn_blocked") {
        blocks.push({
          role: (ev["role"] as string) ?? "",
          reason: (ev["reason"] as string) ?? "unknown",
          ts: (ev["ts"] as string) ?? "",
          discussion: ev["discussion"] ?? null,
        });
        if (blocks.length >= limit) break;
      }
    } catch {
      // skip malformed lines
    }
  }
  return blocks;
}

// ---------------------------------------------------------------------------
// Route handlers
// ---------------------------------------------------------------------------

/** GET /spawn-queue — queue status */
export async function spawnQueueStatusHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/spawn-queue");
  if (rbacResult !== null) return rbacResult;

  return c.json(spawnQueueStatus());
}

/** GET /spawn-queue/pending — pending spawn requests */
export async function spawnQueuePendingHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/spawn-queue/pending");
  if (rbacResult !== null) return rbacResult;

  const state = loadQueueState();
  return c.json({ pending: state.pending });
}

/** GET /spawn-queue/active — active agents */
export async function spawnQueueActiveHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/spawn-queue/active");
  if (rbacResult !== null) return rbacResult;

  const state = loadQueueState();
  return c.json({ active: state.active });
}

/** GET /spawn-blocks — recent blocked-spawn events */
export async function spawnBlocksHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/spawn-blocks");
  if (rbacResult !== null) return rbacResult;

  const limitParam = c.req.query("limit");
  const limit = limitParam ? parseInt(limitParam, 10) : 10;
  const safeLimit = isNaN(limit) || limit < 1 ? 10 : limit;

  return c.json(spawnBlocksResponse(safeLimit));
}

/** GET /spawn-blocks/* — sub-paths (legacy prefix match) */
export async function spawnBlocksSubHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/spawn-blocks");
  if (rbacResult !== null) return rbacResult;

  const limitParam = c.req.query("limit");
  const limit = limitParam ? parseInt(limitParam, 10) : 10;
  const safeLimit = isNaN(limit) || limit < 1 ? 10 : limit;

  return c.json(spawnBlocksResponse(safeLimit));
}
