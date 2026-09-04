"""
REST API gateway — exposes backend modules over HTTP using stdlib http.server.

Wraps budget, registry, control_plane, and agent_cards behind simple GET/POST
endpoints. No new dependencies — uses only the Python standard library plus the
existing backend modules.

Usage:
    python backend/api.py              # listens on :18099
    python backend/api.py --port 9000  # custom port
    python backend/api.py --host 127.0.0.1 --port 18099
    python backend/api.py --no-enable-sse  # disable SSE endpoints
    python backend/api.py --no-dashboard   # disable HTML dashboard route
    python backend/api.py --no-rate-limit  # disable per-IP rate limiting
    python backend/api.py --no-docs        # disable /openapi.json and /docs

Endpoints:
    GET  /health                       → {"ok": true, "loop_last_run": ..., "loop_duration_s": ..., "loop_idle_rate": ...}
    GET  /health/loop                  → loop health for dashboard (lastRun, status, duration)
    GET  /health/modules               → module import health (cached 60s)
    GET  /metrics                      → Prometheus text exposition format (no auth required)
    GET  /budget/status                → budget status snapshot
    POST /budget/init                  → init/reset session budget
    GET  /cost                         → full cost breakdown (session total, per-agent, per-discussion)
    GET  /cost/summary                 → total cost and model breakdown (lightweight)
    GET  /registry                     → full registry (discussions + stats)
    GET  /registry/stats               → velocity stats only
    GET  /control                      → current gates + policies
    GET  /control/gates                → gates only
    GET  /control/audit                → audit log
    POST /control/set                  → set a key (body: {"key": "...", "value": ...})
    GET  /audit                        → filtered audit trail (?source=X&action=Y&actor=Z&since=T&limit=N)
    GET  /audit/stats                  → counts by source and action
    GET  /agents                       → list agent card names
    GET  /agents/<role>                → card for a specific role
    GET  /kpi                          → full KPI snapshot (cached 60s)
    GET  /kpi/velocity                 → velocity subsection only
    GET  /kpi/cycle-time               → PR cycle time subsection only
    GET  /stream/feed                  → SSE stream of agent-feed.jsonl events (real-time)
    GET  /stream/status                → SSE stream of periodic status snapshots (every 10s)
    GET  /stream/events                → SSE stream of ALL event bus events (debugging/monitoring)
    GET  /ws                           → WebSocket endpoint (bidirectional; upgrade required)
    GET  /dashboard                    → HTML dashboard (disable with --no-dashboard)
    GET  /validate                     → validate all known config files; returns per-file error lists
    GET  /openapi.json                 → OpenAPI 3.0.1 spec (disable with --no-docs)
    GET  /docs                         → Swagger UI interactive docs (disable with --no-docs)
    GET  /replays                      → list recent replay metadata (limit=20)
    GET  /replays/<agent_id>           → full event list for one agent run
    GET  /replays/<agent_id>/summary   → header + footer only (no content bulk)
    POST /replays/<agent_id>/start     → start replay; body: {"speed": "1x"|"5x"|"10x"|"instant"}
    POST /replays/pause                → pause active replay
    POST /replays/resume               → resume paused replay
    POST /replays/stop                 → stop active replay
    POST /replays/seek                 → seek to event; body: {"event_number": N}
    GET  /replays/status               → active replay state
    GET  /notifications/history        → last 50 notification dispatch records
    POST /notifications/test           → send test notification to all channels
    GET  /spawn-queue                  → queue status (pending count, active list, utilization %)
    GET  /spawn-queue/pending          → list pending spawn requests
    GET  /spawn-queue/active           → list active agents
    POST /spawn-queue/enqueue          → enqueue a spawn request

Error responses always include {"error": "..."} with an appropriate HTTP status.
"""

from __future__ import annotations

import argparse
import collections
import hmac
import json
import os
import queue
import re as _re_project_name
import sys
import threading as _threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Allow running as a script from repo root: `python backend/api.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agent_cards import AgentCards, AgentNotFoundError  # noqa: E402
from backend.audit_trail import get_audit_trail  # noqa: E402
from backend.api_version import (  # noqa: E402
    CURRENT_VERSION,
    VersionInfo,
    check_version,
    parse_version,
    unversioned_info,
)
from backend.plugin_loader import PluginLoader  # noqa: E402
from backend.budget import BudgetTracker  # noqa: E402
from backend.cost_tracker import CostTracker  # noqa: E402
from backend.config_watcher import ConfigWatcher  # noqa: E402
from backend.control_plane import ControlPlane  # noqa: E402
from backend.dashboard import get_dashboard_html  # noqa: E402
from backend.event_bus import (  # noqa: E402
    AgentOutputEvent,
    BudgetSpendEvent,
    BusEventFileAppender,
    Event,
    FileAppender,
    GateChangeEvent,
    LoopIterationEvent,
    get_bus,
)
from backend.health_monitor import check_loop_health, get_loop_metrics, get_loop_health_dashboard  # noqa: E402
import backend.kpi_engine as kpi_engine  # noqa: E402
import backend.module_health as _module_health  # noqa: E402
from backend.dep_graph import get_cached_dep_graph  # noqa: E402
from backend.metrics import generate_prometheus_metrics  # noqa: E402
from backend.rate_limiter import RateLimiter, SSEConnectionTracker  # noqa: E402
from backend.rbac import RBACManager  # noqa: E402
from backend.registry import DiscussionRegistry  # noqa: E402
from backend.loop_metrics_counters import compute_counters as _compute_loop_counters  # noqa: E402
from backend.replay import get_recorder, start_replay, get_active_replay, stop_active_replay  # noqa: E402
from backend.schema_validator import SchemaValidator  # noqa: E402
from backend.session_manager import SessionManager  # noqa: E402
from backend.websocket import WebSocketHandler  # noqa: E402
import backend.backup as _backup  # noqa: E402
from backend.spawn_queue import get_spawn_queue  # noqa: E402
from backend._repo import REPO as _GH_REPO, REPO_NAME as _GH_REPO_NAME  # noqa: E402
from backend.benchmarks import get_recorder as get_bench_recorder, _stats_to_dict  # noqa: E402
from backend.spawn_guard import SpawnGuard, AcquireStatus, _GUARD as _spawn_guard  # noqa: E402
import backend.graphql_api as _graphql  # noqa: E402
from backend.tracing import (  # noqa: E402
    get_collector,
    make_traceparent,
    parse_traceparent,
    set_remote_context,
    start_span,
)
from backend import state_paths as _state_paths  # noqa: E402


# ---------------------------------------------------------------------------
# _STATE_DIR — resolved at call time (D#1810)
# ---------------------------------------------------------------------------
# This used to be `from backend.state_paths import STATE_DIR as _STATE_DIR`
# at module scope, which froze it at import time and defeated a later
# AUTONOMOUS_TEAM_STATE_DIR override. Module __getattr__ (PEP 562) makes
# external access (`api_mod._STATE_DIR`) resolve fresh on every read, UNLESS
# a caller — several tests do this for isolation — assigns/patches the name
# directly (`patch.object(api_mod, "_STATE_DIR", tmp_path)`), which shadows
# __getattr__ exactly like any other module attribute. `_attr()` routes this
# module's own internal references through the same globals-first-else-
# resolve-fresh logic so both call sites see one consistent value.


def __getattr__(name: str):
    if name == "_STATE_DIR":
        return _state_paths.STATE_DIR
    if name == "_INNOVATE_STATE_PATH":
        # Per-project path under STATE_DIR so each project's Innovate state
        # is independent. Using a CWD-relative path caused AF and projectb
        # backends (both started from the same CWD) to share a single file,
        # making it impossible to disable Innovate for one project without
        # affecting the other.
        return _state_paths.STATE_DIR / "innovate-state.json"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _attr(name: str):
    if name in globals():
        return globals()[name]
    return __getattr__(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(obj: object) -> bytes:
    """Serialize *obj* to UTF-8-encoded JSON."""
    return json.dumps(obj, default=str).encode("utf-8")


_REPO_ROOT = Path(__file__).resolve().parent.parent
_FEED_FILE = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"
_METRICS_FILE = _REPO_ROOT / ".autonomous-team" / "loop-metrics.jsonl"
_CONFIG_FILE = _REPO_ROOT / ".autonomous-team" / "config.json"

# Shared RBAC manager — loaded once at startup; no rbac section → allow-all.
_rbac_manager = RBACManager(_CONFIG_FILE)

# ---------------------------------------------------------------------------
# Project name validation (CWE-22 path traversal guard)
# ---------------------------------------------------------------------------
# The ?project= query param is used as a path component when constructing
# state_dir paths.  Only allow safe slug characters; reject anything that
# could escape the expected directory hierarchy.
_VALID_PROJECT_NAME_RE = _re_project_name.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_project_name(name: str) -> bool:
    """Return True iff *name* is a safe project slug (CWE-22 guard)."""
    return bool(_VALID_PROJECT_NAME_RE.fullmatch(name))


# ---------------------------------------------------------------------------
# Ideas helpers — pending ideas from the project-manager, shown in dashboard
# ---------------------------------------------------------------------------
#
# Each idea is a JSON file in .autonomous-team/blackboard/ideas/<id>.json.
# If the directory is empty on first access, three example ideas are seeded
# so the page is never blank in a fresh checkout.

_IDEAS_SEED = [
    {
        "id": "improve-onboarding",
        "title": "Improve agent onboarding flow",
        "summary": (
            "New agents spend the first iteration reading CLAUDE.md from scratch. "
            "A short structured onboarding prompt injected at spawn time would cut "
            "that ramp significantly."
        ),
        "votes": 0,
        "status": "pending",
        "created_at": "2026-04-12T00:00:00Z",
    },
    {
        "id": "add-rate-limiting",
        "title": "Add per-IP rate limiting to the API",
        "summary": (
            "The API gateway currently has no rate limiting. Adding per-IP limits "
            "with configurable thresholds would prevent runaway agents from "
            "flooding the backend."
        ),
        "votes": 0,
        "status": "pending",
        "created_at": "2026-04-12T00:01:00Z",
    },
    {
        "id": "wire-stripe-billing",
        "title": "Wire Stripe billing to usage metering",
        "summary": (
            "The metering API records token usage per agent, but nothing feeds "
            "that into Stripe. Connecting the two would unlock subscription-based "
            "billing for hosted deployments."
        ),
        "votes": 0,
        "status": "pending",
        "created_at": "2026-04-12T00:02:00Z",
    },
]


import re as _re_ideas

_VALID_IDEA_ID = _re_ideas.compile(r'^[a-z0-9][a-z0-9\-]{0,63}$')


def _validate_idea_id(idea_id: str) -> bool:
    return bool(_VALID_IDEA_ID.match(idea_id))


def _ideas_dir() -> Path:
    """Return the ideas directory, creating it if needed (no auto-seeding)."""
    d = _REPO_ROOT / ".autonomous-team" / "blackboard" / "ideas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_ideas() -> tuple[list, bool]:
    """Return (ideas, source_empty).

    Reads from two sources:
    1. ``.autonomous-team/blackboard/ideas/*.json`` — persisted individual idea records
       (upvotes, dismissals, and promotions are written back here).
    2. ``.autonomous-team/proposed-ideas-*.json`` — batch files written by the
       project-manager during idea-generation runs.  Each file has an ``ideas``
       array; each element becomes a synthetic idea record if it isn't already
       present in the blackboard (dedup by ``title`` prefix match).

    Returns an empty list with ``source_empty=True`` only when both sources are
    empty so the UI shows an explicit empty state instead of stale seeds.
    """
    d = _ideas_dir()
    ideas: list[dict] = []
    seen_titles: set[str] = set()

    # Source 1: blackboard per-idea files (canonical, highest priority)
    for f in d.glob("*.json"):
        try:
            idea = json.loads(f.read_text())
            ideas.append(idea)
            seen_titles.add((idea.get("title") or "").lower()[:60])
        except Exception:  # noqa: BLE001
            pass

    # Source 2: proposed-ideas-*.json batch files from idea-generation runs
    # Each file: {"generated_at": ..., "ideas": [{"title", "rationale", ...}, ...]}
    team_dir = _REPO_ROOT / ".autonomous-team"
    import glob as _glob_ideas  # noqa: PLC0415
    for batch_path in sorted(_glob_ideas.glob(str(team_dir / "proposed-ideas-*.json")), reverse=True):
        try:
            batch = json.loads(Path(batch_path).read_text())
        except Exception:  # noqa: BLE001
            continue
        generated_at = batch.get("generated_at", "")
        for raw_idea in batch.get("ideas", []):
            title = (raw_idea.get("title") or "").strip()
            title_key = title.lower()[:60]
            if title_key in seen_titles:
                continue  # already have this idea from the blackboard
            seen_titles.add(title_key)
            # Synthesise a minimal idea record compatible with the dashboard's Idea type.
            import hashlib as _h  # noqa: PLC0415
            idea_id = "gen-" + _h.sha1(title.encode()).hexdigest()[:12]
            ideas.append({
                "id": idea_id,
                "title": title,
                "summary": raw_idea.get("rationale") or raw_idea.get("what") or "",
                "votes": 0,
                "status": "pending",
                "created_at": generated_at,
                # Extra fields from the batch — kept for rich display
                "classification": raw_idea.get("classification"),
                "acceptance_criteria": raw_idea.get("acceptance_criteria"),
                "estimate": raw_idea.get("estimate"),
            })

    ideas.sort(key=lambda x: (-x.get("votes", 0), x.get("created_at", "")))
    return ideas, len(ideas) == 0


def _save_idea(idea: dict) -> None:
    """Persist a single idea record atomically."""
    if not _validate_idea_id(idea.get("id", "")):
        raise ValueError("invalid idea id")
    p = _ideas_dir() / f"{idea['id']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idea, indent=2))
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Idea mutation helpers — shared by REST handlers and TUI data_layer
# ---------------------------------------------------------------------------

def upvote_idea(idea_id: str) -> dict:
    """Increment vote count for *idea_id*. Returns updated idea dict.

    Raises KeyError if the idea is not found.
    Raises ValueError if idea_id is malformed.
    """
    if not _validate_idea_id(idea_id):
        raise ValueError(f"invalid idea id: {idea_id!r}")
    ideas = {i["id"]: i for i in _load_ideas()[0]}
    if idea_id not in ideas:
        raise KeyError(f"idea {idea_id!r} not found")
    idea = ideas[idea_id]
    idea["votes"] = idea.get("votes", 0) + 1
    _save_idea(idea)
    return idea


def dismiss_idea(idea_id: str) -> dict:
    """Set status='dismissed' for *idea_id*. Returns updated idea dict.

    Raises KeyError if the idea is not found.
    Raises ValueError if idea_id is malformed.
    """
    if not _validate_idea_id(idea_id):
        raise ValueError(f"invalid idea id: {idea_id!r}")
    ideas = {i["id"]: i for i in _load_ideas()[0]}
    if idea_id not in ideas:
        raise KeyError(f"idea {idea_id!r} not found")
    idea = ideas[idea_id]
    idea["status"] = "dismissed"
    _save_idea(idea)
    return idea


def promote_idea(idea_id: str) -> dict:
    """Set status='promoted' for *idea_id* and enqueue a spawn-queue entry.

    Returns updated idea dict.
    Raises KeyError if the idea is not found.
    Raises ValueError if idea_id is malformed.
    """
    if not _validate_idea_id(idea_id):
        raise ValueError(f"invalid idea id: {idea_id!r}")
    ideas = {i["id"]: i for i in _load_ideas()[0]}
    if idea_id not in ideas:
        raise KeyError(f"idea {idea_id!r} not found")
    idea = ideas[idea_id]
    idea["status"] = "promoted"
    _save_idea(idea)
    # Append a stub item to spawn-queue.json so project-manager picks this up.
    sq_path = _REPO_ROOT / ".autonomous-team" / "spawn-queue.json"
    try:
        sq_data = json.loads(sq_path.read_text()) if sq_path.exists() else {}
        pending = sq_data.get("pending", [])
        import datetime  # noqa: PLC0415
        pending.append({
            "id": f"idea-promote-{idea_id}",
            "role": "project-manager",
            "discussion": None,
            "priority": 5,
            "enqueued_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "requested_by": "ideas-api",
            "prompt_context": (
                f"Promoted idea: {idea.get('title', idea_id)}. "
                f"Summary: {idea.get('summary', '')} "
                "Create a Discussion proposal for this idea."
            ),
        })
        sq_data["pending"] = pending
        sq_path.write_text(json.dumps(sq_data, indent=2))
    except Exception:  # noqa: BLE001
        pass  # Non-fatal — idea is still promoted
    return idea


# ---------------------------------------------------------------------------
# Audit feed helper — maps the audit log (state_paths.AUDIT_LOG) to events
# ---------------------------------------------------------------------------
#
# The React AgentFeedPage was originally wired to a WebSocket at /ws that the
# Rust saas-service was supposed to serve but never did (returns 404). Rather
# than build a WebSocket server inside BaseHTTPServer, we expose the same data
# via a polling endpoint that reads the bottom of audit.jsonl. The wire shape
# matches WsEvent in dashboard/src/api/types.ts so the existing rendering code
# stays unchanged.

def _audit_path() -> Path:
    """Resolve the audit log — see backend/state_paths.py.

    Was a module-level relative constant, which resolved against whatever cwd
    the server was started in and wrote/read inside the checkout (D#1967). A
    function, not a constant, so the value is not frozen at import time.
    """
    from backend import state_paths  # noqa: PLC0415
    return state_paths.AUDIT_LOG


_AUDIT_LINE_INDEX_LOCK = _threading.Lock() if False else None  # see below

def _audit_action_to_event(rec: dict, line_no: int) -> dict | None:
    """Map an audit.jsonl record into the WsEvent shape used by the dashboard.

    Returns None for records that don't map cleanly to a feed-worthy event.
    """
    actor = rec.get("actor") or ""
    key = rec.get("key") or ""
    action = rec.get("action") or ""
    new_val = rec.get("new")
    ts = rec.get("ts") or ""

    # Memory writes (success/failure lessons from agents) are the most useful
    # signal — they correspond to a real agent completing real work.
    if key.startswith("memory/") and isinstance(new_val, dict):
        role = new_val.get("role") or actor
        lesson = new_val.get("lesson_type") or "info"
        content = new_val.get("content") or ""
        wire_event = "agent.done" if lesson == "success" else "agent.error"
        return {
            "event": wire_event,
            "timestamp": ts,
            "agentId": str(new_val.get("id") or ""),
            "role": role,
            "content": content,
            "data": {
                "discussion": new_val.get("discussion"),
                "files": new_val.get("files") or [],
                "tags": new_val.get("tags") or [],
            },
            "_seq": line_no,
        }

    # Spawn announcements
    if "spawn" in actor and action == "write" and "agent" in key:
        return {
            "event": "agent.started",
            "timestamp": ts,
            "agentId": key.split("/")[-1] if "/" in key else key,
            "role": actor.replace("spawn-", "").replace("spawner/", ""),
            "content": f"spawned {actor}",
            "_seq": line_no,
        }

    # Budget transitions are interesting at iteration boundaries
    if key == "budget/session_spent" and action == "cas":
        return {
            "event": "loop.started",
            "timestamp": ts,
            "role": "budget",
            "content": f"budget spent: {new_val}",
            "data": {"old": rec.get("old"), "new": new_val},
            "_seq": line_no,
        }

    return None


def _read_audit_events(since: int, limit: int) -> list[dict]:
    """Tail audit.jsonl and return up to `limit` events with line_no > since.

    `since` is the line index from the previous poll. The first poll passes 0,
    in which case we return the most recent `limit` mappable events. Subsequent
    polls pass the `next_since` returned in the prior response.
    """
    p = _audit_path()
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with p.open() as f:
            for line_no, line in enumerate(f, start=1):
                if line_no <= since:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ev = _audit_action_to_event(rec, line_no)
                if ev is not None:
                    out.append(ev)
    except Exception:
        return []
    # If `since=0` and the file is huge, return only the tail.
    if since == 0 and len(out) > limit:
        out = out[-limit:]
    elif len(out) > limit:
        out = out[:limit]
    return out


_PROJECTS_FILE = Path(".autonomous-team/projects.json")
_PROJECTS_LOCK = _threading.Lock()


def _resolve_fleet_project_name(p: dict) -> str:
    """Resolve the fleet.db ``project_name`` key for project record *p*.

    For the project this backend actually serves (``primary`` — the id
    matches this checkout's own repo, ``_GH_REPO_NAME``), resolve through
    ``backend.fleet.project_name.resolve_project_name()`` — the identical
    resolver ``scripts/pre-spawn-check.sh`` registers agents with, so the
    read side and the write side of fleet.db can never disagree again
    (D#2314 D1/F1: they used to — the read side queried ``"fulcrumaxe"``
    while the write side registered under a hardcoded ``"fulcrumaxe"``
    fallback).

    For any other project listed in ``projects.json`` (a different
    coldstarted checkout on the same host, sharing the same fleet.db by
    design — see ``backend/fleet/concurrency.py`` module docstring), this
    backend cannot read that project's own config.json, so its own display
    name is the best available signal.

    Returns ``""`` when nothing is resolvable — callers must treat that as
    "unresolvable", not as an empty-string project.
    """
    project_id = p.get("id", "")
    if project_id and project_id == _GH_REPO_NAME:
        try:
            from backend.fleet.project_name import resolve_project_name  # noqa: PLC0415
            return resolve_project_name()
        except Exception:
            return ""
    return p.get("name", "") or project_id


def _probe_liveness(project_name: str) -> str:
    """Return 'active', 'idle', or 'unknown' for *project_name*.

    Reads live rows from fleet.db (``backend.fleet.concurrency.active_agents``),
    scoped by *project_name* — the only signal here, replacing three
    cron-adjacent signal files that were never written on this host while
    the team merged 43 PRs in a day (D#2314 — see that Discussion for the
    prior signal list). Cron support itself is unchanged; only this probe
    stops depending on it.

    'active': at least one PID-alive row for *project_name*.
    'idle': the read succeeded and returned zero rows — genuinely no agents
      running for this project, not "couldn't tell".
    'unknown': *project_name* is empty (unresolvable) or the fleet.db read
      itself raised. This is the "no signal" state — it must never be
      reported as 'idle'.
    """
    if not project_name:
        return "unknown"
    try:
        from backend.fleet.concurrency import active_agents  # noqa: PLC0415
        rows = active_agents(project_name)
    except Exception:
        return "unknown"
    return "active" if rows else "idle"


def _get_dashboard_config() -> dict:
    """Return runtime config for the React dashboard auto-discovery.

    Priority order for runtime file:
      1. STATE_DIR/dashboard-runtime.json  (written by start-dashboard.sh into the
         state directory — this is always project-specific and never clobbered by
         another project's dashboard startup)
      2. .autonomous-team/dashboard-runtime.json  (legacy repo-level file, kept for
         backward compat but unreliable when multiple projects share the same repo
         working tree — a second project's start-dashboard.sh can overwrite it)

    Falls back to sensible defaults when neither file exists (e.g. when api.py is
    started manually without start-dashboard.sh).
    """
    # State-dir file is authoritative — it's project-scoped and never shared.
    state_runtime_path = _attr("_STATE_DIR") / "dashboard-runtime.json"
    # Repo-level file kept as fallback for manually-started servers.
    repo_runtime_path = _REPO_ROOT / ".autonomous-team" / "dashboard-runtime.json"
    token_path = _REPO_ROOT / ".autonomous-team" / "dashboard-token"

    rpc_base_url = "http://localhost:8765"
    rpc_token = ""
    dashboard_version = "0.1.0"

    # Try state-dir runtime file first, then repo-level fallback.
    for runtime_path in (state_runtime_path, repo_runtime_path):
        if runtime_path.exists():
            try:
                runtime = json.loads(runtime_path.read_text())
                rpc_base_url = runtime.get("rpcBaseUrl", rpc_base_url)
                rpc_token = runtime.get("rpcToken", rpc_token)
                dashboard_version = runtime.get("dashboardVersion", dashboard_version)
                break  # First readable file wins
            except Exception:
                continue  # Try the next candidate

    # The runtime file already captured the project-scoped token at server-bind time.
    # Only fall back to the repo-level dashboard-token file when the runtime file
    # had no usable rpcToken (missing, empty, or unreadable).  Reading it
    # unconditionally would overwrite the project-scoped value with whichever
    # project last rotated the shared file — exactly the multi-project 401 bug.
    if not rpc_token and token_path.exists():
        try:
            rpc_token = token_path.read_text().strip()
        except Exception:
            pass

    return {
        "rpcBaseUrl": rpc_base_url,
        "rpcToken": rpc_token,
        "dashboardVersion": dashboard_version,
    }


def _seed_projects() -> list[dict]:
    """The default project list when projects.json doesn't exist yet."""
    _repo_name = _GH_REPO.split("/", 1)[-1] if "/" in _GH_REPO else _GH_REPO
    return [{
        "id": _repo_name,
        "name": _repo_name,
        "repo": _GH_REPO,
        "createdAt": "2026-04-09T00:00:00Z",
    }]


def _load_projects_raw() -> list[dict]:
    """Read the persisted project list, seeding if missing or unparseable."""
    with _PROJECTS_LOCK:
        if not _PROJECTS_FILE.exists():
            seed = _seed_projects()
            _PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PROJECTS_FILE.write_text(json.dumps(seed, indent=2))
            return seed
        try:
            data = json.loads(_PROJECTS_FILE.read_text())
        except Exception:
            return _seed_projects()
        if not isinstance(data, list):
            return _seed_projects()
        return data


def _save_projects_raw(projects: list[dict]) -> None:
    with _PROJECTS_LOCK:
        _PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROJECTS_FILE.write_text(json.dumps(projects, indent=2))


def _enrich_project(p: dict) -> dict:
    """Attach live health/agents/momentum to a stored project record.

    Registry stats are loaded from the project's own state directory so
    the project list shows each project's actual discussion counts rather
    than the AF host project's data.
    """
    project_name = p.get("name", "")
    try:
        from backend.registry import DiscussionRegistry  # noqa: PLC0415
        if project_name:
            try:
                from backend.state_paths import for_project as _fp_enrich  # noqa: PLC0415
                _enrich_state = _fp_enrich(project_name).state_dir / ".autonomous-team"
                if not _enrich_state.exists():
                    _enrich_state = _fp_enrich(project_name).state_dir
                reg_stats = DiscussionRegistry(state_dir=_enrich_state).stats()
            except Exception:
                reg_stats = DiscussionRegistry().stats()
        else:
            reg_stats = DiscussionRegistry().stats()
    except Exception:
        reg_stats = {"total": 0, "done": 0, "in_progress": 0}

    # availableRoles: count the role catalog (the .claude/agents/*.md files)
    try:
        ac = AgentCards(plugin_loader=_plugin_loader)
        available_roles = len(ac.list_agents())
    except Exception:
        available_roles = 0

    # activeAgents + liveness: both resolve the fleet key through the same
    # helper (`_resolve_fleet_project_name`) so the two halves of the same
    # sentence can never describe two different projects (D#2314 — the prior
    # code resolved these from two different `p` fields: liveness from `id`,
    # activeAgents from `name`). `active_agents_count` stays `None` (never
    # `0`) when the fleet key can't be resolved or the read itself raises —
    # a project that was not successfully queried must never render an
    # agent count (D#2314 "no unearned zero").
    #
    # D#2314 PR2 (F3): the same `active_agents()` rows also carry `role` and
    # `started_at` — already fetched for the count, previously discarded.
    # Surface them as `newestStartedAt` (max started_at) and `roles` (role
    # per live row, freshest first) so the card can render "newest started
    # 4m ago" and up to two "<role> running" clauses. There is no heartbeat
    # column, so this is deliberately labeled "started", never "activity".
    fleet_project_name = _resolve_fleet_project_name(p)
    liveness = _probe_liveness(fleet_project_name)
    active_agents_count = None  # int | None — None means "not successfully queried"
    newest_started_at = None
    roles: list[str] = []
    if liveness != "unknown":
        try:
            from backend.fleet.concurrency import active_agents as _fleet_active_agents  # noqa: PLC0415
            rows = _fleet_active_agents(fleet_project_name)
            active_agents_count = len(rows)
            if rows:
                rows_by_recency = sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)
                newest_started_at = rows_by_recency[0].get("started_at")
                roles = [r.get("role", "") for r in rows_by_recency if r.get("role")]
        except Exception:
            active_agents_count = None
            liveness = "unknown"

    # health: three-state mapping
    # "no loop-runs logs found" = cron has never fired, idle not broken → healthy
    # Any other unhealthy reason = real failure → degraded
    try:
        loop_h = check_loop_health()
        loop_status = loop_h.get("status", "error")
        loop_reason = loop_h.get("reason", "")
        if loop_h.get("healthy"):
            health = "healthy"
            health_reason = None
        elif loop_reason == "no loop-runs logs found":
            # Cron has never fired — idle system, not broken
            health = "healthy"
            health_reason = "no loop activity"
        elif loop_status == "warning":
            health = "degraded"
            health_reason = "loop stale"
        else:
            health = "degraded"
            health_reason = loop_reason or "loop unhealthy"
    except Exception:
        health = "healthy"
        health_reason = None

    in_progress = reg_stats.get("in_progress", 0)
    total = reg_stats.get("total", 0)
    if total == 0:
        momentum = "stalled"
    elif in_progress == 0:
        momentum = "steady"
    elif in_progress > 3:
        momentum = "accelerating"
    else:
        momentum = "steady"

    result: dict = {
        "id": p.get("id", ""),
        "name": p.get("name", ""),
        "repo": p.get("repo", ""),
        "health": health,
        "availableRoles": available_roles,
        "momentum": momentum,
        "createdAt": p.get("createdAt", ""),
        "liveness": liveness,
        "primary": p.get("id") == _GH_REPO_NAME,
    }
    # D#2314: absent, never 0, when the project wasn't successfully queried.
    if active_agents_count is not None:
        result["activeAgents"] = active_agents_count
    if newest_started_at is not None:
        result["newestStartedAt"] = newest_started_at
    if roles:
        result["roles"] = roles
    if health_reason is not None:
        result["healthReason"] = health_reason
    return result


def _list_projects() -> list[dict]:
    return [_enrich_project(p) for p in _load_projects_raw()]


def _slugify(text: str) -> str:
    import re as _re
    s = _re.sub(r"[^a-zA-Z0-9-]+", "-", text.strip().lower()).strip("-")
    return s or "project"


def _create_project(name: str, repo: str) -> dict:
    import datetime as _dt
    projects = _load_projects_raw()
    base_id = _slugify(name)
    pid = base_id
    i = 1
    existing_ids = {p.get("id") for p in projects}
    while pid in existing_ids:
        i += 1
        pid = f"{base_id}-{i}"
    new_project = {
        "id": pid,
        "name": name,
        "repo": repo,
        "createdAt": _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z"),
    }
    projects.append(new_project)
    _save_projects_raw(projects)
    return _enrich_project(new_project)


def _delete_project(project_id: str) -> bool:
    projects = _load_projects_raw()
    new_list = [p for p in projects if p.get("id") != project_id]
    if len(new_list) == len(projects):
        return False
    _save_projects_raw(new_list)
    return True


def _project_sub_endpoint(sub: str, project_id: str = "") -> dict | list | None:
    """Resolve a /api/projects/<id>/<sub> path to JSON.

    *project_id* is the URL segment from ``/api/projects/<id>/…``.  KPI
    sub-paths use it to load the right project's registry so the dashboard
    shows projectb data when projectb is selected, not AF data.

    Returns None if the sub-path is unknown so the dispatcher can try other
    routes.
    """
    if sub == "budget/status":
        # Use a 60-second per-project cache to avoid hammering the blackboard.
        # Cache is keyed by project_id so projectb and AF never share a cached result.
        import time as _time  # noqa: PLC0415
        _all_caches = _project_sub_endpoint.__dict__.setdefault("_budget_cache", {})
        _cache_key = project_id or ""
        _proj_cache = _all_caches.setdefault(_cache_key, {})
        _bucket = int(_time.time() // 60)
        if _proj_cache.get("bucket") != _bucket:
            _af_name = _GH_REPO.split("/", 1)[-1] if "/" in _GH_REPO else _GH_REPO
            _is_global = not project_id or project_id == _af_name
            try:
                from backend.cost_tracker import CostTracker  # noqa: PLC0415
                from backend import subscription_usage as _sub_usage  # noqa: PLC0415
                if _is_global:
                    spend = CostTracker().aggregate_daily_monthly_spend()
                else:
                    from backend.state_paths import for_project as _fp  # noqa: PLC0415
                    from backend.blackboard import Blackboard as _BB  # noqa: PLC0415
                    _paths = _fp(project_id)
                    _bb = _BB(root=_paths.state_dir / "blackboard")
                    spend = CostTracker(bb=_bb).aggregate_daily_monthly_spend()
                limits = _sub_usage.current_plan_limits()
            except Exception:
                spend = {"daily_usd": 0.0, "monthly_usd": 0.0}
                limits = {
                    "daily_usd_cap": 15.0,
                    "monthly_usd_cap": 450.0,
                    "source": "hardcoded-fallback",
                }
            _proj_cache["result"] = {
                "dailySpend": round(spend["daily_usd"], 4),
                "monthlySpend": round(spend["monthly_usd"], 4),
                "dailyLimit": round(limits["daily_usd_cap"], 4),
                "monthlyLimit": round(limits["monthly_usd_cap"], 4),
                "currency": "USD",
                "alertThreshold": 0.8,
                "limitSource": limits["source"],
            }
            _proj_cache["bucket"] = _bucket
        return _proj_cache["result"]

    if sub == "kpi":
        kpi = _get_project_kpi(project_id)
        velocity = (kpi.get("velocity") or {}).get("last_24h") or 0
        cycle = (kpi.get("pr_cycle_time") or {}).get("mean_hours") or 0.0
        idle = (kpi.get("idle_rate") or {}).get("last_24h_pct") or 0.0
        estimation = kpi.get("estimation") or {}
        # accuracy is None when total_measured < min_samples — pass null to frontend
        raw_accuracy = estimation.get("accuracy")
        accuracy = float(raw_accuracy) if raw_accuracy is not None else None
        total_measured = int(estimation.get("total_measured") or 0)
        min_samples = int(estimation.get("min_samples") or 5)
        return {
            "velocity": int(velocity),
            "momentum": int(velocity),
            "cycleTimeMean": round(float(cycle), 2),
            "estimationAccuracy": accuracy,
            "estimationAccuracySampleCount": total_measured,
            "estimationAccuracyMinSamples": min_samples,
            "period": "last_24h",
            "idleRatePct": round(float(idle), 1),
        }

    if sub == "kpi/velocity":
        # Build a per-day velocity series from audit.jsonl over the last 14 days.
        return _kpi_velocity_series(days=14)

    if sub == "kpi/cycle-time":
        kpi = _get_project_kpi(project_id)
        ct = kpi.get("pr_cycle_time") or {}
        mean = float(ct.get("mean_hours") or 0)
        median = float(ct.get("median_hours") or 0)
        # Single phase since we don't separate review vs build vs merge yet.
        return [
            {"phase": "review", "hours": round(median, 2)},
            {"phase": "merge", "hours": round(max(mean - median, 0), 2)},
        ]

    if sub == "spawn-queue":
        return _spawn_queue_status()

    if sub == "spawn-queue/pending":
        return _spawn_queue_status().get("pending", [])

    if sub == "spawn-queue/active":
        return _spawn_queue_status().get("active", [])

    if sub == "agents":
        return _project_agents_list()

    if sub == "control":
        # Read config.json + control_plane gates as the dashboard's ControlSettings.
        try:
            cfg_text = Path(".autonomous-team/config.json").read_text()
            cfg = json.loads(cfg_text)
        except Exception:
            cfg = {}
        gates = cfg.get("gates") or {}
        policies = cfg.get("policies") or {}
        return {
            "autoMerge": bool(gates.get("auto_merge", True)),
            "requireSecurityReview": bool(gates.get("security_review", True)),
            "maxConcurrentAgents": int(
                (policies.get("executor") or {}).get("max_concurrent", 3)
            ),
            "loopIntervalMinutes": int(cfg.get("loop_interval_minutes", 10)),
            "budgetAlertEnabled": bool(gates.get("budget_check", True)),
            "qualityGateThreshold": float(
                (policies.get("code_reviewer") or {}).get("quality_threshold", 0.8)
            ),
        }

    if sub == "control/gates":
        try:
            cfg = json.loads(Path(".autonomous-team/config.json").read_text())
        except Exception:
            cfg = {}
        gates = cfg.get("gates") or {}
        return [
            {"name": k, "enabled": bool(v), "requiredLabels": []}
            for k, v in gates.items()
        ]

    if sub == "control/audit":
        # Tail the last N audit.jsonl entries and project them into AuditEntry shape.
        # Deduplicate by (ts, source, action, key, old, new) so test runs that write
        # the same key/value many times in the same second don't flood the display.
        audit = _audit_path()
        out: list[dict] = []
        if not audit.exists():
            return out
        try:
            lines = audit.read_text().splitlines()[-100:]
        except Exception:
            return out
        seen_rows: set[tuple] = set()
        for i, line in enumerate(reversed(lines)):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not rec.get("ts"):
                continue
            dedup_key = (
                rec.get("ts"),
                rec.get("source"),
                rec.get("action"),
                rec.get("key"),
                str(rec.get("old")),
                str(rec.get("new")),
            )
            if dedup_key in seen_rows:
                continue
            seen_rows.add(dedup_key)
            out.append({
                "id": f"{rec.get('seq', i)}-{i}",
                "timestamp": str(rec.get("ts") or ""),
                "actor": str(rec.get("actor") or ""),
                "action": str(rec.get("action") or ""),
                "target": str(rec.get("key") or ""),
                "details": {
                    "old": rec.get("old"),
                    "new": rec.get("new"),
                    "source": rec.get("source"),
                },
            })
            if len(out) >= 50:
                break
        return out

    if sub == "cost":
        try:
            from backend.budget import BudgetTracker  # noqa: PLC0415
            bt = BudgetTracker().get_status()
        except Exception:
            return {"total": 0, "breakdown": {}}
        spent = int(bt.get("spent") or 0) * 3.0 / 1_000_000
        breakdown: dict[str, float] = {}
        for entry in bt.get("agents", []) or []:
            role = str(entry.get("agent") or "unknown")
            cost = float(entry.get("total") or 0) * 3.0 / 1_000_000
            breakdown[role] = round(breakdown.get(role, 0) + cost, 2)
        return {"total": round(spent, 2), "breakdown": breakdown}

    return None


def _bust_budget_cache() -> None:
    """Clear the 60-second module-level cache on _project_sub_endpoint.

    Call this whenever the budget blackboard is reset so the next GET
    /api/projects/<id>/budget/status reads fresh data rather than returning
    the stale pre-reset value.
    """
    cache = _project_sub_endpoint.__dict__.get("_budget_cache")
    if cache is not None:
        cache.clear()


def _spawn_queue_status() -> dict:
    """Read .autonomous-team/spawn-queue.json and report it in the dashboard shape."""
    p = Path(".autonomous-team/spawn-queue.json")
    pending: list[dict] = []
    active: list[dict] = []
    if p.exists():
        try:
            raw = json.loads(p.read_text())
        except Exception:
            raw = {}
        for item in raw.get("pending") or []:
            pending.append({
                "id": str(item.get("id") or ""),
                "role": str(item.get("role") or ""),
                "discussion": int(item.get("discussion") or 0),
                "priority": int(item.get("priority") or 0),
                "status": "pending",
                "createdAt": str(item.get("enqueued_at") or ""),
            })
        for item in raw.get("active") or []:
            active.append({
                "id": str(item.get("id") or ""),
                "role": str(item.get("role") or ""),
                "discussion": int(item.get("discussion") or 0),
                "priority": int(item.get("priority") or 0),
                "status": "active",
                "createdAt": str(item.get("enqueued_at") or ""),
            })
    # totalToday: count spawn events from agent-feed.jsonl (primary source) and
    # fall back to audit.jsonl.  agent-feed.jsonl is authoritative because
    # scripts/post-agent-hook.sh writes a spawn_attempt event for every spawn.
    total_today = 0
    from datetime import date as _date_sq  # noqa: PLC0415
    today = _date_sq.today().isoformat()

    feed_path = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"
    if feed_path.exists():
        try:
            with feed_path.open() as fh:
                for line in fh:
                    if today not in line:
                        continue
                    if "spawn" not in line.lower():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    etype = rec.get("event_type") or ""
                    if "spawn" in etype.lower():
                        total_today += 1
        except Exception:
            pass

    # Fall back: scan audit.jsonl if agent-feed gave nothing
    if total_today == 0:
        audit = _audit_path()
        if audit.exists():
            try:
                with audit.open() as f:
                    for line in f:
                        if today in line and "spawn" in line:
                            total_today += 1
            except Exception:
                pass

    return {"pending": pending, "active": active, "totalToday": total_today}


def _spawn_blocks_list(limit: int = 10) -> list[dict]:
    """Return recent spawn-block events from agent-feed.jsonl and audit.jsonl.

    A spawn block is recorded when pre-spawn-check.sh rejects a spawn due to
    budget exhaustion, circuit-breaker trip, or worktree-cap overflow.  We
    scan agent-feed.jsonl (primary) and fall back to audit.jsonl for lines
    that contain the word "blocked" and a role field.

    Returns up to ``limit`` events in reverse-chronological order:
        [{"ts": str, "role": str, "reason": SpawnBlockReason, "discussion": int|None}, ...]
    """
    import re as _re_sb  # noqa: PLC0415
    _REASON_MAP = {
        "budget": "budget_exceeded",
        "circuit": "circuit_breaker_open",
        "subscription": "subscription_throttled",
        "worktree_cap": "worktree_cap_reached",
        "worktree": "worktree_cap_reached",
        "concurrency": "concurrency_cap_reached",
    }
    blocks: list[dict] = []

    # Source 1: agent-feed.jsonl — spawn_attempt events with blocked=true
    feed = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"
    if feed.exists():
        try:
            with feed.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line or "block" not in line.lower():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("event_type") not in ("spawn_blocked", "spawn_attempt"):
                        continue
                    payload = rec.get("payload") or {}
                    blocked = payload.get("blocked") or rec.get("blocked")
                    if not blocked:
                        continue
                    reason_raw = (payload.get("block_reason") or rec.get("reason") or "").lower()
                    reason = "unknown"
                    for k, v in _REASON_MAP.items():
                        if k in reason_raw:
                            reason = v
                            break
                    blocks.append({
                        "ts": rec.get("ts") or "",
                        "role": payload.get("role") or rec.get("role") or "",
                        "reason": reason,
                        "discussion": payload.get("discussion") or rec.get("discussion"),
                    })
        except Exception:
            pass

    # Source 2: circuit-breaker-history.jsonl
    cb_hist = _REPO_ROOT / ".autonomous-team" / "circuit-breaker-history.jsonl"
    if cb_hist.exists():
        try:
            with cb_hist.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("event") not in ("tripped", "blocked"):
                        continue
                    blocks.append({
                        "ts": rec.get("ts") or rec.get("timestamp") or "",
                        "role": rec.get("role") or "",
                        "reason": "circuit_breaker_open",
                        "discussion": rec.get("discussion"),
                    })
        except Exception:
            pass

    # Sort newest-first, deduplicate on (ts, role), return limited set
    seen: set[tuple] = set()
    result: list[dict] = []
    for b in sorted(blocks, key=lambda x: x.get("ts") or "", reverse=True):
        key = (b.get("ts"), b.get("role"))
        if key in seen:
            continue
        seen.add(key)
        result.append(b)
        if len(result) >= limit:
            break
    return result


def _project_agents_list() -> list[dict]:
    """Build a recent-agents list from .autonomous-team/spawned/*.json files."""
    out: list[dict] = []
    spawn_dir = Path(".autonomous-team/spawned")
    if not spawn_dir.exists():
        return out
    files = sorted(spawn_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:30]:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        routing = data.get("routing") or {}
        # Filename is "<role>-<scope>-<id>.json"; split off the trailing
        # "-pr-N" or "-discussion-N" to recover the multi-word role.
        stem = f.stem
        for trailer in ("-pr-", "-discussion-"):
            if trailer in stem:
                stem = stem.split(trailer)[0]
                break
        role = str(routing.get("role") or stem)
        pr = routing.get("pr_number")
        disc = routing.get("discussion_ref")
        # Status is "done" if the file's mtime is >5 minutes old, else "running"
        # — proxy until we have real lifecycle tracking.
        import datetime as _dt  # noqa: PLC0415
        mtime = _dt.datetime.fromtimestamp(f.stat().st_mtime, tz=_dt.UTC)
        age_s = (_dt.datetime.now(_dt.UTC) - mtime).total_seconds()
        status = "running" if age_s < 300 else "done"
        out.append({
            "id": f.stem,
            "role": role,
            "status": status,
            "startedAt": mtime.isoformat(),
            "duration": int(age_s),
            **({"discussion": int(disc)} if disc else {}),
            **({"pr": int(pr)} if pr else {}),
        })
    return out


def _kpi_velocity_series(days: int) -> list[dict]:
    """Day-by-day count of memory/success entries in audit.jsonl over `days`."""
    audit = _audit_path()
    if not audit.exists():
        return []
    from datetime import date as _date, timedelta as _td  # noqa: PLC0415
    today = _date.today()
    counts: dict[str, int] = {(today - _td(days=i)).isoformat(): 0 for i in range(days)}
    try:
        with audit.open() as f:
            for line in f:
                if "memory/" not in line or "success" not in line:
                    continue
                # Cheap day-key extraction from the leading ts field
                if line[7:8] != '"' and line[8:9] != '"':
                    continue
                idx = line.find('"ts":')
                if idx < 0:
                    continue
                day = line[idx + 7 : idx + 17]
                if day in counts:
                    counts[day] += 1
    except Exception:
        return []
    series = [
        {"date": d, "points": counts[d], "prs": counts[d]}
        for d in sorted(counts.keys())
    ]
    return series


# ---------------------------------------------------------------------------
# Loop runner state (Path 2 — control plane via Claude Code subprocess)
# ---------------------------------------------------------------------------
#
# Module-level dict tracking active and recent /api/loop/run invocations.
# The React dashboard polls these endpoints to display run status and stream
# output. Polling (vs streaming) lets the dashboard reconnect to in-flight
# runs after page navigation, which an SSE-only design can't easily do.
#
# Each run also tee's its output to .autonomous-team/loop-runs/<id>.log so
# future training data extraction can mine the text. Lines are kept in memory
# for fast polling but log files are the source of truth.


_LOOP_RUNS: dict[str, dict] = {}
_LOOP_RUNS_LOCK = _threading.Lock()
_LOOP_RUNS_MAX = 50  # keep this many recent runs in memory

# Regex for valid project IDs — same pattern as idea_id from PR #282.
# Must start with a lowercase letter or digit; hyphens allowed in the middle.
import re as _re_module
_PROJECT_ID_RE = _re_module.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


def _validate_project_id(project_id: str) -> bool:
    """Return True if project_id matches the safe slug pattern."""
    return bool(_PROJECT_ID_RE.match(project_id))


# Instruction allow-list: letters, digits, whitespace, and safe punctuation.
# This is a crude prompt-injection mitigation; it is NOT a full defense.
# The /api/loop/run endpoint remains privileged — callers can execute arbitrary
# prompts with full filesystem and tool access. Restrict network access at the
# OS/firewall level for untrusted environments.
_INSTRUCTION_SAFE_RE = _re_module.compile(
    r"^[A-Za-z0-9\s.,;:!?()\[\]{}'\"\/\-_=+*&^%$#@<>]+$"
)
_INSTRUCTION_MAX_LEN = 2000


def _validate_instruction(instruction: str) -> tuple[bool, str]:
    """Validate a loop-run instruction.

    Returns (ok, error_message). ok=True means safe to use.
    Rejects instructions longer than 2000 chars or containing characters
    outside the allow-list.
    """
    if len(instruction) > _INSTRUCTION_MAX_LEN:
        return False, f"instruction too long ({len(instruction)} chars; max {_INSTRUCTION_MAX_LEN})"
    if not _INSTRUCTION_SAFE_RE.match(instruction):
        return False, (
            "instruction contains disallowed characters; "
            "only letters, digits, whitespace and .,;:!?()[]{}'\"/\\-_=+*&^%$#@<> are permitted"
        )
    return True, ""


def _audit_loop_run_request(instruction: str, source_ip: str) -> None:
    """Append a one-line audit record to .autonomous-team/loop-run-requests.jsonl.

    Truncates the instruction to 200 chars in the log to avoid unbounded growth
    while still providing enough context for an audit trail.
    """
    import datetime as _dt_audit
    import json as _json_audit
    record = {
        "ts": _dt_audit.datetime.now(_dt_audit.UTC).isoformat().replace("+00:00", "Z"),
        "source_ip": source_ip,
        "instruction_preview": instruction[:200] + ("…" if len(instruction) > 200 else ""),
    }
    try:
        audit_path = Path(".autonomous-team/loop-run-requests.jsonl")
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a") as _af:
            _af.write(_json_audit.dumps(record) + "\n")
    except Exception:  # noqa: BLE001
        pass  # Non-fatal — never let audit I/O break the endpoint


def _start_loop_run(instruction: str, project_id: str = "fulcrumaxe", source: str = "start_loop_run_direct") -> dict:
    """Spawn `claude -p <instruction>` in a daemon thread.

    Returns the run dict (also stored in _LOOP_RUNS). Raises FileNotFoundError
    with a helpful message if the claude binary is missing.

    project_id scopes the log file under
    .autonomous-team/loop-runs/<project_id>/<run_id>.log and is stored in the
    run dict for later filtering.

    source is a label identifying which callsite triggered the spawn (used by
    SpawnGuard for per-source rate limiting and cap enforcement). Callers
    MUST pass the correct source label from the table in Discussion #424.
    SpawnGuard.acquire() is called here before any subprocess is created.
    """
    # --- SpawnGuard: enforce rate limit, cap, and feature gate ---
    _acquire_result = _spawn_guard.acquire(source)
    if _acquire_result.status == AcquireStatus.GATE_DISABLED:
        raise PermissionError(
            f"spawn gate disabled: gates.allow_claude_spawn is false or missing. "
            f"Set it to true in .autonomous-team/config.json to enable spawning."
        )
    if _acquire_result.status == AcquireStatus.RATE_LIMITED:
        raise PermissionError(
            f"spawn rate-limited: source {source!r} must wait "
            f"{_acquire_result.retry_after_seconds}s before next spawn. "
            f"({_acquire_result.message})"
        )
    if _acquire_result.status == AcquireStatus.CAP_REACHED:
        raise PermissionError(
            f"spawn cap reached: {_acquire_result.message}"
        )

    # --- Global spawn-rate budget + circuit breaker (Discussion #427) ---
    # Repo-wide last line of defence: trips when aggregate spawn rate across ALL
    # sources exceeds rolling-window thresholds, layered on top of SpawnGuard.
    try:
        from backend.claude_spawn_tracker import record as _cst_record, SpawnBlocked as _SpawnBlocked
        _cst_record(source=source)
    except ImportError:
        pass  # tracker not yet available (bootstrap)
    except Exception as _cst_exc:  # noqa: BLE001
        if type(_cst_exc).__name__ == "SpawnBlocked":
            import datetime as _dt2
            _err_run_id = _dt2.datetime.now(_dt2.UTC).strftime("%Y%m%dT%H%M%SZ")
            _spawn_guard.release(source)  # release slot we just acquired
            return {
                "run_id": _err_run_id,
                "instruction": instruction,
                "started_at": _err_run_id,
                "finished_at": _err_run_id,
                "exit_code": 503,
                "lines": [],
                "log_path": None,
                "status": "error",
                "error": "spawn_breaker_tripped",
                "project_id": project_id,
            }
        raise

    import datetime as _dt
    import subprocess

    claude_bin = os.environ.get("AF_CLAUDE_BIN") or "claude"
    # Quick existence check before spawning
    import shutil as _shutil
    if _shutil.which(claude_bin) is None:
        raise FileNotFoundError(
            f"claude CLI not found at {claude_bin!r}; install Claude Code or set AF_CLAUDE_BIN env var"
        )

    run_id = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(".autonomous-team/loop-runs") / project_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"

    # Ensure unique run_id even if two starts hit the same second
    counter = 0
    while True:
        with _LOOP_RUNS_LOCK:
            if run_id not in _LOOP_RUNS:
                break
        counter += 1
        run_id = f"{run_id}-{counter}"
        log_path = log_dir / f"{run_id}.log"

    run: dict = {
        "run_id": run_id,
        "instruction": instruction,
        "started_at": run_id,
        "finished_at": None,
        "exit_code": None,
        "lines": [],  # list of stdout lines (no trailing newline)
        "log_path": str(log_path),
        "status": "running",  # running | done | cancelled | error
        "error": None,
        "project_id": project_id,
    }
    with _LOOP_RUNS_LOCK:
        _LOOP_RUNS[run_id] = run
        # Trim to max
        if len(_LOOP_RUNS) > _LOOP_RUNS_MAX:
            old_ids = sorted(_LOOP_RUNS.keys())[: len(_LOOP_RUNS) - _LOOP_RUNS_MAX]
            for old in old_ids:
                if _LOOP_RUNS[old].get("status") != "running":
                    _LOOP_RUNS.pop(old, None)

    def _worker():
        try:
            log_file = log_path.open("w")
            log_file.write(f"# instruction: {instruction}\n# started: {run_id}\n\n")
            log_file.flush()
            proc = subprocess.Popen(
                [claude_bin, "-p", instruction],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                env={**os.environ, "CLAUDE_PROJECT_DIR": os.getcwd()},
            )
            with _LOOP_RUNS_LOCK:
                run["pid"] = proc.pid
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                stripped = line.rstrip("\n")
                with _LOOP_RUNS_LOCK:
                    run["lines"].append(stripped)
                log_file.write(line)
                log_file.flush()
            proc.wait()
            with _LOOP_RUNS_LOCK:
                run["exit_code"] = proc.returncode
                run["finished_at"] = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
                if run.get("status") != "cancelled":
                    run["status"] = "done"
            log_file.write(f"\n# exit: {proc.returncode}\n")
            log_file.flush()
            log_file.close()
            # Record this dashboard-triggered run as a real loop iteration
            # so /health/loop reports "ok" instead of "error". Without this,
            # the cron-based loop is the only thing that updates loop-metrics
            # and the dashboard goes red the moment cron stops.
            try:
                started_dt = _dt.datetime.strptime(run["started_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.UTC)
                finished_dt = _dt.datetime.strptime(run["finished_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.UTC)
                duration_s = max(int((finished_dt - started_dt).total_seconds()), 1)
                counters = _compute_loop_counters(
                    started_dt.isoformat(),
                    finished_dt.isoformat(),
                )
                metric = {
                    "timestamp": finished_dt.isoformat().replace("+00:00", "Z"),
                    "iteration": 0,
                    "duration_s": duration_s,
                    "actions": 1,
                    "agents_spawned": counters["agents_spawned"],
                    "prs_merged": counters["prs_merged"],
                    # Dashboard-triggered runs don't track scan counts — use 0.
                    "discussions_scanned": 0,
                    "prs_scanned": 0,
                    "exit_code": proc.returncode,
                    "trigger": "dashboard",
                    "run_id": run["run_id"],
                }
                _METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
                with _METRICS_FILE.open("a") as mf:
                    mf.write(json.dumps(metric) + "\n")
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            with _LOOP_RUNS_LOCK:
                run["status"] = "error"
                run["error"] = str(exc)
                run["finished_at"] = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        finally:
            # Always release the SpawnGuard slot, whether the spawn succeeded or failed.
            _spawn_guard.release(source)

    t = _threading.Thread(target=_worker, daemon=True)
    t.start()
    return run


def _cancel_loop_run(run_id: str) -> bool:
    """Kill an in-flight loop run by run_id. Returns True if found, False otherwise."""
    import signal
    with _LOOP_RUNS_LOCK:
        run = _LOOP_RUNS.get(run_id)
        if run is None:
            return False
        run["status"] = "cancelled"
        pid = run.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return True


def _get_loop_run(run_id: str, since_line: int = 0, project_id: str | None = None) -> dict | None:
    """Return a run's full state, optionally only lines after index since_line.

    When project_id is given, return None if the run belongs to a different project.
    """
    with _LOOP_RUNS_LOCK:
        run = _LOOP_RUNS.get(run_id)
        if run is None:
            return None
        if project_id is not None and run.get("project_id") != project_id:
            return None
        # Snapshot the lines to avoid races
        all_lines = run["lines"]
        new_lines = all_lines[since_line:] if since_line < len(all_lines) else []
        return {
            "run_id": run["run_id"],
            "instruction": run["instruction"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "exit_code": run["exit_code"],
            "status": run["status"],
            "error": run["error"],
            "log_path": run["log_path"],
            "project_id": run.get("project_id", "fulcrumaxe"),
            "total_lines": len(all_lines),
            "new_lines": new_lines,
        }


def _list_loop_runs(project_id: str | None = None) -> list[dict]:
    """Return summaries of all tracked runs, most recent first.

    When project_id is given, only runs for that project are returned.
    """
    with _LOOP_RUNS_LOCK:
        items = list(_LOOP_RUNS.values())
    if project_id is not None:
        items = [r for r in items if r.get("project_id") == project_id]
    items.sort(key=lambda r: r["started_at"], reverse=True)
    return [
        {
            "run_id": r["run_id"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "exit_code": r["exit_code"],
            "line_count": len(r["lines"]),
            "instruction_preview": r["instruction"][:100],
            "project_id": r.get("project_id", "fulcrumaxe"),
        }
        for r in items
    ]

# ---------------------------------------------------------------------------
# Innovate toggle helpers
# ---------------------------------------------------------------------------

# _INNOVATE_STATE_PATH is resolved at call time via __getattr__ above (not a
# module-level constant) — see D#1810. A direct assignment/patch
# (`monkeypatch.setattr(api_mod, "_INNOVATE_STATE_PATH", ...)`, several tests
# do this) shadows __getattr__ exactly like any other module attribute;
# `_attr()` below honors that from inside this module too.

_INNOVATE_STATE_LOCK = _threading.Lock()


def _read_innovate_state_file() -> dict:
    """Read innovate-state.json, returning defaults if missing or corrupt."""
    try:
        path = _attr("_INNOVATE_STATE_PATH")
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {"last_iteration_at": None, "iteration_count": 0}


def _write_innovate_state_file(state: dict) -> None:
    path = _attr("_INNOVATE_STATE_PATH")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def _innovate_state() -> dict:
    """Return {enabled, last_iteration_at, iteration_count}.

    The state FILE is authoritative for the ``enabled`` flag.  The control-plane
    gate ``gates.idea_generation`` is used only as the initial seed value when
    no file exists yet (first run).  After the first toggle the file always
    contains an explicit ``enabled`` key and takes precedence.  This makes the
    toggle durable across restarts without relying on control_plane.json staying
    in sync.
    """
    with _INNOVATE_STATE_LOCK:
        file_state = _read_innovate_state_file()

    if "enabled" in file_state:
        enabled = bool(file_state["enabled"])
    else:
        # First-run seed: fall back to control-plane gate
        from backend.control_plane import ControlPlane  # noqa: PLC0415
        cp = ControlPlane()
        cp.load()
        enabled = bool(cp.get("gates.idea_generation"))

    return {
        "enabled": enabled,
        "last_iteration_at": file_state.get("last_iteration_at"),
        "iteration_count": int(file_state.get("iteration_count", 0)),
    }


def _set_innovate(enabled: bool) -> dict:
    """Flip the Innovate toggle and persist it to innovate-state.json.

    Both the state file and the control-plane gate are updated so that
    callers reading either source see a consistent value.
    """
    from backend.control_plane import ControlPlane  # noqa: PLC0415
    cp = ControlPlane()
    cp.load()
    cp.set("gates.idea_generation", enabled)
    with _INNOVATE_STATE_LOCK:
        file_state = _read_innovate_state_file()
        file_state["enabled"] = enabled          # persist the new value
        _write_innovate_state_file(file_state)
    return _innovate_state()


# ---------------------------------------------------------------------------
# Test-origin spawn rejection (belt-and-suspenders backend guard)
# ---------------------------------------------------------------------------
# Requests bearing a Puppeteer/HeadlessChrome/Playwright User-Agent are
# almost certainly an E2E test that failed to install request interception.
# We reject them before any state mutation so a runaway test loop can never
# fan out real Claude subprocesses.
#
# NOTE: We do NOT block by Origin alone. The dashboard itself runs at
# localhost:5173 and its browser-timer fires /api/innovate/tick with that
# origin. Blocking on origin alone would prevent legitimate operator use of
# the Innovate loop. The headless-UA check is the reliable discriminator:
# real browsers hitting localhost:5173 send a normal Chrome/Firefox UA, not
# HeadlessChrome. Playwright/Puppeteer sessions always have a headless UA.
#
# Override env vars (set per-process only — never persist in config files):
#   AF_ALLOW_TEST_ORIGIN_SPAWNS=1  — local human-driven dev / Puppeteer with interception
#   AF_MCP_TEST_ORIGIN=1           — MCP-driven Chrome DevTools scenarios (Discussion #475)
#
# Default behaviour when neither env var is set: reject any HeadlessChrome UA,
# returning HTTP 403 {"error":"spawn_blocked_test_origin"}.
# ---------------------------------------------------------------------------

import re as _re
_TEST_UA_RE = _re.compile(r"HeadlessChrome|Puppeteer|playwright", _re.IGNORECASE)
# Kept for reference / future extension; not used as a block criterion on its own
# because the dashboard's Innovate loop fires from this origin legitimately.
_TEST_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})


def _reject_test_origin_spawn(handler: "BaseHTTPRequestHandler") -> bool:  # type: ignore[name-defined]
    """Return True (and send HTTP 403) if the request looks like a test-origin spawn.

    Call at the top of every spawn endpoint handler. If True is returned, the
    caller must ``return`` immediately without performing any state mutation.

    Detection logic:
    - Blocks requests whose User-Agent matches HeadlessChrome, Puppeteer, or
      Playwright (case-insensitive). Real browser sessions never carry these.
    - Does NOT block by Origin alone. The dashboard's Innovate loop fires from
      localhost:5173, which is indistinguishable from a Puppeteer test by
      origin alone. The headless UA is the reliable signal.

    Bypass env vars (must be set on the backend process, never in config files):
    - ``AF_ALLOW_TEST_ORIGIN_SPAWNS=1`` — legacy bypass for local human-driven dev
      with Puppeteer request-interception installed.
    - ``AF_MCP_TEST_ORIGIN=1`` — bypass for MCP-driven Chrome DevTools scenario runs
      (Discussion #475). Set when running ``scripts/run-scenarios.sh`` in live mode
      so HeadlessChrome UA is allowed through cleanly.

    Important: neither env var relaxes the authentication gate — only the UA
    heuristic is bypassed. A request still requires a valid ``af_dashboard_token``
    cookie/header to reach spawn logic.
    """
    import os as _os  # noqa: PLC0415
    if _os.environ.get("AF_ALLOW_TEST_ORIGIN_SPAWNS", "").strip() == "1":
        return False
    if _os.environ.get("AF_MCP_TEST_ORIGIN", "").strip() == "1":
        return False

    ua = handler.headers.get("User-Agent", "")

    if _TEST_UA_RE.search(ua):
        body = '{"error": "spawn_blocked_test_origin"}'
        handler.send_response(403)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body.encode())
        return True

    return False


def _innovate_tick() -> dict:
    """Start one loop run via _start_loop_run and bump the iteration counter."""
    import datetime as _dt  # noqa: PLC0415
    run = _start_loop_run(
        "Run ONE /loop iteration per CLAUDE.md protocol with idea-generation enabled. "
        "Report what you did in under 300 words.",
        source="innovate_tick_internal",
    )
    now_iso = _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")
    with _INNOVATE_STATE_LOCK:
        file_state = _read_innovate_state_file()
        file_state["last_iteration_at"] = now_iso
        file_state["iteration_count"] = int(file_state.get("iteration_count", 0)) + 1
        _write_innovate_state_file(file_state)
    return {"run_id": run["run_id"], "iteration_count": file_state["iteration_count"]}


# ---------------------------------------------------------------------------
# API version request counters (in-process, thread-safe via GIL for int ops)
# ---------------------------------------------------------------------------

# af_api_requests_total{version="v1"} — incremented for every versioned request
_api_requests_versioned: dict[str, int] = {}
# af_api_requests_unversioned_total — incremented for unversioned (deprecated) access
_api_requests_unversioned: int = 0


def _increment_version_counter(version: int, unversioned: bool) -> None:
    """Increment in-process API version counters."""
    global _api_requests_unversioned  # noqa: PLW0603
    if unversioned:
        _api_requests_unversioned += 1
    else:
        key = f"v{version}"
        _api_requests_versioned[key] = _api_requests_versioned.get(key, 0) + 1


def _version_metrics_text() -> str:
    """Return Prometheus text lines for API version counters."""
    lines = [
        "# HELP af_api_requests_total Requests by API version",
        "# TYPE af_api_requests_total counter",
    ]
    for label, count in sorted(_api_requests_versioned.items()):
        lines.append(f'af_api_requests_total{{version="{label}"}} {count}')
    lines.append(
        "# HELP af_api_requests_unversioned_total Requests using deprecated unversioned paths"
    )
    lines.append("# TYPE af_api_requests_unversioned_total counter")
    lines.append(f"af_api_requests_unversioned_total {_api_requests_unversioned}")
    return "\n".join(lines) + "\n"


def _spawn_guard_metrics_text() -> str:
    """Return Prometheus text lines for SpawnGuard counters and in-flight gauges."""
    try:
        stats = _spawn_guard.stats()
    except Exception:  # noqa: BLE001
        return ""
    lines = [
        "# HELP af_claude_spawn_total Total claude -p subprocesses spawned by source",
        "# TYPE af_claude_spawn_total counter",
    ]
    for src, s in sorted(stats["by_source"].items()):
        lines.append(f'af_claude_spawn_total{{source="{src}"}} {s["fires_total"]}')
    lines.append(
        "# HELP af_claude_spawn_in_flight Current in-flight claude subprocesses by source"
    )
    lines.append("# TYPE af_claude_spawn_in_flight gauge")
    for src, s in sorted(stats["by_source"].items()):
        lines.append(f'af_claude_spawn_in_flight{{source="{src}"}} {s["in_flight"]}')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# KPI cache â recompute at most once per 60 seconds
# ---------------------------------------------------------------------------

_kpi_cache: dict = {"data": None, "expires_at": 0.0}

# Per-project KPI cache: {project_name: {"data": dict, "expires_at": float}}
# Capped at 64 entries (LRU eviction) to prevent unbounded growth (CWE-400).
_KPI_PROJECT_CACHE_MAX = 64
_kpi_project_cache: collections.OrderedDict[str, dict] = collections.OrderedDict()

_KPI_EMPTY: dict = {
    "version": 1,
    "computed_at": None,
    "velocity": {"last_24h": 0, "all_time_per_day": 0.0, "total_done": 0},
    "estimation_accuracy": {
        "tasks_with_estimates": 0,
        "mean_absolute_error_hours": None,
        "within_1_5x_pct": None,
    },
    "estimation": {"accuracy": None, "total_measured": 0, "min_samples": 5},
    "idle_rate": {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0},
    "pr_cycle_time": {"mean_hours": None, "median_hours": None, "total_measured": 0},
}


def _get_cached_kpi() -> dict:
    """Return KPI data from cache, recomputing when the TTL has elapsed."""
    now = time.monotonic()
    if _kpi_cache["data"] is None or now >= _kpi_cache["expires_at"]:
        try:
            data = kpi_engine.compute_all()
        except Exception:  # noqa: BLE001
            # Graceful fallback when dependencies are missing
            data = {
                "version": 1,
                "computed_at": None,
                "velocity": {"last_24h": 0, "all_time_per_day": 0.0, "total_done": 0},
                "estimation_accuracy": {
                    "tasks_with_estimates": 0,
                    "mean_absolute_error_hours": None,
                    "within_1_5x_pct": None,
                },
                "idle_rate": {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0},
                "pr_cycle_time": {"mean_hours": None, "median_hours": None, "total_measured": 0},
            }
        _kpi_cache["data"] = data
        _kpi_cache["expires_at"] = now + 60.0
    return _kpi_cache["data"]  # type: ignore[return-value]


def _get_project_kpi(project_name: str) -> dict:
    """Return KPI data for *project_name*, reading from that project's registry.

    Uses a 60-second per-project cache.  Falls back to empty KPI when the
    project's registry does not exist yet.

    For the default AF project, delegates to :func:`_get_cached_kpi` so we
    keep the single shared cache rather than duplicating it.
    """
    _af_name = _GH_REPO.split("/", 1)[-1] if "/" in _GH_REPO else _GH_REPO
    if not project_name or project_name == _af_name:
        return _get_cached_kpi()

    now = time.monotonic()
    # LRU: move to end on hit so least-recently-used entry stays at front.
    if project_name not in _kpi_project_cache:
        _kpi_project_cache[project_name] = {"data": None, "expires_at": 0.0}
        # Evict oldest entry when over cap (CWE-400 guard).
        while len(_kpi_project_cache) > _KPI_PROJECT_CACHE_MAX:
            _kpi_project_cache.popitem(last=False)
    else:
        _kpi_project_cache.move_to_end(project_name)
    bucket = _kpi_project_cache[project_name]
    if bucket["data"] is None or now >= bucket["expires_at"]:
        try:
            from backend.state_paths import for_project as _fp  # noqa: PLC0415
            paths = _fp(project_name)
            # Registry lives in <state_dir>/.autonomous-team/registry.json or
            # directly in <state_dir>/registry.json depending on layout.
            registry_path = paths.state_dir / ".autonomous-team" / "registry.json"
            if not registry_path.exists():
                registry_path = paths.state_dir / "registry.json"
            if registry_path.exists():
                raw = json.loads(registry_path.read_text())
                discussions = raw.get("discussions", []) if isinstance(raw, dict) else []
            else:
                discussions = []
            data = {
                "version": 1,
                "computed_at": None,
                "velocity": kpi_engine.compute_velocity(discussions),
                "estimation_accuracy": kpi_engine.compute_estimation_accuracy(discussions),
                "estimation": kpi_engine.compute_estimation_metrics(discussions),
                "idle_rate": {"last_24h_pct": None, "all_time_pct": None, "total_iterations": 0},
                "pr_cycle_time": kpi_engine.compute_pr_cycle_time(discussions),
            }
        except Exception:  # noqa: BLE001
            data = dict(_KPI_EMPTY)
        bucket["data"] = data
        bucket["expires_at"] = now + 60.0
    return bucket["data"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Shared plugin loader -- instantiated once at module load time
# ---------------------------------------------------------------------------

_plugin_loader = PluginLoader()


class _Handler(BaseHTTPRequestHandler):
    """Route HTTP requests to backend module methods."""

    # Set to False via --no-enable-sse to disable /stream/* endpoints.
    enable_sse: bool = True
    # Set to False via --no-dashboard to disable GET /dashboard.
    enable_dashboard: bool = True
    # Set to False via --no-docs to disable GET /openapi.json and GET /docs.
    enable_docs: bool = True
    # Set from AF_API_AUTH_KEY env var at startup. None means auth disabled.
    auth_key: str | None = None
    # Set to False via --no-rate-limit to disable per-IP rate limiting.
    enable_rate_limit: bool = True

    # Shared rate limiter: 60 requests/minute burst, 1 req/sec refill.
    _rate_limiter: RateLimiter = RateLimiter(
        rate=1.0, burst=60.0, cleanup_interval=60.0, stale_after=600.0
    )
    # Shared SSE connection tracker: 5 concurrent SSE connections per IP.
    _sse_tracker: SSEConnectionTracker = SSEConnectionTracker(max_per_ip=5)

    # Suppress default "200 OK" log lines to keep output clean.
    def log_request(self, code="-", size="-") -> None:  # type: ignore[override]
        pass

    # ------------------------------------------------------------------
    # Dispatch helpers
    # ------------------------------------------------------------------

    def _client_ip(self) -> str:
        """Return the client IP address for rate limiting purposes."""
        return self.client_address[0]

    def _check_rate_limit(self) -> tuple[bool, float]:
        """Check per-IP rate limit. Returns (allowed, remaining_tokens)."""
        if not self.__class__.enable_rate_limit:
            return True, 0.0
        return self.__class__._rate_limiter.check(self._client_ip())

    def _send_429(self) -> None:
        """Send HTTP 429 Too Many Requests with Retry-After header."""
        ip = self._client_ip()
        retry_after = self.__class__._rate_limiter.retry_after(ip)
        body = _json({"error": "rate limit exceeded", "retry_after": retry_after})
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(retry_after))
        self.send_header("X-RateLimit-Remaining", "0")
        self.end_headers()
        self.wfile.write(body)

    def _send(
        self,
        status: int,
        payload: object,
        remaining: float = 0.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        # Inject _api_version into every JSON response body.
        if isinstance(payload, dict):
            payload = {"_api_version": CURRENT_VERSION, **payload}
        body = _json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-RateLimit-Remaining", str(int(remaining)))
        # Propagate trace context to downstream callers via W3C traceparent.
        # Also record the HTTP status code and response size on the active span.
        try:
            from backend.tracing import get_current_span  # noqa: PLC0415
            sp = get_current_span()
            if sp is not None:
                sp.attributes["http.status_code"] = status
                sp.attributes["http.response_content_length"] = len(body)
                self.send_header("traceparent", make_traceparent(sp.trace_id, sp.span_id))
        except Exception:  # noqa: BLE001
            pass
        if extra_headers:
            for header_name, header_value in extra_headers.items():
                self.send_header(header_name, header_value)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self) -> None:
        """Send SSE response headers and flush."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def _sse_event(self, data: object) -> None:
        """Write a single SSE data event to the wire."""
        line = "data: " + json.dumps(data, default=str) + "\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    def _stream_feed(self, feed_file: "Path | None" = None) -> None:
        """Stream agent-feed events to the client as SSE.

        When *feed_file* is None (default / AF project), subscribes to the
        in-process AgentOutputEvent bus — the same source that writes to the
        default feed file.

        When *feed_file* is provided (a different project requested via
        ``?project=``), tails that file by seeking to the end and polling for
        new JSONL lines every 0.5 s with a 30 s heartbeat.  This avoids
        leaking AF's in-process bus events to clients scoped to another project.
        """
        self._send_sse_headers()

        if feed_file is not None:
            # File-tail mode: poll the project's agent-feed.jsonl for new lines.
            import time as _time  # noqa: PLC0415
            POLL_INTERVAL = 0.5
            HEARTBEAT_AFTER = 30.0  # seconds between heartbeats when idle

            try:
                # Seek to end so we only emit new events written after connect.
                fh = open(feed_file, "r", encoding="utf-8", errors="replace") if feed_file.exists() else None  # noqa: PTH123
                start_pos = fh.seek(0, 2) if fh else 0  # type: ignore[union-attr]
                _ = start_pos  # position held by fh internally
                last_heartbeat = _time.monotonic()
                while True:
                    new_events = False
                    if fh:
                        try:
                            for raw in fh:
                                raw = raw.strip()
                                if not raw:
                                    continue
                                try:
                                    ev = json.loads(raw)
                                    self._sse_event(ev)
                                    new_events = True
                                except (json.JSONDecodeError, OSError):
                                    continue
                        except OSError:
                            pass
                    elif feed_file.exists():
                        fh = open(feed_file, "r", encoding="utf-8", errors="replace")  # noqa: PTH123
                        fh.seek(0, 2)  # seek to end
                    now = _time.monotonic()
                    if not new_events and (now - last_heartbeat) >= HEARTBEAT_AFTER:
                        self._sse_event({"type": "heartbeat"})
                        last_heartbeat = now
                    _time.sleep(POLL_INTERVAL)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if fh:
                    fh.close()
            return

        # In-process bus mode (default project / no project param).
        q: queue.Queue = queue.Queue()
        sub_id = get_bus().subscribe(AgentOutputEvent, lambda e: q.put(e))
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                    self._sse_event(event.to_dict())
                except queue.Empty:
                    self._sse_event({"type": "heartbeat"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            get_bus().unsubscribe(sub_id)

    def _stream_events(self) -> None:
        """Subscribe to ALL event types and push each as SSE.

        Useful for debugging and monitoring the full event bus traffic.
        """
        self._send_sse_headers()
        q: queue.Queue = queue.Queue()

        # Subscribe to every known event type.
        from backend.event_bus import (  # noqa: PLC0415
            BudgetSpendEvent,
            GateChangeEvent,
            LoopIterationEvent,
        )

        sub_ids = [
            get_bus().subscribe(AgentOutputEvent, lambda e: q.put(e)),
            get_bus().subscribe(BudgetSpendEvent, lambda e: q.put(e)),
            get_bus().subscribe(GateChangeEvent, lambda e: q.put(e)),
            get_bus().subscribe(LoopIterationEvent, lambda e: q.put(e)),
        ]
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                    data = event.to_dict()
                    data["_event_type"] = type(event).__name__
                    self._sse_event(data)
                except queue.Empty:
                    self._sse_event({"type": "heartbeat"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            for sub_id in sub_ids:
                get_bus().unsubscribe(sub_id)

    def _stream_status(self) -> None:
        """Push a status snapshot every 10 seconds as SSE."""
        self._send_sse_headers()
        try:
            while True:
                snapshot: dict = {}
                # Budget
                try:
                    snapshot["budget"] = BudgetTracker().get_status()
                except Exception:  # noqa: BLE001
                    snapshot["budget"] = None
                # Queue counts
                try:
                    reg = DiscussionRegistry()
                    stats = reg.stats()
                    queue: dict = {}
                    for key in ("SPEC_READY", "IMPLEMENTING", "REVIEWING"):
                        queue[key] = stats.get(key, 0)
                    snapshot["queue"] = queue
                except Exception:  # noqa: BLE001
                    snapshot["queue"] = None
                # Loop-ago (last line of loop-metrics.jsonl)
                try:
                    loop_ago = None
                    if _METRICS_FILE.exists():
                        last_line = ""
                        with _METRICS_FILE.open("r") as fh:
                            for line in fh:
                                stripped = line.strip()
                                if stripped:
                                    last_line = stripped
                        if last_line:
                            loop_ago = json.loads(last_line)
                    snapshot["loop_ago"] = loop_ago
                except Exception:  # noqa: BLE001
                    snapshot["loop_ago"] = None
                # KPI snapshot
                try:
                    snapshot["kpi"] = _get_cached_kpi()
                except Exception:  # noqa: BLE001
                    snapshot["kpi"] = None
                self._sse_event(snapshot)
                time.sleep(10)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _ok(
        self,
        payload: object,
        remaining: float = 0.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._send(200, payload, remaining=remaining, extra_headers=extra_headers)

    def _err(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _deprecation_headers(self, canonical_path: str) -> dict[str, str]:
        """Return Deprecation + Sunset + Link headers for unversioned access."""
        from backend.api_version import unversioned_info as _uvi  # noqa: PLC0415
        info = _uvi()
        return {
            "Deprecation": "true",
            "Sunset": info.sunset_date or "",
            "Link": f"</v{CURRENT_VERSION}{canonical_path}>; rel=\"successor-version\"",
        }

    def _extract_version(self, raw_path: str) -> tuple[VersionInfo | None, str, bool]:
        """Parse version from URL path and Accept-Version header.

        Returns (version_info, canonical_path, is_unversioned).
        version_info is None if the version is invalid (caller should return 400).
        is_unversioned is True when no /v<N>/ prefix was present in the URL.
        """
        url_version, canonical = parse_version(raw_path)
        # Accept-Version header overrides URL version.
        header_version_str = self.headers.get("Accept-Version", "").strip()
        if header_version_str:
            try:
                url_version = int(header_version_str)
            except ValueError:
                pass

        # Determine if the request was truly unversioned (no /vN/ in URL, no header).
        is_unversioned = not raw_path.startswith("/v") and not header_version_str

        try:
            vinfo = check_version(url_version)
        except ValueError:
            return None, canonical, is_unversioned

        return vinfo, canonical, is_unversioned

    def _read_body(self) -> dict:
        """Read and parse JSON request body. Returns {} on any failure."""
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _bearer_token(self) -> str | None:
        """Extract the raw Bearer token from the Authorization header, or None."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header:
            return None
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return parts[1]

    def _check_auth(self) -> bool:
        """Check Bearer token auth. Returns True if auth passes or is disabled.

        Sends 401/403 and returns False when auth fails so the caller can
        return immediately without sending a second response.
        """
        key = self.__class__.auth_key
        if not key:
            # Auth disabled -- always pass.
            return True
        token = self._bearer_token()
        if token is None:
            self._err(401, "unauthorized")
            return False
        # Constant-time comparison to prevent timing attacks.
        if not hmac.compare_digest(token, key):
            self._err(403, "forbidden")
            return False
        return True

    def _check_rbac(self, method: str, path: str) -> bool:
        """Check RBAC role permissions after bearer auth has passed.

        If no rbac section is configured, always returns True (allow-all).
        If RBAC is enabled but the token has no role entry, allow-all as well —
        this preserves backward compatibility with the single-key AF_API_AUTH_KEY
        model where the key is not listed in the rbac.keys table.
        Sends 403 and returns False only when the token has an explicit role that
        does not permit the requested method + path.
        """
        token = self._bearer_token()
        if token is None:
            # No token — auth layer already handled this; skip RBAC.
            return True
        if not _rbac_manager.enabled:
            return True
        # Token not listed in RBAC table → legacy key, allow through.
        if _rbac_manager.get_role_for_token(token) is None:
            return True
        if not _rbac_manager.check(token, method, path):
            self._err(403, "forbidden")
            return False
        return True

    # ------------------------------------------------------------------
    # Route table
    # ------------------------------------------------------------------

    def _record_request(self, method: str, path: str, status: int, response_size: int, elapsed_ms: float) -> None:
        """Record an HTTP request timing sample in the benchmark recorder.

        Runs in a try/except so a benchmarking failure never breaks requests.
        """
        try:
            get_bench_recorder().record(
                "http",
                f"{method} {path}",
                elapsed_ms,
                metadata={"status_code": status, "response_size_bytes": response_size},
            )
        except Exception:  # noqa: BLE001
            pass

    def _start_request_span(self, method: str, raw_path: str):  # type: ignore[return]
        """
        Create a root HTTP span for the incoming request, honouring any
        incoming traceparent header to join an upstream distributed trace.
        """
        incoming = self.headers.get("traceparent", "")
        ctx = parse_traceparent(incoming)
        if ctx:
            set_remote_context(ctx["trace_id"], ctx["parent_id"])
        return start_span(
            f"http.{method.lower()}",
            attributes={
                "http.method": method,
                "http.url": raw_path,
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        _t0 = time.monotonic()
        raw_path = urlparse(self.path).path.rstrip("/")
        vinfo, path, is_unversioned = self._extract_version(raw_path)
        _trace_ctx = self._start_request_span("GET", raw_path)
        _trace_ctx.__enter__()
        try:
            # Reject unsupported explicit version prefixes (e.g. /v2/health) before
            # the fast-path health/metrics checks. Without this guard, /v2/health would
            # silently serve a 200 because canonical path "/health" matches the health
            # handler before vinfo is checked.
            if vinfo is None and not is_unversioned:
                import re as _re_pre
                m = _re_pre.match(r"^/v(\d+)", raw_path)
                bad_v = int(m.group(1)) if m else 0
                self._err(400, f"unsupported API version: {bad_v}")
                return

            # /health and /metrics endpoints are always exempt from auth and rate limiting.
            # parse_version() strips /v1/ prefix, so `path` is always the canonical form.
            if path == "/health":
                dep = self._deprecation_headers(path) if is_unversioned else None
                _increment_version_counter(CURRENT_VERSION, is_unversioned)
                loop_metrics = get_loop_metrics()
                health_body: dict[str, object] = {"ok": True}
                health_body.update(loop_metrics)
                self._ok(health_body, extra_headers=dep)
                return

            if path == "/health/loop":
                dep = self._deprecation_headers(path) if is_unversioned else None
                _increment_version_counter(CURRENT_VERSION, is_unversioned)
                self._ok(get_loop_health_dashboard(), extra_headers=dep)
                return

            if path == "/health/modules":
                dep = self._deprecation_headers(path) if is_unversioned else None
                _increment_version_counter(CURRENT_VERSION, is_unversioned)
                self._ok(_module_health.get_cached_module_health(), extra_headers=dep)
                return

            # ---- Dashboard runtime config endpoint ----
            # Returns {rpcBaseUrl, rpcToken, dashboardVersion} so the React SPA
            # can auto-discover the JSON-RPC backend without localStorage hacks.
            # Restricted to loopback (127.0.0.1 / ::1) — never exposed remotely.
            # Non-localhost callers get 403 regardless of auth state.
            # No CORS response for non-localhost Origin headers.
            #
            # TOKEN HYGIENE: rpcToken is returned in the response body in
            # plaintext. This is acceptable because the endpoint is
            # localhost-only. The React client must never console.log() the
            # config object or any field derived from it — that would expose the
            # bearer token in browser devtools even in production. See D#496.
            if path == "/api/config":
                _cfg_caller_ip = self._client_ip()
                _cfg_is_loopback = _cfg_caller_ip in ("127.0.0.1", "::1", "localhost")
                if not _cfg_is_loopback:
                    self._err(403, "forbidden: /api/config is localhost-only")
                    return
                # Refuse cross-origin requests (non-localhost Origin)
                _origin = self.headers.get("Origin", "")
                if _origin and not (
                    _origin.startswith("http://localhost")
                    or _origin.startswith("http://127.0.0.1")
                ):
                    self._err(403, "forbidden: cross-origin access to /api/config denied")
                    return
                self._ok(_get_dashboard_config())
                return

            # ---- React dashboard compatibility endpoints ----
            # These are aliases/adapters that return the shapes the React SPA
            # expects (see dashboard/src/api/types.ts). They aggregate data from
            # the existing registry/budget/agents systems into the Project and
            # Session shapes the UI renders against.
            if path == "/api/projects":
                self._ok(_list_projects())
                return

            if path == "/api/sessions/current":
                # Allow loopback callers (the React app on localhost) through
                # unconditionally. Remote callers must present a valid
                # AF_API_AUTH_KEY bearer token; if no key is configured, remote
                # access is denied to prevent CWE-306 broken-auth exposure.
                _caller_ip = self._client_ip()
                _is_loopback = _caller_ip in ("127.0.0.1", "::1", "localhost")
                if not _is_loopback:
                    if not self._check_auth():
                        return
                import datetime as _dt  # noqa: PLC0415
                now = _dt.datetime.now(_dt.UTC)
                session = {
                    "id": "dev-session",
                    "userId": "11111111-1111-1111-1111-111111111111",
                    "username": "dev",
                    "avatarUrl": "",
                    "createdAt": now.isoformat(),
                    "expiresAt": (now + _dt.timedelta(hours=24)).isoformat(),
                }
                self._ok(session)
                return

            if path == "/api/sessions":
                self._ok({"sessions": []})
                return

            # ---- Project sub-page endpoints ----
            # The React dashboard's project sub-pages call these.  The project
            # id is now passed through to KPI handlers so each project's
            # registry drives its own velocity / cycle-time numbers.
            if path.startswith("/api/projects/") and "/" in path[len("/api/projects/"):]:
                tail = path[len("/api/projects/"):]
                pid, _, sub = tail.partition("/")
                # CWE-22: validate project id before using as a path component.
                if pid and not _validate_project_name(pid):
                    self._err(400, f"invalid project id: {pid!r}")
                    return
                resp = _project_sub_endpoint(sub, project_id=pid)
                if resp is not None:
                    self._ok(resp)
                    return
                # Fall through if sub didn't match — let other handlers try.

            # ---- Agent activity feed (replaces broken /ws WebSocket) ----
            # Reads the audit log (state_paths.AUDIT_LOG) and maps entries into the
            # WsEvent shape the React dashboard's AgentFeedPage renders. Polling
            # via ?since=<seq> gives the same incremental delivery the WebSocket
            # would have, without needing WebSocket support in BaseHTTPServer.
            if path == "/api/events":
                from urllib.parse import urlparse as _u, parse_qs as _qs  # noqa: PLC0415
                qs = _qs(_u(self.path).query)
                try:
                    since = int(qs.get("since", ["0"])[0])
                except (TypeError, ValueError):
                    since = 0
                try:
                    limit = int(qs.get("limit", ["200"])[0])
                except (TypeError, ValueError):
                    limit = 200
                limit = max(1, min(limit, 1000))
                events = _read_audit_events(since=since, limit=limit)
                next_since = events[-1]["_seq"] if events else since
                # Strip the internal _seq from the wire payload — it's already
                # carried in next_since for the client to use as the next ?since=.
                wire_events = [{k: v for k, v in e.items() if k != "_seq"} for e in events]
                self._ok({"events": wire_events, "next_since": next_since})
                return

            # ---- Fleet projects endpoint ----
            # GET /api/fleet/projects — list every project either fleet-discovery
            # mechanism knows about (backend/fleet/fleet_set.py's resolved union),
            # each with a measured status. This is the handler that actually runs
            # by default: scripts/start-dashboard.sh launches backend/api.py
            # directly, so this branch -- not backend/routers/api_fleet.py's
            # FastAPI route, which still reads discover_running_projects() alone
            # -- is what every adopter following the documented coldstart path
            # hits. Redacted at this boundary via the same shared helper the
            # fleet.projects RPC method uses, so the two can never disagree about
            # which projects exist (D#2239 redaction boundary; D#2317 PR-a item 7).
            if path == "/api/fleet/projects":
                from backend.fleet.fleet_set import (  # noqa: PLC0415
                    resolve_fleet_set,
                    redact_for_dashboard,
                )
                projects = [redact_for_dashboard(p) for p in resolve_fleet_set()]
                self._ok({"projects": projects})
                return

            # ---- Ideas feed ----
            if path == "/api/ideas":
                import datetime as _dt_ideas  # noqa: PLC0415
                _ideas_list, _ideas_empty = _load_ideas()
                self._ok({
                    "ideas": _ideas_list,
                    "source_empty": _ideas_empty,
                    "fetched_at": _dt_ideas.datetime.now(_dt_ideas.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                return

            # ---- Spawn-blocks feed ----
            # GET /api/spawn-blocks[?limit=N] — recent blocked-spawn events for the dashboard
            if path == "/api/spawn-blocks":
                from urllib.parse import urlparse as _u_sb, parse_qs as _qs_sb  # noqa: PLC0415
                _qsp_sb = _qs_sb(_u_sb(self.path).query)
                try:
                    _sb_limit = int((_qsp_sb.get("limit") or ["10"])[0])
                except (TypeError, ValueError):
                    _sb_limit = 10
                _sb_limit = max(1, min(_sb_limit, 100))
                self._ok(_spawn_blocks_list(limit=_sb_limit))
                return

            # ---- Loop runner GET endpoints ----
            if path == "/api/loop/runs":
                # List recent runs (most recent first), summary fields only.
                # Optional ?project_id=... filter.
                from urllib.parse import urlparse as _urlparse, parse_qs as _qs  # noqa: PLC0415
                _qsp = _qs(_urlparse(self.path).query)
                _pid_filter = (_qsp.get("project_id") or [None])[0]
                runs = _list_loop_runs(project_id=_pid_filter)
                self._ok({"runs": runs})
                return

            if path.startswith("/api/loop/runs/"):
                run_id = path[len("/api/loop/runs/"):]
                # Optional query: ?since=N to only return new lines since index N
                from urllib.parse import urlparse as _urlparse, parse_qs as _qs  # noqa: PLC0415
                qs = _qs(_urlparse(self.path).query)
                since = 0
                try:
                    since = int(qs.get("since", ["0"])[0])
                except (TypeError, ValueError):
                    since = 0
                run = _get_loop_run(run_id, since_line=since)
                if run is None:
                    self._err(404, f"run {run_id!r} not found")
                    return
                self._ok(run)
                return

            # ---- Per-project loop run GET endpoints ----
            # GET /api/projects/<id>/loop/runs           — list runs for a project
            # GET /api/projects/<id>/loop/runs/<run_id>  — get one run, project-scoped
            if path.startswith("/api/projects/") and "/loop/runs" in path:
                _proj_tail = path[len("/api/projects/"):]
                _proj_id_pr, _, _loop_tail = _proj_tail.partition("/loop/runs")
                if not _validate_project_id(_proj_id_pr):
                    self._err(400, f"invalid project_id: {_proj_id_pr!r}")
                    return
                # Verify project exists
                _known_ids = {p.get("id") for p in _load_projects_raw()}
                if _proj_id_pr not in _known_ids:
                    self._err(404, f"project {_proj_id_pr!r} not found")
                    return
                if not _loop_tail or _loop_tail == "/":
                    # List runs for this project
                    _proj_runs = _list_loop_runs(project_id=_proj_id_pr)
                    self._ok({"runs": _proj_runs})
                    return
                # /api/projects/<id>/loop/runs/<run_id>
                _run_id_pr = _loop_tail.lstrip("/")
                from urllib.parse import urlparse as _urlparse2, parse_qs as _qs2  # noqa: PLC0415
                _qs_pr = _qs2(_urlparse2(self.path).query)
                _since_pr = 0
                try:
                    _since_pr = int(_qs_pr.get("since", ["0"])[0])
                except (TypeError, ValueError):
                    _since_pr = 0
                _run_pr = _get_loop_run(_run_id_pr, since_line=_since_pr, project_id=_proj_id_pr)
                if _run_pr is None:
                    self._err(404, f"run {_run_id_pr!r} not found in project {_proj_id_pr!r}")
                    return
                self._ok(_run_pr)
                return

            # ---- Innovate toggle GET endpoint ----
            if path == "/api/innovate":
                self._ok(_innovate_state())
                return

            if path == "/metrics":
                base = generate_prometheus_metrics()
                extra = _version_metrics_text()
                spawn_metrics = _spawn_guard_metrics_text()
                combined = (base.rstrip("\n") + "\n" + extra.rstrip("\n") + "\n" + spawn_metrics).encode("utf-8")
                self.send_response(200)
                self.send_header(
                    "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
                )
                self.send_header("Content-Length", str(len(combined)))
                self.end_headers()
                self.wfile.write(combined)
                return

            if vinfo is None:
                # Unknown version requested
                import re as _re
                m = _re.match(r"^/v(\d+)", raw_path)
                bad_v = int(m.group(1)) if m else 0
                self._err(400, f"unsupported API version: {bad_v}")
                return

            if not self._check_auth():
                return

            if not self._check_rbac("GET", path):
                return

            allowed, remaining = self._check_rate_limit()
            if not allowed:
                self._send_429()
                return

            _increment_version_counter(vinfo.version, is_unversioned)
            dep = self._deprecation_headers(path) if is_unversioned else None

            if path == "/rbac/whoami":
                token = self._bearer_token() or ""
                role_name = _rbac_manager.get_role_for_token(token)
                if role_name is None and not _rbac_manager.enabled:
                    role_name = "unrestricted"
                role_info = _rbac_manager.get_role_info(role_name or "") or {}
                self._ok({
                    "role": role_name,
                    "label": role_info.get("label", role_name),
                    "permissions": role_info.get("allow", []),
                }, remaining=remaining, extra_headers=dep)

            elif path == "/budget/status":
                bt = BudgetTracker()
                self._ok(bt.get_status(), remaining=remaining, extra_headers=dep)

            elif path == "/cost":
                ct = CostTracker()
                self._ok(ct.get_session_cost(), remaining=remaining)

            elif path == "/cost/summary":
                ct = CostTracker()
                self._ok(ct.get_summary(), remaining=remaining)

            elif path == "/registry":
                _reg_qs = urlparse(self.path).query
                _reg_project = None
                for _rp in _reg_qs.split("&"):
                    if _rp.startswith("project="):
                        _reg_project = _rp[len("project="):] or None
                        break
                # CWE-22: validate project name before using as a path component.
                if _reg_project and not _validate_project_name(_reg_project):
                    self._err(400, f"invalid project name: {_reg_project!r}")
                    return
                if _reg_project:
                    try:
                        from backend.state_paths import for_project as _fp_reg  # noqa: PLC0415
                        _reg_state = _fp_reg(_reg_project).state_dir / ".autonomous-team"
                        if not _reg_state.exists():
                            _reg_state = _fp_reg(_reg_project).state_dir
                        reg = DiscussionRegistry(state_dir=_reg_state)
                    except Exception:
                        # CWE-209: on error, return empty data — not AF state.
                        self._ok({"discussions": [], "stats": {"done": 0, "total": 0, "in_progress": 0, "spec_ready": 0}}, remaining=remaining, extra_headers=dep)
                        return
                else:
                    reg = DiscussionRegistry()
                data = reg.show()
                data["stats"] = reg.stats()
                self._ok(data, remaining=remaining, extra_headers=dep)

            elif path == "/registry/stats":
                _rstats_qs = urlparse(self.path).query
                _rstats_project = None
                for _rsp in _rstats_qs.split("&"):
                    if _rsp.startswith("project="):
                        _rstats_project = _rsp[len("project="):] or None
                        break
                # CWE-22: validate project name before using as a path component.
                if _rstats_project and not _validate_project_name(_rstats_project):
                    self._err(400, f"invalid project name: {_rstats_project!r}")
                    return
                if _rstats_project:
                    try:
                        from backend.state_paths import for_project as _fp_rstats  # noqa: PLC0415
                        _rstats_state = _fp_rstats(_rstats_project).state_dir / ".autonomous-team"
                        if not _rstats_state.exists():
                            _rstats_state = _fp_rstats(_rstats_project).state_dir
                        reg = DiscussionRegistry(state_dir=_rstats_state)
                    except Exception:
                        # CWE-209: on error, return empty data — not AF state.
                        self._ok({"done": 0, "total": 0, "in_progress": 0, "spec_ready": 0}, remaining=remaining, extra_headers=dep)
                        return
                else:
                    reg = DiscussionRegistry()
                self._ok(reg.stats(), remaining=remaining, extra_headers=dep)

            elif path == "/control":
                cp = ControlPlane()
                cp.load()
                policies = {
                    role: cp.get_policy(role)
                    for role in ("executor", "code-reviewer", "security-reviewer",
                                 "project-manager")
                }
                self._ok({"gates": cp.list_gates(), "policies": policies}, remaining=remaining, extra_headers=dep)

            elif path == "/control/gates":
                cp = ControlPlane()
                cp.load()
                self._ok(cp.list_gates(), remaining=remaining, extra_headers=dep)

            elif path == "/control/audit":
                cp = ControlPlane()
                cp.load()
                self._ok(cp.get_audit_log(), remaining=remaining, extra_headers=dep)

            elif path == "/audit":
                from urllib.parse import parse_qs  # noqa: PLC0415
                qs = parse_qs(urlparse(self.path).query)
                source = qs.get("source", [None])[0]
                action = qs.get("action", [None])[0]
                actor = qs.get("actor", [None])[0]
                since = qs.get("since", [None])[0]
                limit_str = qs.get("limit", ["50"])[0]
                try:
                    limit = int(limit_str)
                except ValueError:
                    limit = 50
                at = get_audit_trail()
                entries = at.query(source=source, action=action, actor=actor, since=since, limit=limit)
                self._ok(entries, remaining=remaining, extra_headers=dep)

            elif path == "/audit/stats":
                at = get_audit_trail()
                self._ok(at.stats(), remaining=remaining, extra_headers=dep)

            elif path == "/agents":
                ac = AgentCards(plugin_loader=_plugin_loader)
                self._ok({"agents": ac.list_agents()}, remaining=remaining, extra_headers=dep)

            elif path.startswith("/agents/") and not path.startswith("/agents/profiles"):
                role = path[len("/agents/"):]
                if not role:
                    self._err(400, "role name required")
                    return
                ac = AgentCards(plugin_loader=_plugin_loader)
                try:
                    self._ok(ac.get_card(role), remaining=remaining, extra_headers=dep)
                except AgentNotFoundError as exc:
                    self._err(404, str(exc))

            elif path == "/plugins":
                plugins = _plugin_loader.list_plugins()
                result = []
                for name in plugins:
                    p = _plugin_loader.get_plugin(name)
                    if p is not None:
                        result.append({
                            "name": p.name,
                            "description": p.description,
                            "version": p.version,
                            "review_pipeline": p.review_pipeline,
                        })
                self._ok({"plugins": result}, remaining=remaining, extra_headers=dep)

            elif path.startswith("/plugins/"):
                name = path[len("/plugins/"):]
                if not name:
                    self._err(400, "plugin name required")
                    return
                p = _plugin_loader.get_plugin(name)
                if p is None:
                    self._err(404, f"plugin '{name}' not found")
                    return
                self._ok({
                    "name": p.name,
                    "description": p.description,
                    "version": p.version,
                    "system_prompt": p.system_prompt,
                    "tools": p.tools,
                    "review_pipeline": p.review_pipeline,
                    "triggers": p.triggers,
                    "source_file": p.source_file,
                }, remaining=remaining, extra_headers=dep)

            elif path == "/kpi":
                _kpi_qs = urlparse(self.path).query
                _kpi_project = None
                for _kp in _kpi_qs.split("&"):
                    if _kp.startswith("project="):
                        _kpi_project = _kp[len("project="):] or None
                        break
                # CWE-22: validate project name before using as a path component.
                if _kpi_project and not _validate_project_name(_kpi_project):
                    self._err(400, f"invalid project name: {_kpi_project!r}")
                    return
                self._ok(_get_project_kpi(_kpi_project or ""), extra_headers=dep)

            elif path == "/kpi/velocity":
                _kpiv_qs = urlparse(self.path).query
                _kpiv_project = None
                for _kpvp in _kpiv_qs.split("&"):
                    if _kpvp.startswith("project="):
                        _kpiv_project = _kpvp[len("project="):] or None
                        break
                # CWE-22: validate project name before using as a path component.
                if _kpiv_project and not _validate_project_name(_kpiv_project):
                    self._err(400, f"invalid project name: {_kpiv_project!r}")
                    return
                self._ok(_get_project_kpi(_kpiv_project or "").get("velocity", {}), extra_headers=dep)

            elif path == "/kpi/cycle-time":
                _kpict_qs = urlparse(self.path).query
                _kpict_project = None
                for _kpctp in _kpict_qs.split("&"):
                    if _kpctp.startswith("project="):
                        _kpict_project = _kpctp[len("project="):] or None
                        break
                # CWE-22: validate project name before using as a path component.
                if _kpict_project and not _validate_project_name(_kpict_project):
                    self._err(400, f"invalid project name: {_kpict_project!r}")
                    return
                self._ok(_get_project_kpi(_kpict_project or "").get("pr_cycle_time", {}), extra_headers=dep)

            elif path == "/deps":
                parsed_url = urlparse(self.path)
                qparams: dict[str, str] = {}
                if parsed_url.query:
                    for part in parsed_url.query.split("&"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            qparams[k] = v
                        else:
                            qparams[part] = ""
                fmt = qparams.get("format", "json")
                mod_name = qparams.get("module", "")
                dg = get_cached_dep_graph()
                if fmt == "dot":
                    dot_text = dg.to_dot().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(dot_text)))
                    self.end_headers()
                    self.wfile.write(dot_text)
                elif fmt == "ascii":
                    ascii_text = dg.to_ascii(mod_name or None).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(ascii_text)))
                    self.end_headers()
                    self.wfile.write(ascii_text)
                else:
                    if mod_name:
                        self._ok(dg.impact(mod_name), extra_headers=dep)
                    else:
                        self._ok(dg.to_json(), extra_headers=dep)

            elif path == "/validate":
                sv = SchemaValidator()
                results = sv.validate_all()
                all_valid = all(len(errors) == 0 for errors in results.values())
                self._ok({"valid": all_valid, "files": results}, remaining=remaining)

            elif path == "/spawn-queue":
                sq = get_spawn_queue()
                self._ok(sq.status(), remaining=remaining)

            elif path == "/spawn-queue/pending":
                sq = get_spawn_queue()
                self._ok({"pending": sq.list_pending()}, remaining=remaining)

            elif path == "/spawn-queue/active":
                sq = get_spawn_queue()
                self._ok({"active": sq.list_active()}, remaining=remaining)

            elif path.startswith("/spawn-blocks"):
                # Return the last N spawn_blocked events from agent-feed.jsonl
                parsed_url = urlparse(self.path)
                qparams: dict[str, str] = {}
                if parsed_url.query:
                    for part in parsed_url.query.split("&"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            qparams[k] = v
                limit_blocks = int(qparams.get("limit", "10"))
                blocks: list[dict] = []
                feed_path = Path(".autonomous-team/agent-feed.jsonl")
                if feed_path.exists():
                    try:
                        lines = feed_path.read_text(encoding="utf-8").splitlines()
                        for line in reversed(lines):
                            if not line.strip():
                                continue
                            try:
                                ev = json.loads(line)
                                if ev.get("event_type") == "spawn_blocked":
                                    blocks.append({
                                        "role": ev.get("role", ""),
                                        "reason": ev.get("reason", "unknown"),
                                        "ts": ev.get("ts", ""),
                                        "discussion": ev.get("discussion"),
                                    })
                                    if len(blocks) >= limit_blocks:
                                        break
                            except json.JSONDecodeError:
                                continue
                    except OSError:
                        pass
                self._ok(blocks, remaining=remaining)

            elif path == "/stream/feed":
                if not self.__class__.enable_sse:
                    self._err(404, "SSE endpoints are disabled (--no-enable-sse)")
                    return
                ip = self._client_ip()
                if not self.__class__._sse_tracker.acquire(ip):
                    self._send_429()
                    return
                # Resolve per-project feed file when ?project= is given.
                _feed_project = None
                _feed_qs = urlparse(self.path).query
                if _feed_qs:
                    for _part in _feed_qs.split("&"):
                        if _part.startswith("project="):
                            _feed_project = _part[len("project="):] or None
                            break
                # CWE-22: validate project name before using as a path component.
                if _feed_project and not _validate_project_name(_feed_project):
                    self._err(400, f"invalid project name: {_feed_project!r}")
                    return
                _feed_file: "Path | None" = None
                if _feed_project:
                    try:
                        from backend.state_paths import for_project as _fp  # noqa: PLC0415
                        _feed_file = _fp(_feed_project).state_dir / "agent-feed.jsonl"
                    except Exception:
                        _feed_file = None
                try:
                    self._stream_feed(feed_file=_feed_file)
                finally:
                    self.__class__._sse_tracker.release(ip)

            elif path == "/stream/status":
                if not self.__class__.enable_sse:
                    self._err(404, "SSE endpoints are disabled (--no-enable-sse)")
                    return
                ip = self._client_ip()
                if not self.__class__._sse_tracker.acquire(ip):
                    self._send_429()
                    return
                try:
                    self._stream_status()
                finally:
                    self.__class__._sse_tracker.release(ip)

            elif path == "/stream/events":
                if not self.__class__.enable_sse:
                    self._err(404, "SSE endpoints are disabled (--no-enable-sse)")
                    return
                ip = self._client_ip()
                if not self.__class__._sse_tracker.acquire(ip):
                    self._send_429()
                    return
                try:
                    self._stream_events()
                finally:
                    self.__class__._sse_tracker.release(ip)

            elif path == "/ws":
                if not self.__class__.enable_sse:
                    self._err(404, "WebSocket endpoint is disabled (--no-streaming)")
                    return
                upgrade = self.headers.get("Upgrade", "").lower()
                connection = self.headers.get("Connection", "").lower()
                if upgrade != "websocket" or "upgrade" not in connection:
                    self._err(400, "expected WebSocket upgrade")
                    return
                ip = self._client_ip()
                if not self.__class__._sse_tracker.acquire(ip):
                    body = _json({"error": "too many connections"})
                    raw = (
                        b"HTTP/1.1 429 Too Many Requests\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                        b"Connection: close\r\n"
                        b"\r\n" + body
                    )
                    try:
                        self.request.sendall(raw)
                    except OSError:
                        pass
                    return
                parsed = urlparse(self.path)
                query_params: dict[str, str] = {}
                if parsed.query:
                    for part in parsed.query.split("&"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            query_params[k] = v
                        else:
                            query_params[part] = ""
                ws_headers = {k: v for k, v in self.headers.items()}
                handler = WebSocketHandler(
                    sock=self.request,
                    headers=ws_headers,
                    auth_key=self.__class__.auth_key,
                    query_params=query_params,
                    enable_streaming=self.__class__.enable_sse,
                )
                try:
                    handler.handle()
                finally:
                    self.__class__._sse_tracker.release(ip)

            elif path == "/dashboard":
                if not self.__class__.enable_dashboard:
                    self._err(404, "dashboard is disabled (--no-dashboard)")
                    return
                html = get_dashboard_html()
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif path == "/openapi.json":
                if not self.__class__.enable_docs:
                    self._err(404, "docs are disabled (--no-docs)")
                    return
                from backend.openapi import generate_spec  # noqa: PLC0415
                body = _json(generate_spec())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif path == "/docs":
                if not self.__class__.enable_docs:
                    self._err(404, "docs are disabled (--no-docs)")
                    return
                from backend.openapi import get_docs_html  # noqa: PLC0415
                body = get_docs_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif path == "/sessions":
                sm = SessionManager()
                self._ok({"sessions": sm.list_sessions()}, remaining=remaining, extra_headers=dep)

            elif path == "/sessions/current":
                sm = SessionManager()
                session = sm.current_session()
                if session is None:
                    self._err(404, "no active session")
                else:
                    self._ok(session, remaining=remaining, extra_headers=dep)

            elif path == "/sessions/compare":
                parsed_url = urlparse(self.path)
                qs: dict[str, str] = {}
                if parsed_url.query:
                    for part in parsed_url.query.split("&"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            qs[k] = v
                id_a = qs.get("a", "")
                id_b = qs.get("b", "")
                if not id_a or not id_b:
                    self._err(400, "query params 'a' and 'b' are required")
                    return
                sm = SessionManager()
                try:
                    result = sm.compare_sessions(id_a, id_b)
                    self._ok(result, remaining=remaining, extra_headers=dep)
                except ValueError as exc:
                    self._err(404, str(exc))

            elif path.startswith("/sessions/"):
                session_id = path[len("/sessions/"):]
                if not session_id:
                    self._err(400, "session_id required")
                    return
                sm = SessionManager()
                session = sm.get_session(session_id)
                if session is None:
                    self._err(404, f"session '{session_id}' not found")
                else:
                    self._ok(session, remaining=remaining, extra_headers=dep)

            elif path == "/replays":
                replays = get_recorder().list_replays()
                self._ok({"replays": replays}, remaining=remaining, extra_headers=dep)

            elif path == "/replays/status":
                eng = get_active_replay()
                if eng is None or not eng.is_alive:
                    self._ok({"active": False}, remaining=remaining, extra_headers=dep)
                else:
                    self._ok(eng.get_status(), remaining=remaining, extra_headers=dep)

            elif path.startswith("/replays/"):
                # Strip leading "/replays/" and split on "/" to detect /summary suffix.
                rest = path[len("/replays/"):]
                if not rest:
                    self._err(400, "agent_id required")
                    return
                parts = rest.split("/", 1)
                agent_id = parts[0]
                suffix = parts[1] if len(parts) > 1 else ""

                if suffix == "summary":
                    result = get_recorder().get_summary(agent_id)
                    if result is None:
                        self._err(404, f"no replay found for agent_id '{agent_id}'")
                    else:
                        self._ok(result, remaining=remaining, extra_headers=dep)
                elif suffix == "":
                    events = get_recorder().get_replay(agent_id)
                    if not events:
                        self._err(404, f"no replay found for agent_id '{agent_id}'")
                    else:
                        self._ok({"agent_id": agent_id, "events": events}, remaining=remaining, extra_headers=dep)
                else:
                    self._err(404, f"unknown replay sub-path: {suffix!r}")

            elif path == "/backups":
                self._ok({"backups": _backup.list_backups()}, remaining=remaining, extra_headers=dep)

            elif path == "/notifications/history":
                from backend.notifier import get_notifier  # noqa: PLC0415
                records = get_notifier().get_history(50)
                self._ok({"notifications": records}, remaining=remaining, extra_headers=dep)

            elif path == "/agents/profiles":
                from backend.agent_profiler import AgentProfiler  # noqa: PLC0415
                parsed_url = urlparse(self.path)
                recompute = "recompute=true" in (parsed_url.query or "")
                profiler = AgentProfiler()
                if recompute:
                    snapshot = profiler.compute()
                else:
                    snapshot = profiler.load_snapshot()
                    if snapshot is None:
                        snapshot = profiler.compute()
                self._ok(snapshot, remaining=remaining, extra_headers=dep)

            elif path == "/agents/profiles/summary":
                from backend.agent_profiler import AgentProfiler  # noqa: PLC0415
                profiler = AgentProfiler()
                snapshot = profiler.load_snapshot()
                if snapshot is None:
                    snapshot = profiler.compute()
                self._ok(snapshot.get("aggregate", {}), remaining=remaining, extra_headers=dep)

            elif path.startswith("/agents/profiles/"):
                role_name = path[len("/agents/profiles/"):]
                if not role_name:
                    self._err(400, "role name required")
                    return
                from backend.agent_profiler import AgentProfiler  # noqa: PLC0415
                profiler = AgentProfiler()
                snapshot = profiler.load_snapshot()
                if snapshot is None:
                    snapshot = profiler.compute()
                role_profile = snapshot.get("roles", {}).get(role_name)
                if role_profile is None:
                    self._err(404, f"no profile data for role '{role_name}'")
                else:
                    self._ok(role_profile, remaining=remaining, extra_headers=dep)

            elif path == "/quality":
                from backend.quality_scorer import QualityScorer  # noqa: PLC0415
                qs = QualityScorer()
                self._ok({"scores": qs.history(limit=20)}, remaining=remaining, extra_headers=dep)

            elif path == "/quality/stats":
                from backend.quality_scorer import QualityScorer  # noqa: PLC0415
                qs = QualityScorer()
                self._ok(qs.stats(), remaining=remaining, extra_headers=dep)

            elif path.startswith("/quality/"):
                pr_str = path[len("/quality/"):]
                if not pr_str:
                    self._err(400, "PR number required")
                    return
                try:
                    pr_number = int(pr_str)
                except ValueError:
                    self._err(400, f"invalid PR number: {pr_str!r}")
                    return
                from backend.quality_scorer import QualityScorer  # noqa: PLC0415
                qs = QualityScorer()
                score = qs._bb.read(f"quality/{pr_number}")
                if score is None:
                    self._err(404, f"no quality score for PR #{pr_number}")
                else:
                    self._ok(score, remaining=remaining, extra_headers=dep)

            elif path == "/memory/lessons":
                from backend.agent_memory import AgentMemory  # noqa: PLC0415
                from urllib.parse import parse_qs  # noqa: PLC0415
                parsed_url = urlparse(self.path)
                qs = parse_qs(parsed_url.query or "")
                role_filter = qs.get("role", [None])[0]
                tags_filter = [t for t in qs.get("tags", [""])[0].split(",") if t] or None
                limit_val = int(qs.get("limit", ["20"])[0])
                mem = AgentMemory()
                lessons = mem.query_lessons(
                    tags=tags_filter,
                    role=role_filter,
                    limit=limit_val,
                    cross_session=True,
                )
                self._ok({"lessons": lessons}, remaining=remaining, extra_headers=dep)

            elif path == "/memory/stats":
                from backend.agent_memory import AgentMemory  # noqa: PLC0415
                mem = AgentMemory()
                self._ok(mem.stats(), remaining=remaining, extra_headers=dep)

            elif path == "/memory/context":
                from backend.agent_memory import AgentMemory  # noqa: PLC0415
                from urllib.parse import parse_qs  # noqa: PLC0415
                parsed_url = urlparse(self.path)
                qs = parse_qs(parsed_url.query or "")
                files_param = qs.get("files", [""])[0]
                files_list = [f for f in files_param.split(",") if f]
                if not files_list:
                    self._err(400, "query param 'files' is required")
                    return
                mem = AgentMemory()
                block = mem.get_context_block(files=files_list)
                self._ok({"context": block}, remaining=remaining, extra_headers=dep)

            elif path == "/benchmarks":
                from urllib.parse import parse_qs  # noqa: PLC0415
                parsed_url = urlparse(self.path)
                qs = parse_qs(parsed_url.query or "")
                window = int(qs.get("window", ["300"])[0])
                rec = get_bench_recorder()
                all_stats = rec.get_all_stats(window_seconds=window)
                self._ok(
                    {"window_seconds": window, "stats": [_stats_to_dict(s) for s in all_stats]},
                    remaining=remaining,
                    extra_headers=dep,
                )

            elif path.startswith("/benchmarks/history"):
                from urllib.parse import parse_qs  # noqa: PLC0415
                parsed_url = urlparse(self.path)
                qs = parse_qs(parsed_url.query or "")
                category = qs.get("category", ["http"])[0]
                operation = qs.get("operation", [None])[0]
                points = int(qs.get("points", ["60"])[0])
                rec = get_bench_recorder()
                history = rec.get_history(category=category, operation=operation, points=points)
                self._ok(
                    {"category": category, "operation": operation, "history": history},
                    remaining=remaining,
                    extra_headers=dep,
                )

            elif path.startswith("/benchmarks/"):
                # /benchmarks/{category} or /benchmarks/{category}/{operation}
                from urllib.parse import parse_qs, unquote  # noqa: PLC0415
                parsed_url = urlparse(self.path)
                qs = parse_qs(parsed_url.query or "")
                window = int(qs.get("window", ["300"])[0])
                parts = path[len("/benchmarks/"):].split("/", 1)
                category = unquote(parts[0])
                operation = unquote(parts[1]) if len(parts) > 1 else None
                rec = get_bench_recorder()
                stats = rec.compute_stats(
                    category=category,
                    operation=operation,
                    window_seconds=window,
                )
                self._ok(_stats_to_dict(stats), remaining=remaining, extra_headers=dep)

            elif path == "/traces":
                from urllib.parse import parse_qs as _pqs  # noqa: PLC0415
                qs = _pqs(urlparse(self.path).query or "")
                limit_str = qs.get("limit", ["50"])[0]
                try:
                    limit_n = max(1, int(limit_str))
                except ValueError:
                    limit_n = 50
                spans = get_collector().peek(limit_n * 20)
                traces_map: dict = {}
                for sp in spans:
                    traces_map.setdefault(sp.trace_id, []).append(sp)
                trace_list = []
                for tid, trace_spans in list(traces_map.items())[-limit_n:]:
                    from backend.trace_export import export_spans as _exp  # noqa: PLC0415
                    trace_list.append({
                        "trace_id": tid,
                        "span_count": len(trace_spans),
                        "resourceSpans": _exp(trace_spans)["resourceSpans"],
                    })
                self._ok({"traces": trace_list, "count": len(trace_list)}, remaining=remaining)

            elif path == "/traces/stats":
                spans = get_collector().peek(10000)
                if not spans:
                    self._ok({
                        "traces_per_minute": 0.0,
                        "avg_spans": 0.0,
                        "p50_duration_ms": 0.0,
                        "p95_duration_ms": 0.0,
                        "error_rate": 0.0,
                    }, remaining=remaining)
                else:
                    now_ns = time.time_ns()
                    one_min_ns = 60 * 1_000_000_000
                    recent = [sp for sp in spans if now_ns - sp.start_time_unix_nano <= one_min_ns]
                    recent_traces = {sp.trace_id for sp in recent}
                    traces_per_min = float(len(recent_traces))

                    by_trace: dict = {}
                    for sp in spans:
                        by_trace.setdefault(sp.trace_id, []).append(sp)
                    avg_spans_val = sum(len(v) for v in by_trace.values()) / len(by_trace)

                    durations_ms = []
                    error_count = 0
                    for sp in spans:
                        if sp.end_time_unix_nano > 0:
                            dur = (sp.end_time_unix_nano - sp.start_time_unix_nano) / 1_000_000
                            durations_ms.append(dur)
                        if sp.status == "ERROR":
                            error_count += 1

                    durations_ms.sort()
                    total = len(durations_ms)
                    p50 = durations_ms[int(total * 0.50)] if total else 0.0
                    p95 = durations_ms[int(total * 0.95)] if total else 0.0
                    error_rate = error_count / len(spans) if spans else 0.0

                    self._ok({
                        "traces_per_minute": traces_per_min,
                        "avg_spans": round(avg_spans_val, 2),
                        "p50_duration_ms": round(p50, 3),
                        "p95_duration_ms": round(p95, 3),
                        "error_rate": round(error_rate, 4),
                    }, remaining=remaining)

            elif path.startswith("/traces/"):
                trace_id = path[len("/traces/"):]
                if not trace_id:
                    self._err(400, "trace_id required")
                else:
                    spans = get_collector().peek(10000)
                    matched = [sp for sp in spans if sp.trace_id == trace_id]
                    if not matched:
                        self._err(404, f"trace '{trace_id}' not found")
                    else:
                        from backend.trace_export import export_spans as _exp  # noqa: PLC0415
                        self._ok({
                            "trace_id": trace_id,
                            "span_count": len(matched),
                            "resourceSpans": _exp(matched)["resourceSpans"],
                        }, remaining=remaining)

            elif path == "/graphql":
                from urllib.parse import parse_qs  # noqa: PLC0415
                qs = parse_qs(urlparse(self.path).query)
                query_str = qs.get("query", [None])[0]
                if not query_str:
                    self._err(400, "query parameter is required")
                    return
                result = _graphql.execute(query_str)
                self._ok(result, remaining=remaining, extra_headers=dep)

            else:
                self._err(404, f"unknown endpoint: {path}")

        except Exception as exc:  # noqa: BLE001
            self._err(500, str(exc))
        finally:
            _trace_ctx.__exit__(None, None, None)
            _elapsed = (time.monotonic() - _t0) * 1000.0
            self._record_request("GET", urlparse(self.path).path, 0, 0, _elapsed)

    def do_POST(self) -> None:  # noqa: N802
        _t0 = time.monotonic()
        raw_path = urlparse(self.path).path.rstrip("/")
        vinfo, path, is_unversioned = self._extract_version(raw_path)
        _trace_ctx_post = self._start_request_span("POST", raw_path)
        _trace_ctx_post.__enter__()
        try:
            if vinfo is None:
                import re as _re
                m = _re.match(r"^/v(\d+)", raw_path)
                bad_v = int(m.group(1)) if m else 0
                self._err(400, f"unsupported API version: {bad_v}")
                return

            if not self._check_auth():
                return

            if not self._check_rbac("POST", path):
                return

            allowed, remaining = self._check_rate_limit()
            if not allowed:
                self._send_429()
                return

            _increment_version_counter(vinfo.version, is_unversioned)
            dep = self._deprecation_headers(path) if is_unversioned else None

            # ---- Loop runner control plane (Path 2 from Reality-Audit) ----
            # POST /api/loop/run        — start a new run, return {run_id, ...}
            # GET  /api/loop/runs       — list of recent runs (use_GET handler)
            # GET  /api/loop/runs/<id>  — full state of one run incl. lines so far
            # POST /api/loop/runs/<id>/cancel — kill an in-flight run
            #
            # Run state lives in a module-level dict so the React dashboard
            # can poll across navigations and reconnect to in-flight work.
            # The dashboard polls every ~1s; output is buffered in memory
            # AND tee'd to .autonomous-team/loop-runs/<id>.log for training.
            # ---- Budget reset (project-scoped) ----
            # POST /api/projects/<id>/budget/reset
            # Clears all budget/ blackboard keys AND busts the 60-second
            # in-process cache so the next GET /budget/status returns zero.
            if path.startswith("/api/projects/") and path.endswith("/budget/reset"):
                _proj_budget_tail = path[len("/api/projects/"):-len("/budget/reset")]
                _proj_budget_id = _proj_budget_tail.rstrip("/")
                if not _proj_budget_id:
                    self._err(400, "project id required")
                    return
                bt = BudgetTracker()
                bt.reset()
                _bust_budget_cache()
                self._ok(
                    {"ok": True, "project": _proj_budget_id, "status": bt.get_status()},
                    remaining=remaining,
                    extra_headers=dep,
                )
                return

            # ---- Project CRUD ----
            if path == "/api/projects":
                body = self._read_body() if self.headers.get("content-length") else {}
                name = (body.get("name") or "").strip()
                repo = (body.get("repo") or "").strip()
                if not name or not repo:
                    self._err(400, "name and repo are required")
                    return
                project = _create_project(name, repo)
                self._ok(project, remaining=remaining, extra_headers=dep)
                return

            # ---- Ideas POST endpoints ----
            if path.startswith("/api/ideas/") and path.endswith("/upvote"):
                idea_id = path[len("/api/ideas/"):-len("/upvote")]
                try:
                    idea = upvote_idea(idea_id)
                except ValueError as _e:
                    self._err(400, str(_e))
                    return
                except KeyError as _e:
                    self._err(404, str(_e))
                    return
                self._ok(idea, remaining=remaining, extra_headers=dep)
                return

            if path.startswith("/api/ideas/") and path.endswith("/dismiss"):
                idea_id = path[len("/api/ideas/"):-len("/dismiss")]
                try:
                    idea = dismiss_idea(idea_id)
                except ValueError as _e:
                    self._err(400, str(_e))
                    return
                except KeyError as _e:
                    self._err(404, str(_e))
                    return
                self._ok(idea, remaining=remaining, extra_headers=dep)
                return

            if path.startswith("/api/ideas/") and path.endswith("/promote"):
                idea_id = path[len("/api/ideas/"):-len("/promote")]
                try:
                    idea = promote_idea(idea_id)
                except ValueError as _e:
                    self._err(400, str(_e))
                    return
                except KeyError as _e:
                    self._err(404, str(_e))
                    return
                self._ok(idea, remaining=remaining, extra_headers=dep)
                return

            if path == "/api/loop/run":
                if _reject_test_origin_spawn(self):
                    return
                # Kill-switch: AF_API_AUTH_KEY must be set before anyone can
                # remotely trigger a loop run. Without it the endpoint is open
                # to the whole network; requiring the key forces an explicit
                # operator decision to expose this privileged endpoint.
                if not self.__class__.auth_key:
                    self._err(
                        503,
                        "loop/run endpoint requires AF_API_AUTH_KEY to be set; "
                        "set the env var and restart the server before enabling remote loop runs",
                    )
                    return
                if not self._check_auth():
                    return
                body = self._read_body() if self.headers.get("content-length") else {}
                instruction = body.get("instruction") or (
                    "Run ONE /loop iteration per CLAUDE.md protocol. "
                    "Report what you did in under 300 words."
                )
                _instr_ok, _instr_err = _validate_instruction(instruction)
                if not _instr_ok:
                    self._err(400, f"invalid instruction: {_instr_err}")
                    return
                req_project_id = (body.get("project_id") or "fulcrumaxe").strip()
                if not _validate_project_id(req_project_id):
                    self._err(400, f"invalid project_id: {req_project_id!r}")
                    return
                _audit_loop_run_request(instruction, self._client_ip())
                try:
                    run = _start_loop_run(instruction, project_id=req_project_id, source="loop_run_global")
                except PermissionError as exc:
                    body_str = str(exc)
                    if "rate-limited" in body_str:
                        # Extract retry_after from guard (re-read stats)
                        _stats = _spawn_guard.stats()
                        _src_stats = _stats["by_source"].get("loop_run_global", {})
                        _retry = 60
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Retry-After", str(_retry))
                        _body = json.dumps({"error": "rate-limited", "source": "loop_run_global", "retry_after_seconds": _retry}).encode()
                        self.send_header("Content-Length", str(len(_body)))
                        self.end_headers()
                        self.wfile.write(_body)
                    elif "spawn gate disabled" in body_str:
                        self._err(503, json.dumps({"error": "spawn gate disabled", "gate": "gates.allow_claude_spawn"}))
                    else:
                        self._err(503, json.dumps({"error": "spawn-cap reached", "source": "loop_run_global"}))
                    return
                except FileNotFoundError as exc:
                    self._err(503, str(exc))
                    return
                self._ok({
                    "run_id": run["run_id"],
                    "started_at": run["started_at"],
                    "log_path": run["log_path"],
                    "instruction": instruction,
                    "project_id": run["project_id"],
                }, remaining=remaining, extra_headers=dep)
                return

            # ---- Per-project loop run POST endpoints ----
            # POST /api/projects/<id>/loop/run — start a run scoped to a project
            if path.startswith("/api/projects/") and path.endswith("/loop/run"):
                if _reject_test_origin_spawn(self):
                    return
                # Same kill-switch as /api/loop/run above.
                if not self.__class__.auth_key:
                    self._err(
                        503,
                        "loop/run endpoint requires AF_API_AUTH_KEY to be set; "
                        "set the env var and restart the server before enabling remote loop runs",
                    )
                    return
                if not self._check_auth():
                    return
                _proj_tail_post = path[len("/api/projects/"):-len("/loop/run")]
                _proj_id_post = _proj_tail_post.rstrip("/")
                if not _validate_project_id(_proj_id_post):
                    self._err(400, f"invalid project_id: {_proj_id_post!r}")
                    return
                # Verify project exists
                _known_ids_post = {p.get("id") for p in _load_projects_raw()}
                if _proj_id_post not in _known_ids_post:
                    self._err(404, f"project {_proj_id_post!r} not found")
                    return
                body = self._read_body() if self.headers.get("content-length") else {}
                instruction = body.get("instruction") or (
                    "Run ONE /loop iteration per CLAUDE.md protocol. "
                    "Report what you did in under 300 words."
                )
                _instr_ok, _instr_err = _validate_instruction(instruction)
                if not _instr_ok:
                    self._err(400, f"invalid instruction: {_instr_err}")
                    return
                _audit_loop_run_request(instruction, self._client_ip())
                try:
                    run = _start_loop_run(instruction, project_id=_proj_id_post, source="loop_run_project")
                except PermissionError as exc:
                    body_str = str(exc)
                    if "rate-limited" in body_str:
                        _retry = 60
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Retry-After", str(_retry))
                        _body = json.dumps({"error": "rate-limited", "source": "loop_run_project", "retry_after_seconds": _retry}).encode()
                        self.send_header("Content-Length", str(len(_body)))
                        self.end_headers()
                        self.wfile.write(_body)
                    elif "spawn gate disabled" in body_str:
                        self._err(503, json.dumps({"error": "spawn gate disabled", "gate": "gates.allow_claude_spawn"}))
                    else:
                        self._err(503, json.dumps({"error": "spawn-cap reached", "source": "loop_run_project"}))
                    return
                except FileNotFoundError as exc:
                    self._err(503, str(exc))
                    return
                self._ok({
                    "run_id": run["run_id"],
                    "started_at": run["started_at"],
                    "log_path": run["log_path"],
                    "instruction": instruction,
                    "project_id": run["project_id"],
                }, remaining=remaining, extra_headers=dep)
                return

            if path.startswith("/api/loop/runs/") and path.endswith("/cancel"):
                run_id = path[len("/api/loop/runs/"):-len("/cancel")]
                ok = _cancel_loop_run(run_id)
                if not ok:
                    self._err(404, f"run {run_id!r} not found")
                    return
                self._ok({"ok": True, "run_id": run_id}, remaining=remaining, extra_headers=dep)
                return

            # ---- Innovate toggle POST endpoints ----
            if path == "/api/innovate/toggle":
                body = self._read_body() if self.headers.get("content-length") else {}
                if "enabled" not in body:
                    self._err(400, "'enabled' is required")
                    return
                try:
                    new_state = _set_innovate(bool(body["enabled"]))
                except Exception as exc:
                    self._err(500, str(exc))
                    return
                self._ok(new_state, remaining=remaining, extra_headers=dep)
                return

            if path == "/api/innovate/tick":
                if _reject_test_origin_spawn(self):
                    return
                try:
                    result = _innovate_tick()
                except PermissionError as exc:
                    body_str = str(exc)
                    if "rate-limited" in body_str:
                        # Parse retry_after from the guard's acquire result message
                        import re as _re_tick  # noqa: PLC0415
                        _m = _re_tick.search(r"wait (\d+)s", body_str)
                        _retry = int(_m.group(1)) if _m else 60
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Retry-After", str(_retry))
                        _b = json.dumps({"error": "rate-limited", "source": "innovate_tick_internal", "retry_after_seconds": _retry}).encode()
                        self.send_header("Content-Length", str(len(_b)))
                        self.end_headers()
                        self.wfile.write(_b)
                    elif "spawn gate disabled" in body_str:
                        self._err(503, json.dumps({"error": "spawn gate disabled", "gate": "gates.allow_claude_spawn"}))
                    else:
                        self._err(503, json.dumps({"error": "spawn-cap reached", "source": "innovate_tick_internal"}))
                    return
                except FileNotFoundError as exc:
                    self._err(503, str(exc))
                    return
                self._ok(result, remaining=remaining, extra_headers=dep)
                return

            if path == "/budget/init":
                body = self._read_body()
                ceiling = body.get("ceiling")  # optional int
                bt = BudgetTracker()
                bt.init_session(ceiling=ceiling)
                self._ok({"ok": True, "status": bt.get_status()}, remaining=remaining, extra_headers=dep)

            elif path == "/control/set":
                body = self._read_body()
                key = body.get("key")
                value = body.get("value")
                if key is None:
                    self._err(400, "'key' is required")
                    return
                if "value" not in body:
                    self._err(400, "'value' is required")
                    return
                cp = ControlPlane()
                cp.load()
                cp.set(key, value)
                self._ok({"ok": True, "key": key, "value": value}, remaining=remaining, extra_headers=dep)

            elif path == "/sessions/start":
                sm = SessionManager()
                session = sm.start_session()
                self._ok(session, remaining=remaining, extra_headers=dep)

            elif path == "/sessions/close":
                sm = SessionManager()
                closed = sm.close_session()
                if closed is None:
                    self._err(404, "no active session to close")
                else:
                    self._ok(closed, remaining=remaining, extra_headers=dep)

            elif path == "/backup":
                info = _backup.create_backup()
                _backup.prune_backups(keep=20)
                self._ok(info, remaining=remaining, extra_headers=dep)

            elif path == "/backup/restore":
                body = self._read_body()
                filename = body.get("filename")
                if not filename:
                    self._err(400, "'filename' is required")
                    return
                try:
                    result = _backup.restore_backup(filename)
                    self._ok(result, remaining=remaining, extra_headers=dep)
                except FileNotFoundError as exc:
                    self._err(404, str(exc))
                except ValueError as exc:
                    self._err(400, str(exc))

            elif path == "/notifications/test":
                from backend.notifier import get_notifier  # noqa: PLC0415
                results = get_notifier().send_test()
                self._ok({"results": results}, remaining=remaining, extra_headers=dep)

            elif path == "/spawn-queue/enqueue":
                body = self._read_body()
                role = body.get("role")
                if not role:
                    self._err(400, "'role' is required")
                    return
                prompt_context = body.get("prompt_context", "")
                discussion = body.get("discussion")
                priority = body.get("priority")
                requested_by = body.get("requested_by", "api")
                sq = get_spawn_queue()
                req_id = sq.enqueue(
                    role=role,
                    discussion=discussion,
                    prompt_context=prompt_context,
                    priority=priority,
                    requested_by=requested_by,
                )
                self._ok({"ok": True, "id": req_id}, remaining=remaining)

            elif path.startswith("/replays/") and path.endswith("/start"):
                # POST /replays/<agent_id>/start
                rest = path[len("/replays/"):]
                agent_id = rest[: -len("/start")]
                if not agent_id:
                    self._err(400, "agent_id required")
                    return
                body = self._read_body()
                speed = body.get("speed", "1x")
                try:
                    eng = start_replay(agent_id, speed=speed)
                    self._ok(
                        {
                            "replay_session_id": eng.replay_session_id,
                            "total_events": len(eng._events),
                        },
                        remaining=remaining,
                        extra_headers=dep,
                    )
                except FileNotFoundError as exc:
                    self._err(404, str(exc))
                except ValueError as exc:
                    self._err(400, str(exc))

            elif path == "/replays/pause":
                eng = get_active_replay()
                if eng is None or not eng.is_alive:
                    self._err(409, "no active replay to pause")
                else:
                    eng.pause()
                    self._ok({"ok": True}, remaining=remaining, extra_headers=dep)

            elif path == "/replays/resume":
                eng = get_active_replay()
                if eng is None or not eng.is_alive:
                    self._err(409, "no active replay to resume")
                else:
                    eng.resume()
                    self._ok({"ok": True}, remaining=remaining, extra_headers=dep)

            elif path == "/replays/stop":
                stopped = stop_active_replay()
                self._ok({"ok": True, "was_active": stopped}, remaining=remaining, extra_headers=dep)

            elif path == "/replays/seek":
                eng = get_active_replay()
                if eng is None or not eng.is_alive:
                    self._err(409, "no active replay to seek")
                    return
                body = self._read_body()
                event_number = body.get("event_number")
                if event_number is None:
                    self._err(400, "'event_number' is required")
                    return
                try:
                    eng.seek(int(event_number))
                    self._ok({"ok": True}, remaining=remaining, extra_headers=dep)
                except (TypeError, ValueError):
                    self._err(400, "'event_number' must be an integer")

            elif path == "/graphql":
                body = self._read_body()
                query_str = body.get("query")
                if not query_str:
                    self._err(400, "'query' is required")
                    return
                result = _graphql.execute(query_str)
                self._ok(result, remaining=remaining, extra_headers=dep)

            else:
                self._err(404, f"unknown endpoint: {path}")

        except Exception as exc:  # noqa: BLE001
            self._err(500, str(exc))
        finally:
            _trace_ctx_post.__exit__(None, None, None)
            _elapsed = (time.monotonic() - _t0) * 1000.0
            self._record_request("POST", urlparse(self.path).path, 0, 0, _elapsed)

    def do_PATCH(self) -> None:  # noqa: N802
        _t0 = time.monotonic()
        raw_path = urlparse(self.path).path.rstrip("/")
        try:
            if not self._check_auth():
                return

            if not self._check_rbac("PATCH", raw_path):
                return

            allowed, remaining = self._check_rate_limit()
            if not allowed:
                self._send_429()
                return

            # PATCH /api/projects/<id>/control — update ControlSettings fields.
            # Maps the dashboard's ControlSettings shape back to control_plane keys.
            # The project id is ignored (single-tenant); all writes go to
            # .autonomous-team/config.json via ControlPlane.set().
            if raw_path.startswith("/api/projects/") and raw_path.endswith("/control"):
                body = self._read_body()
                cp = ControlPlane()
                cp.load()
                # Map ControlSettings fields → control_plane dot-notation keys.
                _SETTINGS_MAP = {
                    "autoMerge":              ("gates.auto_merge",                     bool),
                    "requireSecurityReview":  ("gates.security_review",                bool),
                    "maxConcurrentAgents":    ("policies.executor.max_concurrent",      int),
                    "budgetAlertEnabled":     ("gates.budget_check",                   bool),
                    "qualityGateThreshold":   ("policies.code_reviewer.quality_threshold", float),
                    "loopIntervalMinutes":    ("loop_interval_minutes",                int),
                }
                # Validate maxConcurrentAgents before applying any writes.
                # Reject <= 0: a value of 0 would lock out ALL spawns including
                # the Team Lead bootstrap; negative values are nonsensical.
                # Use reject-outright (400) rather than a silent clamp so the
                # caller always gets deterministic feedback.
                if "maxConcurrentAgents" in body:
                    _mac = body["maxConcurrentAgents"]
                    try:
                        _mac_int = int(_mac)
                    except (TypeError, ValueError):
                        self._err(400, "maxConcurrentAgents must be an integer")
                        return
                    if _mac_int <= 0:
                        self._err(400, "maxConcurrentAgents must be > 0 (0 would lock all spawns; use stop-dashboard.sh instead)")
                        return
                updated: dict = {}
                for field, (cp_key, coerce) in _SETTINGS_MAP.items():
                    if field in body:
                        val = coerce(body[field])
                        cp.set(cp_key, val)
                        updated[field] = val
                # Return the full current settings so the UI can sync.
                cfg_text = cp._path.read_text()
                cfg = json.loads(cfg_text)
                gates = cfg.get("gates") or {}
                policies = cfg.get("policies") or {}
                self._ok({
                    "autoMerge": bool(gates.get("auto_merge", True)),
                    "requireSecurityReview": bool(gates.get("security_review", True)),
                    "maxConcurrentAgents": int(
                        (policies.get("executor") or {}).get("max_concurrent", 3)
                    ),
                    "loopIntervalMinutes": int(cfg.get("loop_interval_minutes", 10)),
                    "budgetAlertEnabled": bool(gates.get("budget_check", True)),
                    "qualityGateThreshold": float(
                        (policies.get("code_reviewer") or {}).get("quality_threshold", 0.8)
                    ),
                })
                return

            self._err(404, f"unknown PATCH route: {raw_path}")
        except Exception as exc:  # noqa: BLE001
            self._err(500, str(exc))
        finally:
            _elapsed = (time.monotonic() - _t0) * 1000.0
            self._record_request("PATCH", raw_path, 0, 0, _elapsed)

    def do_DELETE(self) -> None:  # noqa: N802
        _t0 = time.monotonic()
        raw_path = urlparse(self.path).path.rstrip("/")
        try:
            if not self._check_auth():
                return
            # Project deletion. The fulcrumaxe project itself is
            # protected — deleting the team's own project would orphan all
            # the .autonomous-team/ state and break health checks.
            if raw_path.startswith("/api/projects/") and "/" not in raw_path[len("/api/projects/"):]:
                pid = raw_path[len("/api/projects/"):]
                if pid == "fulcrumaxe":
                    self._err(403, "cannot delete the fulcrumaxe project")
                    return
                if not _delete_project(pid):
                    self._err(404, f"project {pid!r} not found")
                    return
                self._ok({"ok": True, "id": pid})
                return
            self._err(404, f"unknown DELETE route: {raw_path}")
        except Exception as exc:
            self._err(500, str(exc))
        finally:
            _elapsed = (time.monotonic() - _t0) * 1000.0
            self._record_request("DELETE", raw_path, 0, 0, _elapsed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="REST API gateway for fulcrumaxe backend modules"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=18099, help="Port (default: 18099)")
    parser.add_argument(
        "--enable-sse",
        dest="enable_sse",
        action="store_true",
        default=True,
        help="Enable SSE streaming endpoints and /ws WebSocket endpoint (default: enabled)",
    )
    parser.add_argument(
        "--no-enable-sse",  # kept as alias for backward compatibility
        dest="enable_sse",
        action="store_false",
        help="Disable SSE and WebSocket streaming endpoints (alias for --no-streaming)",
    )
    parser.add_argument(
        "--no-streaming",
        dest="enable_sse",
        action="store_false",
        help="Disable SSE streaming endpoints (/stream/*) and the /ws WebSocket endpoint",
    )
    parser.add_argument(
        "--dashboard",
        dest="dashboard",
        action="store_true",
        default=True,
        help="Enable GET /dashboard (default: enabled)",
    )
    parser.add_argument(
        "--no-dashboard",
        dest="dashboard",
        action="store_false",
        help="Disable GET /dashboard (returns 404)",
    )
    parser.add_argument(
        "--docs",
        dest="docs",
        action="store_true",
        default=True,
        help="Enable GET /openapi.json and GET /docs (default: enabled)",
    )
    parser.add_argument(
        "--no-docs",
        dest="docs",
        action="store_false",
        help="Disable GET /openapi.json and GET /docs (returns 404)",
    )
    parser.add_argument(
        "--rate-limit",
        dest="rate_limit",
        action="store_true",
        default=True,
        help="Enable per-IP token-bucket rate limiting (default: enabled)",
    )
    parser.add_argument(
        "--no-rate-limit",
        dest="rate_limit",
        action="store_false",
        help="Disable per-IP rate limiting entirely",
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        default=False,
        help="Exit with an error if AF_API_AUTH_KEY is not set (for production deployments)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        metavar="LEVEL",
        help="Log level (DEBUG/INFO/WARNING/ERROR). Defaults to AF_LOG_LEVEL env var or INFO.",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "text"],
        default="json",
        help="Log format: 'json' (default, one JSON object per line) or 'text' (human-readable).",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        help="Optional subcommand: 'spawn-stats' prints SpawnGuard state from the stats file.",
    )
    return parser.parse_args(argv)


def _cmd_spawn_stats() -> int:
    """Print SpawnGuard stats from the stats file written by the running server."""
    data = SpawnGuard.read_stats_file()
    if data is None:
        print("(no running server / no stats file)")
        return 0
    gate = data.get("gate_enabled", "unknown")
    print(f"gate: gates.allow_claude_spawn = {gate}")
    print(f"global_in_flight: {data.get('global_in_flight', 0)}")
    print()
    by_source = data.get("by_source", {})
    if not by_source:
        print("No spawns recorded since server start.")
        return 0
    col_w = max((len(s) for s in by_source), default=6)
    header = f"{'source':<{col_w}}  {'fires_total':>11}  {'in_flight':>9}  last_fire_ts"
    print(header)
    print("-" * len(header))
    for src in sorted(by_source):
        s = by_source[src]
        last = s.get("last_fire_ts") or "—"
        print(f"{src:<{col_w}}  {s.get('fires_total', 0):>11}  {s.get('in_flight', 0):>9}  {last}")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Handle spawn-stats subcommand before server setup (no logging needed)
    if args.command == "spawn-stats":
        sys.exit(_cmd_spawn_stats())
    elif args.command is not None:
        print(f"Unknown command: {args.command!r}. Known subcommands: spawn-stats", file=sys.stderr)
        sys.exit(1)

    from backend.log import setup_logging
    setup_logging(
        level=args.log_level,
        json_format=(args.log_format == "json"),
    )

    import logging as _logging
    _api_logger = _logging.getLogger(__name__)

    # Auth setup -- read key from environment.
    auth_key = os.environ.get("AF_API_AUTH_KEY") or None
    if args.require_auth and not auth_key:
        _api_logger.error(
            "--require-auth is set but AF_API_AUTH_KEY is not defined in the environment."
        )
        sys.exit(1)
    _Handler.auth_key = auth_key
    _Handler.enable_sse = args.enable_sse
    _Handler.enable_dashboard = args.dashboard
    _Handler.enable_docs = args.docs
    _Handler.enable_rate_limit = args.rate_limit

    # Hard-fail if gates.allow_claude_spawn is missing from config (exit code 78 = EX_CONFIG).
    try:
        _spawn_guard.assert_gate_present()
    except RuntimeError as _sg_exc:
        print(str(_sg_exc), file=sys.stderr)
        print(
            "Remediation: set gates.allow_claude_spawn to true or false in "
            ".autonomous-team/config.json before starting backend/api.py",
            file=sys.stderr,
        )
        sys.exit(78)

    server = ThreadingHTTPServer((args.host, args.port), _Handler)

    # Validate config files on startup — log warnings but do not block.
    try:
        _sv = SchemaValidator()
        _validation_results = _sv.validate_all()
        _any_errors = False
        for _fname, _errs in _validation_results.items():
            if _errs:
                _any_errors = True
                for _err in _errs:
                    _api_logger.warning("config validation: %s: %s", _fname, _err)
        if not _any_errors:
            _api_logger.info("config validation: all files valid")
    except Exception as _exc:  # noqa: BLE001
        _api_logger.warning("config validation failed to run: %s", _exc)

    sse_status = "enabled" if args.enable_sse else "disabled"
    dash_status = "enabled" if args.dashboard else "disabled"
    docs_status = "enabled" if args.docs else "disabled"
    auth_status = "enabled" if auth_key else "disabled"
    rl_status = "enabled" if args.rate_limit else "disabled"
    _api_logger.info(
        "API gateway listening on %s:%s (auth %s, SSE %s, dashboard %s, docs %s, rate-limit %s)",
        args.host, args.port, auth_status, sse_status, dash_status, docs_status, rl_status,
    )
    # Register FileAppender so agent output events are persisted to disk.
    _feed_path = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"
    _appender = FileAppender(_feed_path)
    get_bus().subscribe(AgentOutputEvent, _appender.handle)

    # Register BusEventFileAppender — ADDITIVE subscriber for all 4 event types.
    # Writes events-bus.jsonl with _event_type field so the TS /events handler
    # can tail it and emit full parity without being in-process.
    _bus_events_path = _REPO_ROOT / ".autonomous-team" / "events-bus.jsonl"
    _bus_appender = BusEventFileAppender(_bus_events_path)
    for _et in (AgentOutputEvent, BudgetSpendEvent, GateChangeEvent, LoopIterationEvent):
        get_bus().subscribe(_et, _bus_appender.handle)

    # Start config watcher — logs gate changes and other diffs at runtime.
    _config_path = _REPO_ROOT / ".autonomous-team" / "config.json"
    _config_watcher = ConfigWatcher(_config_path)

    def _on_config_change(old: dict, new: dict) -> None:
        from backend.event_bus import GateChangeEvent  # noqa: PLC0415
        old_gates = old.get("gates", {})
        new_gates = new.get("gates", {})
        all_gate_keys = set(old_gates) | set(new_gates)
        for gate_key in sorted(all_gate_keys):
            old_val = old_gates.get(gate_key)
            new_val = new_gates.get(gate_key)
            if old_val != new_val:
                _api_logger.info("gate change: %s: %s -> %s", gate_key, old_val, new_val)
                get_bus().publish(GateChangeEvent(
                    source="config_watcher",
                    gate_name=gate_key,
                    old_value=bool(old_val),
                    new_value=bool(new_val),
                ))

    _config_watcher.register_callback(_on_config_change)
    _config_watcher.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _api_logger.info("Shutting down.")
    finally:
        _config_watcher.stop()
        server.server_close()


if __name__ == "__main__":
    main()
