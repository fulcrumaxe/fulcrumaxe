"""
Backend server mode — reads JSON prompts from stdin or a named FIFO,
streams events back as newline-delimited JSON. Runs iterations through the
Claude Agent SDK prompt lane (backend.prompt_lane.sdk_lane).

Protocol (newline-delimited JSON over stdin/stdout):
  Input:  {"id": "req-1", "prompt": "...", "session_id": null}
  Output: {"id": "req-1", "type": "thinking", "content": "..."}
          {"id": "req-1", "type": "tool_use", "tool": "bash", "input": {...}}
          {"id": "req-1", "type": "tool_result", "tool": "bash", "output": "..."}
          {"id": "req-1", "type": "content", "content": "..."}
          {"id": "req-1", "type": "done", "session_id": "..."}
          {"id": "req-1", "type": "error", "error": "..."}

Multi-agent extensions (TUI tab support):
  {"id": "req-1", "type": "agent_spawn", "agent_id": "<uuid>", "agent_name": "<role>", "parent_id": "agent-0"}
  {"id": "req-1", "type": "agent_event", "agent_id": "<uuid>", "inner": <BackendEvent>}
  {"id": "req-1", "type": "agent_exit",  "agent_id": "<uuid>", "exit_code": 0}

Top-level (agent-0) events are emitted unwrapped for backward compatibility.
All logging goes to stderr. stdout is the protocol channel only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from backend._repo import REPO as _GH_REPO
from backend._repo import REPO_OWNER as _REPO_OWNER
from backend._repo import REPO_NAME as _REPO_NAME

from backend.prompt_lane import sdk_lane

from backend import state_paths as _state_paths
from backend import dashboard_origins as _dashboard_origins
from backend import rpc_project_scope as _rpc_project_scope

_REPO_ROOT = Path(__file__).resolve().parent.parent

FIFO_PATH = "/tmp/af-trigger.fifo"

#: Legacy location, predates the STATE_DIR convention. Kept only so
#: ``_migrate_legacy_db_path()`` can find and move an old file forward.
_LEGACY_DB_PATH = Path.home() / ".autonomous-forever" / "server.db"


def _db_path() -> Path:
    # Resolved at call time, not import time — see D#1810.
    return _state_paths.STATE_DIR / "server.db"


TOKEN_PATH = _REPO_ROOT / ".autonomous-team" / "dashboard-token"

#: Engine's own agent-feed.jsonl. Bound at import to this checkout by
#: construction, same as before D#2261 PR-b. Kept for the SSE endpoints
#: (/events, /feed), which have no project param and only ever serve this
#: process's own feed. RPC handlers that accept a project param must call
#: _agent_feed_path(project) below instead — using this constant directly
#: for a project-scoped read is the exact class-(c) leak PR-b fixes.
AGENT_FEED_PATH = _REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"


def _agent_feed_path(project: "str | None") -> Path:
    """Resolve agent-feed.jsonl for *project*, call-time (D#2261 PR-b).

    AGENT_FEED_PATH above is a module constant bound to this checkout's own
    __file__ at import — no per-request env override can redirect it, which
    is exactly how agent-feed.jsonl became the reported bug (an adopter
    dashboard serving this engine's own feed under the adopter's name).

    Resolution mirrors _rpc_loop_timeline's for loop-metrics.jsonl, the
    sibling file written to the same .autonomous-team/ directory:
      1. <project's state_dir>/agent-feed.jsonl       (future convention)
      2. <project's own repo checkout>/.autonomous-team/agent-feed.jsonl
         (state_dir.parent / project — where a project writes its own feed
         today, alongside its .autonomous-team/loop-metrics.jsonl)
    When project is falsy, returns the engine's own AGENT_FEED_PATH
    unchanged — existing behavior for the no-project (engine dashboard)
    case is untouched.

    Callers already treat a missing/unreadable file as "no events" (broad
    try/except around .read_text()), so when neither location exists this
    deliberately returns the repo-checkout candidate rather than falling
    back to the engine's own AGENT_FEED_PATH — a nonexistent path yields an
    empty read, never someone else's data.
    """
    if not project:
        return AGENT_FEED_PATH

    from backend.state_paths import for_project as _fp  # noqa: PLC0415
    project_paths = _fp(project)
    candidate = project_paths.state_dir / "agent-feed.jsonl"
    if candidate.exists():
        return candidate
    project_repo_root = project_paths.state_dir.parent / project
    return project_repo_root / ".autonomous-team" / "agent-feed.jsonl"

# CORS: allow-list discovery moved to backend/dashboard_origins.py
# (module-per-feature — server.py is a hub file). One-line alias keeps
# existing call sites and tests working.
_compute_allowed_origins = _dashboard_origins.compute_allowed_origins


logger = logging.getLogger(__name__)


def _emit(event: dict[str, Any]) -> None:
    """Write one JSON line to stdout (the protocol channel)."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _log(msg: str) -> None:
    """Write a log line via the logging module (goes to stderr)."""
    logger.info(msg)


def _migrate_legacy_db_path() -> None:
    """One-time migration of server.db from the pre-STATE_DIR location.

    Historically ``DB_PATH`` was hardcoded to ``_LEGACY_DB_PATH`` above (a
    directory named after this project's pre-rename name), a second state
    directory independent of ``backend.state_paths.STATE_DIR`` (and thus
    invisible to ``AUTONOMOUS_TEAM_STATE_DIR`` overrides). This moves an
    existing legacy file forward into ``STATE_DIR`` so a forker's session
    history isn't silently orphaned by the path change.

    Safe to call on every startup — no-ops once the migration has happened.
    """
    if not _LEGACY_DB_PATH.exists():
        return
    db_path = _db_path()
    if db_path.exists():
        # Both locations have a file. Don't guess which is authoritative —
        # silently overwriting either one could lose history. Keep using the
        # new-location file (it's what every future startup will read) and
        # just warn so an operator can reconcile/delete the legacy copy
        # by hand if they want to.
        _log(
            f"WARNING: legacy DB at {_LEGACY_DB_PATH} and new DB at {db_path} "
            "both exist; leaving both in place and using the new-location DB. "
            "Reconcile manually if the legacy file has data you need."
        )
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Copy first, verify the copy landed, then remove the original —
        # safer than a bare rename if the two paths ever end up on
        # different filesystems (os.rename would raise; shutil.move
        # already falls back to copy+delete, but we want an explicit
        # verification step before touching the source file).
        shutil.copy2(_LEGACY_DB_PATH, db_path)
        if db_path.stat().st_size != _LEGACY_DB_PATH.stat().st_size:
            raise OSError("copied file size mismatch")
        _LEGACY_DB_PATH.unlink()
        _log(f"migrated legacy DB {_LEGACY_DB_PATH} -> {db_path}")
    except OSError as exc:
        _log(
            f"WARNING: failed to migrate legacy DB {_LEGACY_DB_PATH} -> "
            f"{db_path}: {exc}. Leaving legacy file in place; a fresh DB "
            "will be created at the new location."
        )
        # Best-effort cleanup of a partial copy so we don't leave a
        # truncated file at the new location.
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass


async def _handle_request(req: dict[str, Any]) -> None:
    """Dispatch a single request through the prompt lane and stream events to stdout.

    Subagent lifecycle (agent_spawn / agent_event / agent_exit) is emitted
    directly by sdk_lane.run_prompt from TaskStarted/Progress/Notification/
    Updated messages in the same stream — there is no separate proxy step
    here, unlike the pre-migration _install_multiagent_proxy monkey-patch
    the old prompt-lane implementation used.
    """
    req_id = req.get("id", "unknown")
    prompt = req.get("prompt", "")
    session_id: str | None = req.get("session_id")

    if not prompt:
        _emit({"id": req_id, "type": "error", "error": "missing 'prompt' field"})
        return

    _log(f"req={req_id} session={session_id} prompt={prompt[:60]!r}")

    try:
        async for event in sdk_lane.run_prompt(prompt, session_id):
            _emit({"id": req_id, **event})
    except Exception as exc:
        _emit({"id": req_id, "type": "error", "error": str(exc)})


async def _stdin_reader(queue: asyncio.Queue) -> None:
    """Read newline-delimited JSON from stdin and push requests onto the queue."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line_bytes = await reader.readline()
        except Exception as exc:
            _log(f"stdin read error: {exc}")
            break
        if not line_bytes:
            # EOF — stdin closed, which is fine for FIFO-only mode.
            break
        line = line_bytes.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({"type": "error", "error": f"invalid JSON: {exc}"})
            continue
        await queue.put(req)


async def _fifo_reader(queue: asyncio.Queue) -> None:
    """
    Read newline-delimited JSON from the named FIFO and push onto the queue.

    The FIFO is reopened after each writer disconnects (EOF) so that
    subsequent cron triggers are handled correctly.
    """
    # Create FIFO if it does not exist.
    try:
        os.mkfifo(FIFO_PATH)
    except FileExistsError:
        pass
    except Exception as exc:
        _log(f"mkfifo failed: {exc} — FIFO input disabled")
        return

    loop = asyncio.get_running_loop()

    while True:
        try:
            # Open FIFO non-blocking to avoid hanging the event loop.
            # os.O_RDONLY | os.O_NONBLOCK: opens without blocking for a writer.
            fd = os.open(FIFO_PATH, os.O_RDONLY | os.O_NONBLOCK)
            # Switch to blocking mode for actual reads via asyncio.
            os.set_blocking(fd, True)

            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await loop.connect_read_pipe(
                lambda: protocol, os.fdopen(fd, "rb")
            )

            while True:
                try:
                    line_bytes = await reader.readline()
                except Exception:
                    break
                if not line_bytes:
                    # Writer closed — break and reopen.
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as exc:
                    _emit({"type": "error", "error": f"invalid JSON from FIFO: {exc}"})
                    continue
                await queue.put(req)

            transport.close()
            # Brief pause before reopening so we do not spin on a broken FIFO.
            await asyncio.sleep(0.1)

        except Exception as exc:
            _log(f"FIFO error: {exc} — retrying in 1s")
            await asyncio.sleep(1)


async def _run_request_with_timeout(req: dict[str, Any]) -> None:
    """Wrap _handle_request with an optional timeout.

    Set AF_REQUEST_TIMEOUT=0 for no timeout (interactive TUI mode).
    Default is 300s for cron. The TUI launcher should set this to 0.
    """
    timeout = int(os.environ.get("AF_REQUEST_TIMEOUT", "300"))
    req_id = req.get("id", "unknown")
    if timeout <= 0:
        # No timeout — interactive mode
        await _handle_request(req)
    else:
        try:
            await asyncio.wait_for(_handle_request(req), timeout=timeout)
        except asyncio.TimeoutError:
            _log(f"req={req_id} timed out after {timeout}s")
            _emit({"id": req_id, "type": "error", "error": f"iteration timeout after {timeout}s"})
            _emit({"id": req_id, "type": "done", "session_id": None})


async def _dispatcher(queue: asyncio.Queue) -> None:
    """Pull requests from the queue and spawn asyncio tasks per request."""
    while True:
        req = await queue.get()
        asyncio.create_task(
            _run_request_with_timeout(req),
            name=f"req-{req.get('id', 'unknown')}",
        )


async def _main() -> None:
    # Set up structured logging before anything else.
    # Import here to avoid circular issues if server.py is imported as a module.
    from backend.log import setup_logging  # noqa: E402
    setup_logging()

    _log("initializing state")
    _migrate_legacy_db_path()

    legacy_af_vars = [v for v in ("AF_PROVIDER", "AF_API_KEY", "AF_BASE_URL", "AF_MAX_TOKENS") if os.environ.get(v)]
    if legacy_af_vars:
        _log(
            f"WARNING: {', '.join(legacy_af_vars)} set but ignored — the prompt lane now "
            "runs on the Claude Agent SDK (Claude models only, via CLAUDE_CODE_OAUTH_TOKEN / "
            "ANTHROPIC_API_KEY / `claude login`). These vars used to route to the old "
            "OpenAI-compatible provider lane (e.g. local Ollama); that path is gone. "
            "See wiki/Local-Ollama-TUI.md."
        )

    if not sdk_lane.has_credential():
        _emit({
            "type": "error",
            "error": "no SDK credential — set CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or run `claude login`",
        })
        sys.exit(1)

    model_id = os.environ.get("AF_MODEL") or "default"
    _emit({"type": "ready", "version": "0.2.0", "model": model_id})

    queue: asyncio.Queue = asyncio.Queue()

    # Start input readers and dispatcher concurrently.
    tasks = [
        asyncio.create_task(_stdin_reader(queue), name="stdin-reader"),
        asyncio.create_task(_fifo_reader(queue), name="fifo-reader"),
        asyncio.create_task(_dispatcher(queue), name="dispatcher"),
    ]

    try:
        # Run until stdin EOF (stdin reader task finishes).
        # The FIFO reader and dispatcher keep running while stdin is open.
        stdin_task = tasks[0]
        await asyncio.wait(
            [stdin_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Drain any queued items before shutdown.
        while not queue.empty():
            await asyncio.sleep(0.05)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _log("server exiting")


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _load_or_create_token(rotate: bool = False) -> str:
    """Load the bearer token from disk, creating it if absent (or rotating)."""
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if rotate or not TOKEN_PATH.exists():
        token = secrets.token_urlsafe(32)
        TOKEN_PATH.write_text(token)
        TOKEN_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        return token
    return TOKEN_PATH.read_text().strip()


# ---------------------------------------------------------------------------
# JSON-RPC method registry (HTTP adapter)
# ---------------------------------------------------------------------------

_RPC_METHODS: dict[str, Any] = {}


def _rpc_method(name: str):
    """Decorator to register a JSON-RPC method handler."""
    def decorator(fn):
        _RPC_METHODS[name] = fn
        return fn
    return decorator


def _project_stats_db(project: str | None) -> "Path | None":
    """Return the stats.duckdb path for *project*, or None when project is absent.

    Convention: when a handler receives a ``project`` param it calls this helper
    and, if the result is not None, temporarily overrides ``STATS_DB_PATH`` so
    that stats_reader / stats_writer / agent_run_reader pick up the right DB.

    Usage in a handler::

        project = params.get("project") or None
        stats_db = _project_stats_db(project)
        old = os.environ.get("STATS_DB_PATH")
        if stats_db:
            os.environ["STATS_DB_PATH"] = str(stats_db)
        try:
            result = _some_reader()
        finally:
            if stats_db:
                if old is None:
                    os.environ.pop("STATS_DB_PATH", None)
                else:
                    os.environ["STATS_DB_PATH"] = old
    """
    if not project:
        return None
    try:
        from backend.state_paths import for_project as _fp  # noqa: PLC0415
        return _fp(project).stats_db
    except Exception:
        return None


@_rpc_method("loop.start")
def _rpc_loop_start(params: dict) -> dict:
    from backend.control_plane import ControlPlane as _CP  # noqa: PLC0415
    cp = _CP()
    if not cp.gate_enabled("loop_start"):
        raise ValueError("loop_start_disabled_by_gate")
    from backend.active_loops import create_loop
    prompt = str(params.get("prompt", ""))
    cadence = params.get("cadence_seconds")
    if cadence is not None:
        cadence = int(cadence)
    # Use current process PID as a placeholder (real loop runners would register their own PID)
    entry = create_loop(prompt, cadence, os.getpid())
    return {"loop_id": entry["loop_id"], "started_at": entry["started_at"]}


@_rpc_method("loop.stop")
def _rpc_loop_stop(params: dict) -> dict:
    from backend.active_loops import stop_loop
    loop_id = str(params.get("loop_id", ""))
    entry = stop_loop(loop_id)
    if entry is None:
        raise ValueError(f"loop not found: {loop_id}")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"loop_id": loop_id, "stopped_at": now}


@_rpc_method("loop.list")
def _rpc_loop_list(params: dict) -> dict:
    from backend.active_loops import list_loops
    loops = list_loops()
    return {"loops": loops}


@_rpc_method("dashboard.gates_snapshot")
def _rpc_dashboard_gates_snapshot(_params: dict) -> dict:
    """Return all control-plane gates as a flat dict.

    Dashboard reads this on mount to adapt UI (e.g. disable loop.start form
    when gates.loop_start is false).  Read-only — never modifies state.
    """
    from backend.control_plane import ControlPlane as _CP  # noqa: PLC0415
    cp = _CP()
    return {"gates": cp.list_gates()}


@_rpc_method("loop.events")
def _rpc_loop_events(params: dict) -> dict:
    from backend.active_loops import get_loop
    loop_id = str(params.get("loop_id", ""))
    since_event_id = params.get("since_event_id")
    limit = int(params.get("limit", 50))

    loop = get_loop(loop_id)
    if loop is None:
        raise ValueError(f"loop not found: {loop_id}")

    project = params.get("project") or None

    # Read events from agent-feed.jsonl, filtering by loop_id annotation
    events: list[dict] = []
    try:
        lines = _agent_feed_path(project).read_text().splitlines()
        found_since = since_event_id is None
        for line in lines:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            eid = ev.get("id") or ev.get("event_id")
            if not found_since:
                if eid == since_event_id:
                    found_since = True
                continue
            if ev.get("loop_id") == loop_id:
                events.append(ev)
                if len(events) >= limit:
                    break
    except Exception:
        pass

    next_since_id = events[-1].get("id") if events else None
    return {"events": events, "next_since_id": next_since_id}


@_rpc_method("agents.tail")
def _rpc_agents_tail(params: dict) -> dict:
    since = params.get("since")
    limit = int(params.get("limit", 50))
    flt = params.get("filter") or {}
    role_filter = flt.get("role")
    discussion_filter = flt.get("discussion")
    event_type_filter = flt.get("event_type")
    project = params.get("project") or None

    events: list[dict] = []
    try:
        lines = _agent_feed_path(project).read_text().splitlines()
        found_since = since is None
        for line in lines:
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = ev.get("timestamp") or ev.get("ts") or ""
            if not found_since:
                if ts >= since:
                    found_since = True
                else:
                    continue
            if role_filter and ev.get("role") != role_filter:
                continue
            if discussion_filter is not None and ev.get("discussion") != discussion_filter:
                continue
            if event_type_filter and ev.get("event_type") != event_type_filter:
                continue
            events.append(ev)
            if len(events) >= limit:
                break
    except Exception:
        pass

    next_since = events[-1].get("timestamp") if events else since
    return {"events": events, "next_since": next_since}


@_rpc_method("kpi.history")
def _rpc_kpi_history(params: dict) -> list:
    """Return merged-PRs-per-day for the last *days* days.

    Params: {"days": int, "project": str}  (default 30, min 1)
    Returns: [{"date": "YYYY-MM-DD", "count": int}, ...]

    Raises JSON-RPC -32602 when days < 1.

    Per-project: kpi.history runs git log in the project's repo root.  For
    non-AF projects we don't have a local checkout, so we return [] rather
    than leaking AF's git history.
    """
    fixture_path = _REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
    if os.environ.get("AF_E2E_FIXTURES") == "1" and fixture_path.exists():
        try:
            return json.loads(fixture_path.read_text()).get("kpi_history", [])
        except Exception:
            pass

    days_raw = params.get("days", 30)
    try:
        days = int(days_raw)
    except (TypeError, ValueError):
        raise _rpc_invalid_params(f"days must be an integer, got {days_raw!r}")
    if days < 1:
        raise _rpc_invalid_params("days must be >= 1")

    # Per-project scoping: kpi_engine.history() runs git log.  Resolve the
    # project's local checkout path (state_dir.parent / <name>) and pass it
    # to kpi_engine.history().  Returns [] when the checkout doesn't exist so
    # we never serve AF git history to a different project.
    project = params.get("project") or None
    project_repo_root = None
    if project:
        from backend.state_paths import for_project as _fp  # noqa: PLC0415
        project_paths = _fp(project)
        candidate = project_paths.state_dir.parent / project
        if candidate.exists():
            project_repo_root = candidate
        else:
            return []

    from backend.kpi_engine import history as _kpi_history
    return _kpi_history(days, repo_root=project_repo_root)


@_rpc_method("kpi.cycle_time")
def _rpc_kpi_cycle_time(params: dict) -> list:
    """Return cycle-time histogram for merged PRs in the last *days* days.

    Params: {"days": int, "project": str}  (days default 90, min 1)
    Returns: [{"bucket": "0-2h"|"2-6h"|"6-24h"|"24h+", "count": int}, ...]

    Per-project: cycle_time reads the registry and blackboard from REPO_ROOT.
    For non-AF projects without a local checkout, returns zeroed buckets rather
    than leaking AF's data.
    """
    fixture_path = _REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
    if os.environ.get("AF_E2E_FIXTURES") == "1" and fixture_path.exists():
        try:
            return json.loads(fixture_path.read_text()).get("kpi_cycle_time", [])
        except Exception:
            pass

    days_raw = params.get("days", 90)
    try:
        days = int(days_raw)
    except (TypeError, ValueError):
        raise _rpc_invalid_params(f"days must be an integer, got {days_raw!r}")
    if days < 1:
        raise _rpc_invalid_params("days must be >= 1")

    project = params.get("project") or None
    project_repo_root = None
    if project:
        from backend.state_paths import for_project as _fp  # noqa: PLC0415
        project_paths = _fp(project)
        candidate = project_paths.state_dir.parent / project
        if candidate.exists():
            project_repo_root = candidate
        else:
            # No local checkout — return zeroed buckets, don't leak AF data.
            return [
                {"bucket": "0-2h", "count": 0},
                {"bucket": "2-6h", "count": 0},
                {"bucket": "6-24h", "count": 0},
                {"bucket": "24h+", "count": 0},
            ]

    from backend.kpi_engine import cycle_time_histogram as _cth
    return _cth(days=days, repo_root=project_repo_root)


@_rpc_method("loop.timeline")
def _rpc_loop_timeline(params: dict) -> list:
    """Return the last N loop iterations from loop-metrics.jsonl.

    Params: {"limit": int, "project": str}  (default 100, max 500)
    Returns: [{"timestamp", "duration_seconds", "agents_spawned", "prs_merged",
               "discussions_scanned", "prs_scanned", "idle", "error"}, ...]
             ordered oldest → newest. Malformed JSONL lines are silently skipped.

    Per-project: looks for loop-metrics.jsonl in the project's state_dir.
    Falls back to [] rather than serving AF's loop data for other projects.
    """
    fixture_path = _REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
    if os.environ.get("AF_E2E_FIXTURES") == "1" and fixture_path.exists():
        try:
            return json.loads(fixture_path.read_text()).get("loop_timeline", [])
        except Exception:
            pass

    limit_raw = params.get("limit", 100)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        raise _rpc_invalid_params(f"limit must be an integer, got {limit_raw!r}")
    limit = max(1, min(limit, 500))

    # By default exclude test-origin rows (from Puppeteer/E2E runs).
    # Pass include_test=true to bypass the filter for debugging.
    include_test = bool(params.get("include_test", False))

    # Per-project scoping: backend.loop_metrics_path is the one resolver,
    # shared with loop.iteration_detail below and with
    # rpc/stats_loop_idle_ratio.py (D#2327). It returns None when a named
    # project has no reachable metrics file; an empty timeline is the honest
    # answer here — a list of zero iterations says what it means — so don't
    # fall through to this checkout's own data.
    from backend.loop_metrics_path import resolve_loop_metrics_path  # noqa: PLC0415

    project = params.get("project") or None
    metrics_path = resolve_loop_metrics_path(project, _REPO_ROOT)
    if metrics_path is None:
        return []

    import collections
    buf: collections.deque = collections.deque(maxlen=limit)
    with metrics_path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                import sys as _sys
                print(f"loop.timeline: skipping malformed line: {raw[:80]!r}", file=_sys.stderr)
                continue
            # Rows missing 'origin' are treated as "cron" for back-compat.
            row_origin = row.get("origin", "cron")
            if not include_test and row_origin == "test":
                continue
            # Also handle the legacy 'ts' field name (pre-#487 rows).
            # A row of unknown provenance can carry a raw epoch int here
            # instead of an ISO string (D#2315) -- coerce so this always
            # comes out a str, matching what the dashboard reads it as.
            _raw_timestamp = row.get("timestamp") or row.get("ts", "")
            timestamp = _raw_timestamp if isinstance(_raw_timestamp, str) else str(_raw_timestamp)
            # Sanitise duration: historic rows stored the Unix epoch timestamp
            # instead of a delta, producing values in the billions (54+ years).
            # A loop iteration longer than 24 h is definitionally bad data.
            _MAX_ITER_DURATION_S = 86_400  # 24 hours
            raw_dur = row.get("duration_seconds", row.get("duration_s", 0)) or 0
            duration_seconds = raw_dur if raw_dur <= _MAX_ITER_DURATION_S else 0
            buf.append({
                "timestamp": timestamp,
                "duration_seconds": duration_seconds,
                "agents_spawned": row.get("agents_spawned", 0),
                "prs_merged": row.get("prs_merged", 0),
                "discussions_scanned": row.get("discussions_scanned", 0),
                "prs_scanned": row.get("prs_scanned", 0),
                "idle": bool(row.get("idle", False)),
                "error": row.get("error") or None,
            })
    return list(buf)


@_rpc_method("loop.iteration_detail")
def _rpc_loop_iteration_detail(params: dict) -> dict:
    """Return full detail for one loop iteration.

    Params: {"timestamp": str}  (ISO8601, must match a row in loop-metrics.jsonl)
    Returns: {"timestamp", "metrics": <row>, "log": <str|null>, "log_path": <str|null>}

    The log file is located under .autonomous-team/loop-runs/autonomous-forever/
    using the pattern YYYYMMDDTHHMMSSZ.log (UTC, Z suffix). Content is capped at
    64 KB; larger files are truncated with a "[truncated: original size N bytes]" marker.
    Returns log: null when the file does not exist (older entries pre-date that directory).
    """
    _MAX_LOG_BYTES = 64 * 1024  # 64 KB

    fixture_path = _REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
    if os.environ.get("AF_E2E_FIXTURES") == "1" and fixture_path.exists():
        try:
            fixtures = json.loads(fixture_path.read_text())
            detail = fixtures.get("loop_iteration_detail")
            if detail:
                return detail
        except Exception:
            pass

    ts = params.get("timestamp", "")
    if not ts:
        raise _rpc_invalid_params("timestamp is required")

    # Per-project scoping: same resolver as loop.timeline above
    # (backend.loop_metrics_path). The not-found policy deliberately differs
    # from loop.timeline's: this response also carries the run log, so a
    # missing metrics file yields an empty metrics row rather than an empty
    # response. That decision belongs to each caller, which is why the
    # resolver returns a path-or-None instead of deciding for them.
    from backend.loop_metrics_path import resolve_loop_metrics_path  # noqa: PLC0415

    project = params.get("project") or None
    metrics_path = resolve_loop_metrics_path(project, _REPO_ROOT)
    metrics_row: dict = {}
    if metrics_path is not None:
        with metrics_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if row.get("timestamp") == ts:
                    metrics_row = row
                    # Keep scanning — take the last match in case of duplicates
    # Don't error if metrics row not found; return the detail with what we have

    # Normalise counter fields: when the metrics row IS present, default missing
    # counters to 0 so the dashboard never shows '—' for a completed iteration.
    if metrics_row:
        for _counter in ("agents_spawned", "prs_merged", "discussions_scanned", "prs_scanned"):
            if _counter not in metrics_row:
                metrics_row[_counter] = 0

    # Convert ISO timestamp to log filename: 2026-04-11T01:41:20Z → 20260411T014120Z.log
    import re as _re
    m = _re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z?$",
        ts.strip(),
    )
    if not m:
        raise _rpc_invalid_params(
            f"timestamp must be ISO8601 (YYYY-MM-DDTHH:MM:SSZ), got {ts!r}"
        )
    log_content: "str | None" = None
    log_path_str: "str | None" = None

    # Resolve log directory: use project state_dir when scoped, AF default otherwise.
    if project:
        from backend.state_paths import for_project as _fp  # noqa: PLC0415
        _pp = _fp(project)
        _project_repo_root = _pp.state_dir.parent / project
        log_dir = _project_repo_root / ".autonomous-team" / "loop-runs" / project
        if not log_dir.exists():
            log_dir = _pp.state_dir / "loop-runs" / project
    else:
        log_dir = _REPO_ROOT / ".autonomous-team" / "loop-runs" / "autonomous-forever"

    # Prefer the run_id field from the metrics row (Bug 3 fix).
    # The log filename is keyed off run_id, not the timestamp.
    # Fall back to the timestamp-derived filename for legacy rows without run_id.
    run_id = metrics_row.get("run_id") if metrics_row else None

    if run_id:
        # Glob for <run_id>*.log to catch suffix variants (-1, -1-2, etc.)
        # Pick alphabetically-last match for determinism.
        candidates = sorted(log_dir.glob(f"{run_id}*.log"))
        log_file = candidates[-1] if candidates else (log_dir / f"{run_id}.log")
    else:
        ts_fname = f"{m.group(1)}{m.group(2)}{m.group(3)}T{m.group(4)}{m.group(5)}{m.group(6)}Z.log"
        log_file = log_dir / ts_fname

    if log_file.exists():
        log_path_str = str(log_file)
        size = log_file.stat().st_size
        with log_file.open("r", encoding="utf-8", errors="replace") as lf:
            log_content = lf.read(_MAX_LOG_BYTES)
        if size > _MAX_LOG_BYTES:
            log_content += f"\n[truncated: original size {size} bytes]"

    from backend.loop_log_references import extract_references as _extract_refs  # noqa: PLC0415
    references = _extract_refs(log_content or "")

    return {
        "timestamp": ts,
        "metrics": metrics_row,
        "log": log_content,
        "log_path": log_path_str,
        "references": references,
    }


@_rpc_method("cost.per_discussion")
def _rpc_cost_per_discussion(params: dict) -> dict | None:
    """Return cost breakdown for a single Discussion.

    Params: {"discussion": int}
    Returns: per-Discussion cost entry with agent_breakdown and pr_breakdown, or null.
    """
    disc_raw = params.get("discussion")
    if disc_raw is None:
        raise _rpc_invalid_params("discussion parameter required")
    try:
        disc_num = int(disc_raw)
    except (TypeError, ValueError):
        raise _rpc_invalid_params(f"discussion must be an integer, got {disc_raw!r}")

    try:
        from backend.cost_tracker import CostTracker as _CT  # noqa: PLC0415
        full = _CT().get_session_cost()
    except Exception as exc:
        _log(f"cost.per_discussion: CostTracker failed: {exc}")
        return None

    return next(
        (e for e in full.get("by_discussion", []) if e.get("discussion") == disc_num),
        None,
    )


@_rpc_method("cost.by_discussion")
def _rpc_cost_by_discussion(params: dict) -> list:
    """Return top-N discussions by token spend within the last *days* days.

    Params: {"top": int, "days": int}  (top default 10 min 1; days default 90 min 1)
    Returns: [{"discussion": int, "tokens": int, "usd": float}, ...]

    Raises JSON-RPC -32602 when top < 1 or days < 1.
    """
    fixture_path = _REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
    if os.environ.get("AF_E2E_FIXTURES") == "1" and fixture_path.exists():
        try:
            return json.loads(fixture_path.read_text()).get("cost_by_discussion", [])
        except Exception:
            pass

    top_raw = params.get("top", 10)
    try:
        top = int(top_raw)
    except (TypeError, ValueError):
        raise _rpc_invalid_params(f"top must be an integer, got {top_raw!r}")
    if top < 1:
        raise _rpc_invalid_params("top must be >= 1")

    days_raw = params.get("days", 90)
    try:
        days = int(days_raw)
    except (TypeError, ValueError):
        raise _rpc_invalid_params(f"days must be an integer, got {days_raw!r}")
    if days < 1:
        raise _rpc_invalid_params("days must be >= 1")

    import datetime as _dt
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)

    try:
        from backend.cost_tracker import CostTracker as _CT  # noqa: PLC0415
        full = _CT().get_session_cost()
    except Exception as exc:
        _log(f"cost.by_discussion: CostTracker failed: {exc}")
        return []

    # Re-aggregate by_discussion within the days window using by_agent entries.
    # Each by_agent entry has a 'finished' ISO timestamp and 'discussion' number.
    disc_totals: dict[int, dict] = {}
    for agent in full.get("by_agent", []):
        disc = agent.get("discussion")
        if disc is None:
            continue
        disc_int = int(disc)
        finished_str = agent.get("finished")
        if finished_str:
            try:
                finished_dt = _dt.datetime.fromisoformat(finished_str.replace("Z", "+00:00"))
                if finished_dt.tzinfo is None:
                    finished_dt = finished_dt.replace(tzinfo=_dt.timezone.utc)
                if finished_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                # Unparseable timestamp — include the entry (fail open)
                pass
        tokens = (agent.get("input", 0) or 0) + (agent.get("output", 0) or 0)
        usd = float(agent.get("cost_usd") or 0.0)
        if disc_int not in disc_totals:
            disc_totals[disc_int] = {"tokens": 0, "usd": 0.0}
        disc_totals[disc_int]["tokens"] += tokens
        disc_totals[disc_int]["usd"] += usd

    out = sorted(
        [
            {"discussion": disc, "tokens": int(t["tokens"]), "usd": round(t["usd"], 6)}
            for disc, t in disc_totals.items()
        ],
        key=lambda x: x["usd"],
        reverse=True,
    )
    return out[:top]


# ---------------------------------------------------------------------------
# dashboard.pr_detail — join PR meta + linked Discussion + quality + cost
# ---------------------------------------------------------------------------

import re as _re
import subprocess as _pr_subprocess


def _gh_pr_view(pr_number: int, repo: str = _GH_REPO) -> dict | None:
    """Fetch PR metadata via gh CLI. Returns None if PR does not exist.

    *repo* defaults to the engine's own repo (existing no-project behavior);
    dashboard.pr_detail passes the requested project's repo slug, resolved
    via _resolve_repo_for_project (D#2261 PR-b) — _GH_REPO here was a module
    constant bound to this checkout at import, the exact class-(c) leak
    that made an adopter dashboard's PR-detail view serve this engine's PRs.
    """
    result = _pr_subprocess.run(
        [
            "gh", "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "number,title,author,state,mergedAt,additions,deletions,changedFiles,url,body",
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=15,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except Exception:
        return None


def _resolve_linked_discussion(
    pr_number: int, pr_body: str, repo_owner: str = _REPO_OWNER, repo_name: str = _REPO_NAME,
) -> dict | None:
    """Find the Discussion linked to this PR by scanning body references.

    *repo_owner*/*repo_name* default to the engine's own repo; pass the
    requested project's resolved repo (D#2261 PR-b) so a PR's linked
    Discussion is looked up in the same repo the PR itself came from.
    """
    for m in _re.finditer(r'(?:Fixes|Closes|Discussion)\s+#(\d+)', pr_body or '', _re.IGNORECASE):
        n = int(m.group(1))
        q = (
            f'query {{ repository(owner:"{repo_owner}", name:"{repo_name}") {{'
            f'discussion(number:{n}) {{ number title url }} }} }}'
        )
        r = _pr_subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={q}"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=10,
        )
        if r.returncode == 0:
            try:
                data = json.loads(r.stdout)
                d = (data.get("data") or {}).get("repository", {}).get("discussion")
                if d:
                    return {"number": d["number"], "title": d["title"], "url": d["url"]}
            except Exception:
                pass

    # Fallback: scan recent Discussions for STATUS line citing this PR
    q2 = (
        f'query {{ repository(owner:"{repo_owner}", name:"{repo_name}") {{'
        'discussions(first:50, orderBy:{field:UPDATED_AT, direction:DESC}) {'
        'nodes { number title url body } } } }'
    )
    r2 = _pr_subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={q2}"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=15,
    )
    if r2.returncode == 0:
        try:
            nodes = (
                json.loads(r2.stdout)
                .get("data", {})
                .get("repository", {})
                .get("discussions", {})
                .get("nodes", [])
            )
            for d in nodes:
                if f"PR:#{pr_number}" in (d.get("body") or ""):
                    return {"number": d["number"], "title": d["title"], "url": d["url"]}
        except Exception:
            pass

    return None


@_rpc_method("dashboard.pr_detail")
def _rpc_pr_detail(params: dict) -> dict:
    """Return joined PR meta + linked Discussion + quality score + cost.

    Params: {"pr_number": int, "project": str}
    Returns: {pr, discussion, quality, cost, review_rounds}
      or     {"error": "not_found"} when the PR does not exist.

    Per-project (D#2261 PR-b): resolves the requested project's repo via
    _resolve_repo_for_project() and queries that repo instead of the
    engine's own (module-constant _GH_REPO). Blackboard/CostTracker/
    QualityScorer below need no change — they resolve their state-dir paths
    at call time and are already reached by dispatch_scoped()'s per-request
    env override once this method is classified SCOPED.
    """
    # E2E fixture support
    fixture_path = _REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
    if os.environ.get("AF_E2E_FIXTURES") == "1" and fixture_path.exists():
        try:
            fixtures = json.loads(fixture_path.read_text())
            pr_number_raw = params.get("pr_number", 0)
            try:
                pr_num = int(pr_number_raw)
            except (TypeError, ValueError):
                return {"error": "invalid_pr_number"}
            key = f"pr_detail_{pr_num}"
            if key in fixtures:
                return fixtures[key]
            # Generic fixture for any existing PR
            if "pr_detail" in fixtures and pr_num < 999000:
                return fixtures["pr_detail"]
            return fixtures.get("pr_detail_not_found", {"error": "not_found"})
        except Exception:
            pass

    pr_number_raw = params.get("pr_number", 0)
    try:
        pr_number = int(pr_number_raw)
    except (TypeError, ValueError):
        raise _rpc_invalid_params(f"pr_number must be an integer, got {pr_number_raw!r}")
    if pr_number <= 0:
        raise _rpc_invalid_params("pr_number must be a positive integer")

    project = params.get("project") or None
    repo_owner, repo_name = _resolve_repo_for_project(project)
    repo = f"{repo_owner}/{repo_name}"

    pr = _gh_pr_view(pr_number, repo=repo)
    if pr is None:
        return {"error": "not_found"}

    discussion = _resolve_linked_discussion(
        pr_number, pr.get("body") or "", repo_owner=repo_owner, repo_name=repo_name,
    )

    from backend.blackboard import Blackboard as _BB
    from backend.cost_tracker import CostTracker as _CT
    from backend.quality_scorer import QualityScorer as _QS

    bb = _BB()
    quality = bb.read(f"quality/{pr_number}")

    tracker = _CT(bb)
    cost = tracker.per_pr_summary(pr_number)

    scorer = _QS()
    review_rounds = scorer._count_needs_fix_rounds(pr_number)

    return {
        "pr": {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "author": (pr.get("author") or {}).get("login"),
            "state": pr.get("state"),
            "merged_at": pr.get("mergedAt"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "files_changed": pr.get("changedFiles"),
            "html_url": pr.get("url"),
        },
        "discussion": discussion,
        "quality": quality,
        "cost": cost,
        "review_rounds": review_rounds,
    }


def _rpc_invalid_params(msg: str) -> Exception:
    """Return an exception that maps to JSON-RPC error -32602."""

    class _InvalidParams(Exception):
        rpc_code = -32602

    return _InvalidParams(msg)


# ---------------------------------------------------------------------------
# dashboard.pr_list — list all open PRs with gate labels, fix-cycle count,
# age, quality score, and linked Discussion number.
# ---------------------------------------------------------------------------

import subprocess as _pl_subprocess
import time as _pl_time


_PR_LIST_CACHE: dict = {}
_PR_LIST_CACHE_TTL = 30.0  # seconds


def _count_fix_cycles(pr_number: int) -> int:
    """Count how many times code-review-needs-fix was applied (from audit trail)."""
    try:
        from backend.blackboard import Blackboard as _BB2
        bb = _BB2()
        quality = bb.read(f"quality/{pr_number}")
        if quality and isinstance(quality, dict):
            return int(quality.get("review_rounds", 0))
    except Exception:
        pass
    return 0


def _get_quality_score(pr_number: int) -> "float | None":
    """Return total quality score from blackboard, or None if absent."""
    try:
        from backend.blackboard import Blackboard as _BB3
        bb = _BB3()
        quality = bb.read(f"quality/{pr_number}")
        if quality and isinstance(quality, dict):
            total = quality.get("total")
            if total is not None:
                return float(total)
    except Exception:
        pass
    return None


def _find_linked_discussion_number(
    pr_body: str, pr_number: int, repo_owner: str = _REPO_OWNER, repo_name: str = _REPO_NAME,
) -> "int | None":
    """Extract Discussion number from PR body or STATUS line scanning.

    *repo_owner*/*repo_name* default to the engine's own repo; dashboard.pr_list
    passes the requested project's resolved repo (D#2261 PR-b).
    """
    import re as _re2

    for m in _re2.finditer(r'(?:Fixes|Closes|Discussion)\s+#(\d+)', pr_body or '', _re2.IGNORECASE):
        return int(m.group(1))

    # Fallback: scan recent Discussion STATUS lines for PR:#{pr_number}
    try:
        q = (
            f'query {{ repository(owner:"{repo_owner}", name:"{repo_name}") {{'
            'discussions(first:50, orderBy:{field:UPDATED_AT, direction:DESC}) {'
            'nodes { number body } } } }'
        )
        r = _pl_subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={q}"],
            capture_output=True, text=True, timeout=10, cwd=_REPO_ROOT,
        )
        if r.returncode == 0:
            nodes = (
                json.loads(r.stdout)
                .get("data", {})
                .get("repository", {})
                .get("discussions", {})
                .get("nodes", [])
            )
            for d in nodes:
                if f"PR:#{pr_number}" in (d.get("body") or ""):
                    return d["number"]
    except Exception:
        pass

    return None


@_rpc_method("dashboard.pr_list")
def _rpc_pr_list(params: dict) -> list:
    """Return list of open PRs with gate-label state, fix-cycle count, age, and quality score.

    Params: {"project": str}
    Returns: [{number, title, author, age_seconds, labels, fix_cycles,
               quality_score, discussion_number, html_url}, ...]

    Supports fixture injection via AF_E2E_FIXTURES=1.

    Per-project (D#2261 PR-b): resolves the requested project's repo via
    _resolve_repo_for_project() and lists that repo's open PRs instead of
    the engine's own (module-constant _GH_REPO).

    The 30s TTL cache below is keyed on (method, repo_owner, repo_name),
    never on the method name alone — a cache keyed without the repo would
    return one project's PR list to the next project's request within the
    TTL window, the exact bug that kept circuit_breaker.summary UNSCOPABLE
    (D#2261 PR-a review).
    """
    # E2E fixture support
    fixture_path = _REPO_ROOT / ".autonomous-team" / "tmp" / "e2e-fixtures.json"
    if os.environ.get("AF_E2E_FIXTURES") == "1" and fixture_path.exists():
        try:
            fixtures = json.loads(fixture_path.read_text())
            if "pr_list" in fixtures:
                return fixtures["pr_list"]
        except Exception:
            pass

    project = params.get("project") or None
    repo_owner, repo_name = _resolve_repo_for_project(project)
    repo = f"{repo_owner}/{repo_name}"

    # Cache check — keyed on the resolved repo, not just the method name.
    cache_key = ("dashboard.pr_list", repo_owner, repo_name)
    entry = _PR_LIST_CACHE.get(cache_key)
    if entry is not None:
        ts, cached = entry
        if _pl_time.time() - ts <= _PR_LIST_CACHE_TTL:
            return cached

    result = _pl_subprocess.run(
        [
            "gh", "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,title,author,labels,createdAt,body,url",
            "--limit", "100",
        ],
        capture_output=True, text=True, cwd=_REPO_ROOT, timeout=20,
    )
    if result.returncode != 0:
        _log(f"dashboard.pr_list gh pr list failed: {result.stderr[:200]}")
        return []

    try:
        prs_raw = json.loads(result.stdout)
    except Exception as exc:
        _log(f"dashboard.pr_list parse error: {exc}")
        return []

    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    items = []
    for pr in prs_raw:
        pr_number = pr.get("number", 0)
        created_at_str = pr.get("createdAt") or ""
        try:
            created_at = _dt.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_seconds = int((now - created_at).total_seconds())
        except Exception:
            age_seconds = 0

        labels = [lbl.get("name", "") for lbl in (pr.get("labels") or [])]
        fix_cycles = _count_fix_cycles(pr_number)
        quality_score = _get_quality_score(pr_number)
        discussion_number = _find_linked_discussion_number(
            pr.get("body") or "", pr_number, repo_owner=repo_owner, repo_name=repo_name,
        )

        items.append({
            "number": pr_number,
            "title": pr.get("title", ""),
            "author": (pr.get("author") or {}).get("login", None),
            "age_seconds": age_seconds,
            "labels": labels,
            "fix_cycles": fix_cycles,
            "quality_score": quality_score,
            "discussion_number": discussion_number,
            "html_url": pr.get("url", ""),
        })

    _PR_LIST_CACHE[cache_key] = (_pl_time.time(), items)
    return items


_CB_CACHE_TTL = 30.0  # seconds — circuit breaker state changes slowly


@_rpc_method("circuit_breaker.summary")
def _rpc_circuit_breaker_summary(params: dict) -> dict:
    """Return circuit breaker summary with 30s TTL cache.

    Returns: {"tripped": [...], "warnings": [...], "threshold": 3}
    """
    cache_key = ("circuit_breaker.summary",)
    entry = _DISCUSSIONS_CACHE.get(cache_key)
    if entry is not None:
        ts, cached = entry
        if time.time() - ts <= _CB_CACHE_TTL:
            return cached

    import subprocess as _cb_subprocess
    result = _cb_subprocess.run(
        [sys.executable, "backend/circuit_breaker.py", "summary", "--json"],
        capture_output=True,
        text=True,
        timeout=5,
        cwd=_REPO_ROOT,
    )
    if result.returncode != 0:
        _log(f"circuit_breaker summary failed: {result.stderr[:200]}")
        output: dict = {"tripped": [], "warnings": [], "threshold": 3}
    else:
        try:
            output = json.loads(result.stdout)
        except Exception as exc:
            _log(f"circuit_breaker summary parse error: {exc}")
            output = {"tripped": [], "warnings": [], "threshold": 3}

    _DISCUSSIONS_CACHE[cache_key] = (time.time(), output)
    return output


@_rpc_method("circuitBreaker.history")
def _rpc_circuit_breaker_history(params: dict) -> list:
    """Return transition history for a role.

    Params:
      role  (str, required) — agent role to filter
      limit (int, optional, default 20) — max entries to return

    Returns: array of transition objects from the JSONL history file.
    """
    role = params.get("role")
    if not role:
        raise _rpc_invalid_params("role is required")
    limit = int(params.get("limit", 20))
    try:
        from backend.circuit_breaker import history as _cb_history
        return _cb_history(role=role, limit=limit)
    except Exception as exc:
        _log(f"circuitBreaker.history error: {exc}")
        return []


@_rpc_method("team_status.snapshot")
def _rpc_team_status_snapshot(params: dict) -> dict:
    try:
        from backend.team_status import _gather, _load_snapshot  # type: ignore[attr-defined]
        project = params.get("project") or None
        snapshot, stale_msg = _load_snapshot()
        return _gather(snapshot, stale_msg, project=project)
    except Exception as exc:
        return {"error": str(exc), "discussions": {}, "prs": {}, "agents": {}, "budget": {}, "kpi": {}}


@_rpc_method("claude_spawn_tracker.summary")
def _rpc_claude_spawn_tracker_summary(params: dict) -> dict:
    """Return Claude spawn tracker state with 30s TTL cache.

    Returns the same JSON as ``python3 backend/claude_spawn_tracker.py summary --json``.
    """
    _CST_CACHE_TTL = 30
    cache_key = ("claude_spawn_tracker.summary",)
    entry = _DISCUSSIONS_CACHE.get(cache_key)
    if entry is not None:
        ts, cached = entry
        if time.time() - ts <= _CST_CACHE_TTL:
            return cached

    try:
        import subprocess as _cst_subprocess
        result = _cst_subprocess.run(
            [sys.executable, "backend/claude_spawn_tracker.py", "summary", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=_REPO_ROOT,
        )
        if result.returncode == 0:
            output: dict = json.loads(result.stdout)
        else:
            _log(f"claude_spawn_tracker summary failed: {result.stderr[:200]}")
            output = {"tripped": False, "spawns_1h": 0, "spawns_24h": 0,
                      "spend_24h_usd": 0.0, "per_source": {}, "thresholds": {}, "tripped_meta": None}
    except Exception as exc:
        _log(f"claude_spawn_tracker summary error: {exc}")
        output = {"tripped": False, "spawns_1h": 0, "spawns_24h": 0,
                  "spend_24h_usd": 0.0, "per_source": {}, "thresholds": {}, "tripped_meta": None}

    _DISCUSSIONS_CACHE[cache_key] = (time.time(), output)
    return output


# ---------------------------------------------------------------------------
# discussions.list / discussions.get — GitHub Discussions proxy with 60s cache
# ---------------------------------------------------------------------------

import re as _re
import subprocess as _subprocess

_DISCUSSIONS_CACHE: dict[tuple, tuple[float, Any]] = {}
_DISCUSSIONS_CACHE_TTL = 60.0  # seconds
# _REPO_OWNER / _REPO_NAME are imported from backend._repo above (env/project.json
# derived, raises loudly if neither resolves) — do not re-hardcode here, it
# would silently defeat the fork-portability resolution.


def _parse_repo_slug(slug: str) -> tuple[str, str] | None:
    """Split a 'owner/name' slug into (owner, name), or return None if invalid."""
    if not slug or "/" not in slug:
        return None
    owner, _, name = slug.partition("/")
    return (owner, name) if owner and name else None


def _read_slug_from_json(path: Path, *keys: str) -> tuple[str, str] | None:
    """Read a JSON file and try each key in order, returning the first valid (owner, name) pair."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in keys:
            pair = _parse_repo_slug(data.get(key) or "")
            if pair:
                return pair
    except Exception:  # noqa: BLE001
        pass
    return None


def _resolve_repo_for_project(project_name: str | None) -> tuple[str, str]:
    """Return (owner, name) for the given project slug.

    The rule: no project requested returns the engine default; a *named*
    project that cannot be resolved is an error, never a default. Those are
    kept structurally distinct — the first branch below decides "no project
    requested" before anything else runs, and only a project that was
    actually named can reach the raise at the bottom.

    When *project_name* is None or empty, falls back to the module-level
    _REPO_OWNER / _REPO_NAME (existing behaviour — compat for AF callers
    that don't pass a project param).

    When *project_name* is set, reads the project's state-dir files directly
    to find the GitHub repo slug.  Does NOT import for_project() because
    long-running server processes may have a stale sys.modules cache that
    pre-dates that function's addition.  Raises UnresolvableProjectError when
    no repo can be found — silently resolving to the engine's own repo would
    make an unconfigured or mistyped project name serve the engine's data
    under that project's name (D#2268).

    Resolution order:
      0. This process's own STATE_DIR, via state_paths._served_state_dir()
         — the state dir the server actually serves, which may sit outside
         $HOME (D#2259). Same stale-sys.modules concern as above, so the
         import stays lazy and inside this try.
      1. ~/.<name>-state/dashboard-runtime.json   (``repo`` or ``project_repo`` field)
      2. ~/.<name>-state/project.json              (``repo`` field)
      3. Raise UnresolvableProjectError — never fall back to the default pair.

    When step 0, 1, or 2 finds a config file with no repo field, the raised
    message names *that actual file* (tracked as it's found), not a
    recomputed ~/.<name>-state guess — step 0's file can live outside $HOME,
    so guessing the home-anchored path there would name a file that was
    never read and may not even exist.
    """
    if not project_name:
        return _REPO_OWNER, _REPO_NAME

    state_dir = Path.home() / f".{project_name}-state"
    # The actual config file that was found and read but had no repo field —
    # not a recomputed guess. Step 0's served state dir may sit outside
    # $HOME (D#2259), so this must be the real path read, or an error
    # message can end up naming a file that doesn't exist (D#2268 review).
    found_config_path: Path | None = None

    try:
        from backend.state_paths import _served_state_dir  # lazy — see docstring

        served = _served_state_dir(project_name)
        if served is not None:
            served_dir, data = served
            pair = _parse_repo_slug(data.get("repo") or data.get("project_repo") or "")
            if pair:
                return pair
            found_config_path = served_dir / "dashboard-runtime.json"
    except Exception:  # noqa: BLE001
        pass
    try:
        runtime = state_dir / "dashboard-runtime.json"
        if runtime.exists():
            found_config_path = found_config_path or runtime
            pair = _read_slug_from_json(runtime, "repo", "project_repo")
            if pair:
                return pair
        proj = state_dir / "project.json"
        if proj.exists():
            found_config_path = found_config_path or proj
            pair = _read_slug_from_json(proj, "repo")
            if pair:
                return pair
    except Exception:  # noqa: BLE001
        pass

    if found_config_path is not None:
        raise _rpc_project_scope.UnresolvableProjectError(
            f"project {project_name!r} has a state dir but no repo configured — "
            f"add a \"repo\" field (e.g. \"owner/name\") to {found_config_path}"
        )
    raise _rpc_project_scope.UnresolvableProjectError(
        f"project {project_name!r} could not be resolved to a repo — no state dir "
        f"found (expected {state_dir})"
    )


def _discussions_cache_get(key: tuple) -> Any | None:
    entry = _DISCUSSIONS_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.time() - ts > _DISCUSSIONS_CACHE_TTL:
        del _DISCUSSIONS_CACHE[key]
        return None
    return value


def _discussions_cache_set(key: tuple, value: Any) -> None:
    _DISCUSSIONS_CACHE[key] = (time.time(), value)


def _gh_graphql(query: str, variables: dict | None = None) -> dict:
    """Run a GraphQL query via `gh api graphql` and return parsed JSON."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    if variables:
        for k, v in variables.items():
            cmd += ["-F", f"{k}={v}"]
    result = _subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh graphql error: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _extract_status(body: str) -> str:
    """Extract STATUS from <!-- STATUS:... --> comment in discussion body."""
    m = _re.search(r"<!--\s*STATUS:(\w+)", body or "")
    return m.group(1) if m else "UNKNOWN"


def _extract_linked_pr(body: str) -> int | None:
    """Extract PR:#N from STATUS line."""
    m = _re.search(r"<!--\s*STATUS:[^>]*PR:#(\d+)", body or "")
    return int(m.group(1)) if m else None


@_rpc_method("discussions.list")
def _rpc_discussions_list(params: dict) -> dict:
    status_filter = params.get("status", "*")
    q_filter = (params.get("q") or "").lower()
    max_age_days = params.get("max_age_days")
    limit = min(int(params.get("limit", 50)), 200)
    cursor = params.get("cursor")
    project = params.get("project") or None

    repo_owner, repo_name = _resolve_repo_for_project(project)
    cache_key = ("discussions.list", repo_owner, repo_name, status_filter, q_filter, max_age_days, limit, cursor)
    cached = _discussions_cache_get(cache_key)
    if cached is not None:
        return cached

    # Build GraphQL query — fetch up to 100 at a time (GitHub max), post-filter in Python
    after_clause = f', after: "{cursor}"' if cursor else ""
    gql = f"""
    query {{
      repository(owner: "{repo_owner}", name: "{repo_name}") {{
        discussions(first: 100{after_clause}, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{
            number
            title
            body
            url
            createdAt
            updatedAt
            category {{ name }}
            author {{ login }}
          }}
        }}
      }}
    }}
    """
    try:
        data = _gh_graphql(gql)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    disc_data = data.get("data", {}).get("repository", {}).get("discussions", {})
    nodes = disc_data.get("nodes", [])
    page_info = disc_data.get("pageInfo", {})

    now = time.time()
    items = []
    for node in nodes:
        body = node.get("body") or ""
        status = _extract_status(body)
        linked_pr = _extract_linked_pr(body)

        # Status filter
        if status_filter != "*" and status != status_filter:
            continue

        # Title search filter
        title = node.get("title") or ""
        if q_filter and q_filter not in title.lower():
            continue

        # Age filter
        if max_age_days is not None:
            updated_at = node.get("updatedAt") or node.get("createdAt") or ""
            try:
                import datetime as _datetime
                dt = _datetime.datetime.fromisoformat(updated_at.rstrip("Z")).replace(
                    tzinfo=_datetime.timezone.utc
                )
                age_days = (now - dt.timestamp()) / 86400
                if age_days > int(max_age_days):
                    continue
            except Exception:
                pass

        items.append({
            "number": node["number"],
            "title": title,
            "status": status,
            "linkedPr": linked_pr,
            "url": node.get("url"),
            "createdAt": node.get("createdAt"),
            "updatedAt": node.get("updatedAt"),
            "author": (node.get("author") or {}).get("login"),
        })

        if len(items) >= limit:
            break

    # Attach per-Discussion cost (single CostTracker pass — cheap, reads blackboard once).
    try:
        from backend.cost_tracker import CostTracker as _CT_disc  # noqa: PLC0415
        _cost_map: dict[int, float] = {
            e["discussion"]: e.get("total_cost_usd", 0.0)
            for e in _CT_disc().get_session_cost().get("by_discussion", [])
        }
        for item in items:
            cost_val = _cost_map.get(item["number"])
            item["costUsd"] = cost_val  # None when no spend recorded
    except Exception as _ce:
        _log(f"discussions.list: cost injection failed (non-fatal): {_ce}")

    result: dict = {"items": items}
    if page_info.get("hasNextPage"):
        result["next_cursor"] = page_info["endCursor"]

    _discussions_cache_set(cache_key, result)
    return result


@_rpc_method("discussions.get")
def _rpc_discussions_get(params: dict) -> dict:
    number = int(params.get("number", 0))
    if number <= 0:
        raise ValueError("number must be a positive integer")
    project = params.get("project") or None

    repo_owner, repo_name = _resolve_repo_for_project(project)
    cache_key = ("discussions.get", repo_owner, repo_name, number)
    cached = _discussions_cache_get(cache_key)
    if cached is not None:
        return cached

    gql = f"""
    query {{
      repository(owner: "{repo_owner}", name: "{repo_name}") {{
        discussion(number: {number}) {{
          number
          title
          body
          url
          createdAt
          updatedAt
          author {{ login }}
          category {{ name }}
          comments(last: 10) {{
            nodes {{
              body
              createdAt
              author {{ login }}
            }}
          }}
        }}
      }}
    }}
    """
    try:
        data = _gh_graphql(gql)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    disc = data.get("data", {}).get("repository", {}).get("discussion")
    if disc is None:
        raise ValueError(f"Discussion #{number} not found")

    body = disc.get("body") or ""
    status = _extract_status(body)
    linked_pr_num = _extract_linked_pr(body)

    comments = [
        {
            "body": c.get("body") or "",
            "createdAt": c.get("createdAt"),
            "author": (c.get("author") or {}).get("login"),
        }
        for c in (disc.get("comments", {}).get("nodes") or [])
    ]

    linked_pr = None
    if linked_pr_num:
        # Keyed on (repo_owner, repo_name, linked_pr_num), not just the PR
        # number -- two different projects' repos can both have a "#42",
        # and a key without the repo would serve one project's cached PR
        # info to the other (the same cache-without-project bug class as
        # circuit_breaker.summary and the dashboard.pr_list rekey above).
        pr_cache_key = ("pr.info", repo_owner, repo_name, linked_pr_num)
        linked_pr = _discussions_cache_get(pr_cache_key)
        if linked_pr is None:
            pr_gql = f"""
            query {{
              repository(owner: "{repo_owner}", name: "{repo_name}") {{
                pullRequest(number: {linked_pr_num}) {{
                  number
                  url
                  state
                  labels(first: 10) {{
                    nodes {{ name }}
                  }}
                }}
              }}
            }}
            """
            try:
                pr_data = _gh_graphql(pr_gql)
                pr = pr_data.get("data", {}).get("repository", {}).get("pullRequest")
                if pr:
                    linked_pr = {
                        "number": pr["number"],
                        "url": pr["url"],
                        "state": pr["state"],
                        "labels": [l["name"] for l in (pr.get("labels", {}).get("nodes") or [])],
                    }
                    _discussions_cache_set(pr_cache_key, linked_pr)
            except Exception:
                pass

    # Read agent runs from agent-feed.jsonl referencing this discussion.
    # discussions.get was already classified SCOPED (it resolves its own
    # repo via _resolve_repo_for_project), but this nested read used the
    # engine's own AGENT_FEED_PATH regardless of project — the same class-(c)
    # leak PR-b fixes elsewhere, just not caught by the original classification
    # because it's a private helper call inside an already-SCOPED handler
    # rather than the handler's own top-level state access.
    agent_runs: list[dict] = []
    try:
        lines = _agent_feed_path(project).read_text().splitlines()
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            disc_ref = ev.get("discussion") or ev.get("discussion_number")
            if disc_ref is not None and int(disc_ref) == number:
                agent_runs.append({
                    "ts": ev.get("timestamp") or ev.get("ts") or "",
                    "role": ev.get("role") or ev.get("agent") or "",
                    "verdict": ev.get("verdict") or None,
                    "pr": ev.get("pr") or None,
                })
                if len(agent_runs) >= 20:
                    break
    except Exception:
        pass

    result = {
        "discussion": {
            "number": disc["number"],
            "title": disc.get("title") or "",
            "body": body,
            "status": status,
            "url": disc.get("url"),
            "createdAt": disc.get("createdAt"),
            "updatedAt": disc.get("updatedAt"),
            "author": (disc.get("author") or {}).get("login"),
        },
        "comments": comments,
        "linked_pr": linked_pr,
        "agent_runs": agent_runs,
    }

    _discussions_cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# HTTP adapter (ThreadingHTTPServer)
# ---------------------------------------------------------------------------

class _HttpHandler(BaseHTTPRequestHandler):
    """Minimal request router for the JSON-RPC HTTP adapter."""

    # Injected by HttpAdapter before server starts
    _bearer_token: str = ""

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        logger.debug(fmt, *args)

    def _origin(self) -> str:
        return self.headers.get("Origin", "")

    def _cors_ok(self) -> bool:
        return self._origin() in _compute_allowed_origins()

    def _set_cors_headers(self) -> None:
        origin = self._origin()
        allowed = _compute_allowed_origins()
        if origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Vary", "Origin")
        else:
            _dashboard_origins.log_rejected_origin(origin, allowed)

    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip() == self._bearer_token
        return False

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_rpc(self, code: int, message: str, req_id=None) -> None:
        self._send_json(code, {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": message},
        })

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/rpc":
            self._send_error_rpc(404, "not found")
            return

        if not self._auth_ok():
            self._send_error_rpc(401, "unauthorized")
            return

        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b"{}"
        try:
            req = json.loads(body_bytes)
        except Exception:
            self._send_error_rpc(400, "invalid JSON", None)
            return

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        # Belt-and-suspenders: reject test-origin spawns before they reach
        # loop.start (same pattern as api.py's _reject_test_origin_spawn).
        import os as _os_rpc, re as _re_rpc  # noqa: PLC0415
        _SPAWN_METHODS = {"loop.start"}
        if method in _SPAWN_METHODS and _os_rpc.environ.get("AF_ALLOW_TEST_ORIGIN_SPAWNS", "").strip() != "1":
            _ua = self.headers.get("User-Agent", "")
            _origin = self.headers.get("Origin", "")
            _test_ua = _re_rpc.compile(r"HeadlessChrome|Puppeteer|playwright", _re_rpc.IGNORECASE)
            _test_origins = {"http://localhost:5173", "http://127.0.0.1:5173"}
            if _test_ua.search(_ua) or _origin in _test_origins:
                self._send_json(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": "spawn_blocked_test_origin"},
                })
                return

        handler = _RPC_METHODS.get(method)
        if handler is None:
            self._send_json(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })
            return

        try:
            result = _rpc_project_scope.dispatch_scoped(method, params, handler)
            self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            err_code = getattr(exc, "rpc_code", -32000)
            self._send_json(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": err_code, "message": str(exc)},
            })

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/events":
            self._handle_events_sse(qs)
        elif parsed.path == "/feed":
            self._handle_feed_sse(qs)
        else:
            self._send_error_rpc(404, "not found")

    def _check_sse_auth(self, qs: dict) -> bool:
        """SSE: EventSource cannot set headers; accept ?token=... only from allowed origins."""
        if self._auth_ok():
            return True
        if self._cors_ok():
            token_param = qs.get("token", [""])[0]
            return token_param == self._bearer_token
        return False

    def _handle_events_sse(self, qs: dict) -> None:
        if not self._check_sse_auth(qs):
            self.send_response(401)
            self.end_headers()
            return

        loop_id = qs.get("loop_id", [""])[0]
        since = qs.get("since", [""])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._set_cors_headers()
        self.end_headers()

        try:
            self.wfile.write(b"data: {\"type\":\"connected\"}\n\n")
            self.wfile.flush()
            last_pos = 0
            while True:
                time.sleep(1)
                try:
                    text = AGENT_FEED_PATH.read_text()
                    lines = text.splitlines()
                    if len(lines) > last_pos:
                        for line in lines[last_pos:]:
                            try:
                                ev = json.loads(line)
                            except Exception:
                                continue
                            if loop_id and ev.get("loop_id") != loop_id:
                                continue
                            ts = ev.get("timestamp") or ev.get("ts") or ""
                            if since and ts < since:
                                continue
                            payload = json.dumps(ev)
                            self.wfile.write(f"data: {payload}\n\n".encode())
                        last_pos = len(lines)
                        self.wfile.flush()
                except Exception:
                    pass
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_feed_sse(self, qs: dict) -> None:
        if not self._check_sse_auth(qs):
            self.send_response(401)
            self.end_headers()
            return

        since = qs.get("since", [""])[0]
        role_filter = qs.get("filter[role]", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._set_cors_headers()
        self.end_headers()

        try:
            self.wfile.write(b"data: {\"type\":\"connected\"}\n\n")
            self.wfile.flush()
            last_pos = 0
            while True:
                time.sleep(1)
                try:
                    text = AGENT_FEED_PATH.read_text()
                    lines = text.splitlines()
                    if len(lines) > last_pos:
                        for line in lines[last_pos:]:
                            try:
                                ev = json.loads(line)
                            except Exception:
                                continue
                            ts = ev.get("timestamp") or ev.get("ts") or ""
                            if since and ts < since:
                                continue
                            if role_filter and ev.get("role") != role_filter:
                                continue
                            payload = json.dumps(ev)
                            self.wfile.write(f"data: {payload}\n\n".encode())
                        last_pos = len(lines)
                        self.wfile.flush()
                except Exception:
                    pass
        except (BrokenPipeError, ConnectionResetError):
            pass


class HttpAdapter:
    """Runs a ThreadingHTTPServer in a daemon thread alongside the main event loop."""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

        # Inject token into handler class
        _HttpHandler._bearer_token = token

        self._server = ThreadingHTTPServer(("127.0.0.1", port), _HttpHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("HTTP adapter listening on 127.0.0.1:%d", self.port)

    def stop(self) -> None:
        self._server.shutdown()


# ---------------------------------------------------------------------------
# stats.summary / stats.series — Phase 2 RPC methods (Discussion #549)
# ---------------------------------------------------------------------------

def _with_project_stats_db(project: str | None, fn):
    """Run *fn* with STATS_DB_PATH temporarily set to the project's stats.duckdb.

    This is the canonical way to scope stats_reader / stats_writer /
    agent_run_reader calls to a specific project's DuckDB.  All three
    modules check STATS_DB_PATH at the top of their _db_path() function,
    so overriding the env var is sufficient without modifying each module.

    If *project* is None (or the path cannot be resolved) the env var is
    left unchanged — existing behaviour for AF callers that don't pass a
    project param.
    """
    stats_db = _project_stats_db(project)
    old = os.environ.get("STATS_DB_PATH")
    if stats_db:
        os.environ["STATS_DB_PATH"] = str(stats_db)
    try:
        return fn()
    finally:
        if stats_db:
            if old is None:
                os.environ.pop("STATS_DB_PATH", None)
            else:
                os.environ["STATS_DB_PATH"] = old


@_rpc_method("stats.summary")
def _rpc_stats_summary(params: dict) -> dict:
    """Return the latest value for every known metric.

    Params: {"project": str}
    Response: {"metrics": [{"name", "value", "unit", "updated_at_iso"}, ...]}
    """
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from stats_reader import summary as _summary  # noqa: PLC0415

    project = params.get("project") or None
    metrics = _with_project_stats_db(project, _summary)
    return {"metrics": metrics}


@_rpc_method("stats.series")
def _rpc_stats_series(params: dict) -> dict:
    """Return time-ordered data points for one metric.

    Params: name (required), since_hours (optional, default 168), project (optional)
    Response: {"name": str, "points": [{"ts_iso", "value"}, ...]}
    """
    name = params.get("name", "")
    if not name:
        raise ValueError("'name' parameter is required")
    since_hours = int(params.get("since_hours", 168))
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.dirname(__file__))
    from stats_reader import series as _series  # noqa: PLC0415

    project = params.get("project") or None
    points = _with_project_stats_db(project, lambda: _series(name, since_hours=since_hours))
    return {"name": name, "points": points}


@_rpc_method("stats.team_lead_tokens")
def _rpc_stats_team_lead_tokens(params: dict) -> dict:
    from backend.rpc import stats_team_lead_tokens  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_team_lead_tokens.handle(params))


@_rpc_method("stats.cost_spike_history")
def _rpc_stats_cost_spike_history(params: dict) -> dict:
    from backend.rpc import stats_cost_spike_history  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_cost_spike_history.handle(params))


@_rpc_method("stats.role_success_rate")
def _rpc_stats_role_success_rate(params: dict) -> dict:
    from backend.rpc import stats_role_success_rate  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_role_success_rate.handle(params))


@_rpc_method("stats.role_retry_rate")
def _rpc_stats_role_retry_rate(params: dict) -> dict:
    from backend.rpc import stats_role_retry_rate  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_role_retry_rate.handle(params))


@_rpc_method("stats.loop_idle_ratio")
def _rpc_stats_loop_idle_ratio(params: dict) -> dict:
    from backend.rpc import stats_loop_idle_ratio  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_loop_idle_ratio.handle(params))


@_rpc_method("stats.avg_fix_rounds_per_pr")
def _rpc_stats_avg_fix_rounds_per_pr(params: dict) -> dict:
    from backend.rpc import stats_avg_fix_rounds_per_pr  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_avg_fix_rounds_per_pr.handle(params))


@_rpc_method("stats.freshness_list")
def _rpc_stats_freshness_list(params: dict) -> dict:
    from backend.rpc import stats_freshness  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_freshness.handle(params))


@_rpc_method("stats.cosmetic_blocks")
def _rpc_stats_cosmetic_blocks(params: dict) -> dict:
    from backend.rpc import stats_cosmetic_blocks  # noqa: PLC0415
    # cosmetic_blocks reads JSONL telemetry, not DuckDB — no STATS_DB_PATH swap needed.
    # Pass project so the module resolves the correct hook-events directory.
    project = params.get("project") or None
    return stats_cosmetic_blocks.handle(params, project=project)


@_rpc_method("stats.weekly_velocity")
def _rpc_stats_weekly_velocity(params: dict) -> dict:
    from backend.rpc import stats_weekly_velocity  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_weekly_velocity.handle(params))


@_rpc_method("stats.dora")
def _rpc_stats_dora(params: dict) -> dict:
    from backend.rpc import stats_dora  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_dora.handle(params))


@_rpc_method("stats.pre_write_burn")
def _rpc_stats_pre_write_burn(params: dict) -> dict:
    from backend.rpc import stats_pre_write_burn  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_pre_write_burn.handle(params))


@_rpc_method("stats.sdk_vs_cc")
def _rpc_stats_sdk_vs_cc(params: dict) -> dict:
    from backend.rpc import stats_sdk_vs_cc  # noqa: PLC0415
    return stats_sdk_vs_cc.handle(params)


@_rpc_method("stats.parity_trend")
def _rpc_stats_parity_trend(params: dict) -> dict:
    from backend.rpc import stats_parity_trend  # noqa: PLC0415
    return stats_parity_trend.handle(params)


@_rpc_method("stats_duckdb_writers")
def _rpc_stats_duckdb_writers(params: dict) -> dict:
    from backend.rpc import stats_duckdb_writers  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_duckdb_writers.handle(params))


@_rpc_method("stats.dial_usage")
def _rpc_stats_dial_usage(params: dict) -> dict:
    from backend.rpc import stats_dial_usage  # noqa: PLC0415
    return stats_dial_usage.handle(params)


@_rpc_method("stats.dial_rejections")
def _rpc_stats_dial_rejections(params: dict) -> dict:
    from backend.rpc import stats_dial_rejections  # noqa: PLC0415
    return stats_dial_rejections.handle(params)


@_rpc_method("stats.analyst_findings")
def _rpc_stats_analyst_findings(params: dict) -> dict:
    from backend.rpc import stats_analyst_findings  # noqa: PLC0415
    return stats_analyst_findings.handle(params)


@_rpc_method("stats.sdk_lane")
def _rpc_stats_sdk_lane(params: dict) -> dict:
    from backend.rpc import sdk_status  # noqa: PLC0415
    return sdk_status.handle(params)


@_rpc_method("a2a.list_active")
def _rpc_a2a_list_active(params: dict) -> dict:
    from backend.rpc import a2a_active
    return a2a_active.handle(params)


@_rpc_method("a2a.tail")
def _rpc_a2a_tail(params: dict) -> dict:
    from backend.rpc import a2a_tail
    return a2a_tail.handle(params)


@_rpc_method("runs.by_role")
def _rpc_runs_by_role(params: dict) -> dict:
    from backend.rpc import agent_runs  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: agent_runs.handle_by_role(params))


@_rpc_method("runs.percentiles")
def _rpc_runs_percentiles(params: dict) -> dict:
    from backend.rpc import agent_runs  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: agent_runs.handle_percentiles(params))


@_rpc_method("runs.stuck")
def _rpc_runs_stuck(params: dict) -> dict:
    from backend.rpc import agent_runs  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: agent_runs.handle_stuck(params))


@_rpc_method("runs.roundtrip")
def _rpc_runs_roundtrip(params: dict) -> dict:
    from backend.rpc import agent_runs  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: agent_runs.handle_roundtrip(params))


@_rpc_method("runs.active_over_time")
def _rpc_runs_active_over_time(params: dict) -> dict:
    from backend.rpc import agent_runs  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: agent_runs.handle_active_over_time(params))


@_rpc_method("runs.recent")
def _rpc_runs_recent(params: dict) -> dict:
    from backend.rpc import agent_runs  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: agent_runs.handle_recent(params))


@_rpc_method("fleet.projects")
def _rpc_fleet_projects(params: dict) -> dict:
    from backend.rpc import fleet_projects
    return fleet_projects.handle(params)


@_rpc_method("fleet.cost")
def _rpc_fleet_cost(params: dict) -> dict:
    from backend.rpc import fleet_cost
    return fleet_cost.handle(params)


@_rpc_method("fleet.discovery_ack")
def _rpc_fleet_discovery_ack(params: dict) -> dict:
    from backend.rpc import fleet_discovery_ack
    return fleet_discovery_ack.handle(params)


@_rpc_method("fleet.discovery_known")
def _rpc_fleet_discovery_known(params: dict) -> dict:
    from backend.rpc import fleet_discovery_ack
    return fleet_discovery_ack.handle_query(params)


@_rpc_method("fleet.concurrency")
def _rpc_fleet_concurrency(params: dict) -> dict:
    from backend.rpc import fleet_concurrency
    return fleet_concurrency.handle(params)


@_rpc_method("auth_retry.record")
def _rpc_auth_retry_record(params: dict) -> dict:
    from backend.rpc import auth_retry_counter  # noqa: PLC0415
    return auth_retry_counter.handle_record(params)


@_rpc_method("auth_retry.summary")
def _rpc_auth_retry_summary(params: dict) -> dict:
    from backend.rpc import auth_retry_counter  # noqa: PLC0415
    return auth_retry_counter.handle_summary(params)


@_rpc_method("dial.list")
def _rpc_dial_list(params: dict) -> dict:
    from backend.rpc import dial_control  # noqa: PLC0415
    return dial_control.handle_list(params)


@_rpc_method("dial.set")
def _rpc_dial_set(params: dict) -> dict:
    from backend.rpc import dial_control  # noqa: PLC0415
    return dial_control.handle_set(params)


@_rpc_method("stats.verdict_overturns")
def _rpc_stats_verdict_overturns(params: dict) -> dict:
    from backend.rpc import stats_verdict_overturns  # noqa: PLC0415
    return stats_verdict_overturns.handle(params)


@_rpc_method("stats.cost_per_outcome")
def _rpc_stats_cost_per_outcome(params: dict) -> dict:
    from backend.rpc import stats_cost_per_outcome  # noqa: PLC0415
    project = params.get("project") or None
    return _with_project_stats_db(project, lambda: stats_cost_per_outcome.handle(params))


# ---------------------------------------------------------------------------
# Main entrypoint (updated to support --http / --rotate-token)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="autonomous-forever backend server")
    parser.add_argument(
        "--http",
        type=int,
        metavar="PORT",
        default=None,
        help="Also listen for JSON-RPC over HTTP on 127.0.0.1:PORT (default: 8765 from config)",
    )
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="Regenerate the dashboard bearer token before starting.",
    )
    args = parser.parse_args()

    http_port = args.http
    rotate = args.rotate_token

    if http_port is not None or rotate:
        # Load port from config if not specified
        if http_port is None:
            try:
                config_path = _REPO_ROOT / ".autonomous-team" / "config.json"
                cfg = json.loads(config_path.read_text())
                http_port = int(cfg.get("dashboard", {}).get("http_port", 8765))
            except Exception:
                http_port = 8765

        # Enforce localhost-only (sanity check — the server already binds to 127.0.0.1)
        token = _load_or_create_token(rotate=rotate)
        logger.info("Dashboard token written to %s", TOKEN_PATH)

        from backend.active_loops import prune_dead_pids
        prune_dead_pids()

        adapter = HttpAdapter(http_port, token)
        adapter.start()
        print(f"HTTP adapter on 127.0.0.1:{http_port} — token at {TOKEN_PATH}", flush=True)

        # In HTTP-only mode (spawned by E2E fixtures or dashboard CLI), run a simple
        # event loop that just keeps the HTTP adapter thread alive.  We do NOT run
        # the full _main() async pipeline because:
        #   1. _main() tries to set up asyncio stdin readers which fail when stdin is
        #      a pipe managed by a subprocess harness, crashing the event loop.
        #   2. The HTTP adapter is self-contained in a daemon thread — it doesn't need
        #      the agent / stdin / FIFO infrastructure.
        # Block the main thread until SIGTERM / SIGINT so the daemon thread survives.
        import signal
        _stop_event = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: _stop_event.set())
        print(f'{{"type": "ready", "version": "0.2.0", "mode": "http"}}', flush=True)
        _stop_event.wait()
        adapter.stop()
        return

    asyncio.run(_main())


if __name__ == "__main__":
    main()
