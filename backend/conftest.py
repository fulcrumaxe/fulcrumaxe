"""Pytest configuration for the backend package.

Adds the repo root to sys.path, matching tests/conftest.py:16, so a targeted
`python3 -m pytest backend/tests/test_foo.py` invocation resolves fully-qualified
imports (`backend.orchestrator...`, `hooks...`) the same way a full-suite run
does. Previously this inserted `backend/` itself at sys.path[0], which shadowed
the real repo-root `hooks` (and `tests`) packages with the same-named packages
under backend/ — `backend/hooks/` has no `sandbox_rules` or `repo_root`, so any
test collected without an earlier import having already fixed sys.path (as
backend/tests/test_a2a_broker.py incidentally does by sorting first) failed
with a `ModuleNotFoundError` naming a module that plainly exists. No consumer
in backend/tests/ relies on the bare-name imports (`from hooks import ...`)
the old comment described; every consumer uses the fully-qualified form.

Also provides reset_limiter_state — an autouse function-scoped fixture that
clears shared limiter singletons before each test to prevent cross-file
contamination in the full suite run.

Optional API dependencies (fastapi, starlette, uvicorn) may be absent from a
lean dev environment. Three hooks below turn "absent" into a skip while still
failing loudly for anything broken:

1. `pytest_pycollect_makemodule` (via `_OptionalApiDepsModule`) downgrades a
   collection error to a skip when a test module fails to import because of a
   `ModuleNotFoundError` naming exactly `fastapi`, `starlette` or `uvicorn`.
2. `pytest_runtest_setup` / `pytest_runtest_call` do the same for lazy imports
   inside a fixture or test body (the exception, its `__cause__` or its
   `__context__`).
3. `pytest_collection_modifyitems` loads an optional `backend/tests/skiplist.py`
   (`SKIP = {node_id: (reason, "D#<n>")}`, at most 18 entries) and marks those
   tests skipped; malformed or stale entries fail collection.

Every skip requires `importlib.util.find_spec(top) is None`: a
`ModuleNotFoundError` for an installed package (a removed submodule, a
hand-raised error) is a real failure. An exception from the finder itself
counts as "absent". Matching is on `ModuleNotFoundError.name.split(".")[0]`,
never message text. `pytest.Module._getobj()` wraps import errors in
`CollectError`, so the module hook inspects `__cause__`.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

# Add the repo root to sys.path so fully-qualified imports resolve the same
# whether backend/tests/ is run standalone or as part of the full suite.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Modules whose absence at test-collection time downgrades a
# collection error to a skip, rather than failing the run. These are the
# only names checked — matched on ModuleNotFoundError.name.split(".")[0],
# never on message text or a name prefix (see
# backend/tests/test_conftest_optional_api_deps.py).
_OPTIONAL_API_DEPS = frozenset({"fastapi", "starlette", "uvicorn"})


def _is_not_installed(top_level: str) -> bool:
    """True only when `top_level` genuinely cannot be found.

    Every skip in this file is guarded by this check, so a
    `ModuleNotFoundError` that names an *installed* package (a removed or
    renamed submodule, a hand-raised error, a wrapped cause) is a real
    failure, never a skip. An exception raised by the finder machinery
    itself is treated as "absent" (a blocked or broken finder means the
    package cannot be imported).
    """
    try:
        return importlib.util.find_spec(top_level) is None
    except Exception:
        return True


class _OptionalApiDepsModule(pytest.Module):
    """pytest.Module that skips (rather than errors) when collection fails
    solely because an optional API dependency isn't installed."""

    def _getobj(self):
        try:
            return super()._getobj()
        except pytest.Collector.CollectError as exc:
            cause = exc.__cause__
            if isinstance(cause, ModuleNotFoundError):
                top_level = (cause.name or "").split(".")[0]
                if top_level in _OPTIONAL_API_DEPS and _is_not_installed(top_level):
                    pytest.skip(
                        f"optional API dependency '{top_level}' not installed "
                        "(requirements.txt lists it as optional)",
                        allow_module_level=True,
                    )
            raise


def pytest_pycollect_makemodule(module_path, parent):
    return _OptionalApiDepsModule.from_parent(parent, path=module_path)


def _optional_dep_cause(exc: BaseException) -> ModuleNotFoundError | None:
    """Return `exc` (or its `__cause__`/`__context__`, one level each) if it is a
    `ModuleNotFoundError` for exactly one of `_OPTIONAL_API_DEPS` whose package
    is genuinely not installed (`_is_not_installed`), else None.

    Matched on `ModuleNotFoundError.name.split(".")[0]` only — never message
    text or a name prefix — the same rule `_OptionalApiDepsModule` uses above.
    """
    for candidate in (exc, exc.__cause__, exc.__context__):
        if isinstance(candidate, ModuleNotFoundError):
            top_level = (candidate.name or "").split(".")[0]
            if top_level in _OPTIONAL_API_DEPS and _is_not_installed(top_level):
                return candidate
    return None


def _skip_reason_for(found: ModuleNotFoundError) -> str:
    top_level = (found.name or "").split(".")[0]
    return (
        f"optional API dependency '{top_level}' not installed "
        "(requirements.txt lists it as optional)"
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    """Call-time counterpart to `_OptionalApiDepsModule` (a fixture that raises `ModuleNotFoundError` for an optional API
    dependency during setup becomes a skip, not an error. Any other exception
    propagates unchanged (bare `raise` — preserves the original traceback)."""
    try:
        return (yield)
    except BaseException as exc:
        found = _optional_dep_cause(exc)
        if found is None:
            raise
        pytest.skip(_skip_reason_for(found))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Call-time counterpart to `_OptionalApiDepsModule` (a test body that raises `ModuleNotFoundError` for an optional API
    dependency becomes a skip, not a failure. Any other exception propagates
    unchanged (bare `raise` — preserves the original traceback)."""
    try:
        return (yield)
    except BaseException as exc:
        found = _optional_dep_cause(exc)
        if found is None:
            raise
        pytest.skip(_skip_reason_for(found))


# The skip-list loader. `backend/tests/skiplist.py` (optional) holds a single
# `SKIP = {node_id: (reason, "D#<n>")}` dict, capped at `_SKIPLIST_MAX_IDS`
# entries. This conftest never creates that file — it only loads it if present.
_SKIPLIST_MAX_IDS = 18
_SKIPLIST_DEFAULT_PATH = Path(__file__).resolve().parent / "tests" / "skiplist.py"
# Test-only override so tests can point the loader at a temporary skiplist
# file without writing into the repo tree. Production code never sets this.
_SKIPLIST_PATH_ENV = "BACKEND_TESTS_SKIPLIST_PATH"


def _skiplist_path() -> Path:
    override = os.environ.get(_SKIPLIST_PATH_ENV)
    return Path(override) if override else _SKIPLIST_DEFAULT_PATH


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    skiplist_path = _skiplist_path()
    if not skiplist_path.is_file():
        return

    spec = importlib.util.spec_from_file_location(
        "backend_tests_skiplist", skiplist_path
    )
    if spec is None or spec.loader is None:
        raise pytest.UsageError(f"could not load skiplist module from {skiplist_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    skip_map = getattr(module, "SKIP", None)
    if not isinstance(skip_map, dict):
        raise pytest.UsageError(f"{skiplist_path} must define a SKIP dict")

    if len(skip_map) > _SKIPLIST_MAX_IDS:
        raise pytest.UsageError(
            f"{skiplist_path} lists {len(skip_map)} test ID(s), more than the "
            f"cap of {_SKIPLIST_MAX_IDS}"
        )

    for node_id, entry in skip_map.items():
        try:
            reason, discussion_ref = entry
        except (TypeError, ValueError):
            raise pytest.UsageError(
                f"{skiplist_path}: entry for {node_id!r} must be a "
                "(reason, 'D#<n>') 2-tuple"
            ) from None
        if not isinstance(reason, str) or not reason.strip():
            raise pytest.UsageError(f"{skiplist_path}: entry for {node_id!r} has no reason")
        if not re.fullmatch(r"D#\d+", str(discussion_ref or "")):
            raise pytest.UsageError(
                f"{skiplist_path}: entry for {node_id!r} has no 'D#<n>' reference "
                f"(got {discussion_ref!r})"
            )

    items_by_id = {item.nodeid: item for item in items}
    # An ID is stale only when its file was collected in this session (so the
    # test is really gone) or the file no longer exists. An ID for a file this
    # run simply didn't select (`pytest backend/tests/test_a.py`) is not stale.
    collected_files = {item.nodeid.split("::")[0] for item in items}

    def _is_stale(node_id: str) -> bool:
        file_part = node_id.split("::")[0]
        return file_part in collected_files or not (config.rootpath / file_part).exists()

    unmatched = sorted(
        node_id
        for node_id in skip_map
        if node_id not in items_by_id and _is_stale(node_id)
    )
    if unmatched:
        raise pytest.UsageError(
            f"{skiplist_path} lists test ID(s) that match no collected test: "
            + ", ".join(unmatched)
        )

    for node_id, (reason, discussion_ref) in skip_map.items():
        if node_id not in items_by_id:
            continue
        items_by_id[node_id].add_marker(
            pytest.mark.skip(reason=f"{reason} (tracked: {discussion_ref})")
        )


@pytest.fixture(autouse=True)
def reset_limiter_state() -> None:
    """Clear shared limiter state before each test to prevent cross-test contamination.

    Three module-level singletons accumulate state across test files in the full
    suite run, causing spurious 429 / 503 failures that all pass in isolation:

    1. backend.middleware.rate_limit._limiter (RateLimiter) — token buckets drain
       across many requests; later tests see 429 when the bucket was full at start.

    2. backend.routers.streams._ip_tracker (SSEConnectionTracker) — per-IP SSE
       connection counts leak from an unreleased slot in an earlier test.

    3. backend.routers.streams._global_limiter / backend.deps.shared_limiter._limiter
       (GlobalStreamLimiter) — global stream count leaks; later tests see 503.

    This fixture also re-syncs streams._global_limiter to the authoritative
    shl._limiter after any test that reloads backend.deps.shared_limiter
    (e.g. test_env_var_controls_cap), so the id()-identity assertion remains valid.

    All three singletons live in fastapi-backed modules. When fastapi isn't
    installed this is a no-op: there is nothing to reset.
    """
    if importlib.util.find_spec("fastapi") is None:
        return

    # 1. Per-IP token-bucket rate limiter — clear all buckets.
    import backend.middleware.rate_limit as rl_mod

    with rl_mod._limiter._lock:
        rl_mod._limiter._buckets.clear()

    # 2. SSE per-IP connection tracker — reset all counts.
    import backend.routers.streams as streams_mod

    with streams_mod._ip_tracker._lock:
        streams_mod._ip_tracker._counts.clear()

    # 3. Global stream limiter — reset active count and re-sync the module
    #    reference so it stays identical to the shared_limiter singleton.
    import backend.deps.shared_limiter as shl_mod

    with shl_mod._limiter._lock:
        shl_mod._limiter._count = 0

    # Re-point streams._global_limiter at whatever shl._limiter currently is.
    # A prior test may have reloaded shared_limiter (importlib.reload), creating
    # a fresh shl._limiter; without this sync the identity test would fail.
    streams_mod._global_limiter = shl_mod._limiter

    with streams_mod._global_limiter._lock:
        streams_mod._global_limiter._count = 0
