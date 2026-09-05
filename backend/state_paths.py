"""
state_paths.py — single source of truth for runtime-state file paths.

All mutable runtime state lives outside the repo tree so that git worktree
merges can never wipe it.  The directory can be overridden via the env var
``AUTONOMOUS_TEAM_STATE_DIR`` (useful for tests and CI).

Default: ``~/.autonomous-forever-state/``

``AUTONOMOUS_TEAM_STATE_DIR`` must be absolute (``~`` is expanded for you).
A relative value raises :class:`RelativeStateDirError` instead of resolving
against the process cwd — see :func:`_state_dir`.

Usage::

    from backend import state_paths
    from backend.state_paths import ensure_state_dir

    ensure_state_dir()               # idempotent — creates dir + subdirs if missing
    print(state_paths.STATS_DB)      # PosixPath('/home/user/.autonomous-forever-state/stats.duckdb')

Override (e.g. in tests)::

    AUTONOMOUS_TEAM_STATE_DIR=/tmp/test-state python3 backend/budget.py status

Per-project lookup::

    from backend.state_paths import for_project
    paths = for_project("projectb")   # reads ~/.projectb-state/dashboard-runtime.json
    print(paths.state_dir)        # PosixPath('<home>/.projectb-state')
    print(paths.repo)             # "some-org/projectb"  (or None if unknown)

Resolution timing (D#1810)
---------------------------
``STATE_DIR``, ``STATS_DB``, ``STATE_DB``, ``AUDIT_LOG``,
``CIRCUIT_BREAKER_HISTORY``, ``BLACKBOARD_DIR``, ``EXTERNAL_INTAKE_BASELINES``
and ``PARITY_HISTORY`` are **not** module-level constants — they are resolved
on every attribute access via :pep:`562` module ``__getattr__``. Binding one
of them to a name at import time (``from backend.state_paths import STATS_DB``
at module scope, or ``X = state_paths.STATS_DB`` at module scope) freezes it
for the life of the process and defeats any later ``AUTONOMOUS_TEAM_STATE_DIR``
override — this is the exact bug D#1810 fixes. Always go through the module,
even inside a function::

    from backend import state_paths
    ...
    def f():
        return state_paths.STATS_DB   # resolved fresh on every call

``STATS_DB`` precedence (AC-8): ``STATS_DB_PATH`` env var, if set, wins
outright (it is a legacy per-value override, kept for backwards-compat and
used by some tests for isolation without needing ``AUTONOMOUS_TEAM_STATE_DIR``
at all). Otherwise ``STATS_DB`` derives from ``STATE_DIR`` as before.

Pytest guard: with ``PYTEST_CURRENT_TEST`` set (pytest sets this for every
test) and ``AUTONOMOUS_TEAM_STATE_DIR`` unset, accessing any of the seven
``STATE_DIR``-derived values raises :class:`UnsandboxedStatePathError`
instead of silently falling back to the production directory. This applies
whether or not ``STATS_DB_PATH`` is set, EXCEPT that ``STATS_DB_PATH`` itself
is an explicit, unambiguous override and bypasses the guard for ``STATS_DB``
specifically — matching the existing behaviour of
``agent_run_tracker._db_path()``, which checks ``STATS_DB_PATH`` first.
``PARITY_HISTORY`` is not STATE_DIR-derived (it lives in-repo, under
``.autonomous-team/``, overridden separately via ``PARITY_HISTORY_PATH``) and
is not covered by the guard.

Known residual freeze NOT fixed by D#1810 (out of scope, on purpose)
---------------------------------------------------------------------
``backend/blackboard.py:_DEFAULT_ROOT`` still binds ``_resolve_default_root()``
— which reads ``BLACKBOARD_DIR`` from this module — at *its own* import time,
the exact same freeze-a-call-time-resolver pattern this file's
``__getattr__`` exists to eliminate, and the one this PR's ``backend/db.py``
counterpart (``_DB_PATH = _resolve_db_path()``) was explicitly sanctioned to
fix. ``blackboard.py`` is NOT fixed here because it is one of the
legacy-``is_symlink()``-branch files reserved for D#1908 PR 3 — touching it
crosses that Spec's boundary regardless of which line is edited. (That
reserved set was twelve files when this was written; D#1967 deleted the
duplicate resolvers that carried the branch in nine of them, leaving
``blackboard.py`` and ``db.py``.) One
consequence worth knowing: because the pytest guard now fires from inside
``BLACKBOARD_DIR``'s resolution, ``import backend.blackboard`` under pytest
with ``AUTONOMOUS_TEAM_STATE_DIR`` unset now raises at *import/collection*
time rather than at test-run time — a pre-existing bug made louder, not a
new one, but worth flagging since CI is disabled and a collection error is
easy to misread as this PR's fault. D#1908 PR 3 should apply the same
``_DEFAULT_ROOT``-becomes-a-function treatment there.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class UnsandboxedStatePathError(RuntimeError):
    """Raised when a STATE_DIR-derived path is accessed under pytest without
    ``AUTONOMOUS_TEAM_STATE_DIR`` set.

    Without this guard, a test that forgets to set ``AUTONOMOUS_TEAM_STATE_DIR``
    silently resolves to the production runtime-state directory and writes
    synthetic rows there. See D#1810.
    """


class RelativeStateDirError(ValueError):
    """Raised when ``AUTONOMOUS_TEAM_STATE_DIR`` is set to a relative path.

    A relative state dir resolves against the process cwd, so every
    STATE_DIR-derived path lands in whatever tree the process was launched
    from — for anything started at the repo root, that is the checkout
    itself. See D#1967.
    """


def _guard(var_name: str) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "AUTONOMOUS_TEAM_STATE_DIR"
    ):
        raise UnsandboxedStatePathError(
            f"backend.state_paths.{var_name}: refusing to resolve to the "
            "production state dir (~/.autonomous-forever-state/) while "
            "running under pytest. Set AUTONOMOUS_TEAM_STATE_DIR to a "
            "scratch directory before this value is accessed, e.g.: "
            'export AUTONOMOUS_TEAM_STATE_DIR="$(mktemp -d)"'
        )


def _state_dir(var_name: str = "STATE_DIR") -> Path:
    """Resolve the root runtime-state directory. Call-time, not cached.

    ``~`` is expanded. A value that is still not absolute after expansion is
    rejected rather than used: a relative state dir resolves against whatever
    cwd the process happens to have, so ``AUTONOMOUS_TEAM_STATE_DIR=.`` run
    from the repo root scatters ``state.db``, ``stats.duckdb``, ``audit.jsonl``
    and ``blackboard/`` straight into the checkout (D#1967). Failing loudly on
    a misconfigured state dir beats writing a database somewhere nobody will
    look for it.
    """
    env = os.environ.get("AUTONOMOUS_TEAM_STATE_DIR")
    if env is not None:
        # `if env:` here would treat a set-but-empty value the same as unset
        # and silently fall through to the default below — the exact
        # `os.environ.get(var, default)` footgun D#1908 PR 3 fixes at
        # a2a_broker.py. An empty string is a real, if degenerate, relative
        # path (`Path("").is_absolute()` is False), so it takes this branch
        # and is rejected explicitly instead of vanishing into the default.
        path = Path(env).expanduser()
        if not path.is_absolute():
            raise RelativeStateDirError(
                f"AUTONOMOUS_TEAM_STATE_DIR={env!r} is not an absolute path. "
                "Relative state dirs resolve against the current working "
                "directory, which scatters runtime state into whatever tree "
                "the process was launched from. Set it to an absolute path "
                "(e.g. $(mktemp -d), or ~/.autonomous-forever-state)."
            )
        return path
    _guard(var_name)
    return Path.home() / ".autonomous-forever-state"


def _stats_db() -> Path:
    """DuckDB metrics store (stats_writer / stats_reader).

    ``STATS_DB_PATH`` (legacy per-value override) wins outright and bypasses
    the pytest guard — it is an explicit override, not an ambient default.
    """
    env = os.environ.get("STATS_DB_PATH")
    if env:
        return Path(env)
    return _state_dir("STATS_DB") / "stats.duckdb"


def _state_db() -> Path:
    """SQLite key-value store (db.py / SqliteBlackboard)."""
    return _state_dir("STATE_DB") / "state.db"


def _audit_log() -> Path:
    """Append-only audit log (audit_trail.py)."""
    return _state_dir("AUDIT_LOG") / "audit.jsonl"


def _circuit_breaker_history() -> Path:
    """Circuit-breaker history log (circuit_breaker.py)."""
    return _state_dir("CIRCUIT_BREAKER_HISTORY") / "circuit-breaker-history.jsonl"


def _blackboard_dir() -> Path:
    """File-backed blackboard directory (blackboard.py Blackboard class)."""
    return _state_dir("BLACKBOARD_DIR") / "blackboard"


def _external_intake_baselines() -> Path:
    """External-intake approval baseline store (scripts/lib/intake_baseline.py).

    Binds the intake-approved label to the Discussion content that was
    actually reviewed (D#1672) — content hash / lastEditedAt /
    userContentEdits.totalCount observed at approval time, so a
    post-approval edit can be detected and the approval treated as
    dismissed until a human re-approves.
    """
    return _state_dir("EXTERNAL_INTAKE_BASELINES") / "external-intake-baselines.json"


def _parity_history() -> Path:
    """Append-only parity-experiment run history (parity_experiment.py).

    Lives in .autonomous-team/ alongside other team-state JSONL files, not
    under STATE_DIR. Override via PARITY_HISTORY_PATH env var (useful for
    tests). Not covered by the pytest guard — see module docstring.
    """
    env = os.environ.get("PARITY_HISTORY_PATH")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / ".autonomous-team" / "parity-history.jsonl"


_RESOLVERS = {
    "STATE_DIR": _state_dir,
    "STATS_DB": _stats_db,
    "STATE_DB": _state_db,
    "AUDIT_LOG": _audit_log,
    "CIRCUIT_BREAKER_HISTORY": _circuit_breaker_history,
    "BLACKBOARD_DIR": _blackboard_dir,
    "EXTERNAL_INTAKE_BASELINES": _external_intake_baselines,
    "PARITY_HISTORY": _parity_history,
}


def __getattr__(name: str):
    """PEP 562 module-level attribute resolution — fires only for names
    absent from module globals, which is what makes every access call-time.
    """
    resolver = _RESOLVERS.get(name)
    if resolver is not None:
        return resolver()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + list(_RESOLVERS.keys()))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def ensure_state_dir() -> Path:
    """Create STATE_DIR (and required subdirs) if they do not exist.

    Idempotent — safe to call on every startup.

    Returns
    -------
    Path
        The resolved STATE_DIR path.
    """
    state_dir = _state_dir("STATE_DIR")
    state_dir.mkdir(parents=True, exist_ok=True)
    _blackboard_dir().mkdir(parents=True, exist_ok=True)
    return state_dir


# ---------------------------------------------------------------------------
# Per-project path bundle
# ---------------------------------------------------------------------------


@dataclass
class ProjectPaths:
    """Resolved file-system paths for a named project.

    Constructed by :func:`for_project`.  All paths point into the project's
    dedicated state directory (``~/.<name>-state/`` by convention).

    Attributes
    ----------
    name:
        Short project name, e.g. ``"projectb"``.
    state_dir:
        Root of the project's runtime-state directory.
    stats_db:
        DuckDB metrics store for this project.
    state_db:
        SQLite key-value store for this project.
    audit_log:
        Append-only audit trail for this project.
    repo:
        GitHub ``owner/name`` slug, or ``None`` when unknown.
    """

    name: str
    state_dir: Path
    stats_db: Path
    state_db: Path
    audit_log: Path
    repo: str | None


def _served_state_dir(name: str) -> tuple[Path, dict] | None:
    """Return ``(STATE_DIR, runtime_data)`` when this process's own
    ``STATE_DIR`` belongs to *name*, else ``None``. Never raises.

    ``STATE_DIR`` (``AUTONOMOUS_TEAM_STATE_DIR``) is the directory this
    server process was actually configured to serve, which may sit outside
    ``$HOME``. Without this source, ``for_project()`` can only find such an
    adopter by discovering a marker file planted under ``$HOME`` — the exact
    workaround this helper exists to make unnecessary (D#2259).

    A served dir belongs to *name* when its ``dashboard-runtime.json``
    declares ``project_name == name`` (current ``start-dashboard.sh``
    writers), or, for older runtime files predating that field, when the
    directory's own basename is ``.{name}-state``. Without a match
    requirement, a single-project server would answer every project's
    question with its own directory instead of declining (D#2259 AC-4).

    Resolving ``STATE_DIR`` can raise (``UnsandboxedStatePathError`` under
    pytest with the env var unset, ``RelativeStateDirError`` on a relative
    value) and reading/parsing the runtime file can raise
    ``OSError``/``ValueError``. Any of those means this source can't be used
    right now — the caller's existing home-anchored steps still apply.
    """
    try:
        served = _state_dir("STATE_DIR")
        data = json.loads((served / "dashboard-runtime.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        declared = data.get("project_name")
        if declared == name:
            return served, data
        if declared is None and served.name == f".{name}-state":
            return served, data
        return None
    except Exception as exc:  # noqa: BLE001 — a misconfigured/foreign state dir must not 500 the server
        logger.debug("for_project(%r): served-state-dir source unavailable: %s", name, exc)
        return None


def for_project(name: str) -> ProjectPaths:
    """Return a :class:`ProjectPaths` bundle for *name*.

    Resolution order for state_dir:

    0. This process's own ``STATE_DIR``, when its ``dashboard-runtime.json``
       declares this project (see :func:`_served_state_dir`) — lets an
       adopter whose state dir sits outside ``$HOME`` resolve without a
       marker file planted under ``$HOME``.
    1. ``~/.<name>-state/dashboard-runtime.json`` → ``state_dir`` field
    2. ``~/.<name>-state/project.json`` → ``state_dir`` guessed from parent
    3. Fallback: ``~/.<name>-state/``

    Step 0 is additive and never removes a project that resolved via steps
    1-3 before it existed: it only applies when the served dir actually
    declares (or, for old files, is named for) *name*; any other outcome —
    including every exception above — falls through to steps 1-3 unchanged.

    The ``repo`` field is read from whichever config file is found first
    (step 0, then ``dashboard-runtime.json``, then ``project.json``).
    Returns ``None`` when nothing readable contains a repo slug.

    This function is intentionally fast (no network calls, reads ≤ 2 small
    JSON files) and safe to call on every request.

    Parameters
    ----------
    name:
        Short project name, e.g. ``"projectb"`` or ``"autonomous-forever"``.

    Returns
    -------
    ProjectPaths
        Resolved path bundle.  ``state_dir`` is guaranteed to be set even
        when no config file is found (falls back to ``~/.<name>-state/``).
    """
    served = _served_state_dir(name)
    if served is not None:
        served_dir, data = served
        sd = data.get("state_dir")
        state_dir = Path(sd) if sd else served_dir
        repo = data.get("repo") or data.get("project_repo") or None
        return ProjectPaths(
            name=name,
            state_dir=state_dir,
            stats_db=state_dir / "stats.duckdb",
            state_db=state_dir / "state.db",
            audit_log=state_dir / "audit.jsonl",
            repo=repo,
        )

    home = Path.home()
    conventional_state_dir = home / f".{name}-state"

    repo: str | None = None
    state_dir: Path = conventional_state_dir

    # Try dashboard-runtime.json first (written by start-dashboard.sh)
    runtime_json = conventional_state_dir / "dashboard-runtime.json"
    if runtime_json.exists():
        try:
            data = json.loads(runtime_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Prefer explicit state_dir field; fall back to parent dir
                sd = data.get("state_dir")
                if sd:
                    state_dir = Path(sd)
                repo = data.get("repo") or data.get("project_repo") or None
        except (OSError, ValueError):
            pass

    # Fall back to project.json (written by coldstart-project.sh)
    if repo is None:
        project_json = conventional_state_dir / "project.json"
        if project_json.exists():
            try:
                data = json.loads(project_json.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    repo = data.get("repo") or None
            except (OSError, ValueError):
                pass

    return ProjectPaths(
        name=name,
        state_dir=state_dir,
        stats_db=state_dir / "stats.duckdb",
        state_db=state_dir / "state.db",
        audit_log=state_dir / "audit.jsonl",
        repo=repo,
    )


if __name__ == "__main__":
    # Shell callers need STATE_DIR too, and re-deriving it in bash would make a
    # second source of truth. `python3 backend/state_paths.py` prints the
    # resolved directory and nothing else, so it composes: "$(python3
    # backend/state_paths.py)/tree-manifests".
    print(_state_dir("STATE_DIR"))
