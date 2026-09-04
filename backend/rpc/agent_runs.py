"""RPC handlers for agent_run queries (Discussion #635 PR-d).

Registered in backend/server.py as:
    runs.by_role
    runs.percentiles
    runs.stuck
    runs.roundtrip
    runs.active_over_time
    runs.recent

All handlers delegate to backend.agent_run_reader — no business logic here.
"""
from __future__ import annotations

import os
import sys

# Ensure backend/ is importable when the rpc module is loaded from server.py
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import backend.agent_run_reader as _reader


def handle_by_role(params: dict) -> dict:
    """Return all agent_run rows for a given role.

    Params:
        role (str, required) — agent role name, e.g. "executor"
        since_iso (str, optional) — ISO-8601 lower bound (UTC)

    Response:
        {"runs": [...]}  — list of row dicts
    """
    role = params.get("role", "")
    if not role:
        raise ValueError("'role' parameter is required")
    since_iso = params.get("since_iso") or None
    rows = _reader.by_role(role, since_iso=since_iso)
    return {"runs": rows}


def handle_percentiles(params: dict) -> dict:
    """Return duration percentiles across completed runs.

    Params:
        role (str, optional) — filter to one role; omit for all roles
        since_iso (str, optional) — ISO-8601 lower bound (UTC); default 7d

    Response:
        {"p50": float|null, "p95": float|null, "p99": float|null, "sample_size": int}
    """
    role = params.get("role") or None
    since_iso = params.get("since_iso") or None
    return _reader.duration_percentiles(role=role, since_iso=since_iso)


def handle_stuck(params: dict) -> dict:
    """Return in-flight runs older than threshold_seconds with no end_ts.

    Params:
        threshold_seconds (int, optional) — default 1800 (30 min)

    Response:
        {"runs": [...]}  — list of row dicts, oldest first
    """
    threshold = int(params.get("threshold_seconds", 1800))
    rows = _reader.stuck_runs(threshold_seconds=threshold)
    return {"runs": rows}


def handle_roundtrip(params: dict) -> dict:
    """Return executor-done → reviewer-started latency for a PR.

    Params:
        pr (int, required) — GitHub PR number

    Response:
        {"pr": int, "latency_seconds": float|null}
        latency_seconds is null when either endpoint is missing.
    """
    pr_raw = params.get("pr")
    if pr_raw is None:
        raise ValueError("'pr' parameter is required")
    pr = int(pr_raw)
    latency = _reader.roundtrip_latency(pr)
    return {"pr": pr, "latency_seconds": latency}


def handle_active_over_time(params: dict) -> dict:
    """Return time-series of concurrent active agent counts.

    Params:
        since_iso (str, optional) — default 24h ago
        until_iso (str, optional) — default now
        bucket_seconds (int, optional) — default 60

    Response:
        {"points": [{"ts": str, "count": int}, ...]}
    """
    since_iso = params.get("since_iso") or None
    until_iso = params.get("until_iso") or None
    bucket_seconds = int(params.get("bucket_seconds", 60))
    points = _reader.concurrent_active(
        since_iso=since_iso,
        until_iso=until_iso,
        bucket_seconds=bucket_seconds,
    )
    return {"points": points}


def handle_recent(params: dict) -> dict:
    """Return the most recent completed agent_run rows across all roles.

    Params:
        limit (int, optional) — max rows returned; default 50
        since_iso (str, optional) — lower bound; default 7 days

    Response:
        {"runs": [...]}
    """
    limit = int(params.get("limit", 50))
    since_iso = params.get("since_iso") or None
    # by_role with role=None is not exposed; query all roles via percentiles path
    # Use the reader connection directly for a cross-role recent query.
    rows = _reader._recent(limit=limit, since_iso=since_iso)  # noqa: SLF001
    return {"runs": rows}
