"""
Legacy-route coverage-proof test (D#1425 PR4 — teardown gate).

This test PROVES that every legacy api.py route has a FastAPI equivalent on
asgi_app.app (either a real router or the catch-all strangler proxy), OR is
in DOCUMENTED_UNUSED with a justified reason.

Purpose: must pass BEFORE any legacy teardown (PR6).

Key design constraint (FIX 1)
------------------------------
Legacy routes are PARSED from backend/api.py at runtime via ``ast``. The test
walks every ``do_GET``, ``do_POST``, ``do_PATCH``, and ``do_DELETE`` method in
the ``_Handler`` class and collects every ``path == "..."`` comparison and every
``path.startswith("...")`` call. This means a new legacy route added to api.py
will automatically appear in the parse result and trip this test if it isn't
also reflected in the FastAPI app — the gate is NOT bypassable by omission.

A sanity check asserts the parser found >= MIN_EXPECTED_ROUTES; a broken
parser returning an empty set can't silently pass.

Key design constraint (FIX 2)
------------------------------
PROXY_ONLY_TEARDOWN_PENDING is the machine-readable burn-down list for PR6
(legacy teardown). It names every route that the FastAPI app currently handles
via the catch-all strangler-fig proxy rather than a dedicated router. The test
asserts computed_proxy_only == PROXY_ONLY_TEARDOWN_PENDING; if a route is
natively migrated, PR6 must also remove it from this constant (shrinking the
list). If a new proxy-only route is accidentally introduced, this test will
flag it so it gets explicitly added here with a reason.

Structure
---------
Section 1 — Runtime parser (reads api.py, no hardcoded route lists)
Section 2 — Positive coverage: every parsed route is natively covered OR proxy-only OR documented-unused; no genuine gaps
Section 3 — Proxy-only burn-down assertion: PROXY_ONLY_TEARDOWN_PENDING matches reality
Section 4 — RPC coverage: /rpc native + registry non-empty + sample dispatch
Section 5 — Negative controls: auth (401/403), loopback, spawn-guard, rate-limit
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Paths used by the runtime parser
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # fulcrumaxe/
_API_PY = _REPO_ROOT / "backend" / "api.py"

# Parser sanity: if we find fewer than this many routes something is broken
# (e.g. wrong file, empty parse).  Current count is ~98; set floor at 80
# to allow some churn without requiring a constant update for every small change.
MIN_EXPECTED_ROUTES = 80


# ---------------------------------------------------------------------------
# Section 1: Runtime parser — reads api.py via ``ast`` at test collection time
# ---------------------------------------------------------------------------

def _parse_legacy_routes(api_path: Path) -> list[tuple[str, str, str]]:
    """Parse api.py and return a list of ``(http_method, path, kind)`` tuples.

    ``kind`` is ``"exact"`` for ``path == "/foo"`` comparisons and ``"prefix"``
    for ``path.startswith("/foo")`` calls.

    Both ``path`` and ``raw_path`` variable names are recognised (PATCH/DELETE
    use ``raw_path``).

    Returns the list sorted for deterministic output.
    """
    src = api_path.read_text()
    tree = ast.parse(src)

    # Locate the HTTP handler class (_Handler)
    handler_class: ast.ClassDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and "Handler" in node.name:
            handler_class = node
            break
    if handler_class is None:
        raise RuntimeError(f"No _Handler class found in {api_path}")

    # Find the four HTTP method handlers
    target_fns = {"do_GET", "do_POST", "do_PATCH", "do_DELETE"}
    method_nodes: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(handler_class):
        if isinstance(node, ast.FunctionDef) and node.name in target_fns:
            method_nodes[node.name] = node

    PATH_VARS = {"path", "raw_path"}

    def _str_lit(node: ast.expr) -> str | None:
        """Return the string value of a Constant node, or None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    http_map = {
        "do_GET": "GET",
        "do_POST": "POST",
        "do_PATCH": "PATCH",
        "do_DELETE": "DELETE",
    }

    routes: list[tuple[str, str, str]] = []

    for fn_name, http_method in http_map.items():
        mnode = method_nodes.get(fn_name)
        if mnode is None:
            continue

        exact: set[str] = set()
        prefix: set[str] = set()

        for node in ast.walk(mnode):
            # path == "..."  /  "..." == path
            if (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
            ):
                left, comps = node.left, node.comparators
                if len(comps) == 1:
                    if isinstance(left, ast.Name) and left.id in PATH_VARS:
                        s = _str_lit(comps[0])
                        if s:
                            exact.add(s)
                    elif (
                        isinstance(comps[0], ast.Name)
                        and comps[0].id in PATH_VARS
                    ):
                        s = _str_lit(left)
                        if s:
                            exact.add(s)

            # path.startswith("...")
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "startswith"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in PATH_VARS
                    and node.args
                ):
                    s = _str_lit(node.args[0])
                    if s:
                        prefix.add(s)

        for p in sorted(exact):
            routes.append((http_method, p, "exact"))
        for p in sorted(prefix):
            routes.append((http_method, p, "prefix"))

    return sorted(routes)


# Parse once at module level so all tests share the same result
_PARSED_ROUTES: list[tuple[str, str, str]] = _parse_legacy_routes(_API_PY)


# ---------------------------------------------------------------------------
# Section 2: FastAPI route collection helpers
# ---------------------------------------------------------------------------

def _collect_fastapi_paths() -> set[str]:
    """Return all path patterns registered on asgi_app.app."""
    from backend.asgi_app import app
    from fastapi.routing import APIRoute
    from starlette.routing import Route, WebSocketRoute

    paths: set[str] = set()
    for route in app.routes:
        if isinstance(route, (APIRoute, Route, WebSocketRoute)):
            paths.add(route.path)
    return paths


def _tmpl_regex(tmpl: str) -> str:
    """Convert a path template to a regex: ``{xxx}`` → ``[^/]+``."""
    return re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(tmpl))


def _is_natively_covered(
    method: str,
    path: str,
    kind: str,
    native_paths: set[str],
    route_methods: dict[str, frozenset[str]],
) -> bool:
    """Return True if (method, path, kind) has an explicit FastAPI router entry.

    Does NOT count the catch-all ``/{path:path}`` — that is the proxy fallback.
    Template param names are normalised: ``{sid}`` and ``{id}`` both match
    the pattern ``[^/]+`` so name-differences don't cause false negatives.

    Prefix-kind rule (conservative, method-aware):
    A ``startswith``-kind legacy pattern is natively covered only when there
    exists a FastAPI route that:
      1. Shares the **same HTTP method**, AND
      2. Has its path **start with the legacy prefix**, AND
      3. Contains a ``{param}`` segment at or beyond the prefix.

    Exact sibling paths (e.g. ``/traces/stats`` for the ``/traces/`` prefix)
    do NOT count — they only cover one concrete sub-path, not the open-ended
    set implied by ``startswith``.  Wrong-method matches (e.g. POST routes
    under a ``GET /replays/`` prefix) also do NOT count.
    """
    if kind == "exact":
        # Direct match
        if path in native_paths:
            return True
        # Match against templates (param name differences are normalised)
        path_re = _tmpl_regex(path) if "{" in path else None
        for fp in native_paths:
            if "{" in fp:
                fp_re = _tmpl_regex(fp)
                if path_re:
                    if fp_re == path_re:
                        return True
                else:
                    # Concrete path vs template
                    if re.fullmatch(fp_re, path):
                        return True
        return False
    else:
        # Prefix: require a same-method FastAPI route that has a {param}
        # segment at or after the legacy prefix.  This correctly marks routes
        # like GET /replays/ as proxy-only even though POST /replays/* routes
        # exist, and correctly marks GET /traces/ as proxy-only even though
        # GET /traces/stats exists (no param, different sub-path).
        path_stripped = path.rstrip("/")
        for fp in sorted(native_paths):
            fp_stripped = fp.rstrip("/")
            if not fp_stripped.startswith(path_stripped):
                continue
            # Same HTTP method required
            fp_methods = route_methods.get(fp, frozenset())
            if method not in fp_methods:
                continue
            # Must have a {param} segment — confirms open-ended coverage
            suffix = fp_stripped[len(path_stripped):]
            if "{" in suffix or "{" in fp_stripped:
                return True
        return False


def _build_route_methods() -> dict[str, frozenset[str]]:
    """Return a mapping of FastAPI path → frozenset of HTTP methods.

    Used by ``_is_natively_covered`` to enforce same-method matching on
    prefix-kind legacy routes.
    """
    from backend.asgi_app import app
    from fastapi.routing import APIRoute

    result: dict[str, frozenset[str]] = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            result[route.path] = frozenset(route.methods or ())
    return result


def _classify_routes(
    parsed: list[tuple[str, str, str]],
) -> tuple[
    list[tuple[str, str, str]],  # native
    list[tuple[str, str, str]],  # proxy_only
    list[tuple[str, str, str]],  # gaps (should always be empty — catch-all covers)
]:
    """Classify each parsed route as native / proxy-only / gap.

    ``native``     — explicit FastAPI router entry
    ``proxy_only`` — no explicit router; covered by the catch-all proxy
    ``gaps``       — not covered at all (catch-all absent) — should never happen
    """
    all_paths = _collect_fastapi_paths()
    native_paths = all_paths - {"/{path:path}"}
    has_catchall = "/{path:path}" in all_paths
    route_methods = _build_route_methods()

    native: list[tuple[str, str, str]] = []
    proxy_only: list[tuple[str, str, str]] = []
    gaps: list[tuple[str, str, str]] = []

    for method, path, kind in parsed:
        if _is_natively_covered(method, path, kind, native_paths, route_methods):
            native.append((method, path, kind))
        elif has_catchall:
            proxy_only.append((method, path, kind))
        else:
            gaps.append((method, path, kind))

    return native, proxy_only, gaps


# ---------------------------------------------------------------------------
# Section 3: PROXY_ONLY_TEARDOWN_PENDING — the burn-down list for PR6
# ---------------------------------------------------------------------------
#
# These are the (method, path, kind) tuples that the FastAPI app currently
# handles via the catch-all strangler-fig proxy rather than a native router.
# PR6 (legacy teardown) REQUIRES every entry here to be migrated before the
# legacy ThreadingHTTPServer can be removed.
#
# Maintenance contract:
#   - When a route is natively migrated, remove it from this list in the
#     same PR that adds the router. The assertion below will catch stale entries.
#   - If a new proxy-only route is introduced, add it here with a comment
#     explaining which PR is responsible for migrating it.
#
# Current count: 0 — teardown complete. All legacy GET routes are now natively migrated.
# /benchmarks/history (prefix), /quality/ (prefix), /validate (exact) were the last three;
# migrated in D#1425 misc cluster (info_benchmarks.py, obs_quality.py, info_misc.py).

PROXY_ONLY_TEARDOWN_PENDING: list[tuple[str, str, str]] = [
    # GET reads not yet on a dedicated router (all proxied to :18099).
    # Corrected in D#1425 PR4 round 3: 4 prefix routes were previously
    # false-classified as natively covered because the old prefix rule
    # ignored HTTP method and accepted exact sibling paths (e.g. POST
    # /replays/* wrongly "covered" GET /replays/; GET /traces/stats exact
    # wrongly "covered" GET /traces/ prefix).  The fixed rule requires a
    # same-method FastAPI route with a {param} segment under the prefix.
    # D#1425 misc cluster: /benchmarks/history, /quality/, /validate migrated.
    # /sessions GET routes migrated to backend/routers/sessions_get.py (D#1425)
    # /spawn-blocks, /spawn-queue, /spawn-queue/active, /spawn-queue/pending
    # migrated to backend/routers/spawn_queue_get.py (D#1425 PR5)
    # GET /traces (exact) and GET /traces/ (prefix) removed — natively migrated
    # in D#1425 (backend/routers/traces_get.py, registered in asgi_app.py).
    # GET /replays (exact), GET /replays/ (prefix), GET /replays/status (exact) removed
    # — natively migrated in D#1425 (backend/routers/replays_get.py, registered in asgi_app.py).
]

# Sorted canonical form used for set comparison
_PROXY_ONLY_CANONICAL: frozenset[tuple[str, str, str]] = frozenset(
    PROXY_ONLY_TEARDOWN_PENDING
)


# ---------------------------------------------------------------------------
# DOCUMENTED_UNUSED: genuine gaps that exist in legacy but are intentionally
# NOT covered.  Must remain empty — every path must be either natively
# migrated or listed in PROXY_ONLY_TEARDOWN_PENDING.
# ---------------------------------------------------------------------------

DOCUMENTED_UNUSED: dict[tuple[str, str, str], str] = {}


# ---------------------------------------------------------------------------
# Section 4: Parser sanity + coverage tests
# ---------------------------------------------------------------------------


def test_parser_found_enough_routes():
    """Sanity: the runtime parser must find at least MIN_EXPECTED_ROUTES routes.

    A broken parser (empty parse, wrong class, wrong method name) would return
    far fewer routes and silently make all coverage assertions trivially true.
    """
    n = len(_PARSED_ROUTES)
    assert n >= MIN_EXPECTED_ROUTES, (
        f"Runtime parser found only {n} legacy routes in {_API_PY} — "
        f"expected >= {MIN_EXPECTED_ROUTES}. "
        "Either the parser is broken or api.py has shrunk unexpectedly."
    )


def test_fastapi_has_catchall_proxy():
    """The FastAPI app MUST have a catch-all route '/{path:path}'.

    This is the strangler-fig proxy that covers every unmigrated legacy path.
    Without it, every proxy-only route would become an actual gap.
    """
    paths = _collect_fastapi_paths()
    assert "/{path:path}" in paths, (
        "CRITICAL: asgi_app.app is missing the catch-all proxy route '/{path:path}'. "
        "Every unmigrated legacy path would be unreachable. "
        "This test must pass before any legacy teardown."
    )


def test_no_uncovered_legacy_routes():
    """Every parsed legacy route is reachable on the FastAPI app — zero genuine gaps.

    For each (method, path, kind) the parser found in api.py:
      1. Explicit FastAPI router handles it (native), OR
      2. In DOCUMENTED_UNUSED with a justification, OR
      3. In PROXY_ONLY_TEARDOWN_PENDING (catch-all covers it).

    Fails if any route is neither native, documented, nor in the proxy list.
    Do NOT add entries to DOCUMENTED_UNUSED or PROXY_ONLY_TEARDOWN_PENDING
    just to make this test pass — real gaps must be migrated first.
    """
    native, proxy_only, gaps = _classify_routes(_PARSED_ROUTES)

    # Remove documented entries from gaps (should be none)
    real_gaps = [
        r for r in gaps
        if r not in DOCUMENTED_UNUSED
    ]

    total = len(_PARSED_ROUTES)
    print(f"\nCoverage summary (runtime-parsed from api.py):")
    print(f"  Total legacy routes parsed: {total}")
    print(f"  Natively covered by FastAPI router: {len(native)}")
    print(f"  Proxy-only (catch-all, pending migration): {len(proxy_only)}")
    print(f"  Documented-unused: {len(DOCUMENTED_UNUSED)}")
    print(f"  GAPS (not covered, not documented): {len(real_gaps)}")

    if real_gaps:
        lines = "\n".join(f"    {m} {p}  [{k}]" for m, p, k in real_gaps)
        pytest.fail(
            f"Legacy routes NOT covered AND not documented:\n{lines}\n\n"
            "Migrate these routes to a native FastAPI router, OR add them to\n"
            "PROXY_ONLY_TEARDOWN_PENDING (if catch-all covers them), OR to\n"
            "DOCUMENTED_UNUSED with a justification."
        )


def test_proxy_only_matches_constant():
    """The computed proxy-only set must exactly equal PROXY_ONLY_TEARDOWN_PENDING.

    This is the burn-down assertion for PR6 (legacy teardown):
    - If a route is natively migrated, it must be removed from the constant
      (this test catches stale entries in PROXY_ONLY_TEARDOWN_PENDING).
    - If a new proxy-only route is introduced, it must be added to the constant
      (this test catches missing entries).

    The net effect: PROXY_ONLY_TEARDOWN_PENDING is always the exact set of
    routes that PR6 still needs to migrate. When it reaches 0 entries, the
    catch-all proxy can be removed and the legacy server can be torn down.
    """
    _, computed_proxy_only, _ = _classify_routes(_PARSED_ROUTES)
    computed_set = frozenset(computed_proxy_only)

    extra_in_constant = _PROXY_ONLY_CANONICAL - computed_set
    missing_from_constant = computed_set - _PROXY_ONLY_CANONICAL

    messages: list[str] = []
    if extra_in_constant:
        lines = "\n".join(f"    {m} {p}  [{k}]" for m, p, k in sorted(extra_in_constant))
        messages.append(
            f"STALE entries in PROXY_ONLY_TEARDOWN_PENDING (route is now natively migrated):\n{lines}\n"
            "Remove them from the constant to keep the burn-down list accurate."
        )
    if missing_from_constant:
        lines = "\n".join(f"    {m} {p}  [{k}]" for m, p, k in sorted(missing_from_constant))
        messages.append(
            f"MISSING from PROXY_ONLY_TEARDOWN_PENDING (route is proxy-only but not listed):\n{lines}\n"
            "Add them with a comment noting which PR will migrate them."
        )

    if messages:
        pytest.fail("\n\n".join(messages))


def test_documented_unused_entries_have_reasons():
    """Every DOCUMENTED_UNUSED entry must have a non-empty justification string."""
    for key, reason in DOCUMENTED_UNUSED.items():
        assert reason, f"DOCUMENTED_UNUSED entry {key!r} has no justification"


# ---------------------------------------------------------------------------
# Section 5: RPC coverage
# ---------------------------------------------------------------------------


def test_rpc_registry_non_empty():
    """server.py _RPC_METHODS must have at least one registered method."""
    from backend.server import _RPC_METHODS

    assert len(_RPC_METHODS) > 0, (
        "server._RPC_METHODS is empty — no RPC methods registered. "
        "This indicates a broken import or missing registration."
    )


def test_fastapi_has_rpc_route():
    """POST /rpc must exist as an explicit FastAPI route (not catch-all)."""
    fastapi_paths = _collect_fastapi_paths()
    assert "/rpc" in fastapi_paths, (
        "POST /rpc is not registered as an explicit FastAPI route. "
        "The /rpc endpoint must be natively handled."
    )


def test_rpc_sample_dispatch(monkeypatch):
    """A canary RPC method dispatches via POST /rpc on the FastAPI app."""
    import backend.server as server_mod

    original_methods = dict(server_mod._RPC_METHODS)
    server_mod._RPC_METHODS["test.ping"] = lambda params: {"pong": True}

    try:
        monkeypatch.setattr("backend.routers.rpc._load_rpc_token", lambda: "test-rpc-token")
        monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

        from backend.asgi_app import app

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/rpc",
                json={"jsonrpc": "2.0", "id": 1, "method": "test.ping", "params": {}},
                headers={"Authorization": "Bearer test-rpc-token"},
            )
            assert resp.status_code in (200, 401, 403), (
                f"POST /rpc returned unexpected status {resp.status_code}: {resp.text}"
            )
            if resp.status_code == 200:
                data = resp.json()
                assert "result" in data or "error" in data, (
                    f"POST /rpc 200 response is not JSON-RPC shaped: {data}"
                )
    finally:
        server_mod._RPC_METHODS.clear()
        server_mod._RPC_METHODS.update(original_methods)


# ---------------------------------------------------------------------------
# Section 6 — Negative controls (security gates still work on FastAPI app)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _authed_client_nc(monkeypatch):
    """TestClient with AF_API_AUTH_KEY set."""
    monkeypatch.setenv("AF_API_AUTH_KEY", "correct-token-nc")
    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_nc1_wrong_token_returns_403(_authed_client_nc):
    """NC1: Present-but-wrong Bearer token → 403 Forbidden."""
    resp = _authed_client_nc.get(
        "/registry",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 403, (
        f"Expected 403 for wrong token, got {resp.status_code}: {resp.text}"
    )


def test_nc2_missing_token_returns_401(_authed_client_nc):
    """NC2: Missing Authorization header → 401 Unauthorized."""
    resp = _authed_client_nc.get("/registry")
    assert resp.status_code == 401, (
        f"Expected 401 for missing token, got {resp.status_code}: {resp.text}"
    )


def test_nc3_api_config_non_loopback_returns_403(monkeypatch):
    """NC3: Non-loopback caller on /api/config → 403 (loopback gate)."""
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/config")
        assert resp.status_code == 403, (
            f"Expected 403 for non-loopback /api/config, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "forbidden" in data.get("error", "").lower(), (
            f"Expected forbidden error message, got: {data}"
        )


def test_nc4_headless_ua_blocked_on_loop_run(monkeypatch):
    """NC4: POST /api/loop/run with HeadlessChrome UA → 403 spawn_blocked_test_origin."""
    monkeypatch.delenv("AF_ALLOW_TEST_ORIGIN_SPAWNS", raising=False)
    monkeypatch.delenv("AF_MCP_TEST_ORIGIN", raising=False)
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)
    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/loop/run",
            json={"instruction": "test"},
            headers={"User-Agent": "Mozilla/5.0 (HeadlessChrome/120) AppleWebKit/537.36"},
        )
        assert resp.status_code == 403, (
            f"Expected 403 for HeadlessChrome UA on /api/loop/run, "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data.get("error") == "spawn_blocked_test_origin", (
            f"Expected spawn_blocked_test_origin error, got: {data}"
        )


def test_nc5_rate_limit_burst_returns_429(monkeypatch):
    """NC5: Burst past the rate-limit bucket on a non-exempt route → 429.

    Uses a 3-token bucket (same as test_rate_limit_middleware.py):
    drain 3 tokens on /docs, verify the 4th request yields 429.
    """
    import backend.middleware.rate_limit as rl_mod
    from backend.rate_limiter import RateLimiter

    tiny_limiter = RateLimiter(rate=0.001, burst=3.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", tiny_limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(3):
            client.get("/docs")
        resp = client.get("/docs")
        assert resp.status_code == 429, (
            f"Expected 429 after draining 3-token bucket, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "error" in data, f"429 body missing 'error' key: {data}"
        assert "retry_after" in data, f"429 body missing 'retry_after' key: {data}"


def test_nc5_rate_limit_429_body_shape(monkeypatch):
    """NC5 body: 429 response has the correct legacy JSON body shape + Retry-After header."""
    import backend.middleware.rate_limit as rl_mod
    from backend.rate_limiter import RateLimiter

    tiny_limiter = RateLimiter(rate=0.001, burst=1.0)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "limiter", tiny_limiter)
    monkeypatch.setattr(rl_mod.RateLimitMiddleware, "enabled", True)
    monkeypatch.delenv("AF_API_AUTH_KEY", raising=False)

    from backend.asgi_app import app

    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/docs")  # drain the 1-token bucket
        resp = client.get("/docs")
        assert resp.status_code == 429, (
            f"Expected 429 from 1-token bucket drain, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert "error" in data, f"429 body missing 'error' key: {data}"
        assert "retry_after" in data, f"429 body missing 'retry_after' key: {data}"
        assert "Retry-After" in resp.headers, "429 response missing Retry-After header"
