/**
 * /sessions* GET routes — TypeScript port of backend/routers/sessions_get.py.
 *
 * Source fidelity (D#1437 Spec P2):
 * Reads session JSON files from .autonomous-team/sessions/ exactly as the
 * Python SessionManager does. Falls back gracefully to empty list if the
 * directory does not exist.
 *
 * Routes:
 *   GET /sessions               → { sessions: [...] }
 *   GET /sessions/current       → session dict | 404
 *   GET /sessions/compare?a=&b= → { a, b, delta } | 400 | 404
 *   GET /sessions/:session_id   → session dict | 404
 *
 * All routes are RBAC-gated (same method+path strings as Python make_require_rbac).
 *
 * Data path: reads .autonomous-team/sessions/*.json — same files as Python
 * SessionManager (file-backed). No SQLite needed for the TS port because the
 * live environment uses the file-backed manager (state.db does not exist in
 * the default setup; the SqliteSessionManager is a drop-in alternative).
 */

import type { Context } from "hono";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { checkRbac } from "../middleware/rbac-check.js";

// ---------------------------------------------------------------------------
// Paths — mirrors Python SessionManager.SESSIONS_DIR
//
// AUTONOMOUS_TEAM_DIR env var overrides the .autonomous-team directory path.
// This is useful for parity testing from a git worktree where the worktree's
// .autonomous-team/ is sparse (does not contain sessions/).
// In production (running from main repo), the env var is not needed.
// ---------------------------------------------------------------------------

// ts-backend/src/routes/ -> ts-backend/src/ -> ts-backend/ -> repo root
const REPO_ROOT = join(import.meta.dir, "..", "..", "..");
const AUTONOMOUS_TEAM_DIR =
  process.env.AUTONOMOUS_TEAM_DIR ??
  join(REPO_ROOT, ".autonomous-team");
const SESSIONS_DIR = join(AUTONOMOUS_TEAM_DIR, "sessions");

// ---------------------------------------------------------------------------
// Session data types — mirrors Python session dict shape
// ---------------------------------------------------------------------------

interface Session {
  session_id: string;
  started_at: string | null;
  ended_at: string | null;
  iteration_count: number;
  prs_merged: number[];
  discussions_completed: number[];
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Helpers — mirrors Python SessionManager methods
// ---------------------------------------------------------------------------

function readSessionFile(sessionId: string): Session | null {
  const p = join(SESSIONS_DIR, `${sessionId}.json`);
  if (!existsSync(p)) return null;
  try {
    const raw = readFileSync(p, "utf-8");
    const data = JSON.parse(raw) as unknown;
    if (data !== null && typeof data === "object" && !Array.isArray(data)) {
      return data as Session;
    }
    return null;
  } catch {
    return null;
  }
}

function listSessions(limit = 20): Session[] {
  if (!existsSync(SESSIONS_DIR)) return [];
  let files: string[];
  try {
    files = readdirSync(SESSIONS_DIR).filter((f) => f.endsWith(".json"));
  } catch {
    return [];
  }

  const sessions: Session[] = [];
  for (const f of files) {
    try {
      const raw = readFileSync(join(SESSIONS_DIR, f), "utf-8");
      const data = JSON.parse(raw) as unknown;
      if (
        data !== null &&
        typeof data === "object" &&
        !Array.isArray(data) &&
        typeof (data as Record<string, unknown>)["session_id"] === "string"
      ) {
        sessions.push(data as Session);
      }
    } catch {
      // skip malformed files — mirrors Python behavior
    }
  }

  // Sort newest-first by started_at (mirrors Python SessionManager.list_sessions)
  sessions.sort((a, b) => {
    const aTs = a.started_at ?? "";
    const bTs = b.started_at ?? "";
    return bTs.localeCompare(aTs);
  });

  return sessions.slice(0, limit);
}

function currentSession(): Session | null {
  if (!existsSync(SESSIONS_DIR)) return null;
  let files: string[];
  try {
    files = readdirSync(SESSIONS_DIR).filter((f) => f.endsWith(".json"));
  } catch {
    return null;
  }

  for (const f of files) {
    try {
      const raw = readFileSync(join(SESSIONS_DIR, f), "utf-8");
      const data = JSON.parse(raw) as unknown;
      if (
        data !== null &&
        typeof data === "object" &&
        !Array.isArray(data)
      ) {
        const s = data as Session;
        if (s.ended_at === null && typeof s.session_id === "string") {
          return s;
        }
      }
    } catch {
      // skip
    }
  }
  return null;
}

function parseDurationMinutes(session: Session): number | null {
  const startStr = session.started_at;
  if (!startStr) return null;
  const start = new Date(startStr as string);
  if (isNaN(start.getTime())) return null;

  const endStr = session.ended_at;
  const end = endStr ? new Date(endStr as string) : new Date();
  if (isNaN(end.getTime())) return null;

  return (end.getTime() - start.getTime()) / 60000;
}

interface CompareResult {
  a: Session;
  b: Session;
  delta: {
    iterations: number;
    prs: number;
    discussions: number;
    duration_minutes: number | null;
  };
}

interface CompareError {
  error: string;
  status: 404;
}

function compareSessions(idA: string, idB: string): CompareResult | CompareError {
  const a = readSessionFile(idA);
  if (a === null) {
    return { error: `session '${idA}' not found`, status: 404 };
  }
  const b = readSessionFile(idB);
  if (b === null) {
    return { error: `session '${idB}' not found`, status: 404 };
  }

  const durA = parseDurationMinutes(a);
  const durB = parseDurationMinutes(b);

  let durationDelta: number | null = null;
  if (durA !== null && durB !== null) {
    // mirrors Python: round(dur_a - dur_b, 2)
    durationDelta = Math.round((durA - durB) * 100) / 100;
  }

  return {
    a,
    b,
    delta: {
      iterations: a.iteration_count - b.iteration_count,
      prs: a.prs_merged.length - b.prs_merged.length,
      discussions: a.discussions_completed.length - b.discussions_completed.length,
      duration_minutes: durationDelta,
    },
  };
}

// ---------------------------------------------------------------------------
// Route handlers — each checks RBAC before serving data
// ---------------------------------------------------------------------------

/** GET /sessions — list sessions (most recent first, up to 20) */
export async function sessionsListHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/sessions");
  if (rbacResult !== null) return rbacResult;

  const sessions = listSessions();
  return c.json({ sessions });
}

/** GET /sessions/current — current active session or 404 */
export async function sessionsCurrentHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/sessions/current");
  if (rbacResult !== null) return rbacResult;

  const session = currentSession();
  if (session === null) {
    return c.json({ detail: "no active session" }, 404);
  }
  return c.json(session);
}

/** GET /sessions/compare?a=&b= — compare two sessions */
export async function sessionsCompareHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/sessions/compare");
  if (rbacResult !== null) return rbacResult;

  const url = new URL(c.req.url);
  const idA = url.searchParams.get("a") ?? "";
  const idB = url.searchParams.get("b") ?? "";

  if (!idA || !idB) {
    return c.json({ detail: "query params 'a' and 'b' are required" }, 400);
  }

  const result = compareSessions(idA, idB);
  if ("status" in result) {
    return c.json({ detail: result.error }, result.status);
  }
  return c.json(result);
}

/** GET /sessions/:session_id — get session by ID */
export async function sessionsGetByIdHandler(c: Context): Promise<Response> {
  const rbacResult = checkRbac(c, "GET", "/sessions/{session_id}");
  if (rbacResult !== null) return rbacResult;

  const sessionId = c.req.param("session_id");
  if (!sessionId) {
    return c.json({ detail: "session_id required" }, 400);
  }

  const session = readSessionFile(sessionId);
  if (session === null) {
    return c.json({ detail: `session '${sessionId}' not found` }, 404);
  }
  return c.json(session);
}
