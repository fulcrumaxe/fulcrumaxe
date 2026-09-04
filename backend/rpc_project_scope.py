"""backend/rpc_project_scope.py — RPC method classification and per-request
project scoping, shared by both dispatch sites (backend/server.py's legacy
ThreadingHTTPServer and backend/routers/rpc.py's ASGI POST /rpc route).

Background (D#2261, PR-a)
--------------------------
28 of 58 registered RPC methods never referenced a ``project`` param at all,
so a dashboard serving an adopter project's state dir could return the
*serving process's own* (usually the engine's) data under the adopter's
name. This module is the fix's single source of truth:

1. Every method registered in ``backend.server._RPC_METHODS`` is classified
   exactly once as ``SCOPED``, ``GLOBAL``, or ``UNSCOPABLE`` (see below).
2. :func:`dispatch_scoped` is the one place both dispatchers call instead of
   ``handler(params)`` directly, so a fix landing on only one dispatch site
   can't happen again.
3. An ``UNSCOPABLE`` method called with a ``project`` param refuses loudly
   (raises, surfaces as a JSON-RPC error) instead of silently returning
   empty or — worse — the wrong project's data.

Background (D#2261, PR-b)
--------------------------
Four of the methods PR-a classified ``UNSCOPABLE`` (``agents.tail``,
``loop.events``, ``dashboard.pr_list``, ``dashboard.pr_detail``) read a
path or GitHub repo slug bound to the serving checkout at Python import
time — no per-request env override could reach them. PR-b de-anchored each
one at its source in ``backend/server.py`` (a call-time,
project-derived lookup for the agent-feed path; ``_resolve_repo_for_project()``
for the repo slug) and reclassifies them ``SCOPED`` below. The remaining
``UNSCOPABLE`` entries are blocked by a *different* mechanism each — a
shared cache keyed without project, an in-process module cache, a
subprocess that never consults an env var, or genuinely having no
adopter-project equivalent to serve — and de-anchoring a file path would
not fix any of them; see each entry's reason string.

Classification definitions
---------------------------
SCOPED
    The handler already serves correct per-project data for a given
    ``project`` param, or can be made to by having this module override
    ``STATS_DB_PATH`` / ``AUTONOMOUS_TEAM_STATE_DIR`` for the duration of the
    call. Includes handlers that already resolve project-specific paths
    themselves (e.g. ``kpi.history``, ``discussions.list``, and — as of
    PR-b — ``agents.tail``, ``loop.events``, ``dashboard.pr_list``,
    ``dashboard.pr_detail``) — the dispatcher's env override is then a
    harmless no-op layered on top of the handler's own resolution.

GLOBAL
    Data is genuinely not about "one project" in the first place: engine
    control-plane config (``dial.list``), fleet-wide aggregation across all
    discovered projects (``fleet.*``), or live process-local state that has
    no per-project equivalent to begin with (the active-loop registry, the
    single per-host A2A broker). A ``project`` param is accepted but ignored
    — there's nothing to leak, because there's nothing project-specific in
    the response either way.

UNSCOPABLE
    The handler reads through a path bound to the *serving checkout* at
    Python import time (a module-level constant built from ``__file__``, or
    an object built once and cached in ``sys.modules``), shells out to the
    serving process's own GitHub repo, or is blocked by some other
    mechanism (e.g. a shared cache keyed without project) that a per-request
    env override cannot reach. Each entry's reason string names its actual
    blocker — see the module comment above each ``UNSCOPABLE`` block. Until
    fixed: refuse rather than serve the wrong project's data.

A note on env-var scoping and thread safety
--------------------------------------------
The existing ``_with_project_stats_db()`` helper in backend/server.py scopes
30 already-project-aware handlers by temporarily mutating
``os.environ["STATS_DB_PATH"]`` around a single call. That mutation is
process-global and not thread-safe on its own — two concurrent requests for
different projects can interleave inside that narrow window. Moving the same
narrow-window technique up to the dispatcher (to cover the newly-classified
SCOPED handlers here) would widen the window from one reader call to the
whole request, making the existing race *more* likely, not less.

Instead of widening that race, :func:`dispatch_scoped` serializes every
SCOPED call that actually needs an env override behind a single
``threading.Lock`` held for the *entire* handler invocation (not just the
env swap). Two concurrent cross-project SCOPED calls therefore never
interleave — one fully completes (env set, handler runs, env restored)
before the other's env is set. Calls with no ``project`` param (the engine's
own dashboard, which is nearly all traffic) never touch the lock at all, so
this doesn't add contention where scoping isn't happening.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from backend import state_paths

SCOPED = "SCOPED"
GLOBAL = "GLOBAL"
UNSCOPABLE = "UNSCOPABLE"

# Sanity floor for the recurrence-guard test: if a runtime enumeration of
# backend.server._RPC_METHODS ever comes back with fewer than this many
# entries, something is broken in the enumeration itself (e.g. an import
# failure that silently produced an empty dict) — don't let that pass as
# "everything classified".
MIN_REGISTRY_SIZE = 58


class UnscopableMethodError(Exception):
    """Raised when a caller asks an UNSCOPABLE method to serve a specific,
    non-default project. Carries ``rpc_code`` so both dispatch sites' generic
    ``except Exception`` handlers surface it as a JSON-RPC error (non-null
    ``error``, no ``result`` key) rather than a 500 or a silent empty body.
    """

    rpc_code = -32001


class UnresolvableProjectError(Exception):
    """Raised when a caller names a *specific* project that cannot be
    resolved to a repo — a state dir with no repo field configured, or a
    project name with no state dir at all.

    Unlike UnscopableMethodError, the method here *is* scopable — it's the
    project name that failed to resolve. Distinguishing the two gives the
    operator different, more useful advice. Carries the same ``rpc_code``
    as UnscopableMethodError so both dispatch sites surface it as a
    JSON-RPC error (non-null ``error``, no ``result`` key) rather than a
    500 or a silent empty body.
    """

    rpc_code = -32001


class UnclassifiedMethodError(Exception):
    """Raised when a method reaches dispatch_scoped() without a classification.

    This should be unreachable in production: test_rpc_project_scope.py's
    recurrence guard asserts every _RPC_METHODS entry is classified before
    this code ever runs. This is defense in depth, not the primary guard —
    fail closed rather than silently serving unscoped data for a handler
    nobody classified.
    """

    rpc_code = -32001


# ---------------------------------------------------------------------------
# Classification registry — every key in backend.server._RPC_METHODS must
# appear here exactly once. See test_rpc_project_scope.py for the runtime
# completeness check.
# ---------------------------------------------------------------------------

_CLASSIFICATIONS: dict[str, tuple[str, str]] = {
    # -- Live process-local state: no per-project equivalent exists --------
    "loop.start": (GLOBAL, "mutates this server's own active-loop registry "
                            "(backend/active_loops.py, file bound to this "
                            "checkout's REPO_ROOT); there is no per-project "
                            "loop registry to scope to"),
    "loop.stop": (GLOBAL, "same active-loop registry as loop.start"),
    "loop.list": (GLOBAL, "same active-loop registry as loop.start"),
    "dashboard.gates_snapshot": (GLOBAL, "control-plane gates are engine-level "
                                          "config, not per-project data"),
    "a2a.list_active": (GLOBAL, "queries the single live A2A broker over HTTP "
                                 "on a fixed port (backend/rpc/a2a_active.py); "
                                 "it is live in-memory broker state, not "
                                 "project-persisted, and reads no state dir at all"),
    "fleet.projects": (GLOBAL, "fleet-wide aggregator across all discovered "
                                "projects by design (backend/fleet/discovery.py); "
                                "not scoped to a single project"),
    "fleet.cost": (GLOBAL, "fleet-wide cost aggregator across all discovered "
                            "projects by design"),
    "fleet.discovery_ack": (GLOBAL, "writes to the shared fleet known-projects "
                                     "list (~/.autonomous-fleet-state/known.json); "
                                     "project_name is the operand of the write, "
                                     "not a scope filter on the response"),
    "fleet.discovery_known": (GLOBAL, "read-only counterpart to "
                                       "fleet.discovery_ack; reports the same "
                                       "shared known-projects list, fleet-wide "
                                       "by design, not scoped to a single "
                                       "project"),
    "fleet.concurrency": (GLOBAL, "fleet-wide aggregator across all discovered "
                                   "projects by design"),
    "dial.list": (GLOBAL, "control-plane dial config is engine-level by design"),
    "dial.set": (GLOBAL, "mutates the same engine-level dial config as dial.list"),

    # -- Bound at import to the serving checkout: cannot be scoped yet -----
    "circuitBreaker.history": (UNSCOPABLE, "backend.circuit_breaker is imported "
                                            "in-process and cached in "
                                            "sys.modules; its _HISTORY_FILE "
                                            "constant is bound to this "
                                            "checkout's __file__ at that first "
                                            "import and cannot be redirected "
                                            "per-request afterward"),
    "circuit_breaker.summary": (UNSCOPABLE, "the subprocess it spawns is "
                                             "genuinely env-addressable, but "
                                             "_rpc_circuit_breaker_summary's own "
                                             "30s TTL cache in server.py keys "
                                             "on the method name alone "
                                             "(cache_key = "
                                             "(\"circuit_breaker.summary\",)), "
                                             "not on project — a cache hit "
                                             "within the window returns "
                                             "whichever project last populated "
                                             "it, bypassing the subprocess (and "
                                             "the env override) entirely. "
                                             "Refuse until the cache key "
                                             "includes project"),
    "claude_spawn_tracker.summary": (UNSCOPABLE, "the subprocessed script "
                                                  "(backend/claude_spawn_tracker.py) "
                                                  "resolves its config and repo "
                                                  "root from its own __file__, "
                                                  "never consults an env var; "
                                                  "would keep serving this "
                                                  "engine's own spawn counters "
                                                  "under an adopter's name"),
    "stats.parity_trend": (UNSCOPABLE, "falls back to a path bound to "
                                        "state_paths.py's own __file__ when "
                                        "PARITY_HISTORY_PATH is unset, and "
                                        "for_project() has no parity-history "
                                        "field — adopter projects don't run "
                                        "parity experiments, so there is no "
                                        "project-scoped equivalent to serve"),
    "stats.analyst_findings": (UNSCOPABLE, "backend.stats.analyst_findings.load() "
                                            "defaults to RUN_REPORTS_DIR, a "
                                            "module constant bound to this "
                                            "checkout's repo root at import; no "
                                            "adopter equivalent exists"),

    # -- Class (c), de-anchored (D#2261 PR-b) -------------------------------
    # These four were UNSCOPABLE after PR-a: each read a path or repo slug
    # bound to this checkout at import, which no per-request env override
    # could reach. PR-b replaced those bindings with call-time,
    # project-derived lookups (backend.server._agent_feed_path() for the
    # first two, _resolve_repo_for_project() for the gh-shelling pair), so
    # the dispatcher's per-request scoping now reaches them like any other
    # SCOPED handler.
    "loop.events": (SCOPED, "agent-feed.jsonl path is now resolved per "
                             "request via _agent_feed_path(project) instead "
                             "of the import-time AGENT_FEED_PATH constant"),
    "agents.tail": (SCOPED, "same _agent_feed_path(project) resolution as "
                             "loop.events; this is the handler from the "
                             "reported bug (a 2026-07-24 engine event served "
                             "under an adopter project's name) — verified "
                             "fixed against a real gatekeep state dir "
                             "(acceptance item 8)"),
    "dashboard.pr_detail": (SCOPED, "repo slug is now resolved per request "
                                     "via _resolve_repo_for_project(project) "
                                     "instead of the module-level _GH_REPO "
                                     "constant; Blackboard/CostTracker/"
                                     "QualityScorer reads inside the handler "
                                     "were already env-addressable and need "
                                     "no change"),
    "dashboard.pr_list": (SCOPED, "same _resolve_repo_for_project(project) "
                                   "resolution as dashboard.pr_detail; its "
                                   "30s TTL cache is now keyed on "
                                   "(method, repo_owner, repo_name) rather "
                                   "than the method name alone, so it can't "
                                   "repeat the circuit_breaker.summary "
                                   "cross-project cache bug caught in PR-a "
                                   "review"),

    # -- Already self-scoped (pre-existing, correct project resolution) ----
    "kpi.history": (SCOPED, "resolves the adopter's own repo checkout via "
                             "for_project() and returns [] when it doesn't "
                             "exist, rather than falling back to this repo"),
    "kpi.cycle_time": (SCOPED, "same for_project()-based resolution as "
                                "kpi.history; returns zeroed buckets when the "
                                "adopter checkout is absent"),
    "loop.timeline": (SCOPED, "resolves the project's own loop-metrics.jsonl "
                               "via for_project(); returns [] when absent"),
    "loop.iteration_detail": (SCOPED, "same project-aware metrics/log "
                                       "resolution as loop.timeline"),
    "team_status.snapshot": (SCOPED, "threads project= into _gather()/"
                                      "_budget_summary(), which resolves the "
                                      "adopter's own blackboard via "
                                      "for_project(); the loop/discussion "
                                      "counts describe the serving process "
                                      "itself and carry no other project's data"),
    "discussions.list": (SCOPED, "resolves the adopter's own repo slug via "
                                  "_resolve_repo_for_project() before querying "
                                  "GitHub"),
    "discussions.get": (SCOPED, "same _resolve_repo_for_project() resolution "
                                 "as discussions.list"),
    "stats.cosmetic_blocks": (SCOPED, "already threads an explicit project= "
                                       "kwarg into backend.stats.cosmetic_blocks"),
    "cost.per_discussion": (SCOPED, "CostTracker() creates a fresh Blackboard() "
                                     "per call, which resolves "
                                     "state_paths.BLACKBOARD_DIR at call time "
                                     "(fixed by D#1908 PR 3 — no longer frozen "
                                     "at blackboard.py's own import as "
                                     "state_paths.py's docstring still claims); "
                                     "reachable via AUTONOMOUS_TEAM_STATE_DIR"),
    "cost.by_discussion": (SCOPED, "same CostTracker/Blackboard mechanism as "
                                    "cost.per_discussion; agent_spend's DuckDB "
                                    "path also resolves via agent_run_reader, "
                                    "reachable via STATS_DB_PATH"),

    # -- Class (a)/(b): env-addressable, now scoped at the dispatcher -------
    "stats.sdk_vs_cc": (SCOPED, "_db_path() reads state_paths.STATS_DB at call "
                                 "time; reachable via STATS_DB_PATH"),
    "stats.dial_usage": (SCOPED, "falls back to state_paths.STATE_DIR when no "
                                  "explicit state_dir is given; reachable via "
                                  "AUTONOMOUS_TEAM_STATE_DIR"),
    "stats.dial_rejections": (SCOPED, "same STATE_DIR fallback mechanism as "
                                       "stats.dial_usage"),
    "stats.sdk_lane": (SCOPED, "sdk_status() reads AUTONOMOUS_TEAM_STATE_DIR at "
                                "call time for its routing-counts DuckDB read"),
    "a2a.tail": (SCOPED, "reads state_paths.STATE_DIR via a function-local "
                          "import, resolved fresh on every call; reachable via "
                          "AUTONOMOUS_TEAM_STATE_DIR"),
    "auth_retry.record": (SCOPED, "get_blackboard() resolves "
                                   "state_paths.BLACKBOARD_DIR at call time "
                                   "(fixed by D#1908 PR 3); reachable via "
                                   "AUTONOMOUS_TEAM_STATE_DIR"),
    "auth_retry.summary": (SCOPED, "same get_blackboard() mechanism as "
                                    "auth_retry.record"),
    "stats.verdict_overturns": (SCOPED, "overturn_rate_by_role_24h() resolves "
                                         "its DuckDB path via "
                                         "backend.stats_writer._db_path() at "
                                         "call time; reachable via "
                                         "STATS_DB_PATH"),

    # -- Already wrapped in _with_project_stats_db() (unchanged by this PR) -
    "stats.summary": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.series": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.team_lead_tokens": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.cost_spike_history": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.role_success_rate": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.role_retry_rate": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.loop_idle_ratio": (SCOPED, "loop_idle_ratio_24h() opens no DuckDB "
                                       "connection at all -- it reads a "
                                       "repo-relative JSONL path "
                                       "(.autonomous-team/loop-metrics.jsonl "
                                       "next to this checkout), so it is NOT "
                                       "wrapped in _with_project_stats_db(). "
                                       "That path resolution is bound to the "
                                       "serving checkout, not the requested "
                                       "project -- a D#2309-class bug, filed "
                                       "separately; this entry only corrects "
                                       "the prior false claim (D#2315)."),
    "stats.avg_fix_rounds_per_pr": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.freshness_list": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.weekly_velocity": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.dora": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.pre_write_burn": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats_duckdb_writers": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "runs.by_role": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "runs.percentiles": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "runs.stuck": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "runs.roundtrip": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "runs.active_over_time": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "runs.recent": (SCOPED, "already wrapped in _with_project_stats_db()"),
    "stats.cost_per_outcome": (SCOPED, "already wrapped in _with_project_stats_db()"),
}


def classification_for(method: str) -> "tuple[str, str] | None":
    """Return (kind, reason) for *method*, or None if unclassified."""
    return _CLASSIFICATIONS.get(method)


def all_classifications() -> dict[str, tuple[str, str]]:
    """Return a shallow copy of the full classification registry."""
    return dict(_CLASSIFICATIONS)


# ---------------------------------------------------------------------------
# Per-request scoping
# ---------------------------------------------------------------------------

# Held for the *entire* duration of a SCOPED handler call that has a project
# to scope to — not just the env-var swap. See module docstring: this is
# what actually eliminates the concurrency race rather than just narrowing it.
_SCOPE_LOCK = threading.Lock()


class _EnvScope:
    """Context manager: override STATS_DB_PATH and AUTONOMOUS_TEAM_STATE_DIR
    to *project*'s paths for the duration of the with-block, serialized
    against every other concurrent scoped call via _SCOPE_LOCK.
    """

    def __init__(self, project: str):
        self._project = project
        self._old_stats_db: "str | None" = None
        self._old_state_dir: "str | None" = None

    def __enter__(self) -> "_EnvScope":
        _SCOPE_LOCK.acquire()
        paths = state_paths.for_project(self._project)
        self._old_stats_db = os.environ.get("STATS_DB_PATH")
        self._old_state_dir = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
        os.environ["STATS_DB_PATH"] = str(paths.stats_db)
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = str(paths.state_dir)
        return self

    def __exit__(self, *exc_info: object) -> None:
        try:
            if self._old_stats_db is None:
                os.environ.pop("STATS_DB_PATH", None)
            else:
                os.environ["STATS_DB_PATH"] = self._old_stats_db
            if self._old_state_dir is None:
                os.environ.pop("AUTONOMOUS_TEAM_STATE_DIR", None)
            else:
                os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = self._old_state_dir
        finally:
            _SCOPE_LOCK.release()


def dispatch_scoped(method: str, params: dict, handler: Callable[[dict], Any]) -> Any:
    """Call ``handler(params)`` honoring *method*'s project-scoping classification.

    Both backend/server.py (legacy ThreadingHTTPServer, do_POST) and
    backend/routers/rpc.py (ASGI POST /rpc) call this instead of invoking
    ``handler(params)`` directly, so a fix can't land on only one dispatch
    site — see module docstring.
    """
    classification = _CLASSIFICATIONS.get(method)
    if classification is None:
        raise UnclassifiedMethodError(
            f"{method!r} has no entry in rpc_project_scope's classification "
            "registry — refusing rather than serving unscoped data"
        )
    kind, _reason = classification

    project = (params or {}).get("project") or None

    if not project:
        # No project param (the engine's own dashboard, or a GLOBAL-only
        # caller) — existing behavior, untouched, no lock contention.
        return handler(params)

    if kind == UNSCOPABLE:
        raise UnscopableMethodError(
            f"{method!r} cannot be scoped to project {project!r} yet — "
            "de-anchoring it from this checkout is tracked separately "
            "(D#2261 PR-b); refusing rather than serving this process's own "
            "data under the requested project's name"
        )

    if kind == SCOPED:
        with _EnvScope(project):
            return handler(params)

    # GLOBAL: a project param was supplied but there's nothing project-scoped
    # in this response to leak — call through unchanged.
    return handler(params)
