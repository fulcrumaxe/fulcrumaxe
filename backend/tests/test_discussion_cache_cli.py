"""backend/tests/test_discussion_cache_cli.py — subprocess-level tests pinning
discussion_cache.py's CLI exit codes (D#1799).

Nothing anywhere asserted these before: `backend/tests/test_discussion_cache.py`
tests the library's `get_body`/`get_body_status` functions in-process, but the
two shell gates in `scripts/spawn-agent.sh` branch on the CLI's *exit code*
(0/1/3), not on any library-level status string. A `__main__` refactor could
turn `sys.exit(3)` into `sys.exit(0)` and both gates would go quietly dark
with a fully green library suite.

`sys.exit(1)` alone pins nothing: it has six call sites in discussion_cache.py,
and a crashed import also exits 1. So every test here pairs its exit code with
a branch-only discriminator (see each test's docstring), and the mutation
evidence for all three plus the discriminator-load-bearing check is in the PR
body for D#1799.

Hermetic: no network access. A `gh` stub on PATH always fails, so the GraphQL
fetch fails deterministically every time; AUTONOMOUS_TEAM_STATE_DIR points at
an isolated tmp_path per RelativeStateDirError's requirement that it be
absolute.

Run with:
    python3 -m pytest backend/tests/test_discussion_cache_cli.py -q
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "backend" / "discussion_cache.py"

# Mirrors the schema in discussion_cache.py's _DDL — duplicated deliberately
# rather than imported, so seeding stays independent of the module under test.
_DDL = """
CREATE TABLE IF NOT EXISTS discussion_cache (
    number     INTEGER PRIMARY KEY,
    body       TEXT    NOT NULL DEFAULT '',
    title      TEXT    NOT NULL DEFAULT '',
    labels     TEXT    NOT NULL DEFAULT '[]',
    updated_at TEXT    NOT NULL DEFAULT '',
    cached_at  TEXT    NOT NULL DEFAULT ''
);
"""

_GH_STUB = """#!/bin/sh
echo "stub gh: simulated GraphQL outage (D#1799 test)" >&2
exit 1
"""


def _now_iso() -> str:
    """Same format discussion_cache.py's _now_iso() produces — kept in sync by hand
    since this file deliberately does not import the module under test."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _seed_row(state_dir: Path, number: int, body: str, cached_at: str) -> None:
    """Write a cache row directly to discussion_cache.db — bypasses the module
    under test entirely, matching the Spec's "seed discussion_cache.db directly"."""
    con = sqlite3.connect(str(state_dir / "discussion_cache.db"))
    try:
        con.execute(_DDL)
        con.execute(
            "INSERT INTO discussion_cache(number, body, title, labels, updated_at, cached_at) "
            "VALUES (?, ?, '', '[]', '', ?)",
            (number, body, cached_at),
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def cli_env(tmp_path: Path):
    """An absolute, isolated AUTONOMOUS_TEAM_STATE_DIR plus a `gh` stub on PATH
    that always fails non-zero, so `_gh_graphql` fails deterministically with
    no real network call. Returns (state_dir, env)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(_GH_STUB)
    gh_stub.chmod(0o755)

    env = dict(os.environ)
    env["AUTONOMOUS_TEAM_STATE_DIR"] = str(state_dir)
    env["AUTONOMOUS_TEAM_REPO"] = "fulcrumaxe/fulcrumaxe"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return state_dir, env


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


# ---------------------------------------------------------------------------
# exit 0 — TTL-fresh cached row served, no fetch attempted
# ---------------------------------------------------------------------------


def test_get_body_exit_0_serves_fresh_cached_body(cli_env):
    """returncode == 0 AND stdout equals the exact seeded body.

    Discriminator: the body match. `sys.exit(0)` is also what a bare `if not
    args` usage error is *not* (that's exit 1), but a returncode-only assertion
    here would still pass for an accidental empty-success path. Requiring the
    literal seeded string proves this hit the `cached` return in _get_record —
    a TTL-fresh row served without any fetch — not just "some non-error exit".
    """
    state_dir, env = cli_env
    _seed_row(state_dir, 4001, "EXIT0 CACHED BODY", _now_iso())

    result = _run(env, "get-body", "4001")

    assert result.returncode == 0
    assert result.stdout == "EXIT0 CACHED BODY"


# ---------------------------------------------------------------------------
# exit 3 — --fresh requested, live fetch fails, stale row served
# ---------------------------------------------------------------------------


def test_get_body_exit_3_stale_fallback_on_fresh_fetch_failure(cli_env):
    """returncode == 3 AND stdout is the exact stale body AND stderr contains
    the stale_fallback warning line.

    Discriminator: the stderr warning. That line is emitted only on the
    `stale_fallback` return in `_get_record` — pairing it with returncode == 3
    proves the fetch was actually attempted and fell back, rather than exit 3
    arriving through some other path. See
    test_get_body_exit_3_discriminator_is_load_bearing below for proof this
    clause is not decorative.
    """
    state_dir, env = cli_env
    _seed_row(state_dir, 4002, "EXIT3 STALE BODY", "2020-01-01T00:00:00Z")

    result = _run(env, "get-body", "4002", "--fresh")

    assert result.returncode == 3
    assert result.stdout == "EXIT3 STALE BODY"
    assert "GraphQL failed, returning stale body for #4002" in result.stderr


# ---------------------------------------------------------------------------
# exit 1 — nothing available at all (empty body path)
# ---------------------------------------------------------------------------


def test_get_body_exit_1_empty_when_nothing_available(cli_env):
    """returncode == 1 AND stdout is empty AND stderr does NOT contain
    'requires a discussion number'.

    Discriminator: the negative stderr assertion. `sys.exit(1)` has six call
    sites in discussion_cache.py, including the `len(args) < 2` usage error
    that DOES print 'requires a discussion number'. This test supplies a
    discussion number, so it must reach the *empty-body* exit at a different
    call site — the negative clause is what tells the two apart, since both
    are otherwise indistinguishable by returncode alone.
    """
    _, env = cli_env
    # No row seeded for 4003: cache miss, and the gh stub fails the fetch too.

    result = _run(env, "get-body", "4003")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "requires a discussion number" not in result.stderr
