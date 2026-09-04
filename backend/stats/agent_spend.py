"""
backend/stats/agent_spend.py — normalized agent-spend reader for cost_tracker.

`agent_run` (DuckDB) is the authoritative source of per-agent token counts —
see D#2232 -> D#2238 -> D#2247. The blackboard's `budget/agents/` prefix is a
second, lossier copy of the same facts, fed by a shell -> argparse hop
(`record-agent-result.sh` -> `budget.py spend`) that can fail silently (D#2256
fixed one such failure, but the store should not be trusted as complete).

Precedence, never union: for a given Discussion or PR, if `agent_run` yields
any row, `agent_run` alone answers for it; the blackboard is consulted only
when `agent_run` has nothing for that scope. Unioning the two would double-
count once the blackboard writer is healthy again.

Every record carries a `source` field ("agent_run" | "budget_blackboard") so a
caller can tell "no spend" (empty list) from "source unavailable" (a reader
degraded — logged to stderr, never raised).

Public API
----------
records_for_discussion(discussion, bb=None) -> list[dict]
records_for_pr(pr, bb=None) -> list[dict]
all_records(since_iso=None, bb=None) -> list[dict]

Normalized record shape (matches what cost_tracker's aggregation already
expects from a `budget/agents/*` blackboard entry):
    {
        "agent":              str    (role)
        "agent_id":           str
        "input":              int
        "output":             int
        "cache_read_tokens":  int
        "cache_write_tokens": int
        "model":              str
        "discussion":         int | None
        "pr":                 int | None
        "finished":           str | None  (ISO-8601)
        "source":             "agent_run" | "budget_blackboard"
    }
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

_AGENTS_PREFIX = "budget/agents/"
_ORPHAN_ROLE = "orphan-unmatched"


def _log_unavailable(what: str, exc: BaseException) -> None:
    print(f"[agent_spend] {what} unavailable: {exc}", file=sys.stderr)


def _iso_or_none(val: Any) -> str | None:
    """Normalize a DuckDB timestamp value to an ISO-8601 string.

    Mirrors backend.agent_run_reader._row_to_dict's convention: a naive
    datetime is labelled UTC rather than converted (existing behavior this
    module doesn't change). Records elsewhere in cost_tracker (e.g. the
    blackboard fallback) already store `finished` as an ISO string, so this
    keeps the two sources comparable/sortable.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    return str(val)


# ---------------------------------------------------------------------------
# agent_run (DuckDB) side — authoritative
# ---------------------------------------------------------------------------


def _normalize_agent_run_row(row: dict) -> dict:
    """Map an agent_run row to the normalized record shape."""
    return {
        "agent": row.get("role") or "unknown",
        "agent_id": row.get("agent_id"),
        "input": int(row.get("input_tok") or 0),
        "output": int(row.get("output_tok") or 0),
        "cache_read_tokens": int(row.get("cache_read") or 0),
        "cache_write_tokens": int(row.get("cache_write") or 0),
        "model": row.get("model") or "default",
        "discussion": row.get("discussion"),
        "pr": row.get("pr"),
        "finished": _iso_or_none(row.get("end_ts")),
        "source": "agent_run",
    }


def _agent_run_records(
    discussion: int | None = None,
    pr: int | None = None,
    since_iso: str | None = None,
) -> list[dict]:
    """Query agent_run for rows matching the given scope, normalized.

    Returns [] on any failure (missing duckdb, missing/locked DB file, an
    unsandboxed test environment, or any query error) — never raises. Each
    failure is logged to stderr so a caller can distinguish "no spend" from
    "source unavailable" by reading the log, per the Spec's `source`
    requirement.
    """
    try:
        from backend.agent_run_reader import _connect  # noqa: PLC0415

        conn = _connect()
    except Exception as exc:  # noqa: BLE001 — any connect failure degrades to []
        _log_unavailable("agent_run (stats.duckdb)", exc)
        return []

    try:
        # `verdict != 'superseded'` excludes two kinds of housekeeping rows
        # attribute_orphans() (D#2282) writes, both of which would otherwise
        # show up as a spurious zero-cost agent on a discussion that already
        # has a real row: the losing half of a duplicate-session pair, and a
        # pre-registered draft superseded by the real orphan row it matched.
        clauses = ["role != ?", "(verdict IS NULL OR verdict != ?)"]
        params: list[Any] = [_ORPHAN_ROLE, "superseded"]
        if discussion is not None:
            clauses.append("discussion = ?")
            params.append(discussion)
        if pr is not None:
            clauses.append("pr = ?")
            params.append(pr)
        if since_iso is not None:
            clauses.append("start_ts >= ?")
            params.append(since_iso)

        query = (
            "SELECT agent_id, role, discussion, pr, end_ts, model, "
            "input_tok, output_tok, cache_read, cache_write "
            "FROM agent_run WHERE " + " AND ".join(clauses)
        )
        cur = conn.execute(query, params)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
        normalized = [_normalize_agent_run_row(dict(zip(cols, r))) for r in rows]
        # Belt-and-suspenders: the WHERE clause already excludes orphan-unmatched
        # rows, but filter client-side too so this holds even if a caller's
        # query changes underneath us.
        return [r for r in normalized if r["agent"] != _ORPHAN_ROLE]
    except Exception as exc:  # noqa: BLE001 — non-fatal by construction
        _log_unavailable("agent_run query", exc)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# budget/agents/ (blackboard) side — precedence fallback
# ---------------------------------------------------------------------------


def _normalize_blackboard_record(record: dict, key: str) -> dict:
    """Map a budget/agents/* blackboard entry to the normalized record shape."""
    return {
        "agent": record.get("agent", "unknown"),
        "agent_id": record.get("agent_id", key.replace(_AGENTS_PREFIX, "")),
        "input": int(record.get("input", 0) or 0),
        "output": int(record.get("output", 0) or 0),
        "cache_read_tokens": int(record.get("cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(record.get("cache_write_tokens", 0) or 0),
        "model": record.get("model") or "default",
        "discussion": record.get("discussion"),
        "pr": record.get("pr"),
        "finished": record.get("finished"),
        "source": "budget_blackboard",
    }


def _blackboard_records(bb: Any) -> list[dict]:
    """Return every budget/agents/* record, normalized. [] on any error."""
    if bb is None:
        from backend.blackboard import Blackboard  # noqa: PLC0415

        bb = Blackboard()
    try:
        keys = bb.list_keys(_AGENTS_PREFIX)
    except Exception as exc:  # noqa: BLE001
        _log_unavailable("budget/agents/ blackboard", exc)
        return []

    out: list[dict] = []
    for key in keys:
        try:
            record = bb.read(key)
        except Exception as exc:  # noqa: BLE001
            _log_unavailable(f"budget/agents/ blackboard read({key!r})", exc)
            continue
        if not isinstance(record, dict):
            continue
        out.append(_normalize_blackboard_record(record, key))
    return out


def _blackboard_records_for_pr(pr: int, bb: Any) -> list[dict]:
    """Blackboard fallback matcher for a PR — mirrors the pre-D#2256 logic:

    match by the Discussion linked from `quality/<pr>`, or by the PR number
    appearing in the record's `agent_id` (legacy records with no `pr` field).
    """
    if bb is None:
        from backend.blackboard import Blackboard  # noqa: PLC0415

        bb = Blackboard()

    linked_discussion: int | None = None
    try:
        quality_record = bb.read(f"quality/{pr}")
        if isinstance(quality_record, dict):
            linked_discussion = quality_record.get("discussion") or quality_record.get("pr")
    except Exception as exc:  # noqa: BLE001
        _log_unavailable(f"quality/{pr} blackboard read", exc)

    matched: list[dict] = []
    for rec in _blackboard_records(bb):
        if linked_discussion is not None and rec.get("discussion") == linked_discussion:
            matched.append(rec)
        elif str(pr) in str(rec.get("agent_id", "")):
            matched.append(rec)
    return matched


# ---------------------------------------------------------------------------
# Public API — precedence composition
# ---------------------------------------------------------------------------


def records_for_discussion(discussion: int, bb: Any = None) -> list[dict]:
    """Return normalized spend records for one Discussion.

    agent_run wins if it has any row for this discussion; the blackboard is
    consulted only when agent_run yields nothing.
    """
    ar = _agent_run_records(discussion=discussion)
    if ar:
        return ar
    return [r for r in _blackboard_records(bb) if r.get("discussion") == discussion]


def records_for_pr(pr: int, bb: Any = None) -> list[dict]:
    """Return normalized spend records for one PR.

    agent_run wins if it has any row tagged with this PR; the blackboard is
    consulted only when agent_run yields nothing, using the legacy
    discussion-link / agent_id-substring matcher.
    """
    ar = _agent_run_records(pr=pr)
    if ar:
        return ar
    return _blackboard_records_for_pr(pr, bb)


def all_records(since_iso: str | None = None, bb: Any = None) -> list[dict]:
    """Return normalized spend records across all Discussions.

    Precedence is applied per-Discussion: any Discussion with at least one
    agent_run row is represented solely by its agent_run rows. Blackboard
    records for Discussions agent_run has nothing for (including records
    with no discussion at all, e.g. the historical D#1793 / PR #2061 rows)
    pass through unchanged, so they don't vanish from reporting.
    """
    ar = _agent_run_records(since_iso=since_iso)
    ar_discussions = {r["discussion"] for r in ar if r.get("discussion") is not None}

    bb_rows = _blackboard_records(bb)
    fallback = [r for r in bb_rows if r.get("discussion") not in ar_discussions]

    return ar + fallback
