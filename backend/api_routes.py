"""
Route registry for the autonomous-forever REST API.

Each entry describes one endpoint's metadata — used both by api.py for
routing and by openapi.py to build the OpenAPI 3.0.1 spec. Keeping this
in a separate module avoids circular imports between api.py and openapi.py.

Schema per entry:
    path        str    URL path, with <param> placeholders for path params
    method      str    HTTP method (GET | POST)
    summary     str    Short human-readable description (shown in Swagger)
    description str    Longer description (optional)
    tags        list   Tag group names (used to organise Swagger UI sections)
    auth        bool   True when the endpoint requires bearerAuth
    parameters  list   OpenAPI parameter objects (query/path) — optional
    request_body dict  OpenAPI schema for the POST body — optional
    responses   dict   {status_code: {"description": str, "schema": dict}}
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_ERROR_SCHEMA = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
}

_OK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}

_RATE_LIMIT_RESPONSE = {
    "429": {
        "description": "Rate limit exceeded",
        "schema": {
            "type": "object",
            "properties": {
                "error": {"type": "string"},
                "retry_after": {"type": "number"},
            },
        },
    }
}

# ---------------------------------------------------------------------------
# Route registry
# ---------------------------------------------------------------------------

ROUTES: list[dict] = [
    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    {
        "path": "/health",
        "method": "GET",
        "summary": "Health check",
        "description": "Returns {ok: true} if the server is running. No auth required.",
        "tags": ["health"],
        "auth": False,
        "responses": {
            200: {
                "description": "Server is healthy",
                "schema": _OK_SCHEMA,
            },
        },
    },
    {
        "path": "/health/loop",
        "method": "GET",
        "summary": "Loop health status",
        "description": (
            "Returns the age of the last loop iteration, the configured staleness "
            "threshold, and a boolean healthy flag. No auth required."
        ),
        "tags": ["health"],
        "auth": False,
        "responses": {
            200: {
                "description": "Loop health snapshot",
                "schema": {
                    "type": "object",
                    "properties": {
                        "healthy": {"type": "boolean"},
                        "age_seconds": {"type": "number"},
                        "threshold_seconds": {"type": "number"},
                        "last_run_at": {"type": "string", "format": "date-time"},
                    },
                },
            },
        },
    },
    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    {
        "path": "/metrics",
        "method": "GET",
        "summary": "Prometheus metrics",
        "description": (
            "Returns metrics in Prometheus text exposition format (0.0.4). "
            "No auth required. Suitable for scraping by Prometheus or compatible tools."
        ),
        "tags": ["metrics"],
        "auth": False,
        "responses": {
            200: {
                "description": "Prometheus text format metrics",
                "schema": {"type": "string"},
            },
        },
    },
    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------
    {
        "path": "/budget/status",
        "method": "GET",
        "summary": "Budget status",
        "description": "Returns the current session token budget snapshot.",
        "tags": ["budget"],
        "auth": True,
        "responses": {
            200: {
                "description": "Budget snapshot",
                "schema": {
                    "type": "object",
                    "properties": {
                        "used": {"type": "integer"},
                        "ceiling": {"type": "integer"},
                        "remaining": {"type": "integer"},
                        "pct_used": {"type": "number"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/budget/init",
        "method": "POST",
        "summary": "Initialize or reset budget",
        "description": "Initialise (or reset) the session token budget. Optionally supply a ceiling.",
        "tags": ["budget"],
        "auth": True,
        "request_body": {
            "type": "object",
            "properties": {
                "ceiling": {"type": "integer", "description": "Optional token ceiling override."},
            },
        },
        "responses": {
            200: {
                "description": "Budget initialised",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "status": {"type": "object"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Cost tracking
    # ------------------------------------------------------------------
    {
        "path": "/cost",
        "method": "GET",
        "summary": "Full cost breakdown",
        "description": "Returns session total cost in USD, per-agent, per-discussion, and model breakdown.",
        "tags": ["cost"],
        "auth": True,
        "responses": {
            200: {
                "description": "Cost breakdown",
                "schema": {
                    "type": "object",
                    "properties": {
                        "total_cost_usd": {"type": "number"},
                        "by_agent": {"type": "array", "items": {"type": "object"}},
                        "by_discussion": {"type": "array", "items": {"type": "object"}},
                        "model_breakdown": {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/cost/summary",
        "method": "GET",
        "summary": "Cost summary",
        "description": "Returns total session cost and per-model breakdown (lightweight).",
        "tags": ["cost"],
        "auth": True,
        "responses": {
            200: {
                "description": "Cost summary",
                "schema": {
                    "type": "object",
                    "properties": {
                        "total_cost_usd": {"type": "number"},
                        "model_breakdown": {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    {
        "path": "/registry",
        "method": "GET",
        "summary": "Full discussion registry",
        "description": "Returns all tracked GitHub Discussions with their status and velocity stats.",
        "tags": ["registry"],
        "auth": True,
        "responses": {
            200: {
                "description": "Registry data including stats",
                "schema": {
                    "type": "object",
                    "properties": {
                        "discussions": {"type": "array", "items": {"type": "object"}},
                        "stats": {"type": "object"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/registry/stats",
        "method": "GET",
        "summary": "Registry velocity stats",
        "description": "Returns only the velocity/status-count statistics from the registry.",
        "tags": ["registry"],
        "auth": True,
        "responses": {
            200: {
                "description": "Velocity stats",
                "schema": {"type": "object"},
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------
    {
        "path": "/control",
        "method": "GET",
        "summary": "Control plane gates and policies",
        "description": "Returns current feature gates and per-role policies.",
        "tags": ["control"],
        "auth": True,
        "responses": {
            200: {
                "description": "Gates and policies",
                "schema": {
                    "type": "object",
                    "properties": {
                        "gates": {"type": "object"},
                        "policies": {"type": "object"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/control/gates",
        "method": "GET",
        "summary": "Feature gates only",
        "description": "Returns just the gates section of the control plane.",
        "tags": ["control"],
        "auth": True,
        "responses": {
            200: {
                "description": "Gates map",
                "schema": {"type": "object"},
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/control/audit",
        "method": "GET",
        "summary": "Control plane audit log",
        "description": "Returns the ordered list of recent gate/policy changes.",
        "tags": ["control"],
        "auth": True,
        "responses": {
            200: {
                "description": "Audit log entries",
                "schema": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/control/set",
        "method": "POST",
        "summary": "Set a control plane key",
        "description": "Set a gate or policy value. Both key and value are required.",
        "tags": ["control"],
        "auth": True,
        "request_body": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
            },
            "required": ["key", "value"],
        },
        "responses": {
            200: {
                "description": "Key updated",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "key": {"type": "string"},
                        "value": {},
                    },
                },
            },
            400: {
                "description": "Missing key or value",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    {
        "path": "/agents",
        "method": "GET",
        "summary": "List agent card names",
        "description": "Returns the list of role names that have registered agent cards.",
        "tags": ["agents"],
        "auth": True,
        "responses": {
            200: {
                "description": "Agent names",
                "schema": {
                    "type": "object",
                    "properties": {
                        "agents": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/agents/<role>",
        "method": "GET",
        "summary": "Get agent card by role",
        "description": "Returns the full agent card for the given role name.",
        "tags": ["agents"],
        "auth": True,
        "parameters": [
            {
                "name": "role",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Agent role name (e.g. executor, code-reviewer)",
            }
        ],
        "responses": {
            200: {
                "description": "Agent card",
                "schema": {"type": "object"},
            },
            404: {
                "description": "Agent not found",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Plugins
    # ------------------------------------------------------------------
    {
        "path": "/plugins",
        "method": "GET",
        "summary": "List loaded plugins",
        "description": "Returns metadata for all plugins currently loaded by the PluginLoader.",
        "tags": ["plugins"],
        "auth": True,
        "responses": {
            200: {
                "description": "Plugin list",
                "schema": {
                    "type": "object",
                    "properties": {
                        "plugins": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "version": {"type": "string"},
                                    "review_pipeline": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/plugins/<name>",
        "method": "GET",
        "summary": "Get plugin detail",
        "description": "Returns the full plugin card for the given plugin name.",
        "tags": ["plugins"],
        "auth": True,
        "parameters": [
            {
                "name": "name",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Plugin name",
            }
        ],
        "responses": {
            200: {
                "description": "Plugin card",
                "schema": {"type": "object"},
            },
            404: {
                "description": "Plugin not found",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # KPI
    # ------------------------------------------------------------------
    {
        "path": "/kpi",
        "method": "GET",
        "summary": "Full KPI snapshot",
        "description": "Returns the full KPI computation result (cached for 60 seconds).",
        "tags": ["kpi"],
        "auth": True,
        "responses": {
            200: {
                "description": "KPI snapshot",
                "schema": {
                    "type": "object",
                    "properties": {
                        "version": {"type": "integer"},
                        "computed_at": {"type": "string"},
                        "velocity": {"type": "object"},
                        "estimation_accuracy": {"type": "object"},
                        "idle_rate": {"type": "object"},
                        "pr_cycle_time": {"type": "object"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/kpi/velocity",
        "method": "GET",
        "summary": "KPI velocity subsection",
        "description": "Returns just the velocity subsection of the KPI snapshot.",
        "tags": ["kpi"],
        "auth": True,
        "responses": {
            200: {
                "description": "Velocity data",
                "schema": {
                    "type": "object",
                    "properties": {
                        "last_24h": {"type": "integer"},
                        "all_time_per_day": {"type": "number"},
                        "total_done": {"type": "integer"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/kpi/cycle-time",
        "method": "GET",
        "summary": "PR cycle time subsection",
        "description": "Returns just the PR cycle time subsection of the KPI snapshot.",
        "tags": ["kpi"],
        "auth": True,
        "responses": {
            200: {
                "description": "Cycle time data",
                "schema": {
                    "type": "object",
                    "properties": {
                        "mean_hours": {"type": "number"},
                        "median_hours": {"type": "number"},
                        "total_measured": {"type": "integer"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Dependency graph
    # ------------------------------------------------------------------
    {
        "path": "/deps",
        "method": "GET",
        "summary": "Module dependency graph",
        "description": (
            "Static analysis of all backend Python modules. Returns the full dependency "
            "graph as JSON by default. Query params: module=X for single-module impact "
            "analysis, format=dot for Graphviz DOT output, format=ascii for ASCII tree, "
            "format=json (default) for full JSON graph."
        ),
        "tags": ["deps"],
        "auth": True,
        "responses": {
            200: {
                "description": "Dependency graph or impact analysis",
                "schema": {
                    "type": "object",
                    "properties": {
                        "modules": {"type": "array"},
                        "cycles": {"type": "array"},
                        "hubs": {"type": "array"},
                        "stats": {
                            "type": "object",
                            "properties": {
                                "total_modules": {"type": "integer"},
                                "total_edges": {"type": "integer"},
                                "max_depth": {"type": "integer"},
                                "avg_degree": {"type": "number"},
                            },
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # SSE streams
    # ------------------------------------------------------------------
    {
        "path": "/stream/feed",
        "method": "GET",
        "summary": "Agent feed SSE stream",
        "description": (
            "Server-Sent Events stream. Subscribes to AgentOutputEvent on the "
            "internal event bus and pushes each event to connected clients in real time. "
            "Disabled when the server is started with --no-enable-sse."
        ),
        "tags": ["stream"],
        "auth": True,
        "responses": {
            200: {
                "description": "SSE event stream (text/event-stream)",
                "schema": {"type": "string"},
            },
            404: {
                "description": "SSE endpoints are disabled",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/stream/status",
        "method": "GET",
        "summary": "Periodic status SSE stream",
        "description": (
            "Server-Sent Events stream. Pushes a full status snapshot every 10 seconds "
            "including budget, queue counts, loop age, and KPI data. "
            "Disabled when the server is started with --no-enable-sse."
        ),
        "tags": ["stream"],
        "auth": True,
        "responses": {
            200: {
                "description": "SSE status stream (text/event-stream)",
                "schema": {"type": "string"},
            },
            404: {
                "description": "SSE endpoints are disabled",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/stream/events",
        "method": "GET",
        "summary": "All event bus SSE stream",
        "description": (
            "Server-Sent Events stream. Pushes every event published to the internal "
            "event bus (AgentOutputEvent, BudgetSpendEvent, GateChangeEvent, "
            "LoopIterationEvent). Useful for debugging and monitoring. "
            "Disabled when the server is started with --no-enable-sse."
        ),
        "tags": ["stream"],
        "auth": True,
        "responses": {
            200: {
                "description": "SSE all-events stream (text/event-stream)",
                "schema": {"type": "string"},
            },
            404: {
                "description": "SSE endpoints are disabled",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    {
        "path": "/ws",
        "method": "GET",
        "summary": "WebSocket bidirectional endpoint",
        "description": (
            "WebSocket endpoint (RFC 6455). Clients must send an HTTP Upgrade request. "
            "Once connected, the server pushes all event bus events as JSON text frames "
            "and accepts inbound JSON commands: subscribe, unsubscribe, ping. "
            "Auth via ?token=<key> query parameter when AF_API_AUTH_KEY is set. "
            "Disabled when the server is started with --no-streaming (or --no-enable-sse)."
        ),
        "tags": ["stream"],
        "auth": True,
        "parameters": [
            {
                "name": "token",
                "in": "query",
                "required": False,
                "description": "Auth token (required when AF_API_AUTH_KEY is set)",
                "schema": {"type": "string"},
            }
        ],
        "responses": {
            101: {
                "description": "Switching Protocols — WebSocket connection established",
                "schema": {"type": "string"},
            },
            400: {
                "description": "Bad request (missing Upgrade header or key)",
                "schema": _ERROR_SCHEMA,
            },
            403: {
                "description": "Forbidden — invalid or missing auth token",
                "schema": _ERROR_SCHEMA,
            },
            404: {
                "description": "WebSocket endpoint is disabled",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    {
        "path": "/sessions",
        "method": "GET",
        "summary": "List sessions",
        "description": "Returns all recorded loop sessions sorted newest-first (up to 20).",
        "tags": ["sessions"],
        "auth": True,
        "parameters": [
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 20},
                "description": "Maximum number of sessions to return.",
            }
        ],
        "responses": {
            200: {
                "description": "Session list",
                "schema": {
                    "type": "object",
                    "properties": {
                        "sessions": {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/sessions/current",
        "method": "GET",
        "summary": "Current session",
        "description": "Returns the currently-open session (ended_at == null), or 404 if none.",
        "tags": ["sessions"],
        "auth": True,
        "responses": {
            200: {
                "description": "Active session",
                "schema": {"type": "object"},
            },
            404: {
                "description": "No active session",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/sessions/<session_id>",
        "method": "GET",
        "summary": "Get session by ID",
        "description": "Returns one session by its UUID, or 404 if not found.",
        "tags": ["sessions"],
        "auth": True,
        "parameters": [
            {
                "name": "session_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Session UUID",
            }
        ],
        "responses": {
            200: {
                "description": "Session data",
                "schema": {"type": "object"},
            },
            404: {
                "description": "Session not found",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/sessions/start",
        "method": "POST",
        "summary": "Start new session",
        "description": "Creates a new session file and closes any currently-open session.",
        "tags": ["sessions"],
        "auth": True,
        "responses": {
            200: {
                "description": "Newly created session",
                "schema": {"type": "object"},
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/sessions/close",
        "method": "POST",
        "summary": "Close current session",
        "description": "Sets ended_at on the active session. Returns 404 if no session is open.",
        "tags": ["sessions"],
        "auth": True,
        "responses": {
            200: {
                "description": "Closed session",
                "schema": {"type": "object"},
            },
            404: {
                "description": "No active session to close",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/sessions/compare",
        "method": "GET",
        "summary": "Compare two sessions",
        "description": "Returns session A, session B, and arithmetic deltas (iterations, PRs, discussions, duration).",
        "tags": ["sessions"],
        "auth": True,
        "parameters": [
            {
                "name": "a",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
                "description": "Session ID A",
            },
            {
                "name": "b",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
                "description": "Session ID B",
            },
        ],
        "responses": {
            200: {
                "description": "Comparison result with delta",
                "schema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "object"},
                        "b": {"type": "object"},
                        "delta": {
                            "type": "object",
                            "properties": {
                                "iterations": {"type": "integer"},
                                "prs": {"type": "integer"},
                                "discussions": {"type": "integer"},
                                "duration_minutes": {"type": "number"},
                            },
                        },
                    },
                },
            },
            400: {
                "description": "Missing query parameters",
                "schema": _ERROR_SCHEMA,
            },
            404: {
                "description": "One or both sessions not found",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Dashboard / Docs
    # ------------------------------------------------------------------
    {
        "path": "/dashboard",
        "method": "GET",
        "summary": "HTML operations dashboard",
        "description": (
            "Serves a self-contained HTML dashboard for monitoring the autonomous team. "
            "Disabled when the server is started with --no-dashboard."
        ),
        "tags": ["ui"],
        "auth": True,
        "responses": {
            200: {
                "description": "Dashboard HTML page (text/html)",
                "schema": {"type": "string"},
            },
            404: {
                "description": "Dashboard is disabled",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/openapi.json",
        "method": "GET",
        "summary": "OpenAPI 3.0.1 spec",
        "description": (
            "Returns the machine-readable OpenAPI 3.0.1 JSON specification for this API. "
            "Disabled when the server is started with --no-docs."
        ),
        "tags": ["ui"],
        "auth": False,
        "responses": {
            200: {
                "description": "OpenAPI spec (application/json)",
                "schema": {"type": "object"},
            },
            404: {
                "description": "Docs are disabled",
                "schema": _ERROR_SCHEMA,
            },
        },
    },
    {
        "path": "/docs",
        "method": "GET",
        "summary": "Swagger UI interactive docs",
        "description": (
            "Serves a self-contained HTML page that loads Swagger UI from CDN and "
            "points it at /openapi.json. Disabled when the server is started with --no-docs."
        ),
        "tags": ["ui"],
        "auth": False,
        "responses": {
            200: {
                "description": "Swagger UI HTML page (text/html)",
                "schema": {"type": "string"},
            },
            404: {
                "description": "Docs are disabled",
                "schema": _ERROR_SCHEMA,
            },
        },
    },
    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    {
        "path": "/notifications/history",
        "method": "GET",
        "summary": "Notification history",
        "description": (
            "Returns the last 50 notifications dispatched by the notifier, "
            "with event_type, channel_id, channel_type, timestamp, success, and error fields."
        ),
        "tags": ["notifications"],
        "auth": True,
        "responses": {
            200: {
                "description": "Recent notification records",
                "schema": {
                    "type": "object",
                    "properties": {
                        "notifications": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "timestamp": {"type": "string"},
                                    "event_type": {"type": "string"},
                                    "channel_id": {"type": "string"},
                                    "channel_type": {"type": "string"},
                                    "success": {"type": "boolean"},
                                    "error": {"type": "string"},
                                    "severity": {"type": "string"},
                                    "message": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/notifications/test",
        "method": "POST",
        "summary": "Send test notification",
        "description": (
            "Sends a test notification to all configured channels and returns "
            "success/failure for each."
        ),
        "tags": ["notifications"],
        "auth": True,
        "responses": {
            200: {
                "description": "Test results per channel",
                "schema": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "channel_id": {"type": "string"},
                                    "success": {"type": "boolean"},
                                    "error": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Backup / Restore
    # ------------------------------------------------------------------
    {
        "path": "/backup",
        "method": "POST",
        "summary": "Create a state backup",
        "description": (
            "Creates a timestamped tar.gz snapshot of .autonomous-team/ (excluding backups/ "
            "and __pycache__). Automatically prunes old backups to keep at most 20."
        ),
        "tags": ["backup"],
        "auth": True,
        "responses": {
            200: {
                "description": "Backup metadata",
                "schema": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "created_at": {"type": "string", "format": "date-time"},
                    },
                    "required": ["filename", "size_bytes", "created_at"],
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/backups",
        "method": "GET",
        "summary": "List state backups",
        "description": "Returns metadata for all snapshots, sorted newest-first.",
        "tags": ["backup"],
        "auth": True,
        "responses": {
            200: {
                "description": "Backup list",
                "schema": {
                    "type": "object",
                    "properties": {
                        "backups": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filename": {"type": "string"},
                                    "size_bytes": {"type": "integer"},
                                    "created_at": {"type": "string", "format": "date-time"},
                                },
                            },
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/backup/restore",
        "method": "POST",
        "summary": "Restore from a backup",
        "description": (
            "Extracts the named snapshot over the current .autonomous-team/ directory. "
            "A pre-restore safety backup is created automatically before overwriting."
        ),
        "tags": ["backup"],
        "auth": True,
        "request_body": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Backup filename to restore from."},
            },
            "required": ["filename"],
        },
        "responses": {
            200: {
                "description": "Restore result",
                "schema": {
                    "type": "object",
                    "properties": {
                        "restored_from": {"type": "string"},
                        "restored_at": {"type": "string", "format": "date-time"},
                        "safety_backup": {"type": "string"},
                    },
                },
            },
            400: {
                "description": "Missing or invalid filename",
                "schema": _ERROR_SCHEMA,
            },
            404: {
                "description": "Backup not found",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # RBAC
    # ------------------------------------------------------------------
    {
        "path": "/rbac/whoami",
        "method": "GET",
        "summary": "Caller identity and permissions",
        "description": (
            "Returns the role name, human-readable label, and allow-list for the "
            "bearer token used in this request. Useful for dashboards to discover "
            "what endpoints the current key can reach."
        ),
        "tags": ["rbac"],
        "auth": True,
        "responses": {
            200: {
                "description": "Caller role and permissions",
                "schema": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "label": {"type": "string"},
                        "permissions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["role", "label", "permissions"],
                },
            },
            401: {
                "description": "No bearer token supplied",
                "schema": _ERROR_SCHEMA,
            },
            403: {
                "description": "Token does not have permission for this endpoint",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Agent profiler
    # ------------------------------------------------------------------
    {
        "path": "/agents/profiles",
        "method": "GET",
        "summary": "Full agent profile snapshot",
        "description": (
            "Returns per-role performance metrics and aggregate statistics. "
            "Pass ?recompute=true to force a fresh computation from data sources."
        ),
        "tags": ["agents"],
        "auth": True,
        "parameters": [
            {
                "name": "recompute",
                "in": "query",
                "required": False,
                "schema": {"type": "boolean"},
                "description": "If true, recompute profiles before returning.",
            }
        ],
        "responses": {
            200: {
                "description": "Agent profile snapshot",
                "schema": {
                    "type": "object",
                    "properties": {
                        "computed_at": {"type": "string", "format": "date-time"},
                        "roles": {"type": "object"},
                        "aggregate": {"type": "object"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/agents/profiles/summary",
        "method": "GET",
        "summary": "Aggregate agent metrics only",
        "description": (
            "Returns only the cross-role aggregate metrics "
            "(bottleneck_role, most_expensive_role, team_efficiency)."
        ),
        "tags": ["agents"],
        "auth": True,
        "responses": {
            200: {
                "description": "Aggregate metrics",
                "schema": {
                    "type": "object",
                    "properties": {
                        "bottleneck_role": {"type": "string"},
                        "most_expensive_role": {"type": "string"},
                        "team_efficiency": {"type": "number"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/agents/profiles/<role>",
        "method": "GET",
        "summary": "Profile for a specific agent role",
        "description": (
            "Returns performance metrics for a single agent role, "
            "or 404 if no data exists."
        ),
        "tags": ["agents"],
        "auth": True,
        "parameters": [
            {
                "name": "role",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Agent role name (executor, code-reviewer, etc.)",
            }
        ],
        "responses": {
            200: {
                "description": "Role performance profile",
                "schema": {"type": "object"},
            },
            404: {
                "description": "No profile data for this role",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Benchmarks
    # ------------------------------------------------------------------
    {
        "path": "/benchmarks",
        "method": "GET",
        "summary": "Full benchmark stats",
        "description": (
            "Returns rolling p50/p95/p99 statistics for all instrumented categories. "
            "Query param: window=300 (seconds, default 5 min)."
        ),
        "tags": ["benchmarks"],
        "auth": True,
        "parameters": [
            {
                "name": "window",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 300},
                "description": "Rolling window in seconds",
            },
        ],
        "responses": {
            200: {
                "description": "Benchmark stats across all categories",
                "schema": {
                    "type": "object",
                    "properties": {
                        "window_seconds": {"type": "integer"},
                        "stats": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/benchmarks/<category>",
        "method": "GET",
        "summary": "Benchmark stats for one category",
        "description": (
            "Returns rolling p50/p95/p99 for a single category (http, event_bus, spawn, db). "
            "Returns zeroed stats when no data exists — never a 404."
        ),
        "tags": ["benchmarks"],
        "auth": True,
        "parameters": [
            {
                "name": "category",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["http", "event_bus", "spawn", "db"]},
                "description": "Benchmark category",
            },
            {
                "name": "window",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 300},
                "description": "Rolling window in seconds",
            },
        ],
        "responses": {
            200: {
                "description": "Benchmark stats for the category",
                "schema": {"type": "object"},
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/benchmarks/<category>/<operation>",
        "method": "GET",
        "summary": "Benchmark stats for one operation",
        "description": (
            "Returns rolling p50/p95/p99 for a specific operation within a category. "
            "URL-encode the operation name (e.g. GET%20/health)."
        ),
        "tags": ["benchmarks"],
        "auth": True,
        "parameters": [
            {
                "name": "category",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Benchmark category",
            },
            {
                "name": "operation",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Operation name (URL-encoded)",
            },
            {
                "name": "window",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 300},
                "description": "Rolling window in seconds",
            },
        ],
        "responses": {
            200: {
                "description": "Benchmark stats for the operation",
                "schema": {"type": "object"},
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/benchmarks/history",
        "method": "GET",
        "summary": "Benchmark time-series history",
        "description": (
            "Returns up to 60 one-minute buckets of benchmark data for charting. "
            "Query params: category=http, operation=GET+/health, points=60."
        ),
        "tags": ["benchmarks"],
        "auth": True,
        "parameters": [
            {
                "name": "category",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "default": "http"},
                "description": "Category to query",
            },
            {
                "name": "operation",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
                "description": "Operation filter (optional)",
            },
            {
                "name": "points",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 60, "maximum": 60},
                "description": "Number of 1-minute buckets to return",
            },
        ],
        "responses": {
            200: {
                "description": "Time-series history buckets",
                "schema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "operation": {"type": "string"},
                        "history": {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Traces (OpenTelemetry-compatible distributed tracing)
    # ------------------------------------------------------------------
    {
        "path": "/traces",
        "method": "GET",
        "summary": "List recent distributed traces",
        "description": (
            "Returns the last N completed traces (default 50) as OTLP-compatible "
            "ResourceSpans JSON. Each trace groups all spans sharing a trace_id."
        ),
        "tags": ["traces"],
        "auth": True,
        "parameters": [
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 50},
                "description": "Maximum number of distinct traces to return.",
            }
        ],
        "responses": {
            200: {
                "description": "List of traces with OTLP ResourceSpans structure",
                "schema": {
                    "type": "object",
                    "properties": {
                        "traces": {"type": "array"},
                        "count": {"type": "integer"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/traces/stats",
        "method": "GET",
        "summary": "Trace statistics",
        "description": (
            "Returns aggregate statistics: traces/minute, average span count, "
            "p50/p95 duration in milliseconds, and error rate."
        ),
        "tags": ["traces"],
        "auth": True,
        "responses": {
            200: {
                "description": "Trace statistics",
                "schema": {
                    "type": "object",
                    "properties": {
                        "traces_per_minute": {"type": "number"},
                        "avg_spans": {"type": "number"},
                        "p50_duration_ms": {"type": "number"},
                        "p95_duration_ms": {"type": "number"},
                        "error_rate": {"type": "number"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/traces/<trace_id>",
        "method": "GET",
        "summary": "Get all spans for a specific trace",
        "description": (
            "Returns all spans for the given trace_id as OTLP ResourceSpans JSON, "
            "or 404 if the trace is not in the in-memory buffer."
        ),
        "tags": ["traces"],
        "auth": True,
        "parameters": [
            {
                "name": "trace_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "W3C Trace Context trace ID (32 hex chars).",
            }
        ],
        "responses": {
            200: {
                "description": "Spans for the requested trace",
                "schema": {"type": "object"},
            },
            404: {
                "description": "Trace not found",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # Replay engine (playback controls)
    # ------------------------------------------------------------------
    {
        "path": "/replays/<agent_id>/start",
        "method": "POST",
        "summary": "Start replay for an agent trace",
        "description": (
            "Loads the JSONL trace for the given agent_id and begins re-emitting "
            "events through the event bus. If another replay is already active, "
            "it is stopped first. Returns the session ID and total event count."
        ),
        "tags": ["replay"],
        "auth": True,
        "parameters": [
            {
                "name": "agent_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
                "description": "Agent ID whose recorded trace to replay.",
            }
        ],
        "request_body": {
            "type": "object",
            "properties": {
                "speed": {
                    "type": "string",
                    "enum": ["1x", "5x", "10x", "instant"],
                    "default": "1x",
                    "description": "Playback speed multiplier.",
                },
            },
            "example": {"speed": "1x"},
        },
        "responses": {
            200: {
                "description": "Replay started",
                "schema": {
                    "type": "object",
                    "properties": {
                        "replay_session_id": {"type": "string", "format": "uuid"},
                        "total_events": {"type": "integer"},
                    },
                    "required": ["replay_session_id", "total_events"],
                },
            },
            400: {
                "description": "Invalid speed value",
                "schema": _ERROR_SCHEMA,
            },
            404: {
                "description": "No recorded trace found for agent_id",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/replays/pause",
        "method": "POST",
        "summary": "Pause active replay",
        "description": "Pauses event emission at the current position. Returns 409 if no replay is active.",
        "tags": ["replay"],
        "auth": True,
        "responses": {
            200: {
                "description": "Replay paused",
                "schema": _OK_SCHEMA,
            },
            409: {
                "description": "No active replay",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/replays/resume",
        "method": "POST",
        "summary": "Resume paused replay",
        "description": "Resumes event emission from the position where it was paused. Returns 409 if no replay is active.",
        "tags": ["replay"],
        "auth": True,
        "responses": {
            200: {
                "description": "Replay resumed",
                "schema": _OK_SCHEMA,
            },
            409: {
                "description": "No active replay",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/replays/stop",
        "method": "POST",
        "summary": "Stop active replay",
        "description": (
            "Terminates the active replay thread within 1 second. "
            "Returns ok=true regardless of whether a replay was active."
        ),
        "tags": ["replay"],
        "auth": True,
        "responses": {
            200: {
                "description": "Replay stopped",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "was_active": {"type": "boolean"},
                    },
                    "required": ["ok", "was_active"],
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/replays/seek",
        "method": "POST",
        "summary": "Seek to event number",
        "description": (
            "Repositions the replay pointer to the given event index (0-based). "
            "Playback continues from that position. Returns 409 if no replay is active."
        ),
        "tags": ["replay"],
        "auth": True,
        "request_body": {
            "type": "object",
            "properties": {
                "event_number": {
                    "type": "integer",
                    "description": "0-based event index to seek to.",
                },
            },
            "required": ["event_number"],
            "example": {"event_number": 5},
        },
        "responses": {
            200: {
                "description": "Seek scheduled",
                "schema": _OK_SCHEMA,
            },
            400: {
                "description": "Missing or invalid event_number",
                "schema": _ERROR_SCHEMA,
            },
            409: {
                "description": "No active replay",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/replays/status",
        "method": "GET",
        "summary": "Active replay status",
        "description": (
            "Returns the current state of the active replay session. "
            "Returns {active: false} when no replay is running."
        ),
        "tags": ["replay"],
        "auth": True,
        "responses": {
            200: {
                "description": "Replay status",
                "schema": {
                    "type": "object",
                    "properties": {
                        "active": {"type": "boolean"},
                        "agent_id": {"type": "string"},
                        "speed": {"type": "string"},
                        "current_event": {"type": "integer"},
                        "total_events": {"type": "integer"},
                        "paused": {"type": "boolean"},
                        "replay_session_id": {"type": "string", "format": "uuid"},
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    # ------------------------------------------------------------------
    # GraphQL
    # ------------------------------------------------------------------
    {
        "path": "/graphql",
        "method": "GET",
        "summary": "GraphQL query (GET)",
        "description": (
            "Execute a GraphQL query via URL parameter. "
            "Pass the query string as ?query=... "
            "Returns {\"data\": {...}} on success or {\"errors\": [...]} on failure. "
            "Auth: same bearer token as other protected endpoints."
        ),
        "tags": ["graphql"],
        "auth": True,
        "parameters": [
            {
                "name": "query",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
                "description": "GraphQL query string (e.g. {health{ok}})",
            }
        ],
        "responses": {
            200: {
                "description": "GraphQL response envelope",
                "schema": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "object"},
                        "errors": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "string"},
                                    "path": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
    {
        "path": "/graphql",
        "method": "POST",
        "summary": "GraphQL query (POST)",
        "description": (
            "Execute a GraphQL query via POST body. "
            "Send {\"query\": \"...\", \"variables\": {}} as JSON. "
            "Returns {\"data\": {...}} on success or {\"errors\": [...]} on failure. "
            "Auth: same bearer token as other protected endpoints."
        ),
        "tags": ["graphql"],
        "auth": True,
        "request_body": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "GraphQL query string"},
                "variables": {"type": "object", "description": "Query variables (reserved for future use)"},
            },
        },
        "responses": {
            200: {
                "description": "GraphQL response envelope",
                "schema": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "object"},
                        "errors": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "string"},
                                    "path": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            400: {
                "description": "Missing or invalid query",
                "schema": _ERROR_SCHEMA,
            },
            **_RATE_LIMIT_RESPONSE,
        },
    },
]

# ---------------------------------------------------------------------------
# Versioned route table
# ---------------------------------------------------------------------------
# Build VERSIONED_ROUTES by prepending /v1/ to every route that does not
# already have a version prefix. Routes for /openapi.json, /docs, and
# /dashboard are infrastructure endpoints — not versioned in URL.
_UNVERSIONED_PATHS = {"/openapi.json", "/docs", "/dashboard"}


def _make_versioned(route: dict, version: int = 1) -> dict:
    """Return a copy of *route* with /v<version>/ prepended to path."""
    import copy
    r = copy.deepcopy(route)
    r["path"] = f"/v{version}" + r["path"]
    r["deprecated"] = False
    return r


def _make_deprecated(route: dict) -> dict:
    """Return a copy of *route* marked as deprecated (unversioned access)."""
    import copy
    r = copy.deepcopy(route)
    r["deprecated"] = True
    return r


# All versioned routes first, then deprecated unversioned equivalents.
VERSIONED_ROUTES: list[dict] = []
for _route in ROUTES:
    if _route["path"] in _UNVERSIONED_PATHS:
        VERSIONED_ROUTES.append(_route)
    else:
        VERSIONED_ROUTES.append(_make_versioned(_route))
        VERSIONED_ROUTES.append(_make_deprecated(_route))

