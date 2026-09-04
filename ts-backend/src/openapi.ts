/**
 * openapi.ts — OpenAPI 3.1 spec generator for the ts-backend.
 *
 * Builds the spec document object from a route registry. Data-driven:
 * adding a new route = adding an entry to ROUTE_REGISTRY, not editing a
 * hand-maintained JSON blob.
 *
 * Exposed via GET /openapi.json (public, no auth required).
 * The committed snapshot at ts-backend/openapi.json is regenerated with:
 *   bun run openapi:gen
 */

// ---------------------------------------------------------------------------
// Shared schema components
// ---------------------------------------------------------------------------

const COMPONENTS = {
  securitySchemes: {
    BearerAuth: {
      type: "http",
      scheme: "bearer",
      description:
        "Bearer token from AF_API_AUTH_KEY. Required when auth is enabled. " +
        "SSE routes (/feed, /events) also accept ?token=<key> as a query param " +
        "because EventSource clients cannot set custom headers.",
    },
  },
  schemas: {
    // -----------------------------------------------------------------------
    // Error shapes
    // -----------------------------------------------------------------------
    DetailError: {
      type: "object",
      required: ["detail"],
      properties: {
        detail: { type: "string", description: "Human-readable error message" },
      },
    },
    // -----------------------------------------------------------------------
    // Health
    // -----------------------------------------------------------------------
    HealthResponse: {
      type: "object",
      required: ["_api_version", "ok"],
      properties: {
        _api_version: { type: "integer", example: 1 },
        ok: { type: "boolean", example: true },
        loop_last_run: {
          type: ["string", "null"],
          description: "ISO 8601 timestamp of last loop run, or null if unknown",
          example: "2026-05-23T14:00:00Z",
        },
        loop_duration_s: {
          type: ["integer", "null"],
          description: "Duration of last loop run in seconds (truncated), or null",
          example: 42,
        },
        loop_idle_rate: {
          type: ["number", "null"],
          description:
            "Fraction of recent loop iterations that were idle (0.0–1.0), " +
            "computed over last 10 entries. Null if no valid data.",
          example: 0.2,
        },
        malformed_lines: {
          type: "integer",
          description: "Number of malformed lines in loop-metrics.jsonl",
          example: 0,
        },
      },
    },
    // -----------------------------------------------------------------------
    // Sessions
    // -----------------------------------------------------------------------
    Session: {
      type: "object",
      required: ["session_id"],
      properties: {
        session_id: { type: "string", example: "session-2026-05-23" },
        started_at: {
          type: ["string", "null"],
          description: "ISO 8601 start timestamp",
          example: "2026-05-23T08:00:00Z",
        },
        ended_at: {
          type: ["string", "null"],
          description: "ISO 8601 end timestamp, or null for an active session",
          example: null,
        },
        iteration_count: { type: "integer", example: 12 },
        prs_merged: {
          type: "array",
          items: { type: "integer" },
          example: [1430, 1431],
        },
        discussions_completed: {
          type: "array",
          items: { type: "integer" },
          example: [1437],
        },
      },
      additionalProperties: true,
      description: "Session file contents. Additional fields may be present.",
    },
    SessionListResponse: {
      type: "object",
      required: ["sessions"],
      properties: {
        sessions: {
          type: "array",
          items: { $ref: "#/components/schemas/Session" },
          description: "Sessions sorted newest-first, up to 20 entries",
        },
      },
    },
    SessionCompareResponse: {
      type: "object",
      required: ["a", "b", "delta"],
      properties: {
        a: { $ref: "#/components/schemas/Session" },
        b: { $ref: "#/components/schemas/Session" },
        delta: {
          type: "object",
          required: ["iterations", "prs", "discussions", "duration_minutes"],
          properties: {
            iterations: {
              type: "integer",
              description: "a.iteration_count - b.iteration_count",
            },
            prs: {
              type: "integer",
              description: "len(a.prs_merged) - len(b.prs_merged)",
            },
            discussions: {
              type: "integer",
              description:
                "len(a.discussions_completed) - len(b.discussions_completed)",
            },
            duration_minutes: {
              type: ["number", "null"],
              description:
                "Duration difference in minutes (a - b), or null if either " +
                "started_at is absent/invalid",
            },
          },
        },
      },
    },
    // -----------------------------------------------------------------------
    // Spawn queue / blocks
    // -----------------------------------------------------------------------
    RoleUtilization: {
      type: "object",
      required: ["active", "limit"],
      properties: {
        active: { type: "integer", example: 1 },
        limit: { type: "integer", example: 2 },
      },
    },
    SpawnQueueStatus: {
      type: "object",
      required: [
        "pending",
        "active_total",
        "total_limit",
        "utilization_pct",
        "by_role",
        "completed",
        "failed",
      ],
      properties: {
        pending: { type: "integer", description: "Number of pending spawn requests" },
        active_total: { type: "integer", description: "Number of active agents" },
        total_limit: { type: "integer", description: "Configured total concurrency limit" },
        utilization_pct: {
          type: "integer",
          description: "active_total / total_limit * 100, floored",
          example: 33,
        },
        by_role: {
          type: "object",
          additionalProperties: { $ref: "#/components/schemas/RoleUtilization" },
          description: "Per-role active count and limit",
        },
        completed: { type: "integer", description: "Total completed spawns" },
        failed: { type: "integer", description: "Total failed spawns" },
      },
    },
    SpawnQueueEntries: {
      type: "object",
      required: ["pending"],
      properties: {
        pending: {
          type: "array",
          items: { type: "object", additionalProperties: true },
          description: "Raw pending queue entries from spawn-queue.json",
        },
      },
    },
    SpawnQueueActiveEntries: {
      type: "object",
      required: ["active"],
      properties: {
        active: {
          type: "array",
          items: { type: "object", additionalProperties: true },
          description: "Raw active queue entries from spawn-queue.json",
        },
      },
    },
    SpawnBlockEvent: {
      type: "object",
      required: ["role", "reason", "ts"],
      properties: {
        role: { type: "string", example: "executor" },
        reason: { type: "string", example: "concurrency_limit" },
        ts: { type: "string", description: "ISO 8601 timestamp", example: "2026-05-23T14:00:00Z" },
        discussion: {
          description: "Discussion number or null",
          oneOf: [{ type: "integer" }, { type: "null" }],
        },
      },
    },
    // -----------------------------------------------------------------------
    // Stats metrics
    // -----------------------------------------------------------------------
    MetricEntry: {
      type: "object",
      required: ["name", "value", "unit", "updated_at_iso"],
      properties: {
        name: { type: "string", example: "prs_merged" },
        value: {
          description: "Numeric metric value (integer or float)",
          oneOf: [{ type: "number" }, { type: "null" }],
        },
        unit: { type: "string", example: "count" },
        updated_at_iso: {
          type: ["string", "null"],
          description: "ISO 8601 timestamp of last update",
          example: "2026-05-23T14:00:00Z",
        },
      },
    },
    MetricsSummaryResponse: {
      type: "object",
      required: ["metrics"],
      properties: {
        metrics: {
          type: "array",
          items: { $ref: "#/components/schemas/MetricEntry" },
          description: "Latest value per metric, sorted by metric name",
        },
      },
    },
    MetricPoint: {
      type: "object",
      required: ["ts_iso", "value"],
      properties: {
        ts_iso: { type: "string", example: "2026-05-23T14:00:00Z" },
        value: {
          description: "Metric value at this timestamp",
          oneOf: [{ type: "number" }, { type: "null" }],
        },
      },
    },
    MetricsSeriesResponse: {
      type: "object",
      required: ["name", "points"],
      properties: {
        name: { type: "string", example: "prs_merged" },
        points: {
          type: "array",
          items: { $ref: "#/components/schemas/MetricPoint" },
          description: "Time-ordered data points for the requested window",
        },
      },
    },
    // -----------------------------------------------------------------------
    // Budget init
    // -----------------------------------------------------------------------
    BudgetStatus: {
      type: "object",
      required: [
        "ceiling",
        "spent",
        "remaining",
        "per_agent_ceiling",
        "warn_threshold_pct",
        "agents",
      ],
      properties: {
        ceiling: { type: "number", example: 5000000 },
        spent: { type: "number", example: 0 },
        remaining: { type: "number", example: 5000000 },
        per_agent_ceiling: { type: "number", example: 500000 },
        warn_threshold_pct: { type: "number", example: 80 },
        agents: {
          type: "array",
          items: { type: "object", additionalProperties: true },
          description: "Per-agent budget usage records",
        },
      },
    },
    BudgetInitRequest: {
      type: "object",
      properties: {
        ceiling: {
          type: "integer",
          description:
            "Session token ceiling. Must be a positive integer. " +
            "Omit to use the configured default (5,000,000).",
          example: 5000000,
        },
      },
    },
    BudgetInitResponse: {
      type: "object",
      required: ["ok", "status"],
      properties: {
        ok: { type: "boolean", example: true },
        status: { $ref: "#/components/schemas/BudgetStatus" },
      },
    },
    // -----------------------------------------------------------------------
    // JSON-RPC 2.0
    // -----------------------------------------------------------------------
    JsonRpcRequest: {
      type: "object",
      required: ["jsonrpc", "method", "id"],
      properties: {
        jsonrpc: { type: "string", enum: ["2.0"] },
        method: {
          type: "string",
          description:
            "RPC method name. Read-only methods are natively implemented " +
            "(stats.summary, stats.series); other read-only methods are proxied " +
            "to the Python backend. Mutating methods (loop.start, loop.stop, " +
            "fleet.discovery_ack, auth_retry.record, dial.set) return " +
            "method-not-found (-32601) in P6a.",
          example: "stats.summary",
        },
        params: {
          type: "object",
          additionalProperties: true,
          description: "Method-specific parameters (positional array or named object)",
        },
        id: {
          description: "Request ID — string, integer, or null",
          oneOf: [{ type: "string" }, { type: "integer" }, { type: "null" }],
        },
      },
    },
    JsonRpcResponse: {
      type: "object",
      required: ["jsonrpc", "id"],
      properties: {
        jsonrpc: { type: "string", enum: ["2.0"] },
        id: {
          description: "Echoes the request id",
          oneOf: [{ type: "string" }, { type: "integer" }, { type: "null" }],
        },
        result: {
          description: "Present on success; absent on error",
          type: "object",
          additionalProperties: true,
        },
        error: {
          type: "object",
          description: "Present on error; absent on success",
          required: ["code", "message"],
          properties: {
            code: {
              type: "integer",
              description:
                "-32700 parse error, -32600 invalid request, -32601 method not found, " +
                "-32000 server error / auth failure",
            },
            message: { type: "string" },
            data: { additionalProperties: true },
          },
        },
      },
    },
    // -----------------------------------------------------------------------
    // GraphQL
    // -----------------------------------------------------------------------
    GraphqlRequest: {
      type: "object",
      required: ["query"],
      properties: {
        query: {
          type: "string",
          description:
            "GraphQL query string. Supported root fields: health, budget, cost, " +
            "registry, agents, kpi, control, audit, replays, spawnQueue, " +
            "notifications, plugins. __schema and __type introspection are supported.",
          example: "{ health { ok loop { healthy } } }",
        },
      },
    },
    GraphqlResponse: {
      type: "object",
      properties: {
        data: {
          type: "object",
          additionalProperties: true,
          description: "Present when the query executed (may coexist with errors)",
        },
        errors: {
          type: "array",
          items: {
            type: "object",
            required: ["message"],
            properties: {
              message: { type: "string" },
              path: { type: "string" },
            },
          },
          description: "Present when one or more field errors occurred",
        },
      },
    },
    // -----------------------------------------------------------------------
    // SSE frame (described as text, not JSON — for documentation purposes)
    // -----------------------------------------------------------------------
    SseFrame: {
      type: "string",
      description:
        'Server-Sent Events wire format: each event is "data: <json>\\n\\n". ' +
        'The initial frame is {type:"connected"}, heartbeats are {type:"heartbeat"} ' +
        "(every 30s idle). Actual event payloads are JSONL records from agent-feed.jsonl.",
      example: 'data: {"type":"connected"}\n\ndata: {"event_type":"agent_run",...}\n\n',
    },
  },
};

// ---------------------------------------------------------------------------
// Route registry — one entry per distinct path+method combination.
// Each entry maps to a full OpenAPI Operation object.
// ---------------------------------------------------------------------------

interface RouteEntry {
  path: string;
  method: "get" | "post";
  operation: Record<string, unknown>;
}

const ROUTE_REGISTRY: RouteEntry[] = [
  // -------------------------------------------------------------------------
  // GET /health
  // -------------------------------------------------------------------------
  {
    path: "/health",
    method: "get",
    operation: {
      summary: "Health check",
      description:
        "Returns server liveness and loop metrics. Public — no auth required. " +
        "Reads .autonomous-team/loop-metrics.jsonl. " +
        "Dynamic fields (loop_last_run, loop_duration_s, loop_idle_rate) change on every loop run.",
      operationId: "getHealth",
      tags: ["Health"],
      security: [],
      responses: {
        "200": {
          description: "Server is up",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/HealthResponse" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /sessions
  // -------------------------------------------------------------------------
  {
    path: "/sessions",
    method: "get",
    operation: {
      summary: "List sessions",
      description:
        "Returns up to 20 sessions sorted newest-first. " +
        "Reads .autonomous-team/sessions/*.json.",
      operationId: "listSessions",
      tags: ["Sessions"],
      security: [{ BearerAuth: [] }],
      responses: {
        "200": {
          description: "Session list",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SessionListResponse" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /sessions/current
  // -------------------------------------------------------------------------
  {
    path: "/sessions/current",
    method: "get",
    operation: {
      summary: "Current active session",
      description:
        "Returns the session whose ended_at is null, or 404 if no active session exists.",
      operationId: "getCurrentSession",
      tags: ["Sessions"],
      security: [{ BearerAuth: [] }],
      responses: {
        "200": {
          description: "The current active session",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/Session" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "404": {
          description: "No active session",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /sessions/compare
  // -------------------------------------------------------------------------
  {
    path: "/sessions/compare",
    method: "get",
    operation: {
      summary: "Compare two sessions",
      description:
        "Returns both session objects plus a delta of iterations, PRs merged, " +
        "discussions completed, and duration.",
      operationId: "compareSessions",
      tags: ["Sessions"],
      security: [{ BearerAuth: [] }],
      parameters: [
        {
          name: "a",
          in: "query",
          required: true,
          schema: { type: "string" },
          description: "session_id of the first session",
        },
        {
          name: "b",
          in: "query",
          required: true,
          schema: { type: "string" },
          description: "session_id of the second session",
        },
      ],
      responses: {
        "200": {
          description: "Session comparison result",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SessionCompareResponse" },
            },
          },
        },
        "400": {
          description: "Both 'a' and 'b' query params are required",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "404": {
          description: "One or both session IDs not found",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /sessions/{session_id}
  // -------------------------------------------------------------------------
  {
    path: "/sessions/{session_id}",
    method: "get",
    operation: {
      summary: "Get session by ID",
      description:
        "Returns a single session object. Note: /sessions/current and " +
        "/sessions/compare are registered before this route to prevent " +
        "the path parameter from swallowing them.",
      operationId: "getSession",
      tags: ["Sessions"],
      security: [{ BearerAuth: [] }],
      parameters: [
        {
          name: "session_id",
          in: "path",
          required: true,
          schema: { type: "string" },
          description: "The session ID (filename without .json extension)",
        },
      ],
      responses: {
        "200": {
          description: "Session object",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/Session" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "404": {
          description: "Session not found",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /spawn-queue
  // -------------------------------------------------------------------------
  {
    path: "/spawn-queue",
    method: "get",
    operation: {
      summary: "Spawn queue status",
      description:
        "Returns queue depth, active agent count, utilization, and per-role breakdown. " +
        "Reads .autonomous-team/spawn-queue.json (read-only; does not call _cleanup_stale).",
      operationId: "getSpawnQueueStatus",
      tags: ["Spawn Queue"],
      security: [{ BearerAuth: [] }],
      responses: {
        "200": {
          description: "Queue status",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SpawnQueueStatus" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /spawn-queue/pending
  // -------------------------------------------------------------------------
  {
    path: "/spawn-queue/pending",
    method: "get",
    operation: {
      summary: "Pending spawn requests",
      description:
        "Returns the raw list of pending spawn requests from spawn-queue.json.",
      operationId: "getSpawnQueuePending",
      tags: ["Spawn Queue"],
      security: [{ BearerAuth: [] }],
      responses: {
        "200": {
          description: "Pending spawn requests",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SpawnQueueEntries" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /spawn-queue/active
  // -------------------------------------------------------------------------
  {
    path: "/spawn-queue/active",
    method: "get",
    operation: {
      summary: "Active agents",
      description: "Returns the raw list of active agent entries from spawn-queue.json.",
      operationId: "getSpawnQueueActive",
      tags: ["Spawn Queue"],
      security: [{ BearerAuth: [] }],
      responses: {
        "200": {
          description: "Active agent entries",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SpawnQueueActiveEntries" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /spawn-blocks
  // -------------------------------------------------------------------------
  {
    path: "/spawn-blocks",
    method: "get",
    operation: {
      summary: "Recent blocked spawn events",
      description:
        "Returns the most recent spawn_blocked events from agent-feed.jsonl, " +
        "newest-first. /spawn-blocks/* sub-paths return the same data (legacy prefix match).",
      operationId: "getSpawnBlocks",
      tags: ["Spawn Queue"],
      security: [{ BearerAuth: [] }],
      parameters: [
        {
          name: "limit",
          in: "query",
          required: false,
          schema: { type: "integer", default: 10, minimum: 1 },
          description: "Maximum number of events to return (default 10)",
        },
      ],
      responses: {
        "200": {
          description: "Array of blocked spawn events",
          content: {
            "application/json": {
              schema: {
                type: "array",
                items: { $ref: "#/components/schemas/SpawnBlockEvent" },
              },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /stats/metrics/summary
  // -------------------------------------------------------------------------
  {
    path: "/stats/metrics/summary",
    method: "get",
    operation: {
      summary: "Latest metric values",
      description:
        "Returns the latest value for each metric in the DuckDB stats store. " +
        "Mirrors Python stats_reader.summary(). Returns {metrics:[]} when the " +
        "DuckDB file is absent (graceful fallback).",
      operationId: "getMetricsSummary",
      tags: ["Stats"],
      security: [{ BearerAuth: [] }],
      responses: {
        "200": {
          description: "Summary of latest metric values",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/MetricsSummaryResponse" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /stats/metrics/series/{name}
  // -------------------------------------------------------------------------
  {
    path: "/stats/metrics/series/{name}",
    method: "get",
    operation: {
      summary: "Metric time series",
      description:
        "Returns ordered (ts_iso, value) data points for a single metric over " +
        "a configurable look-back window. Mirrors Python stats_reader.series(). " +
        "Returns {name, points:[]} when the DuckDB file is absent or the metric " +
        "has no data in the window.",
      operationId: "getMetricsSeries",
      tags: ["Stats"],
      security: [{ BearerAuth: [] }],
      parameters: [
        {
          name: "name",
          in: "path",
          required: true,
          schema: { type: "string" },
          description: "Metric name (e.g. prs_merged, active_agents)",
          example: "prs_merged",
        },
        {
          name: "since_hours",
          in: "query",
          required: false,
          schema: { type: "integer", default: 168, minimum: 1, maximum: 8760 },
          description: "Look-back window in hours. Default 168 (7 days). Clamped to 1–8760.",
        },
      ],
      responses: {
        "200": {
          description: "Time-series data points for the metric",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/MetricsSeriesResponse" },
            },
          },
        },
        "400": {
          description: "name path parameter missing",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /feed (SSE)
  // -------------------------------------------------------------------------
  {
    path: "/feed",
    method: "get",
    operation: {
      summary: "Agent feed SSE stream",
      description:
        'Server-Sent Events stream that tails .autonomous-team/agent-feed.jsonl. ' +
        'Auth: Bearer header OR ?token= query param (EventSource-compatible). ' +
        'Initial frame: {type:"connected"}. Heartbeat every 30s idle: {type:"heartbeat"}. ' +
        "Poll interval: 500ms. Supports ?since=<iso> and ?filter[role]=<role> filters.",
      operationId: "getFeedStream",
      tags: ["Streaming"],
      security: [{ BearerAuth: [] }],
      parameters: [
        {
          name: "token",
          in: "query",
          required: false,
          schema: { type: "string" },
          description:
            "Bearer token as a query param. Use when Authorization header is not settable " +
            "(EventSource clients).",
        },
        {
          name: "since",
          in: "query",
          required: false,
          schema: { type: "string" },
          description: "ISO 8601 timestamp. Only emit events with ts >= this value.",
        },
        {
          name: "filter[role]",
          in: "query",
          required: false,
          schema: { type: "string" },
          description: "Filter to events where role matches this value exactly.",
        },
      ],
      responses: {
        "200": {
          description: "SSE stream of agent feed events",
          content: {
            "text/event-stream": {
              schema: { $ref: "#/components/schemas/SseFrame" },
            },
          },
          headers: {
            "Content-Type": {
              schema: { type: "string", example: "text/event-stream; charset=utf-8" },
            },
            "X-Accel-Buffering": {
              schema: { type: "string", example: "no" },
              description: "Disables nginx buffering for real-time SSE delivery",
            },
          },
        },
        "401": {
          description: "No credentials provided (auth enabled, no token)",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /events (SSE)
  // -------------------------------------------------------------------------
  {
    path: "/events",
    method: "get",
    operation: {
      summary: "Events SSE stream (typed)",
      description:
        "Server-Sent Events stream that tails agent-feed.jsonl and adds an " +
        "_event_type discriminator field to each event. Event types inferred: " +
        "BudgetSpendEvent, GateChangeEvent, LoopIterationEvent, AgentOutputEvent. " +
        "Auth: Bearer header OR ?token= query param. Supports ?loop_id= and ?since= filters.",
      operationId: "getEventsStream",
      tags: ["Streaming"],
      security: [{ BearerAuth: [] }],
      parameters: [
        {
          name: "token",
          in: "query",
          required: false,
          schema: { type: "string" },
          description: "Bearer token as query param (EventSource-compatible auth).",
        },
        {
          name: "loop_id",
          in: "query",
          required: false,
          schema: { type: "string" },
          description: "Filter to events from a specific loop run.",
        },
        {
          name: "since",
          in: "query",
          required: false,
          schema: { type: "string" },
          description: "ISO 8601 timestamp. Only emit events with ts >= this value.",
        },
      ],
      responses: {
        "200": {
          description: "SSE stream of typed events",
          content: {
            "text/event-stream": {
              schema: { $ref: "#/components/schemas/SseFrame" },
            },
          },
          headers: {
            "Content-Type": {
              schema: { type: "string", example: "text/event-stream; charset=utf-8" },
            },
            "X-Accel-Buffering": {
              schema: { type: "string", example: "no" },
            },
          },
        },
        "401": {
          description: "No credentials provided (auth enabled, no token)",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // POST /budget/init
  // -------------------------------------------------------------------------
  {
    path: "/budget/init",
    method: "post",
    operation: {
      summary: "Initialize budget session",
      description:
        "Writes three file-based blackboard entries: budget/session_ceiling, " +
        "budget/session_spent (0), and budget/per_agent_ceiling. " +
        "Python remains writer of record on the production blackboard; this route " +
        "is loopback-only and parity-gated.",
      operationId: "initBudget",
      tags: ["Budget"],
      security: [{ BearerAuth: [] }],
      requestBody: {
        required: false,
        content: {
          "application/json": {
            schema: { $ref: "#/components/schemas/BudgetInitRequest" },
          },
        },
      },
      responses: {
        "200": {
          description: "Budget initialized successfully",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/BudgetInitResponse" },
            },
          },
        },
        "400": {
          description: "'ceiling' present but not a positive integer",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // POST /rpc
  // -------------------------------------------------------------------------
  {
    path: "/rpc",
    method: "post",
    operation: {
      summary: "JSON-RPC 2.0 dispatch",
      description:
        "JSON-RPC 2.0 endpoint. Auth: RPC token from .autonomous-team/dashboard-token " +
        "(separate from AF_API_AUTH_KEY). Accepts Bearer header OR ?token= query param. " +
        "Natively implemented read-only methods: stats.summary, stats.series. " +
        "Other read-only methods are proxied to the Python backend. " +
        "Mutating methods (loop.start, loop.stop, fleet.discovery_ack, " +
        "auth_retry.record, dial.set) return method-not-found (-32601) in P6a. " +
        "Invalid JSON body → HTTP 400. Auth failure → HTTP 401. " +
        "Unknown method and handler errors → HTTP 200 with JSON-RPC error envelope.",
      operationId: "rpcDispatch",
      tags: ["RPC"],
      security: [],
      requestBody: {
        required: true,
        content: {
          "application/json": {
            schema: { $ref: "#/components/schemas/JsonRpcRequest" },
          },
        },
      },
      responses: {
        "200": {
          description: "JSON-RPC response (success or method-level error)",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/JsonRpcResponse" },
            },
          },
        },
        "400": {
          description: "Invalid JSON body",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/JsonRpcResponse" },
            },
          },
        },
        "401": {
          description: "Auth failure",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/JsonRpcResponse" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // POST /graphql
  // -------------------------------------------------------------------------
  {
    path: "/graphql",
    method: "post",
    operation: {
      summary: "Home-grown GraphQL",
      description:
        "Hand-rolled GraphQL endpoint with 1:1 parity to the Python graphql_api.py " +
        "implementation. Supported root query fields: health, budget, cost, registry, " +
        "agents, kpi, control, audit, replays, spawnQueue, notifications, plugins. " +
        "__schema and __type introspection are supported. Auth: bearer token + RBAC. " +
        "Note: several fields are always null due to documented key-name mismatches " +
        "in the Python reference (budget.used, cost.total_usd, registry.stats.open, etc.).",
      operationId: "graphql",
      tags: ["GraphQL"],
      security: [{ BearerAuth: [] }],
      requestBody: {
        required: true,
        content: {
          "application/json": {
            schema: { $ref: "#/components/schemas/GraphqlRequest" },
          },
        },
      },
      responses: {
        "200": {
          description:
            "GraphQL response. data may coexist with errors when partial results " +
            "are available.",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/GraphqlResponse" },
            },
          },
        },
        "400": {
          description: "'query' field missing from request body",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "401": {
          description: "No credentials provided",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
        "403": {
          description: "Invalid token or RBAC denial",
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/DetailError" },
            },
          },
        },
      },
    },
  },
  // -------------------------------------------------------------------------
  // GET /openapi.json (self-referential)
  // -------------------------------------------------------------------------
  {
    path: "/openapi.json",
    method: "get",
    operation: {
      summary: "OpenAPI spec",
      description:
        "Returns this OpenAPI 3.1 specification document. Public — no auth required.",
      operationId: "getOpenApiSpec",
      tags: ["Meta"],
      security: [],
      responses: {
        "200": {
          description: "OpenAPI 3.1 document",
          content: {
            "application/json": {
              schema: { type: "object", additionalProperties: true },
            },
          },
        },
      },
    },
  },
];

// ---------------------------------------------------------------------------
// Document builder
// ---------------------------------------------------------------------------

export interface OpenApiDocument {
  openapi: string;
  info: Record<string, unknown>;
  servers: Record<string, unknown>[];
  security: Record<string, unknown>[];
  tags: Record<string, unknown>[];
  paths: Record<string, Record<string, unknown>>;
  components: typeof COMPONENTS;
}

/**
 * Build and return the OpenAPI 3.1 document.
 *
 * Consumes ROUTE_REGISTRY to build the paths object — no hand-maintained JSON.
 * Called at server startup (for /openapi.json) and by the `openapi:gen` script.
 */
export function buildOpenApiDocument(): OpenApiDocument {
  const paths: Record<string, Record<string, unknown>> = {};

  for (const entry of ROUTE_REGISTRY) {
    if (!paths[entry.path]) {
      paths[entry.path] = {};
    }
    paths[entry.path][entry.method] = entry.operation;
  }

  return {
    openapi: "3.1.0",
    info: {
      title: "fulcrumaxe ts-backend API",
      version: "0.1.0",
      description:
        "TypeScript/Bun+Hono backend — mirrors the Python backend with 1:1 parity. " +
        "Runs on 127.0.0.1:19099 (loopback-only). Auth: set AF_API_AUTH_KEY to enable; " +
        "unset = auth disabled. SSE routes (/feed, /events) accept ?token= for " +
        "EventSource clients that cannot send custom headers. " +
        "See https://github.com/fulcrumaxe/fulcrumaxe/discussions/1437 " +
        "for the full phased migration plan.",
      contact: {
        url: "https://github.com/fulcrumaxe/fulcrumaxe",
      },
      license: {
        name: "MIT",
      },
    },
    servers: [
      {
        url: "http://127.0.0.1:19099",
        description: "Default loopback server (TS_BACKEND_PORT overrides 19099)",
      },
    ],
    security: [],
    tags: [
      { name: "Health", description: "Server health and loop metrics" },
      { name: "Sessions", description: "Agent session management (read-only)" },
      { name: "Spawn Queue", description: "Spawn queue status and blocked-spawn events" },
      { name: "Stats", description: "DuckDB-backed metric time series and summaries" },
      { name: "Streaming", description: "Server-Sent Events streams (agent feed, typed events)" },
      { name: "Budget", description: "Budget session initialization (P4a mutation)" },
      { name: "RPC", description: "JSON-RPC 2.0 dispatch" },
      { name: "GraphQL", description: "Home-grown GraphQL endpoint" },
      { name: "Meta", description: "Spec and introspection" },
    ],
    paths,
    components: COMPONENTS,
  };
}
