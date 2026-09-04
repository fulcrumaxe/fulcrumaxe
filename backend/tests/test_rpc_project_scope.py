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
import threading
from pathlib import Path

import pytest

from backend import rpc_project_scope as scope

_BACKEND_DIR = Path(__file__).resolve().parent.parent


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
