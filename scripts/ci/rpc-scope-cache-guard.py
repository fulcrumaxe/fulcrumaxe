#!/usr/bin/env python3
"""rpc-scope-cache-guard.py — behavioral guard against project-blind caches
sitting underneath scoped RPC dispatch (D#2309).

Background
----------
Four confirmed instances of the same bug shape landed in this repo, each
found by a different person looking at something else: a cache keyed on
method name alone (or an unscoped module constant), sitting *underneath*
correctly-scoped dispatch (backend.rpc_project_scope.dispatch_scoped).
Dispatch scopes the call correctly; the cache serves the previous caller's
project data anyway. None of the four was found by a test.

This script is not a lint over source text — it never reads a symbol name
or a cache-key literal. It is a behavioral probe at the dispatch surface:
for every RPC method classified SCOPED in backend.rpc_project_scope, it
builds two fixture projects, establishes (where possible) that they answer
differently, then warms one project's caches and calls the other WITHOUT
clearing them — exactly the sequence that caught all four known instances
by hand. A method whose answer doesn't change is not something this black-box
probe can vouch for either way; those are tracked in NON_DISCRIMINABLE below
with a specific, reviewed reason rather than silently skipped (see the
module's "Non-discriminable ledger" section).

The subject set is derived at runtime from
backend.rpc_project_scope.all_classifications() — never a hardcoded list —
so a newly-added SCOPED method is probed (or must be explicitly ledgered)
without anyone touching this file.

Run from the repo root:

    python3 scripts/ci/rpc-scope-cache-guard.py

Exit 0: every SCOPED method is discriminable-and-passing or honestly
        ledgered, and the live canary proved the probe itself works.
Exit 1: a cross-project leak was caught, the canary went undetected, or a
        SCOPED method is neither discriminable nor ledgered.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Per-method wall-clock budget (D#2309 Spec item 11) — a hung subprocess
# (e.g. a `gh` shim invocation that blocks) fails that one method loudly
# instead of silently consuming the CI job's timeout-minutes budget.
PER_METHOD_TIMEOUT_S = 10

CANARY_METHOD = "__canary.project_blind"

# Fields whose values are expected to jitter between two calls made
# microseconds apart (wall-clock timestamps, computed ages) and are never
# themselves the project-identifying content of a response. Comparing them
# literally makes the probe flaky against ordinary CI timing variance —
# exactly the "goes red at random" failure mode the Spec's scoring
# reminder says is worse than under-blocking. They are blanked before any
# equality check; genuine leaks show up in the surrounding fields (titles,
# authors, labels, tokens, roles, ...) which are never blanked.
_VOLATILE_KEY_RE = re.compile(
    r"(?i)(age.?seconds$|age_seconds|^ts$|_ts$|timestamp|created.?at|updated.?at|fetched.?at|generated_at|snapshot_age)"
)


def _normalize(obj):
    """Blank volatile fields, then make list order insignificant.

    Several probed handlers run a bare `GROUP BY` (e.g. stats.freshness_list
    over metric_event) with no `ORDER BY` — DuckDB does not guarantee row
    order for that query shape, so the same underlying data can come back
    in a different list order on two calls with nothing project-related
    involved. Comparing that literally is a second, independent source of
    the "goes red at random" flakiness the Spec's scoring reminder calls
    out (measured directly while building this guard: stats.freshness_list
    flipped between pass/leak/ledgered across identical back-to-back runs
    until list order was normalized here). Sorting every list by its own
    normalized JSON representation makes comparison order-insensitive
    everywhere, uniformly, rather than special-casing one query shape — a
    genuine cross-project leak still shows up as different *content*
    (titles, tokens, roles, ...), never merely as a reordering of identical
    items.
    """
    if isinstance(obj, dict):
        return {
            k: (None if _VOLATILE_KEY_RE.search(k) else _normalize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        normalized = [_normalize(v) for v in obj]
        try:
            normalized.sort(key=lambda v: json.dumps(v, sort_keys=True, default=str))
        except TypeError:
            pass  # unsortable mix — leave order as-is rather than crash the probe
        return normalized
    return obj


# ---------------------------------------------------------------------------
# Non-discriminable ledger (D#2309 Spec item 7)
#
# A SCOPED method lands here only after the guard actually calls it against
# both fixture projects at runtime and observes identical answers (criterion
# 6) — this dict is not consulted until that happens. Every entry names the
# real reason, checked by reading the handler's own implementation, not a
# guess. Growth of this ledger is a deliberate, reviewed act: removing an
# entry means someone made that method's response actually depend on the
# fixture data this guard's two projects differ on.
# ---------------------------------------------------------------------------
NON_DISCRIMINABLE: dict[str, str] = {
    "kpi.history": (
        "kpi_engine.history() runs `git log` against the project's own repo "
        "checkout at <state_dir>/../<project>/; this harness's fixture "
        "projects have no such checkout, so both fall through the "
        "'no local checkout' branch and return [] identically"
    ),
    "kpi.cycle_time": (
        "same git-log-backed kpi_engine.cycle_time_histogram() as "
        "kpi.history; both fixtures fall through to the zeroed-bucket "
        "branch identically for lack of a real checkout"
    ),
    "runs.stuck": (
        "agent_run_reader.stuck_runs() only returns rows with end_ts IS "
        "NULL older than threshold_seconds; this harness's seeded agent_run "
        "rows are always completed (end_ts set), so both fixtures answer "
        "with an empty stuck list"
    ),
    "runs.roundtrip": (
        "requires a required 'pr' param naming a PR with matching "
        "executor-done/reviewer-started agent_run rows; this harness does "
        "not construct a roundtrip pair, so both fixtures raise/return the "
        "same null latency for any pr number supplied"
    ),
    "runs.active_over_time": (
        "concurrent_active() buckets purely by wall-clock time, not by row "
        "identity — the only way two fixtures' single-row seeds could "
        "differ here is via timestamp placement, which is exactly the "
        "volatile-field class this guard normalizes out of every "
        "comparison (see _normalize()) to avoid clock-jitter flakiness"
    ),
    "stats.sdk_vs_cc": (
        "backend.rpc.stats_sdk_vs_cc reads per-role SDK-vs-CC comparison "
        "rows from agent_run's routed_via column; this harness's seeded "
        "rows leave routed_via unset, so both fixtures answer with the "
        "same empty comparison"
    ),
    "stats.dial_usage": (
        "backend.stats.dial_usage.read_dial_usage() reads live dial-level "
        "config and 24h activity counters this harness does not seed; "
        "both fixtures answer with the same all-default dial state"
    ),
    "stats.dial_rejections": (
        "backend.stats.dial_rejections.read_dial_rejections() reads 24h "
        "counts of rejected-directive/sandbox-block audit events this "
        "harness does not seed; both fixtures answer with zero counts"
    ),
    "stats.sdk_lane": (
        "backend.rpc.sdk_status reports live dispatcher readiness and "
        "credential presence for the serving process, not project-stored "
        "data — there is nothing in either fixture project's state dir "
        "for it to read differently"
    ),
    "stats.verdict_overturns": (
        "backend.verdict_overturn.overturn_rate_by_role_24h() requires "
        "populated 'role_verdict' metric_event rows AND a prior-role "
        "overturn pairing this harness does not construct; both fixtures "
        "answer with the same empty rows list"
    ),
    "stats.role_success_rate": (
        "stats_writer.role_success_rate_24h() needs >=5 role_verdict "
        "samples per role before reporting a non-null rate; seeding a "
        "statistically meaningful sample per fixture is out of scope for "
        "this lightweight harness, so both answer with sample_size below "
        "the reporting floor"
    ),
    "stats.role_retry_rate": (
        "same role_verdict/metric_event floor as stats.role_success_rate "
        "(>=5 samples per role) — this harness does not seed enough rows "
        "for either fixture to clear it"
    ),
    "stats.loop_idle_ratio": (
        "stats_writer.loop_idle_ratio_24h() is called by "
        "stats_loop_idle_ratio.handle() with no metrics_path argument, so "
        "it defaults to Path(__file__).resolve().parent.parent/"
        ".autonomous-team/loop-metrics.jsonl — the SERVING CHECKOUT's own "
        "file, not the requested project's. project has no effect on this "
        "handler's output today, so this black-box probe cannot observe "
        "whether the two fixtures differ (they can't, by construction) — "
        "flagged here rather than silently passed; worth a follow-up look "
        "given this repo's history with exactly this class of bug"
    ),
    "stats.avg_fix_rounds_per_pr": (
        "stats_writer.avg_fix_rounds_24h() reads merged-PR fix-round "
        "counts this harness does not seed; both fixtures answer with "
        "sample_size 0"
    ),
    "stats.freshness_list": (
        "backend.stats.stats_freshness watches the freshness of every "
        "*registered* metric_event row across the whole store, not a "
        "single seeded metric; this harness's few seeded rows don't shift "
        "the aggregate freshness picture enough to differ deterministically "
        "between fixtures"
    ),
    "stats.weekly_velocity": (
        "backend.stats.weekly_velocity.weekly_velocity() computes merged-PR "
        "counts per day from the project's PR history, which this harness's "
        "gh shim does not model with per-day granularity; both fixtures "
        "answer with the same all-zero sparkline"
    ),
    "stats.dora": (
        "stats_dora.handle() delegates to analytics_engineer.compute_snapshot "
        "and explicitly ignores its params argument today (marked "
        "'reserved for future project-scoping' in the handler itself) — "
        "project has no effect on this handler's output yet, so this "
        "probe cannot observe a difference by construction"
    ),
    "stats.pre_write_burn": (
        "backend.rpc.stats_pre_write_burn reads agent_run's "
        "first_write_turn/total_turns columns, which this harness's seeded "
        "rows leave NULL; both fixtures answer with the same empty list"
    ),
    "stats_duckdb_writers": (
        "backend.rpc.stats_duckdb_writers introspects OS-level file "
        "descriptors currently open on stats.duckdb across all processes — "
        "live process state, not project-stored data; nothing in either "
        "fixture project changes what this reports"
    ),
    "stats.cost_per_outcome": (
        "backend.cost_per_outcome.cost_per_outcome_rows() joins merged-PR "
        "outcomes with agent spend, keyed off PR-merge history this "
        "harness's gh shim does not model; both fixtures answer with an "
        "empty rows list"
    ),
    "loop.events": (
        "requires an existing loop_id resolvable via "
        "backend.active_loops.get_loop() — a GLOBAL registry file at "
        "<repo_root>/.autonomous-team/active-loops.json (shared server "
        "state, not project-scoped) — before it ever reaches the "
        "project-scoped _agent_feed_path() read; this harness deliberately "
        "does not mutate that shared file to avoid leaving residue in the "
        "checkout, so every call raises 'loop not found' identically "
        "regardless of project. agents.tail exercises the same "
        "_agent_feed_path() read with no such gate and is probed directly "
        "— it is also the actual handler named in the reported bug this "
        "fix addressed (a 2026-07-24 engine event served under an "
        "adopter's name), so the regression this class cares about is "
        "covered"
    ),
    "auth_retry.record": (
        "a write endpoint: its return value is a monotonically-increasing "
        "counter that legitimately changes on every single call regardless "
        "of project, which breaks this probe's warm-then-compare design "
        "(built for idempotent reads) — a 'probed matches neither "
        "reference' result here reflects normal counter growth, not a "
        "leak. auth_retry.summary, the read side that a real cross-project "
        "leak would actually surface through, is probed directly and is "
        "seeded by extra auth_retry.record calls made during fixture setup"
    ),
}


def _canary_handler(params: dict):
    """Deliberately project-blind: cached under a key with no project
    component, exactly the bug shape this whole guard exists to catch.
    """
    key = ("canary",)
    if key in _CANARY_CACHE:
        return _CANARY_CACHE[key]
    value = {"served_for": params.get("project")}
    _CANARY_CACHE[key] = value
    return value


_CANARY_CACHE: dict = {}


# ---------------------------------------------------------------------------
# Fixture environment
# ---------------------------------------------------------------------------

_GH_SHIM = r'''#!/usr/bin/env python3
"""Fixture `gh` shim for rpc-scope-cache-guard.py.

Answers gh CLI invocations the probed handlers make (gh api graphql, gh pr
list, gh pr view) with content that is a pure function of the --repo /
owner+name argument the real handler passed in. No network, no GH_TOKEN, no
per-repo hardcoding: any two distinct repo slugs automatically produce
distinct answers, which is what makes the probed methods discriminable.
"""
import json
import re
import sys


def _repo_from_argv(argv):
    for i, a in enumerate(argv):
        if a == "--repo" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def main():
    argv = sys.argv[1:]
    if not argv:
        print("gh-fixture-shim: no arguments", file=sys.stderr)
        return 1

    if argv[0] == "api" and "graphql" in argv:
        query = None
        for i, a in enumerate(argv):
            if a == "-f" and i + 1 < len(argv) and argv[i + 1].startswith("query="):
                query = argv[i + 1][len("query="):]
        if query is None:
            print("gh-fixture-shim: no query= arg found", file=sys.stderr)
            return 1
        m = re.search(r'owner:\s*"([^"]+)",\s*name:\s*"([^"]+)"', query)
        owner, name = (m.group(1), m.group(2)) if m else ("unknown-owner", "unknown-repo")
        repo = f"{owner}/{name}"

        dm = re.search(r"discussion\(number:\s*(\d+)\)", query)
        prm = re.search(r"pullRequest\(number:\s*(\d+)\)", query)
        listm = "discussions(first" in query

        if dm:
            n = int(dm.group(1))
            body = (
                f"Fixture discussion body for {repo} #{n}\n"
                "<!-- STATUS:SPEC_READY PR:#9001 -->"
            )
            out = {"data": {"repository": {"discussion": {
                "number": n,
                "title": f"Fixture discussion {repo}#{n}",
                "body": body,
                "url": f"https://example.invalid/{repo}/discussions/{n}",
                "createdAt": "2020-01-01T00:00:00Z",
                "updatedAt": "2020-01-01T00:00:00Z",
                "author": {"login": f"user-{owner}"},
                "category": {"name": "General"},
                "comments": {"nodes": []},
            }}}}
        elif prm:
            n = int(prm.group(1))
            out = {"data": {"repository": {"pullRequest": {
                "number": n,
                "url": f"https://example.invalid/{repo}/pull/{n}",
                "state": "OPEN",
                "labels": {"nodes": [{"name": f"label-{owner}"}]},
            }}}}
        elif listm:
            out = {"data": {"repository": {"discussions": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{
                    "number": 501,
                    "title": f"Fixture discussion list for {repo}",
                    "body": f"Fixture body for {repo}\n<!-- STATUS:SPEC_READY -->",
                    "url": f"https://example.invalid/{repo}/discussions/501",
                    "createdAt": "2020-01-01T00:00:00Z",
                    "updatedAt": "2020-01-01T00:00:00Z",
                    "category": {"name": "General"},
                    "author": {"login": f"user-{owner}"},
                }],
            }}}}
        else:
            out = {"data": {"repository": {}}}
        print(json.dumps(out))
        return 0

    if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
        repo = _repo_from_argv(argv) or "unknown/unknown"
        owner = repo.split("/")[0]
        items = [{
            "number": 9001,
            "title": f"Fixture PR for {repo}",
            "author": {"login": f"user-{owner}"},
            "labels": [],
            "createdAt": "2020-01-01T00:00:00Z",
            "body": "",
            "url": f"https://example.invalid/{repo}/pull/9001",
        }]
        print(json.dumps(items))
        return 0

    if argv[0] == "pr" and len(argv) > 1 and argv[1] == "view":
        repo = _repo_from_argv(argv) or "unknown/unknown"
        owner = repo.split("/")[0]
        pr_number = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 0
        out = {
            "number": pr_number,
            "title": f"Fixture PR view for {repo}",
            "author": {"login": f"user-{owner}"},
            "state": "OPEN",
            "mergedAt": None,
            "additions": 1,
            "deletions": 1,
            "changedFiles": 1,
            "url": f"https://example.invalid/{repo}/pull/{pr_number}",
            "body": "",
        }
        print(json.dumps(out))
        return 0

    print(f"gh-fixture-shim: unhandled invocation: {argv}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


class Fixture:
    """Two isolated fixture projects ("alpha", "beta") under a temp $HOME,
    plus a PATH-shimmed `gh` — no network, no secrets, no GH_TOKEN (Spec
    item 10).
    """

    def __init__(self):
        self.tmp_root = Path(tempfile.mkdtemp(prefix="rpc-scope-guard-"))
        self._orig_env = dict(os.environ)
        self.projects = {}

    def __enter__(self):
        os.environ["HOME"] = str(self.tmp_root)
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        # A generic, non-project scratch dir — never named .alpha-state /
        # .beta-state — so this process is itself "sandboxed" even if
        # something downstream runs under pytest later (see module notes
        # in the Spec's Implementation Notes on AUTONOMOUS_TEAM_STATE_DIR).
        own_state = self.tmp_root / ".guard-own-state"
        own_state.mkdir(parents=True, exist_ok=True)
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = str(own_state)
        os.environ.pop("STATS_DB_PATH", None)

        bin_dir = self.tmp_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        gh_path = bin_dir / "gh"
        gh_path.write_text(_GH_SHIM, encoding="utf-8")
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

        for name in ("alpha", "beta"):
            state_dir = self.tmp_root / f".{name}-state"
            state_dir.mkdir(parents=True, exist_ok=True)
            runtime = {
                "project_name": name,
                "state_dir": str(state_dir),
                "repo": f"guardfixture-{name}/repo",
            }
            (state_dir / "dashboard-runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
            self.projects[name] = state_dir

        return self

    def __exit__(self, *exc_info):
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self.tmp_root, ignore_errors=True)
        return False

    def state_dir(self, name: str) -> Path:
        return self.projects[name]


# ---------------------------------------------------------------------------
# Fixture data seeding — plants distinguishing content via the SAME
# production write paths the real system uses (agent_run_tracker,
# stats_writer, Blackboard/BudgetTracker, dispatch_scoped itself for
# auth_retry.record), or by writing the plain per-project files handlers
# already read directly (agent-feed.jsonl, loop-metrics.jsonl, the a2a and
# cosmetic-blocks JSONL logs). Nothing here reaches into cache internals —
# discriminability comes from real per-project data, same as production.
# ---------------------------------------------------------------------------

GUARD_ROLE = "guard-fixture-role"
GUARD_DISCUSSION = 913700501  # implausible as a real Discussion number
DURATIONS = {"alpha": 111.0, "beta": 222.0}
TOKENS = {"alpha": (1000, 500), "beta": (9000, 4000)}


def _seed_agent_feed(state_dir: Path, project: str) -> None:
    line = json.dumps({
        "timestamp": "2020-01-01T00:00:00Z",
        "discussion": 501,
        "role": f"guard-fixture-{project}",
        "verdict": "pass",
        "pr": None,
    })
    (state_dir / "agent-feed.jsonl").write_text(line + "\n", encoding="utf-8")


def _seed_loop_metrics(state_dir: Path, project: str) -> None:
    row = {
        "timestamp": "2026-01-01T00:00:00Z",
        "duration_seconds": DURATIONS[project],
        "agents_spawned": 1,
        "prs_merged": 0,
        "discussions_scanned": 0,
        "prs_scanned": 0,
        "idle": False,
        "origin": "cron",
    }
    (state_dir / "loop-metrics.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def _seed_a2a(state_dir: Path, project: str) -> None:
    a2a_dir = state_dir / "a2a"
    a2a_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": f"guard-{project}-1",
        "from": f"guard-fixture-{project}",
        "to": "team-lead",
        "kind": "status",
        "ts": "2020-01-01T00:00:00Z",
        "body_sha256": "0" * 64,
    }
    (a2a_dir / "messages.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _seed_cosmetic_blocks(state_dir: Path, project: str) -> None:
    import datetime as _dt
    hook_events = state_dir / "hook-events"
    hook_events.mkdir(parents=True, exist_ok=True)
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = {"alpha": 2, "beta": 5}[project]
    lines = [json.dumps({"ts": now_iso}) for _ in range(count)]
    (hook_events / f"cosmetic-blocks-{today}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _seed_agent_run_and_cost(project: str, rpc_project_scope) -> None:
    """Seed the agent_run duckdb table (feeds runs.* and, via
    backend.stats.agent_spend's agent_run-first precedence, cost.*) and the
    blackboard budget/agents record (feeds team_status.snapshot's budget
    summary, a separate mechanism from agent_run).
    """
    import backend.agent_run_tracker as art
    from backend.blackboard import Blackboard
    from backend.budget import BudgetTracker

    input_tok, output_tok = TOKENS[project]
    agent_id = f"guard-fixture-{project}"

    with rpc_project_scope._EnvScope(project):
        art.start_run(agent_id=agent_id, role=GUARD_ROLE, discussion=GUARD_DISCUSSION)
        art.complete_run(
            agent_id=agent_id,
            duration_s=DURATIONS[project],
            verdict="pass",
            model="claude-sonnet-4-6",
            input_tok=input_tok,
            output_tok=output_tok,
        )
        BudgetTracker(bb=Blackboard()).record_spend(
            agent_id=agent_id,
            agent_role=GUARD_ROLE,
            input_tokens=input_tok,
            output_tokens=output_tok,
            discussion=GUARD_DISCUSSION,
            model="claude-sonnet-4-6",
        )


def _seed_metric_events(project: str, rpc_project_scope) -> None:
    import backend.stats_writer as sw

    value = {"alpha": 100.0, "beta": 200.0}[project]
    with rpc_project_scope._EnvScope(project):
        sw.record_iteration_cost(value)
        sw.record_cost_spike(value=value, mu=value / 2, sigma=1.0)
        for i in range(5):
            sw.record_loop_iter(
                duration_s=60.0,
                team_lead_input_tokens=int(value) * (i + 1),
                team_lead_output_tokens=int(value) * (i + 1),
            )
            time.sleep(0.002)  # distinct PK timestamps (ms precision)


def _seed_auth_retry(project: str, rpc_project_scope, dispatch_scoped, handler) -> None:
    """auth_retry.record is itself the write path — seed by calling the real
    RPC an extra time for alpha only, so alpha's total is already ahead of
    beta's before either is probed as the method under test.
    """
    extra_calls = {"alpha": 2, "beta": 0}[project]
    for _ in range(extra_calls):
        dispatch_scoped("auth_retry.record", {"project": project}, handler)


def seed_all(fixture: Fixture, rpc_project_scope, dispatch_scoped, rpc_methods) -> None:
    for project in ("alpha", "beta"):
        state_dir = fixture.state_dir(project)
        _seed_agent_feed(state_dir, project)
        _seed_loop_metrics(state_dir, project)
        _seed_a2a(state_dir, project)
        _seed_cosmetic_blocks(state_dir, project)
        _seed_agent_run_and_cost(project, rpc_project_scope)
        _seed_metric_events(project, rpc_project_scope)
        _seed_auth_retry(project, rpc_project_scope, dispatch_scoped, rpc_methods["auth_retry.record"])


# ---------------------------------------------------------------------------
# Extra per-method params (project is added by the caller)
# ---------------------------------------------------------------------------

EXTRA_PARAMS: dict[str, dict] = {
    "discussions.get": {"number": 501},
    "dashboard.pr_detail": {"pr_number": 9001},
    "loop.iteration_detail": {"timestamp": "2026-01-01T00:00:00Z"},
    "runs.by_role": {"role": GUARD_ROLE},
    "cost.per_discussion": {"discussion": GUARD_DISCUSSION},
    "stats.series": {"name": "iteration_cost_usd"},
}

# Methods whose own return value legitimately changes on every call
# regardless of project (a write endpoint returning a monotonic counter) —
# the leak-probe's "warm A, call B, expect B's own answer" design assumes an
# idempotent read, which does not hold here. These are genuinely
# discriminable (ref_A != ref_B) but must skip the leak-probe step rather
# than have normal mutation misreported as a leak; see their NON_DISCRIMINABLE
# ledger entries for the specific reasoning.
NO_LEAK_PROBE: frozenset[str] = frozenset({"auth_retry.record"})


# ---------------------------------------------------------------------------
# Cache clearing — discovers cache containers rather than hardcoding them
# (Spec Implementation Notes: "prefer discovering module-level dict caches
# by scanning vars() ... so a new cache container is covered without an
# edit"). Applied to backend.server plus every already-imported backend.*
# submodule, so a nested cache inside an rpc/ handler module (the actual
# shape of instance #3 of the four known bugs) is covered too.
# ---------------------------------------------------------------------------

def clear_all_caches() -> None:
    _CANARY_CACHE.clear()
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not mod_name.startswith("backend"):
            continue
        try:
            mod_vars = vars(mod)
        except TypeError:
            continue
        for attr_name, value in list(mod_vars.items()):
            if isinstance(value, dict) and "CACHE" in attr_name.upper():
                value.clear()


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

class _Timeout(Exception):
    pass


def safe_call(dispatch_scoped, method, params, handler):
    """Call through dispatch_scoped, capturing exceptions as comparable
    values instead of propagating them — so a method that raises identically
    for both fixtures is treated as "not discriminable" (needs a ledger
    entry) rather than crashing the whole guard.
    """
    try:
        result = dispatch_scoped(method, params, handler)
        return ("ok", _normalize(result))
    except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
        return ("error", f"{type(exc).__name__}: {exc}")


def probe_method(dispatch_scoped, method, handler, extra_params):
    params_a = {"project": "alpha", **extra_params}
    params_b = {"project": "beta", **extra_params}

    clear_all_caches()
    ref_a = safe_call(dispatch_scoped, method, params_a, handler)
    clear_all_caches()
    ref_b = safe_call(dispatch_scoped, method, params_b, handler)

    if ref_a == ref_b:
        return "not-discriminable", None

    if method in NO_LEAK_PROBE:
        return "not-discriminable", "mutating endpoint — leak-probe skipped, see NON_DISCRIMINABLE ledger"

    clear_all_caches()
    safe_call(dispatch_scoped, method, params_a, handler)  # warm alpha's caches
    probed = safe_call(dispatch_scoped, method, params_b, handler)  # no clear — the leak window

    if probed == ref_b:
        return "pass", None
    # Anything else — an exact swap to ref_A, or a response that mixes some
    # of B's own fields with some of A's (e.g. discussions.get's outer
    # per-repo cache correctly answers B while a nested, unscoped cache
    # inside it still serves A's linked-PR data) — means alpha-warmed state
    # leaked into beta's answer. Criterion 8 only names the full-swap case
    # explicitly, but a partial leak is the same bug and must report the
    # same cross-project-leak diagnostic, not a softer "ambiguous" one.
    return "leak", f"expected {ref_b!r}, got {probed!r}"


def run_with_timeout(fn, timeout_s):
    """Run *fn* with a hard wall-clock timeout, isolated to this process
    (SIGALRM — POSIX only, matches the ubuntu-latest CI runner)."""
    import signal

    if not hasattr(signal, "SIGALRM"):
        return fn()  # best-effort elsewhere; CI runs on Linux

    def _handler(signum, frame):
        raise _Timeout()

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout_s)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))

    import backend.server as server
    import backend.rpc_project_scope as rpc_project_scope

    classifications = rpc_project_scope.all_classifications()
    if len(classifications) < rpc_project_scope.MIN_REGISTRY_SIZE:
        print(
            f"FAIL registry-too-small: all_classifications() returned "
            f"{len(classifications)} entries, expected >= "
            f"{rpc_project_scope.MIN_REGISTRY_SIZE} (rpc_project_scope."
            "MIN_REGISTRY_SIZE) — an import failure or empty enumeration "
            "would otherwise silently pass as 'everything classified'"
        )
        return 1

    scoped_methods = sorted(
        m for m, (kind, _reason) in classifications.items()
        if kind == rpc_project_scope.SCOPED
    )

    dispatch_scoped = rpc_project_scope.dispatch_scoped

    with Fixture() as fixture:
        # --- Live canary (Spec item 9) — proves this probe is not inert on
        # every single run, before a single real method is checked. -------
        server._RPC_METHODS[CANARY_METHOD] = _canary_handler
        rpc_project_scope._CLASSIFICATIONS[CANARY_METHOD] = (
            rpc_project_scope.SCOPED,
            "synthetic canary injected at runtime by rpc-scope-cache-guard.py "
            "— deliberately project-blind cache, proves the guard can still "
            "catch the exact bug shape it exists for; never present on a "
            "clean tree",
        )
        try:
            canary_outcome, canary_detail = run_with_timeout(
                lambda: probe_method(dispatch_scoped, CANARY_METHOD, _canary_handler, {}),
                PER_METHOD_TIMEOUT_S,
            )
        finally:
            del server._RPC_METHODS[CANARY_METHOD]
            del rpc_project_scope._CLASSIFICATIONS[CANARY_METHOD]
            _CANARY_CACHE.clear()

        if canary_outcome != "leak":
            print(
                "FAIL canary-not-detected — this guard has gone inert "
                f"(canary probe outcome: {canary_outcome!r}, detail: {canary_detail!r})"
            )
            return 1
        print(f"canary: detected as expected ({CANARY_METHOD})")

        # --- Seed fixture data via real production write paths -----------
        try:
            seed_all(fixture, rpc_project_scope, dispatch_scoped, server._RPC_METHODS)
        except Exception:
            print("FAIL fixture-seed-error:")
            traceback.print_exc()
            return 1

        # --- Probe every SCOPED method -------------------------------------
        failures: list[str] = []
        ledgered: list[str] = []
        passed: list[str] = []

        for method in scoped_methods:
            handler = server._RPC_METHODS.get(method)
            if handler is None:
                failures.append(f"missing-handler: {method} classified but not registered in _RPC_METHODS")
                continue
            extra = EXTRA_PARAMS.get(method, {})
            try:
                outcome, detail = run_with_timeout(
                    lambda: probe_method(dispatch_scoped, method, handler, extra),
                    PER_METHOD_TIMEOUT_S,
                )
            except _Timeout:
                failures.append(f"timeout: {method} exceeded {PER_METHOD_TIMEOUT_S}s")
                continue
            except Exception:
                failures.append(f"probe-crashed: {method}\n{traceback.format_exc()}")
                continue

            if outcome == "pass":
                passed.append(method)
            elif outcome == "leak":
                failures.append(f"cross-project-leak: {method}")
            elif outcome in ("not-discriminable", "ambiguous"):
                # Both outcomes mean this black-box probe cannot cleanly
                # resolve the method to pass/fail — "not-discriminable"
                # (ref_A == ref_B, Spec criterion 6/7) and "ambiguous" (a
                # mutating endpoint whose result legitimately changes on
                # every call — e.g. a monotonic counter — so it matches
                # neither reference without that being a leak) are both
                # "this method needs a reviewed ledger reason", not a pass.
                if method in NON_DISCRIMINABLE:
                    ledgered.append(method)
                else:
                    tag = "unclassified-observability" if outcome == "not-discriminable" else "ambiguous-result"
                    detail_suffix = f": {detail}" if detail else ""
                    failures.append(f"{tag}: {method}{detail_suffix}")

        print(
            f"rpc-scope-cache-guard: {len(scoped_methods)} SCOPED methods — "
            f"{len(passed)} discriminable+passing, {len(ledgered)} ledgered, "
            f"{len(failures)} failing"
        )
        if passed:
            print("  passing: " + ", ".join(passed))
        if ledgered:
            print("  ledgered (non-discriminable, honest reason on file): " + ", ".join(ledgered))

        if failures:
            print()
            for f in failures:
                print(f"FAIL {f}")
            return 1

        print("rpc-scope-cache-guard: all clear")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
