"""
backend/agent_run_tracker.py — Per-agent run tracking backed by DuckDB.

Records start and end of every agent spawn in the ``agent_run`` table of
``stats.duckdb``.  All writes are non-fatal — a DuckDB failure (locked file,
missing dependency, etc.) is logged to stderr and swallowed so the caller's
main task always succeeds.

# Agent-ID canonical key contract
# =================================
# Every agent_run row is keyed by agent_id, which MUST be identical between
# start_run() and complete_run() for the UPSERT to merge them into one row.
#
# Canonical format:  "{role}-{discussion|nod}-{unix_timestamp}"
#   e.g.  "executor-834-1715000000"
#
# Producer:  scripts/spawn-agent.sh
#   EVENT_ID="${ROLE}-${DISCUSSION:-nod}-$(date +%s)"
#   start_run(agent_id=EVENT_ID)
#   Also written to the spawn prompt as the last line (split below so this
#   comment itself never plants a canonical-shaped id in a reading agent's
#   transcript — see D#1807):
#     "hook_event_" "id=executor-834-1715000000"
#
# Consumer:  scripts/subagent-stop-hook.sh  (SubagentStop hook)
#   Extracts "hook_event_id=..." from the user prompt in the transcript.
#   Falls back to "{role}-{disc}-{session_id}" when not found (legacy path).
#   Passes the extracted id as --event-id to post-agent-hook.sh.
#
# post-agent-hook.sh then calls complete_run(agent_id=HOOK_EVENT_ID).
# Because complete_run now uses INSERT...ON CONFLICT DO UPDATE, a missing
# start_run row is created on the fly so telemetry is never lost.
#
# Summary of the canonical key:
#   spawn-agent.sh:        {role}-{disc}-{timestamp}  ← sets agent_id
#   subagent-stop-hook.sh: reads hook_event_id from transcript  ← same value
#   complete_run():        upserts on agent_id  ← matches or creates

Schema (also created by ``_ensure_schema``)::

    CREATE TABLE agent_run (
        agent_id               VARCHAR PRIMARY KEY,
        role                   VARCHAR NOT NULL,
        discussion             INTEGER,
        pr                     INTEGER,
        start_ts               TIMESTAMPTZ NOT NULL,
        end_ts                 TIMESTAMPTZ,
        duration_s             DOUBLE,
        verdict                VARCHAR,
        model                  VARCHAR,
        input_tok              INTEGER,
        output_tok             INTEGER,
        cache_read             INTEGER,
        cache_write            INTEGER,
        cache_creation_tokens  INTEGER,
        blocked_reason         VARCHAR,
        event_id               VARCHAR,
        first_write_turn       INTEGER,
        total_turns            INTEGER,
        routed_via             TEXT,
        auto_routed            BOOLEAN
    );
    CREATE INDEX idx_agent_run_role_start ON agent_run(role, start_ts);
    CREATE INDEX idx_agent_run_pr ON agent_run(pr);

CLI usage (for spawn-agent.sh / post-agent-hook.sh integration in PR-b/c)::

    python3 backend/agent_run_tracker.py start \\
        --agent-id executor-635-1715000000 \\
        --role executor \\
        --discussion 635 \\
        --pr 42 \\
        --event-id executor-635-1715000000 \\
        --model claude-sonnet-4-6

    python3 backend/agent_run_tracker.py complete \\
        --agent-id executor-635-1715000000 \\
        --verdict done \\
        --input-tokens 62000 \\
        --output-tokens 8400 \\
        --cache-read 0 \\
        --cache-write 0

    python3 -m backend.agent_run_tracker backfill
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Role written on a brand-new row created by complete_run() when no start_run()
# row exists for the given agent_id (the two id namespaces did not join — see
# D#1812). Deliberately distinct from the legitimate role value "unknown" so
# these orphans are queryable in one WHERE clause instead of being
# indistinguishable from rows that are unknown for other reasons.
_ORPHAN_ROLE = "orphan-unmatched"

# D#2282 — orphan agent_ids encode the discussion in their own text:
#   "<role>-<discussion>-a<hex>"   e.g. "executor-2263-a04b46cb3d1f97a8e"
# `discussion` is "0" when the id was written before the discussion number
# was known ("nod" in the spawn-agent.sh comment above; -0- rows are not
# recoverable from the id alone and must stay orphaned). Attribution below
# is deliberately string parsing, not fuzzy/timestamp matching — a wrong
# attribution is worse than an orphaned row.
_ORPHAN_ID_RE = re.compile(r"^(?P<role>[a-z][a-z-]*)-(?P<discussion>\d+)-a(?P<hex>[0-9a-f]+)$")

# Roles this team recognizes (CLAUDE.md's `general-purpose` hard-stop list).
# A parsed role outside this set is not guessed at by attribute_orphans() —
# the row is left orphaned rather than promoted under an invented role.
_KNOWN_ROLES: frozenset[str] = frozenset({
    "executor", "code-reviewer", "security-reviewer", "project-manager",
    "acceptance-tester", "browser-tester", "mission-analyst",
    "technical-architect", "product-owner", "cost-analyst",
    "performance-expert", "security-expert", "run-analyst",
    "feedback-scanner", "quality-sweep", "visual-verifier", "docs-writer",
    "incident-commander", "release-manager", "researcher",
})


def _parse_orphan_id(agent_id: str) -> dict[str, Any] | None:
    """Parse an orphan agent_id of shape ``<role>-<discussion>-a<hex>``.

    Returns ``{"role": str, "discussion": int, "hex": str}``, or ``None`` if
    the id doesn't match that shape at all.
    """
    m = _ORPHAN_ID_RE.match(agent_id)
    if not m:
        return None
    return {
        "role": m.group("role"),
        "discussion": int(m.group("discussion")),
        "hex": m.group("hex"),
    }


# Allow running as a script from the repo root: `python3 backend/agent_run_tracker.py`.
# Both paths below resolve through backend.state_paths, so the package has to be
# importable even when this file is executed directly.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# DB path — one resolver, shared with agent_run_reader via state_paths
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    """Return the DuckDB stats path.

    Delegates to ``state_paths.STATS_DB``, which already handles the
    ``STATS_DB_PATH`` override. The in-repo legacy fallback this used to carry
    is gone: it was one of two byte-identical copies (the other in
    agent_run_reader) that nothing checked for agreement, and it could point a
    writer at a database inside the checkout (D#1967).
    """
    from backend import state_paths  # noqa: PLC0415
    return state_paths.STATS_DB


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _ensure_schema(conn: Any) -> None:
    """Create agent_run table and indexes if they do not already exist.

    Also runs idempotent column migrations for tables created before new columns
    were added (cache_creation_tokens, first_write_turn, total_turns).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_run (
            agent_id               VARCHAR PRIMARY KEY,
            role                   VARCHAR NOT NULL,
            discussion             INTEGER,
            pr                     INTEGER,
            start_ts               TIMESTAMPTZ NOT NULL,
            end_ts                 TIMESTAMPTZ,
            duration_s             DOUBLE,
            verdict                VARCHAR,
            model                  VARCHAR,
            input_tok              INTEGER,
            output_tok             INTEGER,
            cache_read             INTEGER,
            cache_write            INTEGER,
            cache_creation_tokens  INTEGER,
            blocked_reason         VARCHAR,
            event_id               VARCHAR,
            first_write_turn       INTEGER,
            total_turns            INTEGER,
            routed_via             TEXT,
            auto_routed            BOOLEAN
        )
    """)
    # Backward-compat column migrations.
    # DuckDB does not support PRAGMA table_info; use information_schema instead.
    try:
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='agent_run'"
            ).fetchall()
        }
        if "cache_creation_tokens" not in cols:
            conn.execute("ALTER TABLE agent_run ADD COLUMN cache_creation_tokens INTEGER")
        if "first_write_turn" not in cols:
            conn.execute("ALTER TABLE agent_run ADD COLUMN first_write_turn INTEGER")
        if "total_turns" not in cols:
            conn.execute("ALTER TABLE agent_run ADD COLUMN total_turns INTEGER")
        if "routed_via" not in cols:
            conn.execute("ALTER TABLE agent_run ADD COLUMN routed_via TEXT")
        if "auto_routed" not in cols:
            conn.execute("ALTER TABLE agent_run ADD COLUMN auto_routed BOOLEAN")
    except Exception:  # noqa: BLE001
        pass  # migration is best-effort; table may not exist yet on first call
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_run_role_start "
        "ON agent_run(role, start_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_run_pr "
        "ON agent_run(pr)"
    )


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

def start_run(
    agent_id: str,
    role: str,
    discussion: int | None = None,
    pr: int | None = None,
    event_id: str | None = None,
    model: str | None = None,
) -> None:
    """Insert a new agent_run row with start_ts=now, end_ts=NULL.

    Non-fatal: exceptions are logged and swallowed.

    Parameters
    ----------
    agent_id:   Unique identifier for this run (typically the event_id from
                pre-spawn-check).
    role:       Agent role name (e.g. "executor", "code-reviewer").
    discussion: Optional Discussion number.
    pr:         Optional PR number.
    event_id:   Idempotency key (same as agent_id when called from spawn-agent.sh).
    model:      Model identifier, e.g. "claude-sonnet-4-6".
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        logger.warning("agent_run_tracker: duckdb not installed — skipping start_run")
        return

    try:
        now = datetime.now(timezone.utc)
        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db))
        try:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_run
                    (agent_id, role, discussion, pr, start_ts, event_id, model)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [agent_id, role, discussion, pr, now, event_id, model],
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_tracker.start_run failed (non-fatal): %s", exc)


def _validate_token_count(value: int | None, field: str) -> int | None:
    """Return value if it is a non-negative int, else log a warning and return None.

    Fail-closed: invalid values are discarded rather than stored as bad data.
    Rejects None (pass-through), negative ints, and non-int types.
    """
    if value is None:
        return None
    if not isinstance(value, int):
        logger.warning(
            "agent_run_tracker: rejecting non-int %s=%r (expected non-negative int)",
            field, value,
        )
        return None
    if value < 0:
        logger.warning(
            "agent_run_tracker: rejecting negative %s=%d (expected non-negative int)",
            field, value,
        )
        return None
    return value


def complete_run(
    agent_id: str,
    end_ts: datetime | None = None,
    duration_s: float | None = None,
    verdict: str | None = None,
    model: str | None = None,
    input_tok: int | None = None,
    output_tok: int | None = None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    cache_creation_tokens: int | None = None,
    blocked_reason: str | None = None,
    first_write_turn: int | None = None,
    total_turns: int | None = None,
    routed_via: str | None = None,
    auto_routed: bool | None = None,
    role: str | None = None,
    discussion: int | None = None,
    start_ts: datetime | None = None,
) -> None:
    """UPSERT an agent_run row with completion data.

    Uses INSERT ... ON CONFLICT (agent_id) DO UPDATE so the call is idempotent
    and works whether or not start_run() ran first.  When start_run() did run
    first, the existing row is updated in place.  When it did not (e.g. the
    SubagentStop hook fired before spawn-agent.sh recorded the start), a new
    row is created with start_ts = end_ts.

    duration_s is computed from (end_ts - start_ts) when not supplied.  For
    newly-created rows where start_ts == end_ts the duration will be 0.

    All token fields are validated as non-negative ints.  Invalid values are
    discarded (fail-closed) with a warning — they are never stored as bad data.

    Non-fatal: exceptions are logged and swallowed.

    Parameters
    ----------
    agent_id:              Matches the agent_id used in start_run.
    end_ts:                Completion timestamp (defaults to now UTC).
    duration_s:            If omitted, computed from end_ts − start_ts.
    verdict:               Agent verdict string (e.g. "done", "pass", "needs-fix").
    model:                 Model identifier (fills in if missing from start_run).
    input_tok:             Regular input token count (non-negative int).
    output_tok:            Output token count (non-negative int).
    cache_read:            Cache-read token count (non-negative int).
    cache_write:           Cache-write token count (non-negative int).
    cache_creation_tokens: Cache-creation input token count (non-negative int).
    blocked_reason:        Why the run was blocked, if applicable.
    first_write_turn:      1-indexed turn number of first Edit/Write tool call (non-negative int).
    total_turns:           Total number of conversation turns in the run (non-negative int).
    routed_via:            Which spawn path was used: "sdk" or "cc". NULL for pre-D#1331 rows.
    auto_routed:           True when the run was routed to SDK via SDK_AUTO_ROUTE / should_auto_route
                           (no manual --sdk-lane opt-in). False for explicit --sdk-lane opt-in.
                           NULL for CC runs and pre-D#1364 rows.
    role:                  Real role for this completion (e.g. "executor"), as known by the
                           caller at completion time (D#2316 PR-b). Used ONLY when no
                           start_run() row already exists for agent_id — i.e. only on the
                           INSERT branch. An existing row's role is never overwritten by this
                           (no-clobber invariant — item 11): a real role recorded at start_run()
                           time always wins over whatever complete_run() is told later. When the
                           row is being newly created and role is None or not in _KNOWN_ROLES,
                           the row is stamped _ORPHAN_ROLE exactly as before (no-guessing
                           invariant — item 10): an unrecognised role is never invented into a
                           real one, it stays a queryable orphan.
    discussion:            Real Discussion number for this completion, paired with role. Same
                           INSERT-only, no-clobber treatment as role.
    start_ts:              A recoverable start time for this run (e.g. the agent's own first
                           transcript timestamp), used ONLY on the INSERT branch to compute an
                           honest duration_s instead of the start_ts==end_ts fallback. When this
                           is None and the INSERT branch fires, duration_s is written as NULL —
                           never 0 — because a 0s duration would read as a measurement instead of
                           "we don't know" (item 12).
    """
    # Validate all token fields before touching the DB.
    input_tok = _validate_token_count(input_tok, "input_tok")
    output_tok = _validate_token_count(output_tok, "output_tok")
    cache_read = _validate_token_count(cache_read, "cache_read")
    cache_write = _validate_token_count(cache_write, "cache_write")
    cache_creation_tokens = _validate_token_count(cache_creation_tokens, "cache_creation_tokens")
    first_write_turn = _validate_token_count(first_write_turn, "first_write_turn")
    total_turns = _validate_token_count(total_turns, "total_turns")

    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        logger.warning("agent_run_tracker: duckdb not installed — skipping complete_run")
        return

    try:
        if end_ts is None:
            end_ts = datetime.now(timezone.utc)

        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db))
        try:
            _ensure_schema(conn)

            # Look up any existing start_run() row for this agent_id. This tells
            # us (a) which UPSERT branch is about to fire — UPDATE if a row
            # exists, INSERT if not — and (b) start_ts to compute duration from
            # when the caller didn't supply duration_s directly.
            existing_row = conn.execute(
                "SELECT start_ts FROM agent_run WHERE agent_id = ?",
                [agent_id],
            ).fetchone()
            row_exists = existing_row is not None and existing_row[0] is not None

            # Normalize the caller-supplied start_ts (D#2316 PR-b), if any, the
            # same way an existing row's stored start_ts is normalized below —
            # naive datetimes are assumed UTC (they come from parsing a
            # transcript timestamp that is already UTC wall-clock).
            if start_ts is not None and hasattr(start_ts, "tzinfo") and start_ts.tzinfo is None:
                start_ts = start_ts.replace(tzinfo=timezone.utc)
            if hasattr(end_ts, "tzinfo") and end_ts.tzinfo is None:
                end_ts = end_ts.replace(tzinfo=timezone.utc)

            # Compute duration from a recoverable start time if not supplied.
            computed_duration = duration_s
            if computed_duration is None:
                if row_exists:
                    start = existing_row[0]
                    # DuckDB may return a datetime or a string
                    if isinstance(start, str):
                        start = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    if hasattr(start, "tzinfo") and start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    computed_duration = (end_ts - start).total_seconds()
                elif start_ts is not None:
                    # INSERT branch, but the caller supplied a recoverable start
                    # time (e.g. the agent's own first transcript timestamp) —
                    # compute an honest duration from it instead of guessing 0.
                    computed_duration = (end_ts - start_ts).total_seconds()
                else:
                    # INSERT branch with no recoverable start time anywhere.
                    # NULL, not 0 — a 0s duration reads as a measurement, and
                    # this run's actual duration was never observed (item 12).
                    computed_duration = None

            # Row start_ts to write on the INSERT branch: the caller-supplied
            # recoverable start time when we have one, else the historical
            # start_ts == end_ts fallback (the column is NOT NULL, so this
            # can't be left unset — duration_s carries the "unknown" signal
            # instead, per the comment above).
            insert_start_ts = start_ts if start_ts is not None else end_ts

            # Resolve the role/discussion this row gets IF the INSERT branch
            # fires. No-guessing invariant (item 10, D#2282): an unrecognised
            # role is never promoted — it stays _ORPHAN_ROLE exactly as before.
            insert_role = role if role in _KNOWN_ROLES else _ORPHAN_ROLE
            insert_discussion = discussion if insert_role != _ORPHAN_ROLE else None

            if not row_exists:
                # The INSERT branch is about to fire: no start_run() row matched
                # this agent_id. This is not fatal (the docstring above explains
                # why the INSERT branch legitimately exists), but a silent orphan
                # looks exactly like "nothing to report" from the outside. Say so.
                logger.warning(
                    "agent_run_tracker.complete_run: no started row for "
                    "agent_id=%s — creating a row with role=%s (orphan unless "
                    "caller supplied a resolved role). The id given to "
                    "complete_run did not match any start_run agent_id.",
                    agent_id, insert_role,
                )

            # INSERT ... ON CONFLICT DO UPDATE makes complete_run idempotent.
            # If start_run() ran first, the existing row is updated in-place —
            # role and discussion are deliberately absent from the DO UPDATE SET
            # list below, so a real role recorded at start_run() time can never
            # be clobbered by whatever complete_run() is told later (no-clobber
            # invariant, item 11). If start_run() never ran, a new row is
            # created using insert_role/insert_discussion (D#2316 PR-b) instead
            # of unconditionally hardcoding _ORPHAN_ROLE, since the caller often
            # already knows the real role at completion time.
            # All token fields use COALESCE so later calls can fill in missing data.
            conn.execute(
                """
                INSERT INTO agent_run
                    (agent_id, role, discussion, start_ts,
                     end_ts, duration_s, verdict, model,
                     input_tok, output_tok, cache_read, cache_write,
                     cache_creation_tokens, blocked_reason, event_id,
                     first_write_turn, total_turns, routed_via, auto_routed)
                VALUES (?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?)
                ON CONFLICT (agent_id) DO UPDATE SET
                    end_ts                = excluded.end_ts,
                    duration_s            = COALESCE(excluded.duration_s,     agent_run.duration_s),
                    verdict               = COALESCE(excluded.verdict,         agent_run.verdict),
                    model                 = COALESCE(excluded.model,           agent_run.model),
                    input_tok             = COALESCE(excluded.input_tok,       agent_run.input_tok),
                    output_tok            = COALESCE(excluded.output_tok,      agent_run.output_tok),
                    cache_read            = COALESCE(excluded.cache_read,      agent_run.cache_read),
                    cache_write           = COALESCE(excluded.cache_write,     agent_run.cache_write),
                    cache_creation_tokens = COALESCE(excluded.cache_creation_tokens,
                                                     agent_run.cache_creation_tokens),
                    blocked_reason        = COALESCE(excluded.blocked_reason,  agent_run.blocked_reason),
                    first_write_turn      = COALESCE(excluded.first_write_turn,
                                                     agent_run.first_write_turn),
                    total_turns           = COALESCE(excluded.total_turns,
                                                     agent_run.total_turns),
                    routed_via            = COALESCE(excluded.routed_via,
                                                     agent_run.routed_via),
                    auto_routed           = COALESCE(excluded.auto_routed,
                                                     agent_run.auto_routed)
                """,
                [
                    agent_id,
                    insert_role,          # only used by the INSERT branch (row_exists=False)
                    insert_discussion,    # only used by the INSERT branch (row_exists=False)
                    insert_start_ts,      # only used by the INSERT branch (row_exists=False)
                    end_ts,
                    computed_duration,
                    verdict,
                    model,
                    input_tok,
                    output_tok,
                    cache_read,
                    cache_write,
                    cache_creation_tokens,
                    blocked_reason,
                    agent_id,          # event_id == agent_id for new rows
                    first_write_turn,
                    total_turns,
                    routed_via,
                    auto_routed,
                ],
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_tracker.complete_run failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Reconcile ghost open runs
# ---------------------------------------------------------------------------

def reconcile_open_runs(
    live_ids: list[str] | None = None,
    stale_after_min: int | None = None,
    db_path: Path | None = None,
) -> int:
    """Auto-close open agent_run rows that are stale ghosts.

    A row is a ghost when it is BOTH:
      (a) absent from live_ids (not in the caller's positive-liveness set), AND
      (b) older than stale_after_min minutes (outside the grace window).

    Rows that are in live_ids OR younger than stale_after_min are NEVER closed.
    This preserves running agents and recently-started agents whose worktree
    registration may not yet have propagated.

    Parameters
    ----------
    live_ids:         IDs of agents that are confirmed still running (from the
                      worktree registry).  Empty list means no agents are live.
                      None is treated as empty — no agent is assumed live by
                      default, so the grace-window is the only safety net.
    stale_after_min:  Minutes after start_ts before an open row is eligible for
                      auto-close.  Defaults to the control-plane policy
                      ``policies.team_lead.agent_run_stale_after_min`` (default 30).
    db_path:          Override the DuckDB path (for testing).

    Returns the number of rows closed.
    """
    if live_ids is None:
        live_ids = []

    if stale_after_min is None:
        try:
            from backend.control_plane import get_value  # noqa: PLC0415
            val = get_value("policies.team_lead.agent_run_stale_after_min")
            stale_after_min = int(val) if val is not None else 30
        except Exception:  # noqa: BLE001
            stale_after_min = 30

    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        logger.warning("agent_run_tracker: duckdb not installed — skipping reconcile_open_runs")
        return 0

    if db_path is None:
        db_path = _db_path()

    if not db_path.exists():
        return 0

    try:
        now = datetime.now(timezone.utc)
        conn = duckdb.connect(str(db_path))
        try:
            _ensure_schema(conn)

            # Fetch open rows that are older than the grace window.
            # We do the live_ids filter in Python to avoid SQL parameter-list
            # quoting complexity across DuckDB versions.
            rows = conn.execute(
                """
                SELECT agent_id
                FROM agent_run
                WHERE end_ts IS NULL
                  AND start_ts < NOW() - INTERVAL (? || ' minutes')
                """,
                [str(stale_after_min)],
            ).fetchall()

            stale_ids = [r[0] for r in rows if r[0] not in live_ids]

            if not stale_ids:
                return 0

            closed = 0
            for agent_id in stale_ids:
                conn.execute(
                    """
                    UPDATE agent_run
                    SET end_ts    = ?,
                        verdict   = 'reconciled-stale',
                        duration_s = COALESCE(
                            duration_s,
                            epoch(? - start_ts)
                        )
                    WHERE agent_id = ? AND end_ts IS NULL
                    """,
                    [now, now, agent_id],
                )
                closed += 1

            logger.info(
                "reconcile_open_runs: closed %d stale ghost(s) (stale_after_min=%d, live=%d)",
                closed, stale_after_min, len(live_ids),
            )
            return closed
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_run_tracker.reconcile_open_runs failed (non-fatal): %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Population — what fraction of agent_run rows carry any token data (D#2282, PR-b)
# ---------------------------------------------------------------------------

def population(since_iso: str | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """Report what fraction of ``agent_run`` rows carry any token data.

    This is the headline health check behind D#2282: most agent_run rows are
    pre-registered drafts (``scripts/spawn-agent.sh``) that never receive a
    matching ``complete_run()`` call, so they sit at 0 tokens forever. A
    baseline measured 2026-09-03 put this at 1.217% (7,393 rows / 90 with
    tokens, all-time, Linux dev host) — re-measure rather than trust that
    number, the ratio moves as the queue runs.

    Read-only and non-fatal: degrades to a zero-row report (never raises) on
    a missing dependency, a missing DB file, or lock contention — the Spec's
    constraint that this must never block on a DB the live loop is writing.

    Returns a dict with ``rows``, ``with_tokens``, ``rate``, ``orphan_rows``,
    ``orphan_id_parseable``, ``scope``, and ``host`` — the last two are
    non-empty strings in the payload itself, not just a log line, so a
    caller comparing two measurements can tell what each one actually
    covered.
    """
    host = socket.gethostname()
    scope = f"agent_run, start_ts >= {since_iso}" if since_iso else "agent_run, full table (no time filter)"
    empty: dict[str, Any] = {
        "rows": 0,
        "with_tokens": 0,
        "rate": 0.0,
        "orphan_rows": 0,
        "orphan_id_parseable": 0,
        "scope": scope,
        "host": host,
    }

    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        print("agent_run_tracker: duckdb not installed — population reports zero rows", file=sys.stderr)
        return empty

    if db_path is None:
        db_path = _db_path()

    if not db_path.exists():
        print(f"agent_run_tracker: {db_path} does not exist — population reports zero rows", file=sys.stderr)
        return empty

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
    except Exception as exc:  # noqa: BLE001 — lock contention or any other connect failure
        print(f"agent_run_tracker: population could not open {db_path} (non-fatal): {exc}", file=sys.stderr)
        return empty

    try:
        clauses = ["1=1"]
        params: list[Any] = []
        if since_iso is not None:
            clauses.append("start_ts >= ?")
            params.append(since_iso)
        where_sql = " AND ".join(clauses)

        total_rows = conn.execute(
            f"SELECT COUNT(*) FROM agent_run WHERE {where_sql}", params
        ).fetchone()[0]
        with_tokens = conn.execute(
            f"SELECT COUNT(*) FROM agent_run WHERE {where_sql} "
            "AND coalesce(input_tok, 0) + coalesce(output_tok, 0) > 0",
            params,
        ).fetchone()[0]
        orphan_ids = conn.execute(
            f"SELECT agent_id FROM agent_run WHERE {where_sql} AND role = ?",
            [*params, _ORPHAN_ROLE],
        ).fetchall()

        orphan_rows_n = len(orphan_ids)
        orphan_id_parseable = sum(1 for (aid,) in orphan_ids if _ORPHAN_ID_RE.match(aid or ""))
        rate = (with_tokens / total_rows) if total_rows else 0.0

        return {
            "rows": total_rows,
            "with_tokens": with_tokens,
            "rate": round(rate, 6),
            "orphan_rows": orphan_rows_n,
            "orphan_id_parseable": orphan_id_parseable,
            "scope": scope,
            "host": host,
        }
    except Exception as exc:  # noqa: BLE001 — non-fatal by construction
        print(f"agent_run_tracker: population query failed (non-fatal): {exc}", file=sys.stderr)
        return empty
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Orphan attribution (D#2282, PR-c)
# ---------------------------------------------------------------------------

def attribute_orphans(dry_run: bool = True, db_path: Path | None = None) -> dict[str, Any]:
    """Attribute ``orphan-unmatched`` rows back to their real discussion.

    Deterministic string parsing on the agent_id, not fuzzy/timestamp
    matching (D#2282 finding 2). Rows whose id encodes discussion "0" (the
    discussion wasn't known yet when the row was written) are left orphaned
    — not recoverable by id, and a wrong guess is worse than an orphaned row.

    Dedupe-before-attribute (D#2282 finding 3): the same session is
    sometimes logged twice under two different agent_ids that share the
    trailing ``a<hex>`` suffix — once before its discussion was known, once
    after. Grouping by that suffix and keeping only the largest snapshot
    (by token count) avoids summing two views of one run into ~2x. The
    group's winner is promoted (role + discussion rewritten); every other
    member of the group is marked ``verdict='superseded'`` so a later run
    never re-considers it — this is what makes the pass idempotent.

    Also supersedes zero-token supersession targets (acceptance #14): a
    pre-registered draft row for the same (role, discussion) that never
    received tokens is redundant once a real row exists for that pair, and
    is marked ``verdict='superseded'`` (reusing the marker already used by
    scripts/spawn-agent.sh:327) rather than left as a live zero.

    Non-fatal by construction: degrades to an all-zero report (never
    raises, never partially writes) on a missing dependency, a missing DB
    file, or lock contention. With ``dry_run=True`` (the default) nothing is
    written; the returned counts describe what a real run would do.
    """
    host = socket.gethostname()
    scope = "agent_run, role='orphan-unmatched', full table (all-time)"
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "scope": scope,
        "host": host,
        "candidates": 0,
        "candidate_tokens": 0,
        "skipped_zero_discussion": 0,
        "skipped_unparseable": 0,
        "skipped_invalid_role": 0,
        "duplicate_groups": 0,
        "rows_written": 0,
        "rows_superseded": 0,
        "zero_token_superseded": 0,
    }

    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        print("agent_run_tracker: duckdb not installed — attribute-orphans is a no-op", file=sys.stderr)
        return result

    if db_path is None:
        db_path = _db_path()

    if not db_path.exists():
        print(f"agent_run_tracker: {db_path} does not exist — attribute-orphans is a no-op", file=sys.stderr)
        return result

    try:
        # read-only for a dry run (no reason to compete for a write lock just
        # to preview); read-write only when actually asked to mutate.
        conn = duckdb.connect(str(db_path), read_only=dry_run)
    except Exception as exc:  # noqa: BLE001 — lock contention or any other connect failure
        print(
            f"agent_run_tracker: attribute-orphans could not open {db_path} "
            f"(non-fatal, likely lock contention): {exc}",
            file=sys.stderr,
        )
        return result

    try:
        rows = conn.execute(
            "SELECT agent_id, input_tok, output_tok, start_ts FROM agent_run "
            "WHERE role = ? AND (verdict IS NULL OR verdict != ?)",
            [_ORPHAN_ROLE, "superseded"],
        ).fetchall()

        groups: dict[str, list[dict[str, Any]]] = {}
        skipped_zero_discussion = 0
        skipped_unparseable = 0
        skipped_invalid_role = 0

        for agent_id, in_tok, out_tok, start_ts in rows:
            parsed = _parse_orphan_id(agent_id or "")
            if parsed is None:
                skipped_unparseable += 1
                continue
            if parsed["role"] not in _KNOWN_ROLES:
                skipped_invalid_role += 1
                continue
            groups.setdefault(parsed["hex"], []).append({
                "agent_id": agent_id,
                "role": parsed["role"],
                "discussion": parsed["discussion"],
                "tokens": int(in_tok or 0) + int(out_tok or 0),
                "start_ts": start_ts,
            })

        # Reporting counts are per-row classifications (candidates +
        # skipped_zero_discussion + skipped_unparseable + skipped_invalid_role
        # == total orphan rows scanned) — independent of the group
        # consolidation below, which decides what actually gets written.
        candidates = 0
        candidate_tokens = 0
        for members in groups.values():
            for m in members:
                if m["discussion"] == 0:
                    skipped_zero_discussion += 1
                else:
                    candidates += 1
                    candidate_tokens += m["tokens"]

        duplicate_groups = 0
        to_write: list[tuple[str, str, int]] = []
        to_supersede: list[str] = []

        for members in groups.values():
            real_discussions = {m["discussion"] for m in members if m["discussion"] != 0}
            roles = {m["role"] for m in members}
            if not real_discussions or len(real_discussions) > 1 or len(roles) > 1:
                # No recoverable discussion, or the group disagrees with
                # itself — never guess. Leave every member orphaned.
                continue

            discussion = next(iter(real_discussions))
            role = next(iter(roles))

            if len(members) > 1:
                duplicate_groups += 1

            # Largest snapshot wins (D#2282 finding 3) — tokens only grow
            # between repeated writes of the same session.
            winner = max(members, key=lambda m: (m["tokens"], m["start_ts"] or ""))
            to_write.append((winner["agent_id"], role, discussion))
            for m in members:
                if m["agent_id"] != winner["agent_id"]:
                    to_supersede.append(m["agent_id"])

        rows_written = 0
        rows_superseded = 0
        zero_token_superseded = 0

        if not dry_run:
            for agent_id, role, discussion in to_write:
                conn.execute(
                    "UPDATE agent_run SET role = ?, discussion = ? WHERE agent_id = ?",
                    [role, discussion, agent_id],
                )
                rows_written += 1

                ghosts = conn.execute(
                    "SELECT agent_id FROM agent_run "
                    "WHERE role = ? AND discussion = ? AND agent_id != ? "
                    "AND coalesce(input_tok, 0) + coalesce(output_tok, 0) = 0 "
                    "AND (verdict IS NULL OR verdict != ?)",
                    [role, discussion, agent_id, "superseded"],
                ).fetchall()
                for (ghost_id,) in ghosts:
                    conn.execute(
                        "UPDATE agent_run SET verdict = ? WHERE agent_id = ?",
                        ["superseded", ghost_id],
                    )
                    zero_token_superseded += 1

            for agent_id in to_supersede:
                conn.execute(
                    "UPDATE agent_run SET verdict = ? WHERE agent_id = ?",
                    ["superseded", agent_id],
                )
                rows_superseded += 1

        result.update({
            "candidates": candidates,
            "candidate_tokens": candidate_tokens,
            "skipped_zero_discussion": skipped_zero_discussion,
            "skipped_unparseable": skipped_unparseable,
            "skipped_invalid_role": skipped_invalid_role,
            "duplicate_groups": duplicate_groups,
            "rows_written": rows_written if not dry_run else len(to_write),
            "rows_superseded": rows_superseded if not dry_run else len(to_supersede),
            "zero_token_superseded": zero_token_superseded,
        })
        return result
    except Exception as exc:  # noqa: BLE001 — non-fatal by construction
        print(f"agent_run_tracker: attribute-orphans failed (non-fatal): {exc}", file=sys.stderr)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backfill from audit_trail
# ---------------------------------------------------------------------------

def _audit_log_path() -> Path:
    """Return the audit log path.

    Delegates to ``state_paths.AUDIT_LOG``. The old version guarded its
    scratch-dir branch with ``if p.exists()``, so a *fresh* (still empty)
    AUTONOMOUS_TEAM_STATE_DIR lost to the in-repo legacy path — which meant
    the one thing every reviewer is told to do for isolation, point the env
    var at a clean directory, silently did not isolate this reader (D#1967).
    """
    from backend import state_paths  # noqa: PLC0415
    return state_paths.AUDIT_LOG


def backfill(audit_path: Path | None = None, db_path: Path | None = None) -> int:
    """Reconstruct agent_run rows from audit_trail entries.

    Reads all audit.jsonl entries where ``source`` is "agent" or
    ``action`` is "spawn" / "complete" / "agent_done", groups them by
    ``event_id``, and inserts one agent_run row per group.

    Idempotent: uses INSERT OR IGNORE for rows whose agent_id already exists,
    and UPDATE for end_ts/verdict when a completion event is found for a
    row whose end_ts is still NULL.

    Returns the number of rows inserted or updated.
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        logger.warning("agent_run_tracker: duckdb not installed — skipping backfill")
        return 0

    if audit_path is None:
        audit_path = _audit_log_path()
    if db_path is None:
        db_path = _db_path()

    # Collect entries from audit.jsonl (and audit.jsonl.1 if present)
    entries: list[dict] = []
    for suffix in ["", ".1"]:
        path = Path(str(audit_path) + suffix) if suffix else audit_path
        if path.exists():
            try:
                with path.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except OSError as exc:
                logger.warning("backfill: could not read %s: %s", path, exc)

    if not entries:
        logger.info("backfill: no audit entries found at %s", audit_path)
        return 0

    # Group events by event_id into run candidates
    # Each group accumulates: role, discussion, pr, model, start_ts, end_ts,
    # verdict, input_tok, output_tok
    runs: dict[str, dict] = {}

    for entry in entries:
        new_val = entry.get("new") or {}
        if not isinstance(new_val, dict):
            try:
                new_val = json.loads(new_val) if isinstance(new_val, str) else {}
            except (json.JSONDecodeError, TypeError):
                new_val = {}

        # event_id can live at top level or inside "new"
        eid = entry.get("event_id") or new_val.get("event_id")
        if not eid:
            continue

        action = entry.get("action", "")
        source = entry.get("source", "")

        # Determine if this is a start or completion event
        is_start = action in ("spawn", "agent_start", "start") or (
            source in ("spawn_agent", "pre_spawn", "pre-spawn-check")
        )
        is_complete = action in (
            "agent_done", "complete", "agent_complete", "post_agent",
            "agent_end", "verdict",
        ) or source in ("post_agent_hook", "post-agent-hook")

        if not (is_start or is_complete):
            continue

        if eid not in runs:
            runs[eid] = {
                "agent_id": eid,
                "role": None,
                "discussion": None,
                "pr": None,
                "start_ts": None,
                "end_ts": None,
                "verdict": None,
                "model": None,
                "input_tok": None,
                "output_tok": None,
                "cache_read": None,
                "cache_write": None,
            }

        run = runs[eid]

        # Extract fields from entry top-level and from "new" dict
        role = entry.get("actor") or new_val.get("role") or new_val.get("agent")
        disc = new_val.get("discussion")
        pr = new_val.get("pr")
        model = new_val.get("model")
        verdict = new_val.get("verdict")

        tokens = new_val.get("tokens") or {}
        input_tok = new_val.get("input_tokens") or tokens.get("input")
        output_tok = new_val.get("output_tokens") or tokens.get("output")
        cache_read = new_val.get("cache_read_tokens") or new_val.get("cache_read")
        cache_write = new_val.get("cache_write_tokens") or new_val.get("cache_write")

        ts_str = entry.get("ts")
        ts = None
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        # Fill in fields (first non-None wins for start fields)
        if role and not run["role"]:
            run["role"] = role
        if disc is not None and run["discussion"] is None:
            run["discussion"] = int(disc) if disc else None
        if pr is not None and run["pr"] is None:
            run["pr"] = int(pr) if pr else None
        if model and not run["model"]:
            run["model"] = model

        if is_start and ts and run["start_ts"] is None:
            run["start_ts"] = ts
        if is_complete:
            if ts:
                run["end_ts"] = ts
            if verdict:
                run["verdict"] = verdict
            if input_tok is not None:
                run["input_tok"] = int(input_tok)
            if output_tok is not None:
                run["output_tok"] = int(output_tok)
            if cache_read is not None:
                run["cache_read"] = int(cache_read)
            if cache_write is not None:
                run["cache_write"] = int(cache_write)

    # Write to DuckDB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    count = 0
    try:
        _ensure_schema(conn)
        for run in runs.values():
            # Skip entirely if no role or no start_ts — too little data
            if not run["role"] or run["start_ts"] is None:
                continue

            # Compute duration if both timestamps known
            dur = None
            if run["start_ts"] and run["end_ts"]:
                dur = (run["end_ts"] - run["start_ts"]).total_seconds()

            # INSERT OR IGNORE for new rows
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_run
                    (agent_id, role, discussion, pr, start_ts, end_ts,
                     duration_s, verdict, model, input_tok, output_tok,
                     cache_read, cache_write, event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run["agent_id"],
                    run["role"],
                    run["discussion"],
                    run["pr"],
                    run["start_ts"],
                    run["end_ts"],
                    dur,
                    run["verdict"],
                    run["model"],
                    run["input_tok"],
                    run["output_tok"],
                    run["cache_read"],
                    run["cache_write"],
                    run["agent_id"],  # event_id == agent_id
                ],
            )

            # UPDATE end_ts / verdict for existing rows that were open
            if run["end_ts"] is not None:
                conn.execute(
                    """
                    UPDATE agent_run SET
                        end_ts     = COALESCE(end_ts, ?),
                        duration_s = COALESCE(duration_s, ?),
                        verdict    = COALESCE(verdict, ?)
                    WHERE agent_id = ? AND end_ts IS NULL
                    """,
                    [run["end_ts"], dur, run["verdict"], run["agent_id"]],
                )

            count += 1
    finally:
        conn.close()

    return count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="agent_run_tracker — start, complete, or backfill agent run records",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # --- start ---
    s = sub.add_parser("start", help="Record agent run start (INSERT)")
    s.add_argument("--agent-id", required=True)
    s.add_argument("--role", required=True)
    s.add_argument("--discussion", type=int, default=None)
    s.add_argument("--pr", type=int, default=None)
    s.add_argument("--event-id", default=None)
    s.add_argument("--model", default=None)

    # --- complete ---
    c = sub.add_parser("complete", help="Record agent run completion (UPSERT)")
    c.add_argument("--agent-id", required=True)
    c.add_argument("--verdict", default=None)
    c.add_argument("--model", default=None)
    c.add_argument("--input-tokens", type=int, default=None)
    c.add_argument("--output-tokens", type=int, default=None)
    c.add_argument("--cache-read", type=int, default=None)
    c.add_argument("--cache-write", type=int, default=None)
    c.add_argument("--cache-creation-tokens", type=int, default=None)
    c.add_argument("--blocked-reason", default=None)
    c.add_argument("--first-write-turn", type=int, default=None)
    c.add_argument("--total-turns", type=int, default=None)
    c.add_argument(
        "--role", default=None,
        help="Real role for this run (D#2316 PR-b), used only when no start_run() "
             "row exists yet. Ignored (never clobbers) when a row already exists.",
    )
    c.add_argument(
        "--discussion", type=int, default=None,
        help="Real Discussion number, paired with --role. Same insert-only, "
             "no-clobber treatment.",
    )
    c.add_argument(
        "--start-ts", default=None,
        help="ISO8601 recoverable start time for this run (e.g. the agent's own "
             "first transcript timestamp), used only when no start_run() row "
             "exists yet, to compute an honest duration_s instead of writing 0.",
    )

    # --- backfill ---
    b = sub.add_parser("backfill", help="Reconstruct runs from audit_trail (idempotent)")
    b.add_argument("--audit-path", default=None, help="Override audit.jsonl path")
    b.add_argument("--db-path", default=None, help="Override stats.duckdb path")

    # --- reconcile ---
    r = sub.add_parser(
        "reconcile",
        help="Auto-close stale ghost open runs not in the live-id set",
    )
    r.add_argument(
        "--live-ids",
        nargs="*",
        default=None,
        metavar="AGENT_ID",
        help="Space-separated agent IDs confirmed still running.  "
             "Rows for these IDs are never closed regardless of age.",
    )
    r.add_argument(
        "--stale-after-min",
        type=int,
        default=None,
        metavar="N",
        help="Minutes after start_ts before a row is eligible for auto-close "
             "(default: control-plane policy or 30).",
    )
    r.add_argument("--db-path", default=None, help="Override stats.duckdb path")

    # --- population (D#2282, PR-b) ---
    p = sub.add_parser(
        "population",
        help="Report the fraction of agent_run rows carrying any token data",
    )
    p.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="Restrict to rows with start_ts >= this timestamp.",
    )
    p.add_argument("--db-path", default=None, help="Override stats.duckdb path")
    p.add_argument("--json", action="store_true", dest="json_output")

    # --- attribute-orphans (D#2282, PR-c) ---
    ao = sub.add_parser(
        "attribute-orphans",
        help="Attribute orphan-unmatched rows back to their real discussion "
             "by parsing the agent_id (see agent_run_tracker.attribute_orphans)",
    )
    ao.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything (default: write).",
    )
    ao.add_argument("--db-path", default=None, help="Override stats.duckdb path")
    ao.add_argument("--json", action="store_true", dest="json_output")

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        start_run(
            agent_id=args.agent_id,
            role=args.role,
            discussion=args.discussion,
            pr=args.pr,
            event_id=args.event_id,
            model=args.model,
        )
        return 0

    if args.command == "complete":
        cli_start_ts = None
        if args.start_ts:
            try:
                cli_start_ts = datetime.fromisoformat(args.start_ts.replace("Z", "+00:00"))
            except ValueError:
                logger.warning(
                    "agent_run_tracker: ignoring unparseable --start-ts=%r", args.start_ts,
                )
        complete_run(
            agent_id=args.agent_id,
            verdict=args.verdict,
            model=args.model,
            input_tok=args.input_tokens,
            output_tok=args.output_tokens,
            cache_read=args.cache_read,
            cache_write=args.cache_write,
            cache_creation_tokens=args.cache_creation_tokens,
            blocked_reason=args.blocked_reason,
            first_write_turn=args.first_write_turn,
            total_turns=args.total_turns,
            role=args.role,
            discussion=args.discussion,
            start_ts=cli_start_ts,
        )
        return 0

    if args.command == "backfill":
        audit_path = Path(args.audit_path) if args.audit_path else None
        db_path = Path(args.db_path) if args.db_path else None
        n = backfill(audit_path=audit_path, db_path=db_path)
        print(f"backfill: {n} rows processed")
        return 0

    if args.command == "population":
        db_path = Path(args.db_path) if args.db_path else None
        result = population(since_iso=args.since, db_path=db_path)
        if args.json_output:
            print(json.dumps(result))
        else:
            print(
                f"rows={result['rows']} with_tokens={result['with_tokens']} "
                f"rate={result['rate']:.4f} orphan_rows={result['orphan_rows']} "
                f"orphan_id_parseable={result['orphan_id_parseable']} "
                f"scope={result['scope']!r} host={result['host']!r}"
            )
        return 0

    if args.command == "attribute-orphans":
        db_path = Path(args.db_path) if args.db_path else None
        result = attribute_orphans(dry_run=args.dry_run, db_path=db_path)
        if args.json_output:
            print(json.dumps(result))
        else:
            print(json.dumps(result, indent=2))
        return 0

    if args.command == "reconcile":
        db_path = Path(args.db_path) if args.db_path else None
        n = reconcile_open_runs(
            live_ids=args.live_ids,
            stale_after_min=args.stale_after_min,
            db_path=db_path,
        )
        print(f"reconciled: {n} rows")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
