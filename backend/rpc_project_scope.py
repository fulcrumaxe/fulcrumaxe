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

Background (D#2327, PR-a)
--------------------------
Nineteen entries below shared one justification string — "already wrapped in
``_with_project_stats_db()``" — that nobody had checked. Being wrapped was
never the right question. That wrapper scopes by redirecting
``STATS_DB_PATH``; whether that reaches a handler depends on what the
handler *reads*. Four of the nineteen read something else:

* ``stats.loop_idle_ratio`` reads ``.autonomous-team/loop-metrics.jsonl``
  from the serving checkout. Wrapped, and scoped nothing — the worst shape,
  because the wrapper's presence was what the registry cited as evidence.
* ``stats.weekly_velocity`` and ``stats.cost_per_outcome`` shell out to
  ``gh``; both fell back to the *serving* checkout's repo slug.
* ``stats.dora`` reads module constants built from ``__file__`` at import
  and shells ``gh`` at a module-level slug. Nothing per-request reaches it.

The first three are now scoped for real, by the mechanism that reaches their
actual data, and decline (``UnresolvableProjectError``) rather than fall back
when a named project cannot be resolved. ``stats.dora`` is reclassified
``UNSCOPABLE``.

Worse than the unaudited prose, briefly: PR #2330 corrected
``stats.loop_idle_ratio``'s reason to say plainly that it was *not* wrapped
and was bound to the serving checkout — while leaving the classification
``SCOPED``. The field a human reads and the field a machine reads disagreed,
and the machine-read one was the false one. :data:`_DATA_SOURCES` and
``scripts/ci/rpc-scope-registry-guard.py` exist so that cannot recur
silently: the guard fails the build on a ``SCOPED`` entry whose own reason
denies being scoped, and on a ``SCOPED`` entry whose audited data source is
the serving checkout.

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

    # -- Audited: wrapper reaches the data (D#2327 PR-a) --------------------
    # These entries once shared one unverified string ("already wrapped in
    # _with_project_stats_db()"), 19 of them. Being wrapped is not the
    # question -- the question is whether the wrapper's STATS_DB_PATH
    # redirect reaches the data the handler actually reads. Each reason
    # below now cites the concrete read site that was checked; the
    # machine-readable form of the same answer is in _DATA_SOURCES.
    "stats.summary": (SCOPED, "already wrapped in _with_project_stats_db(), "
                               "and the wrapper reaches its data: "
                               "stats_reader.summary() -> _open_conn() -> "
                               "stats_connection.get_read_connection() -> "
                               "stats_writer._db_path() -> state_paths.STATS_DB "
                               "(backend/stats_reader.py:28-31, 241)"),
    "stats.series": (SCOPED, "already wrapped in _with_project_stats_db(), and "
                              "the wrapper reaches its data: same "
                              "_open_conn()/STATS_DB path as stats.summary "
                              "(backend/stats_reader.py:275). D#2327's body "
                              "marked this one 'appears not to be wrapped' "
                              "from a bounded window on a long function; read "
                              "whole, backend/server.py:2196 delegates through "
                              "the wrapper like the rest"),
    "stats.team_lead_tokens": (SCOPED, "already wrapped in "
                                        "_with_project_stats_db(), and the "
                                        "wrapper reaches its data: "
                                        "stats_writer.team_lead_tokens_percentiles() "
                                        "-> _db_path() -> state_paths.STATS_DB "
                                        "(backend/stats_writer.py:248)"),
    "stats.cost_spike_history": (SCOPED, "already wrapped in "
                                          "_with_project_stats_db(), and the "
                                          "wrapper reaches its data: "
                                          "stats_writer.cost_spike_history() -> "
                                          "_db_path() (backend/stats_writer.py:629)"),
    "stats.role_success_rate": (SCOPED, "already wrapped in "
                                         "_with_project_stats_db(), and the "
                                         "wrapper reaches its data: "
                                         "stats_writer.role_success_rate_24h() "
                                         "-> _db_path() "
                                         "(backend/stats_writer.py:461)"),
    "stats.role_retry_rate": (SCOPED, "already wrapped in "
                                       "_with_project_stats_db(), and the "
                                       "wrapper reaches its data: "
                                       "stats_writer.role_retry_rate_24h() -> "
                                       "_db_path() (backend/stats_writer.py:405)"),
    "stats.loop_idle_ratio": (SCOPED, "reads "
                                       ".autonomous-team/loop-metrics.jsonl, "
                                       "not DuckDB "
                                       "(backend/stats_writer.py:534-539), so "
                                       "the STATS_DB_PATH redirect is beside "
                                       "the point here. The handler resolves "
                                       "the requested project's own metrics "
                                       "file itself via "
                                       "backend.loop_metrics_path and passes it "
                                       "to loop_idle_ratio_24h() explicitly "
                                       "(backend/rpc/stats_loop_idle_ratio.py), "
                                       "declining with "
                                       "UnresolvableProjectError when the "
                                       "project has no reachable metrics file. "
                                       "SCOPED because the handler scopes "
                                       "itself -- see the D#2327 PR-a section "
                                       "of the module docstring for what this "
                                       "used to do"),
    "stats.avg_fix_rounds_per_pr": (SCOPED, "already wrapped in "
                                             "_with_project_stats_db(), and the "
                                             "wrapper reaches its data: "
                                             "stats_writer.avg_fix_rounds_24h() "
                                             "-> _db_path() "
                                             "(backend/stats_writer.py:689)"),
    "stats.freshness_list": (SCOPED, "already wrapped in "
                                      "_with_project_stats_db(), and the "
                                      "wrapper reaches its data: "
                                      "stats_freshness_watchdog.check() -> "
                                      "_query_freshness() -> _db_path() -> "
                                      "state_paths.STATS_DB "
                                      "(backend/stats_freshness_watchdog.py:66-69, "
                                      "80-85). Its module-level REPO import is "
                                      "used only by warn_stale(), not by "
                                      "check()"),
    "stats.weekly_velocity": (SCOPED, "opens no DuckDB connection; it shells "
                                       "`gh pr list --repo <slug>` "
                                       "(backend/stats/weekly_velocity.py:55-60), "
                                       "so the STATS_DB_PATH redirect is beside "
                                       "the point here. The handler resolves "
                                       "the requested project's slug itself "
                                       "(backend/rpc/stats_weekly_velocity.py "
                                       "_resolve_repo -> state_paths.for_project) "
                                       "and, since D#2327 PR-a, declines with "
                                       "UnresolvableProjectError when a named "
                                       "project resolves to no slug rather than "
                                       "letting weekly_velocity()'s `repo or "
                                       "_REPO` default take over "
                                       "(weekly_velocity.py:144)"),
    "stats.pre_write_burn": (SCOPED, "already wrapped in "
                                      "_with_project_stats_db(), and the "
                                      "wrapper reaches its data: its own "
                                      "_db_path() -> state_paths.STATS_DB, then "
                                      "duckdb.connect on that path "
                                      "(backend/rpc/stats_pre_write_burn.py:15-18, 31)"),
    "stats_duckdb_writers": (SCOPED, "already wrapped in "
                                      "_with_project_stats_db(), and the "
                                      "wrapper reaches its data: "
                                      "backend/stats/duckdb_writers.py:36-39 "
                                      "resolves state_paths.STATS_DB at call "
                                      "time and :132-139 runs lsof against that "
                                      "redirected path. What it reports is live "
                                      "OS state about the project's own DB file "
                                      "(see also D#2326)"),
    "runs.by_role": (SCOPED, "already wrapped in _with_project_stats_db(), and "
                              "the wrapper reaches its data: "
                              "agent_run_reader.by_role() -> _connect() -> "
                              "_db_path() -> state_paths.STATS_DB "
                              "(backend/agent_run_reader.py:37-40, 94). The "
                              "agent_run table lives in the DuckDB the redirect "
                              "moves, not the state-dir SQLite store -- which "
                              "settles all six runs.* rows at once"),
    "runs.percentiles": (SCOPED, "already wrapped in _with_project_stats_db(), "
                                  "and the wrapper reaches its data: "
                                  "agent_run_reader.duration_percentiles() -> "
                                  "_connect() (backend/agent_run_reader.py:162)"),
    "runs.stuck": (SCOPED, "already wrapped in _with_project_stats_db(), and "
                            "the wrapper reaches its data: "
                            "agent_run_reader.stuck_runs() -> _connect() "
                            "(backend/agent_run_reader.py:239)"),
    "runs.roundtrip": (SCOPED, "already wrapped in _with_project_stats_db(), "
                                "and the wrapper reaches its data: "
                                "agent_run_reader.roundtrip_latency() -> "
                                "_connect() (backend/agent_run_reader.py:282)"),
    "runs.active_over_time": (SCOPED, "already wrapped in "
                                       "_with_project_stats_db(), and the "
                                       "wrapper reaches its data: "
                                       "agent_run_reader.concurrent_active() -> "
                                       "_connect() "
                                       "(backend/agent_run_reader.py:501)"),
    "runs.recent": (SCOPED, "already wrapped in _with_project_stats_db(), and "
                             "the wrapper reaches its data: "
                             "agent_run_reader._recent() -> _connect() "
                             "(backend/agent_run_reader.py:344)"),
    "stats.cost_per_outcome": (SCOPED, "joins two sources: per-PR spend, "
                                        "which _with_project_stats_db() does "
                                        "reach via STATS_DB, and a merged-PR "
                                        "list from `gh pr list --repo <slug>` "
                                        "(backend/cost_per_outcome.py:42), "
                                        "which it does not. Since D#2327 PR-a "
                                        "the handler resolves that slug per "
                                        "request from state_paths.for_project "
                                        "and threads it through "
                                        "cost_per_outcome_rows(repo=...), "
                                        "declining with "
                                        "UnresolvableProjectError when a named "
                                        "project resolves to no slug -- PR "
                                        "numbers collide across repos, so "
                                        "mismatching the two halves is not a "
                                        "harmless empty result"),

    # -- Audited: bound to the serving checkout at import (D#2327 PR-a) -----
    "stats.dora": (UNSCOPABLE, "was classified SCOPED on the shared 'already "
                                "wrapped' string; it is wrapped, and the "
                                "wrapper reaches nothing it reads. "
                                "analytics_engineer.compute_snapshot() reads "
                                "_RELEASES_DIR (analytics_engineer.py:49) and "
                                "kpi_engine.REGISTRY (kpi_engine.py:27), both "
                                "module constants built from "
                                "Path(__file__).resolve().parent.parent at "
                                "import and cached in sys.modules, and "
                                "_compute_cfr() shells `gh api graphql` at the "
                                "module-level REPO "
                                "(analytics_engineer.py:97-102). PR #2312's "
                                "code-reviewer was right that this handler "
                                "ignores the project param entirely. Refuse "
                                "rather than answer a cross-project request "
                                "with the serving checkout's DORA numbers; "
                                "de-anchoring analytics_engineer, "
                                "release_manager and kpi_engine is a separate "
                                "job"),
}


def classification_for(method: str) -> "tuple[str, str] | None":
    """Return (kind, reason) for *method*, or None if unclassified."""
    return _CLASSIFICATIONS.get(method)


def all_classifications() -> dict[str, tuple[str, str]]:
    """Return a shallow copy of the full classification registry."""
    return dict(_CLASSIFICATIONS)


# ---------------------------------------------------------------------------
# Data-source ledger (D#2327 PR-a)
# ---------------------------------------------------------------------------
# A reason string is prose: a human can read it, nothing can check it. That
# is how nineteen methods came to share one unaudited claim, and how one of
# them ended up classified SCOPED next to a justification saying it was not
# scoped. This is the same answer in a form a guard can consume: for each
# audited method, *what* it reads, which decides whether a per-request
# override reaches it at all.
#
# Deliberately partial. It carries exactly the methods D#2327 PR-a audited
# first-hand by reading each handler's read path -- not every classified
# method. data_source_for() returns None for anything else, which a guard
# should treat as "not audited, must be probed or explicitly ledgered"
# rather than as a pass. Asserting a value here for a method nobody read
# would recreate the defect this ledger exists to end.

# The read bottoms out in a DuckDB connection whose path resolves through
# state_paths.STATS_DB, so _with_project_stats_db()'s STATS_DB_PATH
# redirect (and _EnvScope's) reaches it.
DS_STATS_DB = "stats_db"

# The read is a filesystem path resolved per request from the requested
# project. The env redirect is irrelevant; the handler scopes itself.
DS_PROJECT_PATH = "project_path"

# The read shells out to GitHub against a repo slug resolved per request
# from the requested project. Same: the handler scopes itself.
DS_PROJECT_REPO = "project_repo"

# The read is bound to the serving checkout -- a module constant built from
# __file__ at import, or a slug resolved once and cached in sys.modules.
# No per-request override reaches it. A SCOPED classification on one of
# these is a lie, which is what the consistency guard checks.
DS_SERVING_CHECKOUT = "serving_checkout"

DATA_SOURCES_REACHED_BY_PROJECT = frozenset(
    {DS_STATS_DB, DS_PROJECT_PATH, DS_PROJECT_REPO}
)

_DATA_SOURCES: dict[str, str] = {
    "stats.summary": DS_STATS_DB,
    "stats.series": DS_STATS_DB,
    "stats.team_lead_tokens": DS_STATS_DB,
    "stats.cost_spike_history": DS_STATS_DB,
    "stats.role_success_rate": DS_STATS_DB,
    "stats.role_retry_rate": DS_STATS_DB,
    "stats.avg_fix_rounds_per_pr": DS_STATS_DB,
    "stats.freshness_list": DS_STATS_DB,
    "stats.pre_write_burn": DS_STATS_DB,
    "stats_duckdb_writers": DS_STATS_DB,
    "runs.by_role": DS_STATS_DB,
    "runs.percentiles": DS_STATS_DB,
    "runs.stuck": DS_STATS_DB,
    "runs.roundtrip": DS_STATS_DB,
    "runs.active_over_time": DS_STATS_DB,
    "runs.recent": DS_STATS_DB,
    # Reads .autonomous-team/loop-metrics.jsonl, not DuckDB. Was
    # DS_SERVING_CHECKOUT before D#2327 PR-a resolved the path per project.
    "stats.loop_idle_ratio": DS_PROJECT_PATH,
    # Shell out to `gh` against a per-request slug. cost_per_outcome also
    # reads STATS_DB for the spend half, but the repo slug is the half that
    # was broken and the half that decides the row set.
    "stats.weekly_velocity": DS_PROJECT_REPO,
    "stats.cost_per_outcome": DS_PROJECT_REPO,
    # Module constants from __file__ plus a module-level REPO slug.
    "stats.dora": DS_SERVING_CHECKOUT,
}


def data_source_for(method: str) -> "str | None":
    """Return the audited data source for *method*, or None if unaudited.

    None means "nobody has read this handler's read path", not "safe".
    """
    return _DATA_SOURCES.get(method)


def all_data_sources() -> dict[str, str]:
    """Return a shallow copy of the audited data-source ledger."""
    return dict(_DATA_SOURCES)


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
    kind, reason = classification

    project = (params or {}).get("project") or None

    if not project:
        # No project param (the engine's own dashboard, or a GLOBAL-only
        # caller) — existing behavior, untouched, no lock contention.
        return handler(params)

    if kind == UNSCOPABLE:
        # Carry the entry's own reason: each UNSCOPABLE method is blocked by
        # a different mechanism, and the message used to name one specific
        # follow-up (D#2261 PR-b) for all of them — wrong for every entry
        # added since (D#2327 PR-a).
        raise UnscopableMethodError(
            f"{method!r} cannot be scoped to project {project!r} — refusing "
            "rather than serving this process's own data under the "
            f"requested project's name. Blocker: {reason}"
        )

    if kind == SCOPED:
        with _EnvScope(project):
            return handler(params)

    # GLOBAL: a project param was supplied but there's nothing project-scoped
    # in this response to leak — call through unchanged.
    return handler(params)
