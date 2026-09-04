"""
Example request/response data for every API endpoint.

Keyed by "METHOD /path" (uppercase method, exact path from ROUTES).
Each entry contains:
    response_body   dict  Realistic example response
    request_body    dict  Example request body (POST endpoints only)
    description     str   One-line human description for the Getting Started panel
    getting_started bool  True for the 5 most useful endpoints (featured in the panel)
    curl_extra      str   Extra curl flags for the Copy-as-curl button (e.g. -d '...')
"""

from __future__ import annotations

EXAMPLES: dict[str, dict] = {
    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    "GET /health": {
        "response_body": {"ok": True, "uptime_seconds": 3600},
        "description": "Quick sanity check — returns {ok: true} if the server is alive.",
        "getting_started": True,
    },
    "GET /health/loop": {
        "response_body": {
            "healthy": True,
            "age_seconds": 245,
            "threshold_seconds": 900,
            "last_run_at": "2026-04-10T12:34:00Z",
        },
        "description": "Check whether the autonomous loop is still firing on schedule.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------
    "GET /budget/status": {
        "response_body": {
            "used": 485000,
            "ceiling": 2000000,
            "remaining": 1515000,
            "pct_used": 24.25,
        },
        "description": "See how many tokens have been consumed and what headroom remains.",
        "getting_started": True,
    },
    "POST /budget/init": {
        "request_body": {"ceiling": 2000000},
        "response_body": {
            "ok": True,
            "status": {
                "used": 0,
                "ceiling": 2000000,
                "remaining": 2000000,
                "pct_used": 0.0,
            },
        },
        "description": "Reset the token budget, optionally with a new ceiling.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Cost
    # ------------------------------------------------------------------
    "GET /cost": {
        "response_body": {
            "total_cost_usd": 1.23,
            "by_agent": [
                {"role": "executor", "cost_usd": 0.72},
                {"role": "code-reviewer", "cost_usd": 0.31},
            ],
            "by_discussion": [
                {"discussion": 200, "cost_usd": 0.45},
            ],
            "model_breakdown": [
                {"model": "claude-sonnet-4-6", "cost_usd": 1.23, "calls": 14},
            ],
        },
        "description": "Full cost breakdown by agent, discussion, and model.",
        "getting_started": False,
    },
    "GET /cost/summary": {
        "response_body": {
            "total_cost_usd": 1.23,
            "model_breakdown": [
                {"model": "claude-sonnet-4-6", "cost_usd": 1.23, "calls": 14},
            ],
        },
        "description": "Lightweight cost summary — total and per-model.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    "GET /registry": {
        "response_body": {
            "discussions": [
                {
                    "number": 200,
                    "title": "Interactive API playground",
                    "status": "IMPLEMENTING",
                    "pr": None,
                    "created_at": "2026-04-10T00:00:00Z",
                },
                {
                    "number": 199,
                    "title": "Wiki sync improvements",
                    "status": "DONE",
                    "pr": 198,
                    "created_at": "2026-04-09T12:00:00Z",
                },
                {
                    "number": 195,
                    "title": "Add codebase indexer",
                    "status": "SPEC_READY",
                    "pr": None,
                    "created_at": "2026-04-08T08:00:00Z",
                },
            ],
            "stats": {
                "total": 3,
                "done": 1,
                "in_progress": 1,
                "queued": 1,
            },
        },
        "description": "All tracked Discussions with status — the team's backlog at a glance.",
        "getting_started": True,
    },
    "GET /registry/stats": {
        "response_body": {
            "total": 3,
            "done": 1,
            "in_progress": 1,
            "queued": 1,
            "velocity_last_24h": 2,
        },
        "description": "Velocity counters from the discussion registry.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------
    "GET /control": {
        "response_body": {
            "gates": {"pause_loop": False, "enable_sse": True},
            "policies": {"executor": {"max_retries": 3}},
        },
        "description": "Current feature gates and per-role policies.",
        "getting_started": False,
    },
    "GET /control/gates": {
        "response_body": {"pause_loop": False, "enable_sse": True},
        "description": "Just the feature gates section of the control plane.",
        "getting_started": False,
    },
    "GET /control/audit": {
        "response_body": [
            {
                "ts": "2026-04-10T11:00:00Z",
                "key": "pause_loop",
                "old": True,
                "new": False,
                "actor": "team-lead",
            }
        ],
        "description": "Recent gate/policy change history.",
        "getting_started": False,
    },
    "POST /control/set": {
        "request_body": {"key": "pause_loop", "value": True},
        "response_body": {"ok": True, "key": "pause_loop", "value": True},
        "description": "Toggle a feature gate or update a policy value.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    "GET /agents": {
        "response_body": {
            "agents": [
                "executor",
                "code-reviewer",
                "security-reviewer",
                "project-manager",
                "team-lead",
            ]
        },
        "description": "List all registered agent roles.",
        "getting_started": True,
    },
    "GET /agents/<role>": {
        "response_body": {
            "role": "executor",
            "status": "idle",
            "last_active": "2026-04-10T12:30:00Z",
            "tasks_completed": 47,
            "avg_tokens_per_task": 18500,
            "current_discussion": None,
        },
        "description": "Full agent card for a specific role — status, last active, task counts.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Plugins
    # ------------------------------------------------------------------
    "GET /plugins": {
        "response_body": {
            "plugins": [
                {
                    "name": "wiki-sync",
                    "description": "Syncs repo wiki from Discussion bodies",
                    "version": "1.0.0",
                    "review_pipeline": "STANDARD",
                },
            ]
        },
        "description": "Metadata for all loaded plugins.",
        "getting_started": False,
    },
    "GET /plugins/<name>": {
        "response_body": {
            "name": "wiki-sync",
            "description": "Syncs repo wiki from Discussion bodies",
            "version": "1.0.0",
            "review_pipeline": "STANDARD",
            "hooks": ["post_merge", "pre_spec"],
        },
        "description": "Full plugin card for a specific plugin.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # KPI
    # ------------------------------------------------------------------
    "GET /kpi": {
        "response_body": {
            "version": 1,
            "computed_at": "2026-04-10T12:00:00Z",
            "velocity": {
                "last_24h": 3,
                "all_time_per_day": 2.1,
                "total_done": 47,
            },
            "estimation_accuracy": {
                "mean_error_pct": 12.5,
                "within_20pct": 0.78,
            },
            "idle_rate": {
                "pct_idle": 8.3,
                "idle_periods": 2,
            },
            "pr_cycle_time": {
                "mean_hours": 1.4,
                "median_hours": 1.1,
                "total_measured": 32,
            },
        },
        "description": "Full KPI snapshot — velocity, cycle time, estimation accuracy.",
        "getting_started": True,
    },
    "GET /kpi/velocity": {
        "response_body": {
            "last_24h": 3,
            "all_time_per_day": 2.1,
            "total_done": 47,
        },
        "description": "Velocity subsection — discussions completed per day.",
        "getting_started": False,
    },
    "GET /kpi/cycle-time": {
        "response_body": {
            "mean_hours": 1.4,
            "median_hours": 1.1,
            "total_measured": 32,
        },
        "description": "PR cycle time from open to merge.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Dependency graph
    # ------------------------------------------------------------------
    "GET /deps": {
        "response_body": {
            "modules": [
                {"name": "api", "imports": ["api_routes", "openapi", "budget"]},
                {"name": "budget", "imports": ["db"]},
            ],
            "cycles": [],
            "hubs": [{"name": "db", "in_degree": 12}],
            "stats": {
                "total_modules": 38,
                "total_edges": 94,
                "max_depth": 6,
                "avg_degree": 2.47,
            },
        },
        "description": "Static import graph for all backend Python modules.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # SSE streams
    # ------------------------------------------------------------------
    "GET /stream/feed": {
        "response_body": "data: {\"event\": \"agent_output\", \"role\": \"executor\", \"text\": \"PR created\"}\n\n",
        "description": "Server-Sent Events stream of live agent output.",
        "getting_started": False,
    },
    "GET /stream/status": {
        "response_body": {"enabled": True, "connected_clients": 2},
        "description": "Check whether the SSE feed is enabled and how many clients are connected.",
        "getting_started": False,
    },
    "GET /stream/events": {
        "response_body": [
            {
                "ts": "2026-04-10T12:34:00Z",
                "role": "executor",
                "event": "agent_output",
                "text": "Preflight passed.",
            }
        ],
        "description": "Recent buffered events from the agent feed (non-streaming).",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Spawn queue
    # ------------------------------------------------------------------
    "GET /spawn-queue": {
        "response_body": {
            "queue": [
                {
                    "id": "sq-001",
                    "role": "code-reviewer",
                    "discussion": 200,
                    "status": "pending",
                    "enqueued_at": "2026-04-10T12:35:00Z",
                }
            ],
            "active": [
                {
                    "id": "sq-000",
                    "role": "executor",
                    "discussion": 199,
                    "status": "running",
                    "started_at": "2026-04-10T12:30:00Z",
                }
            ],
        },
        "description": "Current spawn queue — pending and active agent spawns.",
        "getting_started": False,
    },
    "POST /spawn-queue/enqueue": {
        "request_body": {
            "role": "code-reviewer",
            "discussion": 200,
            "pr": 201,
            "priority": 1,
        },
        "response_body": {
            "ok": True,
            "id": "sq-002",
            "role": "code-reviewer",
            "discussion": 200,
            "status": "pending",
        },
        "description": "Enqueue an agent spawn request.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    "POST /notifications/test": {
        "request_body": {"channel": "team-log", "message": "Test notification from API"},
        "response_body": {"ok": True, "channel": "team-log", "delivered": True},
        "description": "Send a test notification to verify delivery.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------
    "GET /audit": {
        "response_body": {
            "entries": [
                {
                    "ts": "2026-04-10T12:00:00Z",
                    "actor": "team-lead",
                    "action": "auto_merge",
                    "target": "PR #198",
                },
            ],
            "total": 1,
        },
        "description": "Audit log of all team actions.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------
    "GET /backup/status": {
        "response_body": {
            "last_backup": "2026-04-10T06:00:00Z",
            "backup_count": 12,
            "latest_file": "backup-2026-04-10T06:00:00Z.tar.gz",
        },
        "description": "Status of the most recent state backup.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    "POST /validate": {
        "request_body": {"schema": "agent-output", "payload": {"agent": "executor", "verdict": "done"}},
        "response_body": {"ok": True, "valid": True, "errors": []},
        "description": "Validate a payload against a named schema.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Replays
    # ------------------------------------------------------------------
    "GET /replays": {
        "response_body": {
            "replays": [
                {
                    "id": "replay-001",
                    "discussion": 198,
                    "created_at": "2026-04-09T14:00:00Z",
                    "events": 42,
                }
            ]
        },
        "description": "List all recorded session replays.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Benchmarks
    # ------------------------------------------------------------------
    "GET /benchmarks": {
        "response_body": {
            "benchmarks": [
                {
                    "name": "preflight_duration",
                    "p50_ms": 420,
                    "p95_ms": 980,
                    "runs": 15,
                }
            ]
        },
        "description": "Performance benchmark results collected during loop runs.",
        "getting_started": False,
    },
    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------
    "GET /traces": {
        "response_body": {
            "traces": [
                {
                    "trace_id": "tr-abc123",
                    "discussion": 200,
                    "agent": "executor",
                    "duration_ms": 32500,
                    "spans": 8,
                }
            ]
        },
        "description": "Distributed traces for recent agent runs.",
        "getting_started": False,
    },
}

# The 5 Getting Started endpoints in display order
GETTING_STARTED_ORDER = [
    "GET /health",
    "GET /budget/status",
    "GET /registry",
    "GET /agents",
    "GET /kpi",
]
