#!/usr/bin/env python3
"""rpc-project-blind-guard.py — behavioral guard against RPC methods that
answer every project with the same thing (D#2327 PR-b).

Why a second probe, next to rpc-scope-cache-guard.py
-----------------------------------------------------
The two guards look superficially alike — same dispatch surface, two fixture
projects, a runtime-derived subject set, a live canary, a per-method timeout —
and they catch opposite bugs. Stating the difference precisely, because it is
the whole reason this file exists:

* A **cache leak** makes project A's request answer with project B's data.
  A and B disagree with each other and one of them is wrong. The cache guard
  finds it by warming A's caches and then calling B *without clearing*, and
  asserting B still gets B's own answer.

* **Uniform project-blindness** makes A and B answer with a *third* thing —
  the serving checkout's numbers — identically, every time, for everybody.
  There is nothing to leak, because nothing project-specific is ever read.

A leak detector reads uniform blindness as clean: warm A, call B, B's answer
is unchanged, therefore no leak. That is exactly what happened. D#2309's
cache guard shipped in PR #2312 and ``stats.loop_idle_ratio`` sat underneath
it wrapped-but-unscoped, reading the serving checkout's
``.autonomous-team/loop-metrics.jsonl`` for every caller, until D#2327 was
filed by hand. The cache guard did not miss it through a bug; a
same-answer-for-everyone method is invisible to a same-answer-as-before
assertion.

So this guard's assertion is the inverse: seed **deliberately different**
data in each fixture project, call each one with the caches cleared in
between, and fail a method whose two answers are identical.

    cache guard   : A_after_warming_B == A_alone           (else: leak)
    this guard    : A                 != B                 (else: blind)

Two things make that assertion mean something rather than merely pass
-----------------------------------------------------------------------
**1. Echoing the project name back is not scoping.** Many handlers put the
``project`` param straight into their response, and a fixture whose seeded
data is derived from the project name ("role-alpha" vs "role-beta") makes
every method look discriminating whether it read anything or not. Every
distinguishing value this fixture seeds is therefore an opaque token
(``pelican`` / ``walrus``) or a distinct number — never the project name —
and every occurrence of the project name is scrubbed from both answers
before they are compared.

What survives that scrub is *usually* a difference that could only have come
from reading the project's data — but not always, and the honest statement of
the limit belongs here rather than in a reviewer's head. The two fixtures are
seeded one after the other, so every row each project writes carries its own
wall-clock time. Any response field echoing one of those times differs
between the projects no matter what the handler read, and
:data:`_VOLATILE_KEY_RE` blanking them is a denylist over *field-name shapes*
— it removes the timestamps whose names it recognises, not the ones it does
not. The self-stability check below does not cover this either: the leaking
value is written once at seed time and then read back identically on every
call, so the response is perfectly stable and still says nothing about
scoping.

That is not hypothetical, and it was found in this file rather than reasoned
about. The rule originally anchored on ``_at`` / ``_ts`` and missed
``updated_at_iso``, ``ts_iso``, ``last_spike_iso``, ``last_seen`` and
``latency_seconds``; five of thirty-six methods reported ``discriminating``
purely on seed-write times.

The check that finds this is cheap and is wired in — run
``--self-check``, which seeds both projects identically and reports every
method still claiming to discriminate. On identical seeds the honest count is
zero, and anything above zero names a field the rule above has not learned
yet. **Run it after touching the seeds, the normalizer, or that rule.**

**2. Volatile fields make a blind method look sighted.** The cache guard
normalizes timestamps because jitter between two calls would turn a clean
method red at random. Here the same jitter fails the *other* way: a
project-blind handler that stamps its response with ``generated_at`` returns
two different answers to two calls and is waved through. Same helper,
opposite failure mode — there a false positive, here a false negative — and
here it is load-bearing for the guard meaning anything at all.

The canaries (Spec item 14)
----------------------------
Two synthetic methods are injected at runtime, and both must land where
expected before a single real method is probed:

* ``__canary.project_blind`` reads the *serving checkout's*
  loop-metrics.jsonl regardless of the requested project — the exact shape of
  the bug this guard exists for. It must be flagged.
* ``__canary.project_scoped`` reads the *requested project's* file through
  ``backend.loop_metrics_path``. It must not be flagged.

The second is not decoration. Without it, a fixture that silently stopped
seeding differing data would report every method blind, the blind canary
would still be "detected", and the guard would be loudly, uniformly wrong.
The sighted canary is what proves the seeds discriminate.

Run from the repo root:

    python3 scripts/ci/rpc-project-blind-guard.py

Exit 0: every SCOPED method answers two differently-seeded projects
        differently, or is honestly ledgered, and both canaries landed.
Exit 1: a project-blind method was found, a non-discriminable method has no
        ledger entry, either canary went the wrong way, or the registry
        enumeration came back implausibly small.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Per-method wall-clock budget (Spec item 19), matching the cache guard's —
# a hung handler (a `gh` shim invocation that blocks, a DuckDB lock) fails
# that one method loudly instead of consuming the CI job's timeout budget.
PER_METHOD_TIMEOUT_S = 10

CANARY_BLIND = "__canary.project_blind"
CANARY_SIGHTED = "__canary.project_scoped"

PROJECTS = ("alpha", "beta")

# Every distinguishing value below is deliberately NOT derived from the
# project name — see the module docstring, point 1. `pelican` and `walrus`
# survive the project-name scrub; "alpha" and "beta" do not.
SEEDS: dict[str, dict] = {
    "alpha": {
        "token": "pelican",
        "duration": 111.0,
        "in_tok": 1000,
        "out_tok": 500,
        "loop_rows": 6,
        "loop_idle": 1,
        "cost": 100.0,
        "cosmetic": 2,
        "fix_rounds": [0, 1, 1, 2, 3],
        "verdicts": ["pass"] * 5 + ["needs-fix"],
        "pr_count": 3,
        "first_write_turn": 2,
        "total_turns": 20,
        "routed_via": "sdk",
        "roundtrip_gap_s": 60,
        "dial_accepted": 2,
        "dial_rejected": 1,
        "auth_retries": 3,
        "open_runs": 1,
        "release_count": 1,
    },
    "beta": {
        "token": "walrus",
        "duration": 222.0,
        "in_tok": 9000,
        "out_tok": 4000,
        "loop_rows": 6,
        "loop_idle": 4,
        "cost": 200.0,
        "cosmetic": 5,
        "fix_rounds": [2, 3, 3, 4, 5],
        "verdicts": ["pass"] * 2 + ["needs-fix"] * 4,
        "pr_count": 7,
        "first_write_turn": 9,
        "total_turns": 30,
        "routed_via": "claude-code",
        "roundtrip_gap_s": 300,
        "dial_accepted": 5,
        "dial_rejected": 4,
        "auth_retries": 8,
        "open_runs": 3,
        "release_count": 3,
    },
}

GUARD_ROLE = "guard-fixture-role"
GUARD_DISCUSSION = 913700502  # implausible as a real Discussion number
GUARD_PR = 9001


def _repo_slug(project: str) -> str:
    """Fixture repo slug for *project*, built from its opaque token so the
    slug is not erased by the project-name scrub."""
    return f"guardfixture-{SEEDS[project]['token']}/repo"


# ---------------------------------------------------------------------------
# Answer normalization
# ---------------------------------------------------------------------------

# Wider than the cache guard's equivalent, and deliberately so: there a
# missed volatile field turns a clean method red at random, here it silently
# waves a project-blind method through.
#
# This rule shipped anchored on `_at$` / `_ts$` / `^ts$` and missed the
# spelling this codebase actually uses most: the `_iso` suffix. All three of
# `updated_at_iso`, `ts_iso` and `last_spike_iso` end in `_iso`, not in `_at`
# or `_ts`, and so did `last_seen` and `latency_seconds` in neither. Measured
# on the unfixed rule with both fixture projects seeded identically: five of
# thirty-six methods still reported `discriminating`, every one of them on a
# wall-clock write time. Hence `_iso$` for the whole ISO-suffixed family, and
# `latency` alongside age / elapsed / uptime as another duration derived from
# a timestamp this harness does not control.
#
# `_pids$` is not a timestamp but belongs to the same class: PR #31 gave
# stats_duckdb_writers a provenance block carrying `inspected_pids` /
# `uninspected_pids`, live counts of processes on the HOST. Measured over
# eight runs of this guard before they were blanked, that method — which
# reports nothing project-specific whatsoever — came back `discriminating`
# once, because the host count happened to move between the alpha call and
# the beta call. The self-stability check below only catches that when the
# move lands between the two same-project calls instead; landing between
# projects reads as a pass. A live host counter is never evidence of
# scoping, so it is blanked rather than left to a coin flip.
#
# It is still a denylist over field-name shapes, and denylists are only ever
# as good as the shapes someone thought of — see the module docstring's note
# on `--self-check`, which is how you find out.
_VOLATILE_KEY_RE = re.compile(
    r"(?i)(age.?seconds$|age_seconds|^ts$|_ts$|_iso$|timestamp|_at$|At$"
    r"|last_seen|generated_at|snapshot_age|elapsed|uptime|latency|_pids$|now$)"
)


def _scrub_project_names(text: str) -> str:
    """Replace every fixture project name in *text* with a fixed placeholder.

    A response that differs only by the project name it was handed is a
    response that echoed a parameter, not one that read that project's data.
    State-dir paths (``/.alpha-state/``) fall out of this for the same
    reason: the handler was told where to look, and repeating the address
    back is not evidence it looked.
    """
    for project in PROJECTS:
        text = re.sub(rf"\b{re.escape(project)}\b", "<PROJECT>", text)
    return text


def _normalize(obj):
    """Blank volatile fields, scrub project names, make list order
    insignificant — so the only surviving difference between two answers is
    the seeded, project-specific *data*."""
    if isinstance(obj, dict):
        return {
            _scrub_project_names(str(k)): (
                None if _VOLATILE_KEY_RE.search(str(k)) else _normalize(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        normalized = [_normalize(v) for v in obj]
        try:
            normalized.sort(key=lambda v: json.dumps(v, sort_keys=True, default=str))
        except TypeError:
            pass
        return normalized
    if isinstance(obj, str):
        return _scrub_project_names(obj)
    return obj


# ---------------------------------------------------------------------------
# Non-discriminable ledger (Spec item 16)
#
# A method lands here only after this guard actually calls it against both
# fixture projects at runtime and observes identical answers. Every reason
# below was checked by reading that handler's own read path, and says which
# of the two possibilities it is: the harness does not seed what the method
# reads, or the method is genuinely blind and is classified accordingly.
# An identical-answering method that is NOT in here fails the build.
# ---------------------------------------------------------------------------
NON_DISCRIMINABLE: dict[str, str] = {
    "kpi.history": (
        "HARNESS, not blindness. backend/server.py:599-615 resolves the "
        "project's own checkout (state_dir.parent / <name>) and passes it to "
        "kpi_engine.history(repo_root=...), which runs `git log` there "
        "(backend/kpi_engine.py:433-463). The per-project seam is real and "
        "the handler already returns [] rather than the serving checkout's "
        "history when the checkout is absent. This fixture builds state dirs, "
        "not git checkouts, so both projects take that [] branch. Building "
        "two throwaway repos with differing commit histories would make this "
        "discriminable, at the cost of a git dependency and two subprocess "
        "trees per run."
    ),
    "kpi.cycle_time": (
        "HARNESS, not blindness. Same per-project repo_root seam as "
        "kpi.history — kpi_engine.cycle_time_histogram(repo_root=...) reads "
        "the registry and blackboard/discussions/<N>.json under that root "
        "(backend/kpi_engine.py:496-521). Neither fixture project has those "
        "files, so both fall through to the all-zero four-bucket histogram."
    ),
    "loop.events": (
        "HARNESS, not blindness — and the read this would cover is covered "
        "elsewhere. backend/server.py:498 resolves loop_id through "
        "backend.active_loops.get_loop() BEFORE reaching the project-scoped "
        "_agent_feed_path(project) read at :507. That registry is a global "
        "file in the serving checkout, shared server state; seeding it means "
        "writing into the checkout this guard runs from, which it does not "
        "do. Both projects therefore raise the same 'loop not found' at the "
        "gate. agents.tail exercises the very same _agent_feed_path() read "
        "with no such gate, is probed here, and discriminates — so a "
        "project-blind agent-feed read would still be caught."
    ),
    "stats_duckdb_writers": (
        "LIVE OS STATE, not project-stored data — and since PR #31 it is "
        "live enough to be unstable rather than merely identical. "
        "backend/stats/duckdb_writers.py resolves stats.duckdb through "
        "state_paths.STATS_DB at call time, so the per-request redirect does "
        "reach it (PR-a audited it DS_STATS_DB); what it then reports is "
        "which OS processes hold descriptors on that path. This harness "
        "opens and closes its connections around each call, so neither "
        "project has a live writer to report. PR #31 added /proc scanning "
        "with an `inspected_pids` / `uninspected_pids` provenance block — "
        "counts of processes on the *host*, which move between two calls "
        "made milliseconds apart. Left alone those counts made this method "
        "read as `discriminating` on roughly one run in eight, purely on "
        "when the host count happened to move; `_pids$` is in "
        "_VOLATILE_KEY_RE for that reason, and with it blanked the method "
        "lands here deterministically. The reason is unchanged either way: "
        "nothing seedable in a fixture project changes what it reports, "
        "short of spawning a process to hold each file open for the length "
        "of the probe."
    ),
    "runs.roundtrip": (
        "HARNESS. roundtrip_latency() "
        "(backend/agent_run_reader.py:270-300) returns the gap between the "
        "executor side's end_ts and the reviewer side's start_ts, and its "
        "whole payload is that one figure plus the pr number it was asked "
        "about. This fixture can set the executor's end_ts but not the "
        "reviewer's start_ts: complete_run() honours a supplied start_ts "
        "only on its INSERT branch, and tagging a row with a PR at all "
        "requires going through start_run() first, which stamps start_ts "
        "with the call time. So the only value that can differ between two "
        "projects here is part seeded and part seed-write wall clock — "
        "which is exactly the class _VOLATILE_KEY_RE blanks, and it does "
        "blank `latency_seconds`. Giving agent_run_tracker a way to record "
        "a PR-tagged row at a caller-chosen start_ts would make this "
        "genuinely discriminable; that is a change to the tracker, not to "
        "this guard."
    ),
}


# ---------------------------------------------------------------------------
# Canaries
# ---------------------------------------------------------------------------

def _canary_blind_handler(params: dict) -> dict:
    """Deliberately project-blind: reads the SERVING CHECKOUT's
    loop-metrics.jsonl whatever project was asked for — byte-for-byte the
    shape stats.loop_idle_ratio had before D#2327 PR-a.
    """
    from backend.stats_writer import loop_idle_ratio_24h

    return loop_idle_ratio_24h()


def _canary_sighted_handler(params: dict) -> dict:
    """The control: resolves the REQUESTED project's own metrics file. Proves
    the fixture seeds data that actually discriminates — without it, a
    fixture that stopped seeding would report every method blind and the
    blind canary would still 'pass'.
    """
    from backend.loop_metrics_path import resolve_loop_metrics_path
    from backend.stats_writer import loop_idle_ratio_24h

    path = resolve_loop_metrics_path(params.get("project") or None)
    if path is None:
        return {"unreachable": True}
    return loop_idle_ratio_24h(str(path))


# ---------------------------------------------------------------------------
# Fixture environment
# ---------------------------------------------------------------------------

_GH_SHIM = r'''#!/usr/bin/env python3
"""Fixture `gh` shim for rpc-project-blind-guard.py.

Answers the gh invocations probed handlers make with content that is a pure
function of the --repo / owner+name argument passed in. No network, no
GH_TOKEN. Row counts come from GUARD_FIXTURE_PR_COUNTS in the environment so
this file hardcodes no repo slug: the fixture owns which slug gets how many
PRs, and the shim just honours it.
"""
import json
import os
import re
import sys

_MERGED_AT = "MERGED_AT_PLACEHOLDER"


def _repo_from_argv(argv):
    for i, a in enumerate(argv):
        if a == "--repo" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _pr_count(repo):
    try:
        counts = json.loads(os.environ.get("GUARD_FIXTURE_PR_COUNTS", "{}"))
    except ValueError:
        counts = {}
    return int(counts.get(repo, 1))


def main():
    argv = sys.argv[1:]
    if not argv:
        print("gh-blind-shim: no arguments", file=sys.stderr)
        return 1

    if argv[0] == "api" and "graphql" in argv:
        query = None
        for i, a in enumerate(argv):
            if a == "-f" and i + 1 < len(argv) and argv[i + 1].startswith("query="):
                query = argv[i + 1][len("query="):]
        if query is None:
            print("gh-blind-shim: no query= arg found", file=sys.stderr)
            return 1
        m = re.search(r'owner:\s*"([^"]+)",\s*name:\s*"([^"]+)"', query)
        owner, name = (m.group(1), m.group(2)) if m else ("unknown-owner", "unknown-repo")
        repo = "%s/%s" % (owner, name)
        n_extra = _pr_count(repo)

        dm = re.search(r"discussion\(number:\s*(\d+)\)", query)
        prm = re.search(r"pullRequest\(number:\s*(\d+)\)", query)
        listm = "discussions(first" in query

        if dm:
            n = int(dm.group(1))
            out = {"data": {"repository": {"discussion": {
                "number": n,
                "title": "Fixture discussion %s#%d" % (repo, n),
                "body": "Fixture discussion body for %s #%d\n<!-- STATUS:SPEC_READY PR:#9001 -->" % (repo, n),
                "url": "https://example.invalid/%s/discussions/%d" % (repo, n),
                "createdAt": "2020-01-01T00:00:00Z",
                "updatedAt": "2020-01-01T00:00:00Z",
                "author": {"login": "user-%s" % owner},
                "category": {"name": "General"},
                "comments": {"nodes": []},
            }}}}
        elif prm:
            n = int(prm.group(1))
            out = {"data": {"repository": {"pullRequest": {
                "number": n,
                "url": "https://example.invalid/%s/pull/%d" % (repo, n),
                "state": "OPEN",
                "labels": {"nodes": [{"name": "label-%s" % owner}]},
            }}}}
        elif listm:
            out = {"data": {"repository": {"discussions": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{
                    "number": 500 + i,
                    "title": "Fixture discussion %d for %s" % (i, repo),
                    "body": "Fixture body %d for %s\n<!-- STATUS:SPEC_READY -->" % (i, repo),
                    "url": "https://example.invalid/%s/discussions/%d" % (repo, 500 + i),
                    "createdAt": "2020-01-01T00:00:00Z",
                    "updatedAt": "2020-01-01T00:00:00Z",
                    "category": {"name": "General"},
                    "author": {"login": "user-%s" % owner},
                } for i in range(n_extra)],
            }}}}
        else:
            out = {"data": {"repository": {}}}
        print(json.dumps(out))
        return 0

    if argv[0] == "pr" and len(argv) > 1 and argv[1] == "list":
        repo = _repo_from_argv(argv) or "unknown/unknown"
        owner = repo.split("/")[0]
        items = [{
            "number": 9001 + i,
            "title": "Fixture PR %d for %s" % (i, repo),
            "author": {"login": "user-%s" % owner},
            "labels": [],
            "createdAt": "2020-01-01T00:00:00Z",
            "mergedAt": _MERGED_AT,
            "body": "",
            "url": "https://example.invalid/%s/pull/%d" % (repo, 9001 + i),
        } for i in range(_pr_count(repo))]
        print(json.dumps(items))
        return 0

    if argv[0] == "pr" and len(argv) > 1 and argv[1] == "view":
        repo = _repo_from_argv(argv) or "unknown/unknown"
        owner = repo.split("/")[0]
        pr_number = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 0
        out = {
            "number": pr_number,
            "title": "Fixture PR view for %s" % repo,
            "author": {"login": "user-%s" % owner},
            "state": "OPEN",
            "mergedAt": _MERGED_AT,
            "additions": _pr_count(repo),
            "deletions": 1,
            "changedFiles": 1,
            "url": "https://example.invalid/%s/pull/%d" % (repo, pr_number),
            "body": "",
        }
        print(json.dumps(out))
        return 0

    print("gh-blind-shim: unhandled invocation: %s" % (argv,), file=sys.stderr)
    return 1


if __name__ == "__main__":
    if "--self-check" in sys.argv[1:]:
        raise SystemExit(self_check())
    raise SystemExit(main())
'''


class Fixture:
    """Two isolated fixture projects under a temp $HOME, plus a PATH-shimmed
    `gh` — no network, no secrets, no GH_TOKEN (Spec constraints)."""

    def __init__(self):
        self.tmp_root = Path(tempfile.mkdtemp(prefix="rpc-blind-guard-"))
        self._orig_env = dict(os.environ)
        self.projects: dict[str, Path] = {}

    def __enter__(self):
        os.environ["HOME"] = str(self.tmp_root)
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        # This process's own state dir — generic, never one of the fixture
        # project dirs, so nothing this guard writes lands in the operator's
        # production ~/.autonomous-forever-state/.
        own_state = self.tmp_root / ".guard-own-state"
        own_state.mkdir(parents=True, exist_ok=True)
        os.environ["AUTONOMOUS_TEAM_STATE_DIR"] = str(own_state)
        os.environ.pop("STATS_DB_PATH", None)

        bin_dir = self.tmp_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        gh_path = bin_dir / "gh"
        merged_at = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        gh_path.write_text(
            _GH_SHIM.replace("MERGED_AT_PLACEHOLDER", merged_at), encoding="utf-8"
        )
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

        os.environ["GUARD_FIXTURE_PR_COUNTS"] = json.dumps(
            {_repo_slug(p): SEEDS[p]["pr_count"] for p in PROJECTS}
        )

        for name in PROJECTS:
            state_dir = self.tmp_root / f".{name}-state"
            state_dir.mkdir(parents=True, exist_ok=True)
            runtime = {
                "project_name": name,
                "state_dir": str(state_dir),
                "repo": _repo_slug(name),
            }
            (state_dir / "dashboard-runtime.json").write_text(
                json.dumps(runtime), encoding="utf-8"
            )
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
# Seeding — differing data per project, via the production write paths, with
# every distinguishing value an opaque token or a distinct number.
# ---------------------------------------------------------------------------

# One anchor for the whole run, so both fixture projects get byte-identical
# row *timestamps* and differ only in the values attached to them. That is
# what lets a handler taking a timestamp param (loop.iteration_detail) be
# asked the same question of both projects.
_ANCHOR = datetime.now(timezone.utc).replace(microsecond=0)


def _anchored_iso(minutes_ago: int = 0) -> str:
    return (_ANCHOR - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_loop_metrics(state_dir: Path, project: str) -> None:
    """Seed >= 5 rows inside the 24h window with differing idle counts.

    The cache guard's ledger records stats.loop_idle_ratio as non-discriminable
    in *its* harness because one seeded row is below loop_idle_ratio_24h()'s
    5-sample reporting floor, and names this guard as the right place to fix
    that. This is that fix: 6 rows each, 1 idle for one project and 4 for the
    other, so the two ratios differ and the method is genuinely probed here.
    """
    seed = SEEDS[project]
    rows = []
    for i in range(seed["loop_rows"]):
        idle = i < seed["loop_idle"]
        rows.append(json.dumps({
            "timestamp": _anchored_iso(minutes_ago=10 + i),
            "duration_seconds": seed["duration"] + i,
            "agents_spawned": 0 if idle else 2,
            "prs_merged": 0,
            "discussions_scanned": 0,
            "prs_scanned": 0,
            "idle": idle,
            "origin": "cron",
        }))
    (state_dir / "loop-metrics.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _seed_agent_feed(state_dir: Path, project: str) -> None:
    line = json.dumps({
        "timestamp": _anchored_iso(minutes_ago=5),
        "discussion": 500,
        "role": SEEDS[project]["token"],
        "verdict": "pass",
        "pr": None,
    })
    (state_dir / "agent-feed.jsonl").write_text(line + "\n", encoding="utf-8")


def _seed_a2a(state_dir: Path, project: str) -> None:
    a2a_dir = state_dir / "a2a"
    a2a_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": f"guard-{SEEDS[project]['token']}-1",
        "from": SEEDS[project]["token"],
        "to": "team-lead",
        "kind": "status",
        "ts": _anchored_iso(minutes_ago=5),
        "body_sha256": "0" * 64,
    }
    (a2a_dir / "messages.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _seed_cosmetic_blocks(state_dir: Path, project: str) -> None:
    hook_events = state_dir / "hook-events"
    hook_events.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [json.dumps({"ts": _anchored_iso()}) for _ in range(SEEDS[project]["cosmetic"])]
    (hook_events / f"cosmetic-blocks-{today}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _seed_audit_log(state_dir: Path, project: str) -> None:
    """Seed <state_dir>/audit.jsonl with differing dial-change and
    dial-rejection counts — the state-dir half of what stats.dial_usage and
    stats.dial_rejections read."""
    seed = SEEDS[project]
    rows = []
    for _ in range(seed["dial_accepted"]):
        rows.append(json.dumps({
            "kind": "dial_change",
            "timestamp": _anchored_iso(minutes_ago=30),
            "class": "agent.spawn",
        }))
    for _ in range(seed["dial_rejected"]):
        rows.append(json.dumps({
            "kind": "dial_directive_rejected",
            "timestamp": _anchored_iso(minutes_ago=20),
            "class": "agent.spawn",
            "reason": "ceiling_violation",
        }))
    (state_dir / "audit.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _seed_dora_releases(state_dir: Path, project: str) -> None:
    """Seed <project_root>/.autonomous-team/releases/*.json — release
    records analytics_engineer.compute_snapshot() reads for stats.dora
    (D#2518), via the project_root the handler resolves as
    ``state_dir.parent / project`` (backend/rpc/stats_dora.py, same seam
    kpi.history/kpi.cycle_time use — see their ledger entries above).
    Differing release counts per project, each merged well inside the
    trailing-7-day window compute_dora_snapshot() checks, make
    deploy_frequency_per_day genuinely differ rather than needing a ledger
    entry: unlike kpi.history/kpi.cycle_time this data is plain JSON files,
    not a git checkout, so seeding it here is cheap.
    """
    project_root = state_dir.parent / project
    releases_dir = project_root / ".autonomous-team" / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    for i in range(SEEDS[project]["release_count"]):
        record = {
            "id": f"guard-{SEEDS[project]['token']}-{i:03d}",
            "pr_numbers": [GUARD_PR + i],
            "merged_at": _anchored_iso(minutes_ago=120 + i),
            "merge_shas": [],
            "risk": "low",
            "rollback_command": "git revert HEAD --no-edit",
            "runbook_needed": False,
        }
        (releases_dir / f"guard-{SEEDS[project]['token']}-{i:03d}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )


def _seed_auth_retry(project: str, dispatch_scoped, rpc_methods) -> None:
    """auth_retry.record IS the write path — seed auth_retry.summary by
    calling the real RPC a different number of times per project."""
    handler = rpc_methods.get("auth_retry.record")
    if handler is None:
        return
    for _ in range(SEEDS[project]["auth_retries"]):
        dispatch_scoped("auth_retry.record", {"project": project}, handler)


def _seed_duckdb(project: str, rpc_project_scope) -> None:
    """Seed the agent_run table and metric_event rows through the real write
    paths, inside the project's own env scope."""
    import backend.agent_run_tracker as art
    import backend.stats_writer as sw
    from backend.blackboard import Blackboard
    from backend.budget import BudgetTracker

    seed = SEEDS[project]
    token = seed["token"]

    with rpc_project_scope._EnvScope(project):
        # Completed run — feeds runs.by_role / runs.percentiles / runs.recent.
        art.start_run(agent_id=f"guard-{token}", role=GUARD_ROLE, discussion=GUARD_DISCUSSION)
        art.complete_run(
            agent_id=f"guard-{token}",
            duration_s=seed["duration"],
            verdict="pass",
            model="claude-sonnet-4-6",
            input_tok=seed["in_tok"],
            output_tok=seed["out_tok"],
        )
        # Differing numbers of started-but-never-completed runs. These feed
        # runs.stuck (rows with end_ts IS NULL) and runs.active_over_time,
        # which counts runs in flight at each bucket and treats an open run
        # as in flight through now — so "how many are open" is a seeded,
        # non-timestamp difference the bucket counts carry.
        for i in range(seed["open_runs"]):
            art.start_run(
                agent_id=f"guard-{token}-open-{i}",
                role=GUARD_ROLE,
                discussion=GUARD_DISCUSSION,
            )
        # An executor run tagged with a PR, plus a reviewer start a fixed
        # gap later — feeds runs.roundtrip (executor-done -> reviewer-start
        # latency) and, because per_pr_summary() reads PR-tagged agent_run
        # rows, stats.cost_per_outcome's spend half. first_write_turn /
        # total_turns feed stats.pre_write_burn.
        # roundtrip_latency() reads the executor side's end_ts and the
        # reviewer side's start_ts. start_ts is honoured only on
        # complete_run()'s INSERT branch and these rows go through
        # start_run() first (the only way to tag a row with a PR), so the
        # seeded difference is carried by the executor's end_ts: 60s vs 300s
        # back from a shared anchor, a 240s gap no seeding drift approaches.
        exec_end = _ANCHOR - timedelta(seconds=seed["roundtrip_gap_s"])
        art.start_run(
            agent_id=f"guard-{token}-exec",
            role="executor",
            discussion=GUARD_DISCUSSION,
            pr=GUARD_PR,
        )
        art.complete_run(
            agent_id=f"guard-{token}-exec",
            role="executor",
            discussion=GUARD_DISCUSSION,
            start_ts=exec_end - timedelta(seconds=seed["duration"]),
            end_ts=exec_end,
            duration_s=seed["duration"],
            verdict="pass",
            model="claude-sonnet-4-6",
            input_tok=seed["in_tok"],
            output_tok=seed["out_tok"],
            first_write_turn=seed["first_write_turn"],
            total_turns=seed["total_turns"],
            routed_via=seed["routed_via"],
        )
        reviewer_start = _ANCHOR
        art.start_run(
            agent_id=f"guard-{token}-review",
            role="code-reviewer",
            discussion=GUARD_DISCUSSION,
            pr=GUARD_PR,
        )
        art.complete_run(
            agent_id=f"guard-{token}-review",
            role="code-reviewer",
            discussion=GUARD_DISCUSSION,
            start_ts=reviewer_start,
            end_ts=reviewer_start + timedelta(seconds=30),
            duration_s=30.0,
            verdict="pass",
            model="claude-sonnet-4-6",
            input_tok=seed["in_tok"],
            output_tok=seed["out_tok"],
            first_write_turn=seed["first_write_turn"],
            total_turns=seed["total_turns"],
            routed_via=seed["routed_via"],
        )

        BudgetTracker(bb=Blackboard()).record_spend(
            agent_id=f"guard-{token}",
            agent_role=GUARD_ROLE,
            input_tokens=seed["in_tok"],
            output_tokens=seed["out_tok"],
            discussion=GUARD_DISCUSSION,
            model="claude-sonnet-4-6",
        )

        sw.record_iteration_cost(seed["cost"])
        sw.record_cost_spike(value=seed["cost"], mu=seed["cost"] / 2, sigma=1.0)
        for i in range(6):
            sw.record_loop_iter(
                duration_s=seed["duration"],
                team_lead_input_tokens=seed["in_tok"] * (i + 1),
                team_lead_output_tokens=seed["out_tok"] * (i + 1),
            )
            time.sleep(0.002)  # distinct PK timestamps (ms precision)

        # >= 5 role_verdict samples per role, differing pass/fail mix — the
        # reporting floor role_success_rate_24h()/role_retry_rate_24h() apply.
        for verdict in seed["verdicts"]:
            sw.emit_verdict(role=GUARD_ROLE, verdict=verdict)
            time.sleep(0.002)

        # A metric name unique to this project. stats.freshness_list's only
        # per-project fields are last_ts and age_seconds — both in the
        # volatile class this guard blanks — so without a differing metric
        # *name* its row set is identical for any two projects whatever it
        # read. The token is opaque, not the project name (module docstring,
        # point 1).
        sw.record(
            metric=f"guard_probe_{token}",
            value=1.0,
            unit="event",
            tags={},
            source="guard-fixture",
        )

        # >= 5 fix_rounds_per_pr rows, differing distributions.
        for rounds in seed["fix_rounds"]:
            sw.record(
                metric="fix_rounds_per_pr",
                value=float(rounds),
                unit="rounds",
                tags={"pr": str(GUARD_PR)},
                source="guard-fixture",
            )
            time.sleep(0.002)


def seed_all(fixture: Fixture, rpc_project_scope, dispatch_scoped, rpc_methods) -> None:
    for project in PROJECTS:
        state_dir = fixture.state_dir(project)
        _seed_loop_metrics(state_dir, project)
        _seed_agent_feed(state_dir, project)
        _seed_a2a(state_dir, project)
        _seed_cosmetic_blocks(state_dir, project)
        _seed_audit_log(state_dir, project)
        _seed_dora_releases(state_dir, project)
        _seed_duckdb(project, rpc_project_scope)
        _seed_auth_retry(project, dispatch_scoped, rpc_methods)


# ---------------------------------------------------------------------------
# Extra per-method params (project is added by the caller)
# ---------------------------------------------------------------------------

EXTRA_PARAMS: dict[str, dict] = {
    "discussions.get": {"number": 500},
    "dashboard.pr_detail": {"pr_number": GUARD_PR},
    "runs.by_role": {"role": GUARD_ROLE},
    "runs.roundtrip": {"pr": GUARD_PR},
    "runs.stuck": {"threshold_seconds": 0},
    "cost.per_discussion": {"discussion": GUARD_DISCUSSION},
    "stats.series": {"name": "iteration_cost_usd"},
    # Both projects seed a loop-metrics row at this exact timestamp with
    # different values attached — the same question asked of both.
    "loop.iteration_detail": {"timestamp": _anchored_iso(minutes_ago=10)},
}

# Write endpoints whose return value legitimately changes on every call
# (a monotonic counter): they would read as "discriminating" for a reason
# that has nothing to do with the project, which is a false pass, not a
# false alarm. Excluded from the subject set and named here so the exclusion
# is reviewable rather than silent.
MUTATING: dict[str, str] = {
    "auth_retry.record": (
        "a write endpoint returning a monotonically-increasing counter — two "
        "calls differ because the counter advanced, not because the projects "
        "differ, so this probe cannot say anything about it either way. "
        "auth_retry.summary, the read side a project-blind implementation "
        "would actually surface through, IS probed and is seeded by these "
        "very calls."
    ),
}


def clear_all_caches() -> None:
    """Clear module-level dict caches across backend.* so a cached answer
    from the previous project is never mistaken for a fresh identical one —
    caching is the *other* guard's subject, and leaving a warm cache here
    would let a leak masquerade as discrimination."""
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
    values. An exception is a legitimate answer here: a method that declines
    for one project and answers for the other IS discriminating, and a method
    that raises the same normalized error for both is not."""
    try:
        return ("ok", _normalize(dispatch_scoped(method, params, handler)))
    except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
        return ("error", _scrub_project_names(f"{type(exc).__name__}: {exc}"))


def probe_method(dispatch_scoped, method, handler, extra_params):
    """Return (outcome, detail) for one method.

    Calls the same project twice before comparing projects. A response that
    is not even equal to itself carries something the comparison cannot
    attribute — a live clock in a field the volatile rule missed, a counter,
    a pid — and "A differs from B" then proves nothing about scoping. That
    is reported as `unstable` and needs a ledger entry, rather than being
    counted as a pass. Without this check the only defence is the volatile
    regex, and a regex only blanks the fields someone thought of:
    stats_duckdb_writers' `checked_at` sat outside it and made that method
    look scoped on any run whose two calls straddled a second boundary.
    """
    params_a = {"project": "alpha", **extra_params}
    params_b = {"project": "beta", **extra_params}

    clear_all_caches()
    ans_a1 = safe_call(dispatch_scoped, method, params_a, handler)
    clear_all_caches()
    ans_a2 = safe_call(dispatch_scoped, method, params_a, handler)
    if ans_a1 != ans_a2:
        return "unstable", (
            f"two calls for the same project disagreed: {ans_a1!r} then {ans_a2!r}"
        )

    clear_all_caches()
    ans_b = safe_call(dispatch_scoped, method, params_b, handler)

    if ans_a1 == ans_b:
        return "blind", f"both projects answered {ans_a1!r}"
    return "discriminating", None


def run_with_timeout(fn, timeout_s):
    """Run *fn* with a hard wall-clock timeout (SIGALRM — POSIX, matching the
    ubuntu-latest CI runner)."""
    import signal

    if not hasattr(signal, "SIGALRM"):
        return fn()

    def _handler(signum, frame):
        raise _Timeout()

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout_s)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _probe_once(dispatch_scoped, method, handler, extra):
    try:
        return run_with_timeout(
            lambda: probe_method(dispatch_scoped, method, handler, extra),
            PER_METHOD_TIMEOUT_S,
        )
    except _Timeout:
        return "timeout", f"exceeded {PER_METHOD_TIMEOUT_S}s"
    except Exception:
        return "crashed", traceback.format_exc()


def _probe(dispatch_scoped, method, handler, extra):
    """Probe one method, re-probing once on `unstable`.

    A response that varies between two identical calls is either a genuine
    property of the handler (a counter, a live clock) — in which case it
    repeats — or a one-off boundary crossing that happened to fall between
    two calls microseconds apart. Failing the build on the second kind is
    the "goes red at random" failure mode, so `unstable` has to survive a
    second independent probe before it is reported. Nothing else is retried:
    a `blind` or `timeout` verdict is not made truer by asking twice.
    """
    outcome, detail = _probe_once(dispatch_scoped, method, handler, extra)
    if outcome != "unstable":
        return outcome, detail
    return _probe_once(dispatch_scoped, method, handler, extra)


def self_check() -> int:
    """Seed both fixture projects IDENTICALLY and report what still claims
    to discriminate.

    This is the falsification run for the guard's own comparison. With the
    same data on both sides, a method that still answers differently is
    answering on something other than project data — a wall-clock write time
    in a field :data:`_VOLATILE_KEY_RE` does not recognise. The honest count
    is zero; anything above it names a field that rule has to learn.

    It lives here rather than in someone's scratch directory because the
    hole it finds was in this file, and the only reason it was found is that
    someone thought to seed both sides the same. That should not have to be
    re-invented.

    The loop-metrics seed is deliberately left differing so the sighted
    canary still lands and the run reaches the real methods; its three
    consumers are expected to discriminate and are named in the output.
    """
    import copy

    expected = {"stats.loop_idle_ratio", "loop.timeline", "loop.iteration_detail"}
    keep = {k: SEEDS["beta"][k] for k in ("loop_rows", "loop_idle")}
    SEEDS["beta"] = copy.deepcopy(SEEDS["alpha"])
    SEEDS["beta"].update(keep)

    print(
        "self-check: both projects seeded identically apart from "
        "loop-metrics (kept differing so the sighted canary lands).\n"
        "self-check: expected discriminating set is at most "
        + ", ".join(sorted(expected))
        + "\n"
    )
    _SELF_CHECK_EXPECTED.update(expected)
    return main()


# Populated by self_check(); empty on a normal run.
_SELF_CHECK_EXPECTED: set[str] = set()


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))

    import backend.server as server
    import backend.rpc_project_scope as rpc_project_scope

    classifications = rpc_project_scope.all_classifications()
    if len(classifications) < rpc_project_scope.MIN_REGISTRY_SIZE:
        print(
            f"FAIL registry-too-small: all_classifications() returned "
            f"{len(classifications)} entries, expected >= "
            f"{rpc_project_scope.MIN_REGISTRY_SIZE} — an import failure or an "
            "empty enumeration must not pass as 'everything is discriminating'"
        )
        return 1

    # Spec item 17: the subject set is derived at runtime, never hardcoded, so
    # a newly-added SCOPED method is probed (or must be ledgered) with no edit
    # to this file.
    scoped_methods = sorted(
        m for m, (kind, _reason) in classifications.items()
        if kind == rpc_project_scope.SCOPED and m not in MUTATING
    )

    dispatch_scoped = rpc_project_scope.dispatch_scoped

    with Fixture() as fixture:
        try:
            seed_all(fixture, rpc_project_scope, dispatch_scoped, server._RPC_METHODS)
        except Exception:
            print("FAIL fixture-seed-error:")
            traceback.print_exc()
            return 1

        # --- Canaries (Spec item 14) --------------------------------------
        canary_specs = (
            (CANARY_BLIND, _canary_blind_handler, "blind",
             "reads the serving checkout's loop-metrics.jsonl for every "
             "project — the exact pre-D#2327 bug shape"),
            (CANARY_SIGHTED, _canary_sighted_handler, "discriminating",
             "reads the requested project's own loop-metrics.jsonl — proves "
             "the fixture seeds data that actually discriminates"),
        )
        for name, handler, expected, why in canary_specs:
            server._RPC_METHODS[name] = handler
            rpc_project_scope._CLASSIFICATIONS[name] = (
                rpc_project_scope.SCOPED,
                f"synthetic canary injected at runtime by "
                f"rpc-project-blind-guard.py — {why}; never present on a "
                "clean tree",
            )
            try:
                outcome, detail = _probe(dispatch_scoped, name, handler, {})
            finally:
                del server._RPC_METHODS[name]
                del rpc_project_scope._CLASSIFICATIONS[name]
            if outcome != expected:
                print(
                    f"FAIL canary-wrong-outcome: {name} came back {outcome!r}, "
                    f"expected {expected!r} — this guard cannot be trusted on "
                    f"this run (detail: {detail!r})"
                )
                return 1
            print(f"canary: {name} -> {outcome} (as expected)")

        # --- Probe every SCOPED method -------------------------------------
        failures: list[str] = []
        ledgered: list[str] = []
        passed: list[str] = []

        for method in scoped_methods:
            handler = server._RPC_METHODS.get(method)
            if handler is None:
                failures.append(
                    f"missing-handler: {method} classified SCOPED but not "
                    "registered in _RPC_METHODS"
                )
                continue
            outcome, detail = _probe(
                dispatch_scoped, method, handler, EXTRA_PARAMS.get(method, {})
            )
            if outcome == "discriminating":
                passed.append(method)
            elif outcome in ("blind", "unstable"):
                if method in NON_DISCRIMINABLE:
                    ledgered.append(method)
                else:
                    tag = "project-blind" if outcome == "blind" else "unstable-response"
                    failures.append(f"{tag}: {method} — {detail}")
            elif outcome == "timeout":
                failures.append(f"timeout: {method} {detail}")
            else:
                failures.append(f"probe-crashed: {method}\n{detail}")

        print(
            f"rpc-project-blind-guard: {len(scoped_methods)} SCOPED methods "
            f"probed — {len(passed)} discriminating, {len(ledgered)} ledgered, "
            f"{len(failures)} failing"
        )
        if passed:
            print("  discriminating: " + ", ".join(passed))
        if ledgered:
            print("  ledgered (identical answers, reviewed reason on file): "
                  + ", ".join(ledgered))
        if MUTATING:
            print("  excluded (mutating write endpoints): "
                  + ", ".join(sorted(MUTATING)))

        if _SELF_CHECK_EXPECTED:
            # Self-check run: `blind` everywhere is the PASS condition, so
            # the normal failure list is not the verdict. What matters is
            # whether anything outside the loop-metrics consumers still
            # claims to discriminate on identical data.
            unexplained = sorted(set(passed) - _SELF_CHECK_EXPECTED)
            if unexplained:
                print()
                print(
                    "FAIL self-check: %d method(s) discriminate on identical "
                    "seeds — each is answering on something other than "
                    "project data, and _VOLATILE_KEY_RE has to learn the "
                    "field: %s" % (len(unexplained), ", ".join(unexplained))
                )
                return 1
            print("self-check: no method discriminates on identical seeds")
            return 0

        if failures:
            print()
            for f in failures:
                print(f"FAIL {f}")
            return 1

        print("rpc-project-blind-guard: all clear")
        return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv[1:]:
        raise SystemExit(self_check())
    raise SystemExit(main())
