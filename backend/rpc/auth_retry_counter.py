"""RPC handlers: auth_retry.record and auth_retry.summary

Telemetry for the 401-retry recovery path added in PR #1064.

Every time the frontend recovers from a 401 via the retry path it POSTs
auth_retry.record. This handler increments a counter row in the project
blackboard (key: auth_retry_count) and appends an ISO8601 timestamp to
auth_retry_timestamps so auth_retry.summary can compute a 24-hour count.

auth_retry.summary returns:
  { count_24h: int, count_total: int, last_seen: iso8601 | null }
"""
from __future__ import annotations

import datetime
import json

from backend.blackboard import get_blackboard


_TOTAL_KEY = "auth_retry_count"
_TS_KEY = "auth_retry_timestamps"  # JSON list of ISO8601 strings, newest last


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _cutoff_iso() -> str:
    """ISO8601 timestamp for exactly 24 hours ago."""
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()


def handle_record(params: dict) -> dict:  # noqa: ARG001
    """Increment the auth-retry counter. Best-effort: never raises to the caller."""
    try:
        bb = get_blackboard()

        # Increment total counter
        current = bb.read(_TOTAL_KEY)
        new_total = (int(current) if isinstance(current, (int, float)) else 0) + 1
        bb.write(_TOTAL_KEY, new_total, updated_by="auth_retry_counter")

        # Append timestamp for 24h windowing
        raw = bb.read(_TS_KEY)
        if isinstance(raw, list):
            timestamps = list(raw)
        elif isinstance(raw, str):
            try:
                timestamps = json.loads(raw)
                if not isinstance(timestamps, list):
                    timestamps = []
            except Exception:  # noqa: BLE001
                timestamps = []
        else:
            timestamps = []

        timestamps.append(_now_iso())
        # Prune entries older than 48 hours to keep the list bounded
        cutoff_48h = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
        ).isoformat()
        timestamps = [t for t in timestamps if t >= cutoff_48h]
        bb.write(_TS_KEY, timestamps, updated_by="auth_retry_counter")

        return {"recorded": True, "count_total": new_total}
    except Exception:  # noqa: BLE001
        # Best-effort: swallow all errors so the 401 retry still proceeds
        return {"recorded": False}


def handle_summary(params: dict) -> dict:  # noqa: ARG001
    """Return { count_24h, count_total, last_seen }."""
    try:
        bb = get_blackboard()

        total_raw = bb.read(_TOTAL_KEY)
        count_total = int(total_raw) if isinstance(total_raw, (int, float)) else 0

        ts_raw = bb.read(_TS_KEY)
        if isinstance(ts_raw, list):
            timestamps = list(ts_raw)
        elif isinstance(ts_raw, str):
            try:
                timestamps = json.loads(ts_raw)
                if not isinstance(timestamps, list):
                    timestamps = []
            except Exception:  # noqa: BLE001
                timestamps = []
        else:
            timestamps = []

        cutoff = _cutoff_iso()
        count_24h = sum(1 for t in timestamps if t >= cutoff)
        last_seen = timestamps[-1] if timestamps else None

        return {
            "count_24h": count_24h,
            "count_total": count_total,
            "last_seen": last_seen,
        }
    except Exception:  # noqa: BLE001
        return {"count_24h": 0, "count_total": 0, "last_seen": None}
