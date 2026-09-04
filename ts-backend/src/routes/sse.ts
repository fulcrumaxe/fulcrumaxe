/**
 * /feed and /events SSE routes — TypeScript port of backend/routers/rpc_sse.py.
 *
 * Source fidelity (D#1437 Spec P5):
 * These are the SSE streaming routes that mirror Python rpc_sse.py exactly.
 *
 * Auth model — EventSource cannot set headers, so BOTH routes accept auth via:
 *   - Authorization: Bearer <token>  (standard header)
 *   - ?token=<token>                 (query param; EventSource-compatible)
 * Returns 401 when no credential provided (auth enabled); 403 for wrong token.
 *
 * These routes are listed as PUBLIC_ROUTES in middleware/auth.ts, so the
 * default-deny middleware passes them through. Auth is enforced below via
 * the query-param-aware check — identical to how Python rpc_sse.py works.
 *
 * SSE wire format (byte-equivalent to Python):
 *   data: <json.dumps(data, default=str)>\n\n
 *   Heartbeat: data: {"type":"heartbeat"}\n\n
 *   Initial:   data: {"type":"connected"}\n\n
 *   Poll interval: 500ms (mirrors rpc_sse.py POLL_INTERVAL)
 *   Heartbeat interval: 30s idle (mirrors routers/streams.py _HEARTBEAT_INTERVAL)
 *
 * /feed — tails .autonomous-team/agent-feed.jsonl for new JSONL lines.
 *   Applies optional ?since=<ts> and ?filter[role]=<role> filters.
 *
 * /events — tails .autonomous-team/agent-feed.jsonl for all events,
 *   emitting each with an added _event_type field derived from event content.
 *   Applies optional ?loop_id= and ?since= filters.
 *   (The Python version subscribes to an in-process event bus; the TS backend
 *   is standalone and sources from the same persisted feed file. Observable
 *   behavior — event frames with _event_type — is identical.)
 *
 * Legacy-envelope middleware exempts SSE routes automatically (Rule 1:
 * text/event-stream content-type passes through unchanged).
 */

import type { Context } from "hono";
import { streamSSE } from "hono/streaming";
import { timingSafeEqual } from "node:crypto";
import { existsSync, openSync, readSync, fstatSync, closeSync } from "node:fs";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Paths — mirrors Python rpc_sse.py _REPO_ROOT / feed_file
//
// AUTONOMOUS_TEAM_DIR is read at request time (not module load time) so that
// tests can override it per-test via process.env.AUTONOMOUS_TEAM_DIR.
// ---------------------------------------------------------------------------

// ts-backend/src/routes/ -> ts-backend/src/ -> ts-backend/ -> repo root
const REPO_ROOT = join(import.meta.dir, "..", "..", "..");

/** Return the feed file path, resolving AUTONOMOUS_TEAM_DIR at call time. */
function getFeedFile(): string {
  const teamDir =
    process.env.AUTONOMOUS_TEAM_DIR ?? join(REPO_ROOT, ".autonomous-team");
  return join(teamDir, "agent-feed.jsonl");
}

/**
 * Return the events-bus file path, resolving AUTONOMOUS_TEAM_DIR at call time.
 *
 * events-bus.jsonl is written by backend/event_bus.py BusEventFileAppender —
 * an additive subscriber that persists all 4 bus event types (AgentOutputEvent,
 * BudgetSpendEvent, LoopIterationEvent, GateChangeEvent) with the _event_type
 * field already embedded.  Tailing this file gives /events full parity with
 * Python's /events without the TS backend needing to be in-process.
 */
function getEventsBusFile(): string {
  const teamDir =
    process.env.AUTONOMOUS_TEAM_DIR ?? join(REPO_ROOT, ".autonomous-team");
  return join(teamDir, "events-bus.jsonl");
}

// ---------------------------------------------------------------------------
// SSE wire format constants — mirrors routers/streams.py
// ---------------------------------------------------------------------------

const HEARTBEAT_INTERVAL_MS = 30_000; // 30s idle heartbeat
const POLL_INTERVAL_MS = 500;          // 500ms poll (mirrors rpc_sse.py POLL_INTERVAL)

// ---------------------------------------------------------------------------
// Auth helpers — mirrors rpc_sse.py _sse_auth_ok / _auth_missing
// ---------------------------------------------------------------------------

function getAuthKey(): string | null {
  return process.env.AF_API_AUTH_KEY ?? null;
}

/**
 * Constant-time comparison (CWE-208 prevention).
 * Mirrors Python hmac.compare_digest().
 */
function timingSafeTokenEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a, "utf8");
  const bBuf = Buffer.from(b, "utf8");
  if (aBuf.length !== bBuf.length) {
    const maxLen = Math.max(aBuf.length, bBuf.length);
    const aPad = Buffer.alloc(maxLen, 0);
    const bPad = Buffer.alloc(maxLen, 0);
    aBuf.copy(aPad);
    bBuf.copy(bPad);
    timingSafeEqual(aPad, bPad); // constant-time pass, result discarded
    return false;
  }
  return timingSafeEqual(aBuf, bBuf);
}

/**
 * Return true when the request carries a valid credential.
 * Accepts Authorization: Bearer <key> OR ?token=<key>.
 * When AF_API_AUTH_KEY is unset, auth is disabled — every request passes.
 * Mirrors rpc_sse.py _sse_auth_ok().
 */
function sseAuthOk(c: Context, tokenParam: string | null): boolean {
  const key = getAuthKey();
  if (key === null) return true;

  // Check Authorization: Bearer <key> header first.
  const authHeader = c.req.header("Authorization") ?? "";
  if (authHeader.startsWith("Bearer ")) {
    const bearer = authHeader.slice(7);
    if (timingSafeTokenEqual(bearer, key)) return true;
  }

  // Fall back to ?token= query param.
  if (tokenParam !== null && timingSafeTokenEqual(tokenParam, key)) return true;

  return false;
}

/**
 * Return true when NO credential was provided (→ 401).
 * Mirrors rpc_sse.py _auth_missing().
 */
function authMissing(c: Context, tokenParam: string | null): boolean {
  const key = getAuthKey();
  if (key === null) return false;
  const authHeader = c.req.header("Authorization") ?? "";
  const hasBearer = authHeader.startsWith("Bearer ");
  const hasToken = tokenParam !== null;
  return !hasBearer && !hasToken;
}

// ---------------------------------------------------------------------------
// File-tail helper — mirrors rpc_sse.py _filtered_feed_gen file I/O
// Tracks byte position in an open fd; reads new bytes since last poll.
// ---------------------------------------------------------------------------

interface TailState {
  fd: number;
  pos: number;
}

/** Open feed file and seek to end. Returns null if file does not exist. */
function openFeedAtEnd(filePath: string): TailState | null {
  if (!existsSync(filePath)) return null;
  const fd = openSync(filePath, "r");
  const stat = fstatSync(fd);
  return { fd, pos: stat.size };
}

/** Read any new content appended since state.pos. Returns raw JSONL lines. */
function readNewLines(state: TailState): string[] {
  const stat = fstatSync(state.fd);
  if (stat.size <= state.pos) return [];

  const toRead = stat.size - state.pos;
  const buf = Buffer.allocUnsafe(toRead);
  // readSync with explicit position (does not advance fd cursor — safe to call
  // repeatedly; we track pos ourselves).
  const bytesRead = readSync(state.fd, buf, 0, toRead, state.pos);
  state.pos += bytesRead;

  return buf
    .subarray(0, bytesRead)
    .toString("utf8")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
}

// ---------------------------------------------------------------------------
// SSE response wrapper — applies parity headers Python sets but Hono omits.
//
// Python's StreamingResponse(media_type="text/event-stream") yields:
//   Content-Type: text/event-stream; charset=utf-8   (FastAPI appends charset)
//   X-Accel-Buffering: no                            (set explicitly in rpc_sse.py)
//
// Hono's streamSSE sets "text/event-stream" (no charset) and does not set
// X-Accel-Buffering. We wrap the returned Response to match Python exactly.
// ---------------------------------------------------------------------------

function applySseParity(res: Response): Response {
  const headers = new Headers(res.headers);
  headers.set("Content-Type", "text/event-stream; charset=utf-8");
  headers.set("X-Accel-Buffering", "no");
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

// ---------------------------------------------------------------------------
// _event_type inference for /events (mirrors the Python event bus types)
// ---------------------------------------------------------------------------

/**
 * Infer the _event_type string from an event payload that lacks _event_type.
 *
 * events-bus.jsonl events written by BusEventFileAppender already carry
 * _event_type, so this function is only needed for legacy events that pre-date
 * the new subscriber (e.g. events read from agent-feed.jsonl).
 *
 * Discriminates by unique fields from backend/event_bus.py dataclass definitions:
 *   BudgetSpendEvent   — input_tokens | output_tokens
 *   GateChangeEvent    — gate_name (unique; new_value/old_value also appear)
 *   LoopIterationEvent — iteration_id (unique field in LoopIterationEvent)
 *   AgentOutputEvent   — default (agent_id, content, event_subtype)
 */
function inferEventType(ev: Record<string, unknown>): string {
  if ("input_tokens" in ev || "output_tokens" in ev) return "BudgetSpendEvent";
  if ("gate_name" in ev) return "GateChangeEvent";
  if ("iteration_id" in ev || "duration_seconds" in ev || "agents_spawned" in ev)
    return "LoopIterationEvent";
  return "AgentOutputEvent"; // default (most common event type)
}

// ---------------------------------------------------------------------------
// /feed handler — tails agent-feed.jsonl with optional since/role filters
// ---------------------------------------------------------------------------

export async function feedHandler(c: Context): Promise<Response> {
  const tokenParam = c.req.query("token") ?? null;
  const since = c.req.query("since") ?? null;
  const roleFilter = c.req.query("filter[role]") ?? null;

  if (authMissing(c, tokenParam)) {
    return c.json({ detail: "unauthorized" }, 401);
  }
  if (!sseAuthOk(c, tokenParam)) {
    return c.json({ detail: "forbidden" }, 403);
  }

  // Capture feedFile at request time so it stays stable across the stream lifetime.
  const feedFilePath = getFeedFile();

  return applySseParity(streamSSE(
    c,
    async (stream) => {
      // Initial connected frame — mirrors Python rpc_sse.py behaviour.
      await stream.writeSSE({ data: '{"type":"connected"}' });

      let tailState = openFeedAtEnd(feedFilePath);
      let lastHeartbeat = Date.now();

      while (!stream.closed) {
        // If file didn't exist on connect, try again on each poll.
        if (tailState === null) {
          tailState = openFeedAtEnd(feedFilePath);
        }

        let newEvents = false;

        if (tailState !== null) {
          const lines = readNewLines(tailState);
          for (const raw of lines) {
            let ev: Record<string, unknown>;
            try {
              ev = JSON.parse(raw) as Record<string, unknown>;
            } catch {
              continue;
            }

            // Apply ?since= filter — mirrors rpc_sse.py ts comparison.
            const ts = (ev["timestamp"] ?? ev["ts"] ?? "") as string;
            if (since !== null && ts < since) continue;

            // Apply ?filter[role]= filter.
            if (roleFilter !== null && ev["role"] !== roleFilter) continue;

            await stream.writeSSE({ data: JSON.stringify(ev) });
            newEvents = true;
          }
        }

        const now = Date.now();
        if (!newEvents && now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
          await stream.writeSSE({ data: '{"type":"heartbeat"}' });
          lastHeartbeat = now;
        }

        await Bun.sleep(POLL_INTERVAL_MS);
      }

      if (tailState !== null) {
        try {
          closeSync(tailState.fd);
        } catch {
          // Ignore close errors on disconnect
        }
      }
    },
    async (_err, stream) => {
      await stream.close();
    }
  ));
}

// ---------------------------------------------------------------------------
// /events handler — tails events-bus.jsonl for all 4 bus event types
//
// events-bus.jsonl is written by backend/event_bus.py BusEventFileAppender.
// Each line already carries _event_type (same as Python _bus_gen wire format).
// /feed continues to tail agent-feed.jsonl (AgentOutputEvent only, no change).
// ---------------------------------------------------------------------------

export async function eventsHandler(c: Context): Promise<Response> {
  const tokenParam = c.req.query("token") ?? null;
  const loopId = c.req.query("loop_id") ?? null;
  const since = c.req.query("since") ?? null;

  if (authMissing(c, tokenParam)) {
    return c.json({ detail: "unauthorized" }, 401);
  }
  if (!sseAuthOk(c, tokenParam)) {
    return c.json({ detail: "forbidden" }, 403);
  }

  // Tail events-bus.jsonl (all 4 event types) — NOT agent-feed.jsonl.
  // Captured at request time so the path stays stable for the stream lifetime.
  const eventsBusFilePath = getEventsBusFile();

  return applySseParity(streamSSE(
    c,
    async (stream) => {
      // Initial connected frame — mirrors Python rpc_sse.py behaviour.
      await stream.writeSSE({ data: '{"type":"connected"}' });

      let tailState = openFeedAtEnd(eventsBusFilePath);
      let lastHeartbeat = Date.now();

      while (!stream.closed) {
        if (tailState === null) {
          tailState = openFeedAtEnd(eventsBusFilePath);
        }

        let newEvents = false;

        if (tailState !== null) {
          const lines = readNewLines(tailState);
          for (const raw of lines) {
            let ev: Record<string, unknown>;
            try {
              ev = JSON.parse(raw) as Record<string, unknown>;
            } catch {
              continue;
            }

            // Apply ?loop_id= filter — mirrors Python rpc_sse.py behaviour.
            if (loopId !== null && ev["loop_id"] !== loopId) continue;

            // Apply ?since= filter.
            const ts = (ev["timestamp"] ?? ev["ts"] ?? "") as string;
            if (since !== null && ts < since) continue;

            // events-bus.jsonl already carries _event_type from BusEventFileAppender.
            // Fall back to field inference only for legacy events that predate the new subscriber.
            if (!("_event_type" in ev)) {
              ev["_event_type"] = inferEventType(ev);
            }

            await stream.writeSSE({ data: JSON.stringify(ev) });
            newEvents = true;
          }
        }

        const now = Date.now();
        if (!newEvents && now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
          await stream.writeSSE({ data: '{"type":"heartbeat"}' });
          lastHeartbeat = now;
        }

        await Bun.sleep(POLL_INTERVAL_MS);
      }

      if (tailState !== null) {
        try {
          closeSync(tailState.fd);
        } catch {
          // Ignore close errors
        }
      }
    },
    async (_err, stream) => {
      await stream.close();
    }
  ));
}
