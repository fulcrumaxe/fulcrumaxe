/**
 * TypeScript backend — Bun + Hono server (P1: auth/RBAC parity).
 *
 * Port: 19099 (loopback-only 127.0.0.1)
 * Dedicated port, not used by any other service:
 *   Python API:    127.0.0.1:18099
 *   Python RPC:    127.0.0.1:8765
 *   Python SSE:    127.0.0.1:8420
 *   Dashboard UI:  127.0.0.1:5173
 *   TS backend:    127.0.0.1:19099  ← this server
 *
 * Start: bun run src/index.ts
 *   or:  bun start   (from ts-backend/)
 *
 * Env:
 *   TS_BACKEND_PORT        — override port (default 19099)
 *   AF_API_AUTH_KEY        — enable auth; unset = auth disabled (mirrors Python)
 *   AF_RATE_LIMIT_DISABLED — set to "1" to disable rate limiting
 *
 * Middleware order (mirrors Python asgi_app.py middleware stack):
 *   1. Rate-limit middleware (per-IP token bucket, true peer IP, never XFF)
 *   2. Default-deny auth (PUBLIC_ROUTES exact + PUBLIC_PREFIXES startswith)
 * Route-level:
 *   3. Loopback gate (applied per-route via loopbackGateMiddleware)
 *   4. Origin guard (applied per spawn-trigger route via originGuardMiddleware)
 */

import { Hono } from "hono";
import { defaultDenyMiddleware } from "./middleware/auth.js";
import { rateLimitMiddleware } from "./middleware/rate-limit.js";
import { legacyEnvelopeMiddleware } from "./middleware/legacy-envelope.js";
import { healthHandler } from "./routes/health.js";
import {
  sessionsListHandler,
  sessionsCurrentHandler,
  sessionsCompareHandler,
  sessionsGetByIdHandler,
} from "./routes/sessions.js";
import {
  spawnQueueStatusHandler,
  spawnQueuePendingHandler,
  spawnQueueActiveHandler,
  spawnBlocksHandler,
  spawnBlocksSubHandler,
} from "./routes/spawn-queue.js";
import {
  statsMetricsSummaryHandler,
  statsMetricsSeriesHandler,
} from "./routes/stats-metrics.js";
import { budgetInitHandler } from "./routes/budget-init.js";
import { feedHandler, eventsHandler } from "./routes/sse.js";
import { rpcDispatchHandler } from "./routes/rpc.js";
import { graphqlHandler } from "./routes/graphql.js";
import { buildOpenApiDocument } from "./openapi.js";
import { statsDb } from "./config/state-paths.js";

const app = new Hono();

// ---------------------------------------------------------------------------
// Middleware stack — order is security-critical (mirrors Python middleware order)
// ---------------------------------------------------------------------------

// 0. Legacy-envelope middleware — wraps all responses to inject _api_version.
//    Mirrors Python LegacyEnvelopeMiddleware (outermost middleware in Python).
//    In Hono, middleware runs on the RESPONSE path in registration order —
//    registering this first means it wraps the response last (outermost).
app.use("*", legacyEnvelopeMiddleware);

// 1. Rate-limit middleware — before auth (mirrors Python middleware registration order)
//    Keyed on true peer IP from Bun socket, NEVER X-Forwarded-For (CWE-348).
app.use("*", rateLimitMiddleware);

// 2. Default-deny auth middleware — MUST be registered before any routes.
//    Present from request #1; AUTH_KEY unset = auth disabled.
//    P1: now includes PUBLIC_PREFIXES startswith + timingSafeEqual.
app.use("*", defaultDenyMiddleware);

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

// GET /health — public, zero-DB, mirrors Python backend/routers/health.py
app.get("/health", healthHandler);

// GET /openapi.json — public, returns the OpenAPI 3.1 spec document
// Listed in PUBLIC_ROUTES in middleware/auth.ts so default-deny passes it through.
app.get("/openapi.json", (c) => c.json(buildOpenApiDocument()));

// ---------------------------------------------------------------------------
// P2 routes — auth+RBAC gated, SQLite/file-backed reads
// IMPORTANT: /sessions/current and /sessions/compare must be registered BEFORE
// /sessions/:session_id to prevent the param route swallowing them.
// ---------------------------------------------------------------------------

// GET /sessions — list sessions (bearer auth + RBAC required)
app.get("/sessions", sessionsListHandler);

// GET /sessions/current — current active session
app.get("/sessions/current", sessionsCurrentHandler);

// GET /sessions/compare?a=&b= — compare two sessions
app.get("/sessions/compare", sessionsCompareHandler);

// GET /sessions/:session_id — get session by ID
app.get("/sessions/:session_id", sessionsGetByIdHandler);

// GET /spawn-queue — queue status
app.get("/spawn-queue", spawnQueueStatusHandler);

// GET /spawn-queue/pending — pending spawn requests
app.get("/spawn-queue/pending", spawnQueuePendingHandler);

// GET /spawn-queue/active — active agents
app.get("/spawn-queue/active", spawnQueueActiveHandler);

// GET /spawn-blocks — recent blocked-spawn events
app.get("/spawn-blocks", spawnBlocksHandler);

// GET /spawn-blocks/* — sub-paths (legacy prefix match)
app.get("/spawn-blocks/*", spawnBlocksSubHandler);

// ---------------------------------------------------------------------------
// P3 routes — auth+RBAC gated, DuckDB-backed reads (stats.duckdb)
// ---------------------------------------------------------------------------

// GET /stats/metrics/summary — latest value per metric (mirrors stats_reader.summary)
app.get("/stats/metrics/summary", statsMetricsSummaryHandler);

// GET /stats/metrics/series/:name?since_hours=168 — time-series for one metric
app.get("/stats/metrics/series/:name", statsMetricsSeriesHandler);

// ---------------------------------------------------------------------------
// P4a routes — auth+RBAC gated, POST mutations (loopback-only, parity-gated)
// Python remains writer of record. TS write paths are opt-in, parity-proven.
// ---------------------------------------------------------------------------

// POST /budget/init — initialize budget session (writes 3 blackboard keys to state.db)
// Chosen as the P4a proof route: bounded write to 3 rows, no side effects beyond
// state.db, deterministic response, no subprocess spawns.
app.post("/budget/init", budgetInitHandler);

// ---------------------------------------------------------------------------
// P5 routes — SSE streaming (query-param-aware auth, NOT behind default-deny)
// /feed and /events are PUBLIC_ROUTES — default-deny passes them through.
// Each handler does its own auth check (Bearer header OR ?token= query param)
// to support EventSource clients that cannot set headers.
// ---------------------------------------------------------------------------

// GET /feed — SSE stream of agent-feed.jsonl events (mirrors rpc_sse.py /feed)
app.get("/feed", feedHandler);

// GET /events — SSE stream of all event types with _event_type (mirrors rpc_sse.py /events)
app.get("/events", eventsHandler);

// ---------------------------------------------------------------------------
// P6a routes — JSON-RPC 2.0 dispatch (RPC token auth, NOT default-deny)
// /rpc is in PUBLIC_ROUTES so default-deny lets requests through.
// The handler self-authenticates against the RPC token (separate from
// AF_API_AUTH_KEY) — mirroring how Python registers /rpc.
// ---------------------------------------------------------------------------

// POST /rpc — JSON-RPC 2.0 dispatch (read-only methods + dispatch parity)
app.post("/rpc", rpcDispatchHandler);

// ---------------------------------------------------------------------------
// P6c routes — home-grown GraphQL /graphql endpoint (auth+RBAC gated)
// ---------------------------------------------------------------------------

// POST /graphql — home-grown hand-rolled GraphQL (mirrors Python graphql_api.py)
app.post("/graphql", graphqlHandler);

// ---------------------------------------------------------------------------
// Server boot
// ---------------------------------------------------------------------------

const PORT = parseInt(process.env.TS_BACKEND_PORT ?? "19099", 10);
const HOST = "127.0.0.1";

export default {
  port: PORT,
  hostname: HOST,
  fetch: app.fetch,
};

if (import.meta.main) {
  console.log(`[ts-backend] Bun+Hono server listening on http://${HOST}:${PORT}`);
  console.log(`[ts-backend] Routes: GET /health (public)`);
  console.log(`[ts-backend] Routes: GET /sessions /sessions/current /sessions/compare /sessions/:id (auth+RBAC)`);
  console.log(`[ts-backend] Routes: GET /spawn-queue /spawn-queue/pending /spawn-queue/active /spawn-blocks (auth+RBAC)`);
  console.log(`[ts-backend] Routes: GET /stats/metrics/summary /stats/metrics/series/:name (auth+RBAC, DuckDB)`);
  console.log(`[ts-backend] Routes: POST /budget/init (auth+RBAC, P4a mutation parity)`);
  console.log(`[ts-backend] Routes: GET /feed /events (P5 SSE, query-param-aware auth)`);
  console.log(`[ts-backend] Routes: POST /rpc (P6a JSON-RPC 2.0, RPC-token auth, read-only methods)`);
  console.log(`[ts-backend] Routes: POST /graphql (P6c home-grown GraphQL, auth+RBAC gated)`);

  console.log(`[ts-backend] DuckDB: ${process.env.STATS_DB_PATH ?? statsDb()}`);
  console.log(`[ts-backend] Auth: ${process.env.AF_API_AUTH_KEY ? "enabled" : "disabled (AF_API_AUTH_KEY not set)"}`);
  console.log(`[ts-backend] Rate limit: ${process.env.AF_RATE_LIMIT_DISABLED === "1" ? "disabled" : "enabled (1 req/s, burst 60)"}`);
}
