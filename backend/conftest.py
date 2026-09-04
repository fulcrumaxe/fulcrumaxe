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
"""

import sys
from pathlib import Path

import pytest

# Add the repo root to sys.path so fully-qualified imports resolve the same
# whether backend/tests/ is run standalone or as part of the full suite.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


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
    """
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
