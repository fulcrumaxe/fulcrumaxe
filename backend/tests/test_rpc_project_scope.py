"""
Recurrence guard + behavior tests for backend/rpc_project_scope.py
(D#2261 PR-a and PR-b).

Covers Spec (Acceptance) items 1-10:
  1. This file exits 0 under `python3 -m pytest backend/tests/test_rpc_project_scope.py -q`.
  2. Registry completeness — every backend.server._RPC_METHODS key is classified
     exactly once. Mirrors the runtime-parse pattern in
     test_legacy_route_coverage.py: enumerate the real registry, not a
     hand-copied list, so a new @_rpc_method handler that forgets to classify
     itself fails this test rather than silently shipping unscoped.
  3. Sanity floor — the enumerated registry has >= MIN_REGISTRY_SIZE entries,
     so a broken enumeration (e.g. an import failure yielding an empty dict)
     can't pass silently.
  4. Every GLOBAL/UNSCOPABLE classification carries a non-empty reason string.
  5. Fail-closed — calling circuitBreaker.history (UNSCOPABLE) with
     project=<other> returns a JSON-RPC error and never touches the
     underlying handler. (PR-a's version of this test used agents.tail as
     the exemplar; PR-b de-anchors agents.tail to SCOPED — see items 8-10
     below — so the fail-closed exemplar moved to a method that's still
     genuinely blocked.)
  6. No-regression — every SCOPED/GLOBAL method called with no project param
     passes through to the handler unchanged.
  7. Concurrency — two simultaneous different-project requests never see each
     other's data, repeated well past the 20-iteration floor.
  8. Live probe (PR-b) — agents.tail with project=gatekeep, against a real
     constructed project layout, returns no event predating the project's
     own data — specifically none of the engine's own pre-existing feed.
  9. Both dispatchers (PR-b) — server.py's do_POST and routers/rpc.py's
     ASGI route both delegate to the same dispatch_scoped(); a fix landing
     on only one can't happen because there's only one call to diverge from.
  10. De-anchoring (PR-b) — none of the four class-(c) handlers named in the
      Discussion's table (agents.tail, loop.events, dashboard.pr_list,
      dashboard.pr_detail) are still classified UNSCOPABLE.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend import rpc_project_scope as scope

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT_FOR_SUBPROCESS = _BACKEND_DIR.parent


# ---------------------------------------------------------------------------
# Items 2-4: registry completeness against the REAL _RPC_METHODS registry.
# ---------------------------------------------------------------------------


def _live_rpc_methods() -> dict:
    """Import backend.server and return its live _RPC_METHODS registry.

    A plain import (not an AST parse) is deliberate here: _RPC_METHODS is
    populated by @_rpc_method decorators running at import time, so importing
    the module IS the runtime enumeration — there's no separate "declared vs
    registered" gap the way there is for api.py's string-literal routes in
    test_legacy_route_coverage.py.
    """
    from backend import server as server_mod

    return server_mod._RPC_METHODS


def test_sanity_floor_on_enumeration():
    """A broken enumeration returning an empty/tiny set must not pass silently."""
    methods = _live_rpc_methods()
    assert len(methods) >= scope.MIN_REGISTRY_SIZE, (
        f"expected >= {scope.MIN_REGISTRY_SIZE} registered RPC methods, "
        f"got {len(methods)} — enumeration may be broken"
    )


def test_every_registered_method_is_classified_exactly_once():
    """Every _RPC_METHODS key must appear in the classification registry.

    Not bypassable by omission: a new @_rpc_method handler that never gets a
    line in rpc_project_scope._CLASSIFICATIONS fails this test immediately,
    the same guarantee test_legacy_route_coverage.py gives for api.py routes.
    """
    methods = _live_rpc_methods()
    classifications = scope.all_classifications()

    unclassified = sorted(set(methods) - set(classifications))
    assert not unclassified, (
        f"RPC methods missing a project-scope classification: {unclassified} "
        "— add an entry to rpc_project_scope._CLASSIFICATIONS"
    )

    # Classification registry should not carry stale entries for methods that
    # no longer exist either — keeps the registry honest as handlers are removed.
    stale = sorted(set(classifications) - set(methods))
    assert not stale, (
        f"rpc_project_scope classifies methods no longer in _RPC_METHODS: {stale}"
    )

    for method, classification in classifications.items():
        assert isinstance(classification, tuple) and len(classification) == 2, (
            f"{method!r} classification must be a (kind, reason) tuple"
        )
        kind, _reason = classification
        assert kind in (scope.SCOPED, scope.GLOBAL, scope.UNSCOPABLE), (
            f"{method!r} has unknown classification kind {kind!r}"
        )


def test_global_and_unscopable_reasons_are_non_empty():
    """Every GLOBAL/UNSCOPABLE entry must carry a written reason (Decision 2)."""
    for method, (kind, reason) in scope.all_classifications().items():
        if kind in (scope.GLOBAL, scope.UNSCOPABLE):
            assert reason and reason.strip(), (
                f"{method!r} is classified {kind} but has an empty reason string"
            )


# ---------------------------------------------------------------------------
# Item 5: fail-closed for UNSCOPABLE methods.
# ---------------------------------------------------------------------------


def test_unscopable_method_refuses_with_project_param():
    """circuitBreaker.history (UNSCOPABLE) must refuse rather than serve this
    process's own data under a different project's name.

    PR-a's version of this test used agents.tail as the UNSCOPABLE exemplar;
    PR-b de-anchors agents.tail to SCOPED (see the live-probe tests below),
    so the exemplar moved to a handler that's still genuinely blocked:
    backend.circuit_breaker is cached in sys.modules after first import, and
    its _HISTORY_FILE constant is bound to this checkout's __file__ at that
    import — de-anchoring the path doesn't help once it's already cached.
    """
    kind, _reason = scope.classification_for("circuitBreaker.history")
    assert kind == scope.UNSCOPABLE

    def _handler_should_never_be_called(_params: dict) -> dict:
        raise AssertionError(
            "handler must not be invoked for an UNSCOPABLE method with a "
            "foreign project param — refusal must happen before dispatch"
        )

    with pytest.raises(scope.UnscopableMethodError) as exc_info:
        scope.dispatch_scoped(
            "circuitBreaker.history",
            {"project": "gatekeep"},
            _handler_should_never_be_called,
        )
    # Must carry an rpc_code so both dispatch sites' `getattr(exc, "rpc_code", ...)`
    # surfaces this as a non-null JSON-RPC error rather than a generic -32000.
    assert hasattr(exc_info.value, "rpc_code")


def test_every_unscopable_method_refuses_with_project_param():
    """Generalizes the agents.tail check to every UNSCOPABLE method."""
    for method, (kind, _reason) in scope.all_classifications().items():
        if kind != scope.UNSCOPABLE:
            continue

        def _boom(_params: dict) -> dict:
            raise AssertionError(f"{method} handler must not be invoked")

        with pytest.raises(scope.UnscopableMethodError):
            scope.dispatch_scoped(method, {"project": "some-other-project"}, _boom)


def test_unscopable_method_without_project_param_passes_through():
    """The engine's own dashboard (no project param) must be unaffected."""
    sentinel = {"history": ["engine's own data"]}

    def _handler(_params: dict) -> dict:
        return sentinel

    result = scope.dispatch_scoped("circuitBreaker.history", {}, _handler)
    assert result is sentinel


# ---------------------------------------------------------------------------
# Item 6: no-regression for SCOPED/GLOBAL methods with no project param.
# ---------------------------------------------------------------------------


def test_scoped_and_global_methods_pass_through_unchanged_without_project():
    """Every SCOPED/GLOBAL method, called with no project param, must reach
    the handler untouched and return its result unchanged — identical in
    shape to pre-change behavior.
    """
    for method, (kind, _reason) in scope.all_classifications().items():
        if kind not in (scope.SCOPED, scope.GLOBAL):
            continue

        sentinel = {"ok": True, "method": method}

        def _handler(_params: dict, _sentinel=sentinel) -> dict:
            return _sentinel

        result = scope.dispatch_scoped(method, {}, _handler)
        assert result is sentinel, f"{method}: dispatch_scoped altered a no-project call"


def test_circuit_breaker_summary_is_unscopable_not_scoped():
    """Regression test for the code-review finding on PR #2266.

    circuit_breaker.summary's own handler (_rpc_circuit_breaker_summary in
    backend/server.py) has a 30s TTL cache keyed on the method name alone —
    `cache_key = ("circuit_breaker.summary",)`, no project in the key. The
    underlying subprocess genuinely is env-addressable (it inherits our
    narrow env override and re-resolves Blackboard() at its own fresh
    import), but a cache hit within the 30s window bypasses that subprocess
    — and the env override with it — entirely, silently returning whichever
    project last populated the shared cache. That's exactly the
    "plausible, populated, wrong" bug class D#2261 exists to close, just
    probabilistic instead of deterministic.

    This method must stay UNSCOPABLE until the cache is keyed on
    (method, project) too. This test exists so a future change that flips
    it back to SCOPED without fixing the cache gets caught here rather than
    by a second independent code review.
    """
    kind, reason = scope.classification_for("circuit_breaker.summary")
    assert kind == scope.UNSCOPABLE
    assert "cache" in reason.lower(), (
        "reason string should explain the actual blocker (the project-blind "
        "cache), not just restate the classification"
    )

    def _handler_should_never_be_called(_params: dict) -> dict:
        raise AssertionError(
            "handler (and its cache) must not be invoked for a foreign project"
        )

    with pytest.raises(scope.UnscopableMethodError):
        scope.dispatch_scoped(
            "circuit_breaker.summary",
            {"project": "gatekeep"},
            _handler_should_never_be_called,
        )


def test_unclassified_method_fails_closed():
    """Defense in depth: a method with no registry entry must refuse, not
    silently serve unscoped data, even though this is unreachable in
    practice (test_every_registered_method_is_classified_exactly_once above
    guarantees full coverage of the real registry).
    """
    def _boom(_params: dict) -> dict:
        raise AssertionError("handler must not be invoked for an unclassified method")

    with pytest.raises(scope.UnclassifiedMethodError):
        scope.dispatch_scoped("totally.made.up.method", {"project": "x"}, _boom)


# ---------------------------------------------------------------------------
# Item 7: concurrency — two simultaneous different-project requests must
# never see each other's data. Repeated well past the 20-iteration floor.
# ---------------------------------------------------------------------------


@pytest.fixture
def two_project_state_dirs(tmp_path, monkeypatch):
    """Build two fake projects under tmp_path, each with a distinctly-marked
    a2a/messages.jsonl, and redirect Path.home() so state_paths.for_project()
    resolves into tmp_path instead of the real home directory.

    a2a.tail is the concurrency probe: it's SCOPED via a function-local
    `from state_paths import STATE_DIR` (resolved fresh on every call), so
    it genuinely exercises the AUTONOMOUS_TEAM_STATE_DIR override rather than
    a handler that would pass this test even with no scoping at all.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    for name, marker in (("projA", "PROJECT_A_SECRET"), ("projB", "PROJECT_B_SECRET")):
        a2a_dir = tmp_path / f".{name}-state" / "a2a"
        a2a_dir.mkdir(parents=True)
        line = json.dumps({
            "id": f"{name}-msg-1",
            "from": "agent-x",
            "to": "agent-y",
            "kind": "note",
            "body_sha256": marker,
            "ts": "2026-09-03T00:00:00Z",
        })
        (a2a_dir / "messages.jsonl").write_text(line + "\n", encoding="utf-8")

    return tmp_path


def test_a2a_tail_is_classified_scoped():
    """Guard the concurrency test's premise: if a2a.tail's classification
    ever changes, this test should fail loudly rather than the concurrency
    test below silently testing nothing meaningful.
    """
    kind, _reason = scope.classification_for("a2a.tail")
    assert kind == scope.SCOPED


def test_concurrent_cross_project_calls_never_cross_contaminate(two_project_state_dirs):
    from backend.rpc import a2a_tail

    REPEATS = 25  # > the 20-iteration floor required by Spec item 7
    barrier = threading.Barrier(2)
    results: dict[str, list] = {"projA": [], "projB": []}
    errors: list[BaseException] = []

    def _worker(project: str) -> None:
        try:
            for _ in range(REPEATS):
                barrier.wait(timeout=5)  # maximize actual thread overlap
                result = scope.dispatch_scoped(
                    "a2a.tail",
                    {"project": project},
                    a2a_tail.handle,
                )
                results[project].append(result)
        except BaseException as exc:  # noqa: BLE001 — surface in main thread
            errors.append(exc)

    t_a = threading.Thread(target=_worker, args=("projA",))
    t_b = threading.Thread(target=_worker, args=("projB",))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert len(results["projA"]) == REPEATS
    assert len(results["projB"]) == REPEATS

    for result in results["projA"]:
        entries = result["entries"]
        assert entries, "projA call returned no entries"
        for entry in entries:
            assert entry["body_sha256"] == "PROJECT_A_SECRET", (
                f"projA call leaked foreign data: {entry}"
            )

    for result in results["projB"]:
        entries = result["entries"]
        assert entries, "projB call returned no entries"
        for entry in entries:
            assert entry["body_sha256"] == "PROJECT_B_SECRET", (
                f"projB call leaked foreign data: {entry}"
            )


# ---------------------------------------------------------------------------
# Item 10: none of the class-(c) handlers named in the Discussion's table
# are still classified UNSCOPABLE after PR-b's de-anchoring.
# ---------------------------------------------------------------------------


def test_class_c_handlers_are_no_longer_unscopable():
    """agents.tail, loop.events, dashboard.pr_list, and dashboard.pr_detail
    were UNSCOPABLE after PR-a (import-time-bound feed path / repo slug).
    PR-b de-anchors each at its source in backend/server.py and reclassifies
    all four SCOPED here.
    """
    for method in ("agents.tail", "loop.events", "dashboard.pr_list", "dashboard.pr_detail"):
        kind, reason = scope.classification_for(method)
        assert kind == scope.SCOPED, (
            f"{method} is still {kind!r} after PR-b — expected SCOPED"
        )
        assert reason and reason.strip()


# ---------------------------------------------------------------------------
# Item 9: both dispatch sites delegate to the same dispatch_scoped(). A fix
# landing on only one dispatcher must be structurally impossible to miss.
# ---------------------------------------------------------------------------


def test_both_dispatch_sites_call_dispatch_scoped():
    server_src = (_BACKEND_DIR / "server.py").read_text(encoding="utf-8")
    rpc_router_src = (_BACKEND_DIR / "routers" / "rpc.py").read_text(encoding="utf-8")

    assert "_rpc_project_scope.dispatch_scoped(method, params, handler)" in server_src, (
        "backend/server.py's legacy do_POST must call handler(params) through "
        "rpc_project_scope.dispatch_scoped(), not directly"
    )
    assert "_rpc_project_scope.dispatch_scoped(method, params, handler)" in rpc_router_src, (
        "backend/routers/rpc.py's ASGI POST /rpc route must call handler(params) "
        "through rpc_project_scope.dispatch_scoped(), not directly"
    )


# ---------------------------------------------------------------------------
# Item 8: live probe — agents.tail with project=gatekeep must never return an
# event from the engine's own feed. Reproduces the reported bug's shape with
# a real (constructed) project layout and a real dispatch_scoped() call,
# rather than asserting on how the path string is built (D#2149).
# ---------------------------------------------------------------------------


def test_agents_tail_project_scoped_excludes_engine_feed(tmp_path, monkeypatch):
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    # The engine's own agent-feed.jsonl — before PR-b, agents.tail read this
    # unconditionally regardless of the project param (AGENT_FEED_PATH was a
    # module constant bound to this checkout at import). Reproduces the
    # reported bug's actual event shape (2026-07-24, spawn_attempt).
    engine_feed = tmp_path / "engine-checkout" / ".autonomous-team" / "agent-feed.jsonl"
    engine_feed.parent.mkdir(parents=True)
    engine_feed.write_text(
        json.dumps({
            "timestamp": "2026-07-24T07:30:15Z",
            "event_type": "spawn_attempt",
            "role": "code-reviewer",
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(srv, "AGENT_FEED_PATH", engine_feed)

    # gatekeep's own repo checkout — first commit 2026-09-02, so its own feed
    # only ever has events from that date onward.
    gatekeep_feed = tmp_path / "gatekeep" / ".autonomous-team" / "agent-feed.jsonl"
    gatekeep_feed.parent.mkdir(parents=True)
    gatekeep_feed.write_text(
        json.dumps({
            "timestamp": "2026-09-02T10:00:00Z",
            "event_type": "spawn_attempt",
            "role": "executor",
        }) + "\n",
        encoding="utf-8",
    )

    result = scope.dispatch_scoped(
        "agents.tail",
        {"project": "gatekeep", "limit": 50},
        srv._RPC_METHODS["agents.tail"],
    )

    events = result["events"]
    assert events, "expected gatekeep's own event to come back"
    for ev in events:
        ts = ev.get("timestamp") or ev.get("ts") or ""
        assert ts >= "2026-09-02", (
            f"agents.tail project=gatekeep returned a pre-2026-09-02 event: "
            f"{ev!r} — this is the engine's own feed leaking under gatekeep's name"
        )
    assert all(ev.get("role") == "executor" for ev in events), (
        "returned an event that didn't come from gatekeep's own feed"
    )


def test_agents_tail_no_project_still_reads_engine_feed(tmp_path, monkeypatch):
    """No-regression companion to the probe above: the engine's own dashboard
    (no project param) must be unaffected by _agent_feed_path()'s new
    per-project resolution.
    """
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    engine_feed = tmp_path / "engine-checkout" / ".autonomous-team" / "agent-feed.jsonl"
    engine_feed.parent.mkdir(parents=True)
    engine_feed.write_text(
        json.dumps({
            "timestamp": "2026-07-24T07:30:15Z",
            "event_type": "spawn_attempt",
            "role": "code-reviewer",
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(srv, "AGENT_FEED_PATH", engine_feed)

    result = scope.dispatch_scoped(
        "agents.tail", {"limit": 50}, srv._RPC_METHODS["agents.tail"],
    )
    events = result["events"]
    assert any(ev.get("timestamp") == "2026-07-24T07:30:15Z" for ev in events), (
        "engine's own dashboard (no project param) stopped seeing its own feed"
    )


def test_agent_feed_path_helper_resolution(tmp_path, monkeypatch):
    """Unit-level check on _agent_feed_path() itself, independent of any one
    RPC handler: no project -> AGENT_FEED_PATH; a project with its own repo
    checkout -> that checkout's .autonomous-team/agent-feed.jsonl.
    """
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert srv._agent_feed_path(None) == srv.AGENT_FEED_PATH
    assert srv._agent_feed_path("") == srv.AGENT_FEED_PATH

    expected = tmp_path / "someproj" / ".autonomous-team" / "agent-feed.jsonl"
    assert srv._agent_feed_path("someproj") == expected


# ---------------------------------------------------------------------------
# dashboard.pr_list / dashboard.pr_detail — real dispatch_scoped() calls with
# a fake `gh` subprocess, confirming the resolved project repo (not the
# engine's own _GH_REPO) is what actually gets shelled out to, and that the
# pr_list TTL cache can't repeat the circuit_breaker.summary cache bug.
# ---------------------------------------------------------------------------


def _write_project_repo(tmp_path: Path, name: str, repo: str) -> None:
    state_dir = tmp_path / f".{name}-state"
    state_dir.mkdir()
    (state_dir / "dashboard-runtime.json").write_text(json.dumps({"repo": repo}))


def test_dashboard_pr_list_uses_resolved_project_repo_and_keys_cache_on_it(
    tmp_path, monkeypatch,
):
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("AF_E2E_FIXTURES", raising=False)
    srv._PR_LIST_CACHE.clear()

    _write_project_repo(tmp_path, "gatekeep", "acme/gatekeep")
    _write_project_repo(tmp_path, "otherproj", "acme/otherproj")

    captured_repos: list[str] = []

    class _FakeResult:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            captured_repos.append(cmd[cmd.index("--repo") + 1])
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = scope.dispatch_scoped(
        "dashboard.pr_list", {"project": "gatekeep"}, srv._RPC_METHODS["dashboard.pr_list"],
    )
    assert result == []
    assert captured_repos == ["acme/gatekeep"]

    # A different project, called immediately after (well inside the 30s
    # TTL), must NOT be served gatekeep's cached (empty, here) result for
    # its own repo — it must issue its own `gh pr list --repo` call. This is
    # exactly the bug class that kept circuit_breaker.summary UNSCOPABLE
    # (a cache keyed without project silently serving a foreign project's
    # cached response).
    scope.dispatch_scoped(
        "dashboard.pr_list", {"project": "otherproj"}, srv._RPC_METHODS["dashboard.pr_list"],
    )
    assert captured_repos == ["acme/gatekeep", "acme/otherproj"], (
        "dashboard.pr_list did not issue a fresh gh call for a different "
        "project within the cache TTL — the cache key isn't keyed on repo"
    )

    # Calling gatekeep again within the TTL DOES hit its own cache (no third
    # subprocess call) — the cache still works, just correctly scoped now.
    scope.dispatch_scoped(
        "dashboard.pr_list", {"project": "gatekeep"}, srv._RPC_METHODS["dashboard.pr_list"],
    )
    assert captured_repos == ["acme/gatekeep", "acme/otherproj"], (
        "expected gatekeep's second call to be served from its own cache entry"
    )


def test_dashboard_pr_detail_uses_resolved_project_repo(tmp_path, monkeypatch):
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("AF_E2E_FIXTURES", raising=False)

    _write_project_repo(tmp_path, "gatekeep", "acme/gatekeep")

    captured: list[list[str]] = []

    class _FakeResult:
        returncode = 1  # PR not found -- short-circuits before Blackboard/CostTracker
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = scope.dispatch_scoped(
        "dashboard.pr_detail",
        {"pr_number": 42, "project": "gatekeep"},
        srv._RPC_METHODS["dashboard.pr_detail"],
    )
    assert result == {"error": "not_found"}
    assert captured, "gh pr view was never called"
    view_cmd = captured[0]
    assert view_cmd[:3] == ["gh", "pr", "view"]
    assert view_cmd[view_cmd.index("--repo") + 1] == "acme/gatekeep", (
        f"dashboard.pr_detail queried {view_cmd!r} instead of the resolved "
        "project repo"
    )


# ---------------------------------------------------------------------------
# Code-review fix (PR #2305): discussions.get's nested pr.info cache was
# keyed on linked_pr_num alone, not on the resolved repo. Two different
# projects whose repos each have their own "PR #42" would share the cache
# entry -- the second project's discussions.get call would silently return
# the first project's cached PR info. This is the fourth instance of the
# same bug class (circuit_breaker.summary, dashboard.pr_list, the
# agent-feed.jsonl nested read, and now this) -- a cache keyed without a
# project-derived component sitting underneath otherwise-correct scoping.
# ---------------------------------------------------------------------------


def test_discussions_get_pr_info_cache_keyed_on_repo(tmp_path, monkeypatch):
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _write_project_repo(tmp_path, "projA", "acme/projA")
    _write_project_repo(tmp_path, "projB", "acme/projB")

    srv._DISCUSSIONS_CACHE.clear()

    def _fake_graphql(query, variables=None):
        if "pullRequest" in query:
            if 'name: "projA"' in query:
                return {"data": {"repository": {"pullRequest": {
                    "number": 42, "url": "http://x/projA/42", "state": "OPEN",
                    "labels": {"nodes": []},
                }}}}
            if 'name: "projB"' in query:
                return {"data": {"repository": {"pullRequest": {
                    "number": 42, "url": "http://x/projB/42", "state": "MERGED",
                    "labels": {"nodes": []},
                }}}}
            return {"data": {"repository": {"pullRequest": None}}}
        # discussion query -- same discussion #1 in both repos, linking PR #42
        # via the STATUS-line convention _extract_linked_pr() actually parses.
        return {"data": {"repository": {"discussion": {
            "number": 1, "title": "t",
            "body": "<!-- STATUS:MERGED PR:#42 -->",
            "url": "http://x",
            "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
            "author": {"login": "a"}, "category": {"name": "c"},
            "comments": {"nodes": []},
        }}}}

    monkeypatch.setattr(srv, "_gh_graphql", _fake_graphql)

    result_a = scope.dispatch_scoped(
        "discussions.get", {"number": 1, "project": "projA"}, srv._RPC_METHODS["discussions.get"],
    )
    result_b = scope.dispatch_scoped(
        "discussions.get", {"number": 1, "project": "projB"}, srv._RPC_METHODS["discussions.get"],
    )

    assert result_a["linked_pr"]["url"] == "http://x/projA/42"
    assert result_b["linked_pr"]["url"] == "http://x/projB/42", (
        "projB's discussions.get returned projA's cached pr.info -- the "
        "nested pr.info cache key must include repo_owner/repo_name"
    )


# ---------------------------------------------------------------------------
# D#2518 -- stats.dora: three module constants (analytics_engineer._RELEASES_DIR,
# kpi_engine.REGISTRY, analytics_engineer's module-level REPO) bound stats.dora
# to the serving checkout at import, so a per-request project param reached
# nothing. Spec (Acceptance) items 1-8.
# ---------------------------------------------------------------------------


def test_stats_dora_is_scoped_not_unscopable():
    """Item 6: stats.dora must no longer be classified UNSCOPABLE, and its
    reason must name the new resolution rather than repeat the shared
    'already wrapped' string that produced the wrong classification for
    stats.loop_idle_ratio (D#2330).
    """
    kind, reason = scope.classification_for("stats.dora")
    assert kind == scope.SCOPED, f"stats.dora is still {kind!r} after D#2518"
    assert reason and reason.strip()
    assert "already wrapped in _with_project_stats_db()" not in reason, (
        "stats.dora's reason must name its own resolution, not repeat the "
        "shared wrapper string that was never true for this handler"
    )


def test_stats_dora_data_source_is_reachable_by_project():
    """The registry-consistency guard (scripts/ci/rpc-scope-registry-guard.py)
    refuses a SCOPED entry whose audited data source is DS_SERVING_CHECKOUT.
    Pin the post-fix value here too so a regression is caught by pytest, not
    only by the CI guard script.
    """
    source = scope._DATA_SOURCES.get("stats.dora")
    assert source in scope.DATA_SOURCES_REACHED_BY_PROJECT, (
        f"stats.dora data source {source!r} is not one a per-request "
        "override reaches"
    )


def test_stats_dora_binds_no_path_or_repo_slug_at_import():
    """Item 3: importing analytics_engineer and kpi_engine must bind no path
    and no repo slug at import time -- asserted on a fresh interpreter with a
    clean sys.modules, not a re-import into a warm one (a re-import returns
    the cached module and would pass against the unfixed code).

    Pre-fix, analytics_engineer._RELEASES_DIR and kpi_engine.REGISTRY were
    module constants built from Path(__file__).resolve().parent.parent at
    import, and analytics_engineer.REPO was `from backend._repo import REPO`
    at module level. Post-fix, all three are resolved lazily (a zero-arg
    accessor function, or a local import inside the function that needs the
    value) so neither module binds a fixed path or repo slug as an importable
    attribute.
    """
    probe = (
        "import backend.analytics_engineer as ae\n"
        "import backend.kpi_engine as ke\n"
        "assert not hasattr(ae, '_RELEASES_DIR'), "
        "'_RELEASES_DIR must not be a bound module attribute'\n"
        "assert not hasattr(ae, 'REPO'), "
        "'REPO must not be a bound module attribute'\n"
        "assert not hasattr(ke, 'REGISTRY'), "
        "'REGISTRY must not be a bound module attribute'\n"
        "assert callable(ae._releases_dir), "
        "'_releases_dir must be a callable accessor'\n"
        "assert callable(ke._registry_path), "
        "'_registry_path must be a callable accessor'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT_FOR_SUBPROCESS),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"fresh-interpreter import bound a path or repo slug:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


def _write_project_layout(
    tmp_path: Path,
    name: str,
    repo: "str | None",
    release_count: int,
) -> None:
    """Build a project's dashboard-runtime.json (repo, may be omitted) and
    its local checkout's .autonomous-team/{releases,registry.json}, matching
    the resolution convention backend/server.py's kpi.history/kpi.cycle_time
    RPC handlers already use: state_dir.parent / project.
    """
    import datetime as _dt

    state_dir = tmp_path / f".{name}-state"
    state_dir.mkdir()
    runtime: dict = {}
    if repo is not None:
        runtime["repo"] = repo
    (state_dir / "dashboard-runtime.json").write_text(json.dumps(runtime))

    checkout = tmp_path / name / ".autonomous-team"
    releases_dir = checkout / "releases"
    releases_dir.mkdir(parents=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    for i in range(release_count):
        (releases_dir / f"release-{i}.json").write_text(json.dumps({
            "id": f"2026-09-01-{i:03d}",
            "merged_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }))
    (checkout / "registry.json").write_text(json.dumps({"discussions": []}))


def _fake_gh_run(cmd, **kwargs):
    """Minimal `gh` stand-in for stats.dora's two subprocess calls: `gh pr
    list` (lead time) and `gh api graphql` (CFR bug discussions). Both
    return an empty-but-valid payload so neither field forces a real network
    call or introduces nondeterminism into the deploy-frequency assertion.
    """
    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    r = _Result()
    if cmd[:3] == ["gh", "pr", "list"]:
        r.stdout = "[]"
    elif cmd[:2] == ["gh", "api"]:
        r.stdout = json.dumps(
            {"data": {"repository": {"discussions": {"nodes": []}}}}
        )
    return r


def test_stats_dora_project_param_returns_that_projects_data(tmp_path, monkeypatch):
    """Item 4: stats.dora {"project": <other project>} must return that
    project's data, asserted on a value that differs between the two
    projects -- not merely on a non-empty response.
    """
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(subprocess, "run", _fake_gh_run)

    _write_project_layout(tmp_path, "projA", "acme/projA", release_count=1)
    _write_project_layout(tmp_path, "projB", "acme/projB", release_count=5)

    result_a = scope.dispatch_scoped(
        "stats.dora", {"project": "projA"}, srv._RPC_METHODS["stats.dora"],
    )
    result_b = scope.dispatch_scoped(
        "stats.dora", {"project": "projB"}, srv._RPC_METHODS["stats.dora"],
    )

    assert result_a["deploy_frequency_per_day"] != result_b["deploy_frequency_per_day"], (
        "projA (1 release) and projB (5 releases) must report different "
        "deploy_frequency_per_day -- identical values would mean stats.dora "
        "served one project's (or the serving checkout's) data for both"
    )
    assert result_a["deploy_frequency_per_day"] == round(1 / 7.0, 4)
    assert result_b["deploy_frequency_per_day"] == round(5 / 7.0, 4)


def test_stats_dora_no_project_param_unaffected(tmp_path, monkeypatch):
    """No-regression companion: the serving checkout's own dashboard (no
    project param) must be unaffected by the new project_root/repo params.
    """
    from backend import server as srv

    monkeypatch.setattr(subprocess, "run", _fake_gh_run)

    result = scope.dispatch_scoped(
        "stats.dora", {}, srv._RPC_METHODS["stats.dora"],
    )
    assert "applicable" in result
    assert isinstance(result["deploy_frequency_per_day"], float)


def test_stats_dora_declines_when_project_has_no_repo(tmp_path, monkeypatch):
    """Item 5: when the requested project declares no repo, stats.dora must
    decline -- raise, not fall back to the serving checkout's repo and not
    silently substitute an empty/zeroed response. The raise is what makes
    this distinguishable from an empty result: dispatch_scoped never
    returns for a declined call, whereas a genuinely-empty project (no
    releases, no registry entries) still returns a normal (if mostly-zero)
    dict.
    """
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(subprocess, "run", _fake_gh_run)

    _write_project_layout(tmp_path, "norepoproj", repo=None, release_count=1)

    with pytest.raises(scope.UnresolvableProjectError) as exc_info:
        scope.dispatch_scoped(
            "stats.dora", {"project": "norepoproj"}, srv._RPC_METHODS["stats.dora"],
        )
    assert hasattr(exc_info.value, "rpc_code"), (
        "UnresolvableProjectError must carry rpc_code so both dispatch "
        "sites surface this as a non-null JSON-RPC error"
    )


def test_stats_dora_declined_response_distinguishable_from_empty_project(
    tmp_path, monkeypatch,
):
    """Item 5 (continued): a project with a resolvable repo but genuinely no
    data (no releases, empty registry) must NOT raise -- it returns an
    empty-but-normal response. Only the no-repo case raises. This is the
    companion assertion that makes the decline "distinguishable from an
    empty result" rather than merely different in isolation.
    """
    from backend import server as srv

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(subprocess, "run", _fake_gh_run)

    _write_project_layout(tmp_path, "emptyproj", "acme/emptyproj", release_count=0)

    result = scope.dispatch_scoped(
        "stats.dora", {"project": "emptyproj"}, srv._RPC_METHODS["stats.dora"],
    )
    assert result["deploy_frequency_per_day"] == 0.0
    assert result["applicable"] is False
