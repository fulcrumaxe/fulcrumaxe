"""backend/verdict_overturn.py — Verdict-overturn ledger (Discussion #1397).

Records when a role's pass/done verdict on a PR is later contradicted by a
different role returning needs-fix or fail.  Surfaces per-role overturn rate
for the dashboard reliability tile.

Kind enum (PR1 implements downstream_needs_fix only):
  downstream_needs_fix — a later agent on the same PR returns needs-fix/fail
                         after an earlier agent passed/done.
  red_main             — (future)
  vacuous_test         — (future)
  false_fail           — (future)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from backend.stats_writer import record as _record

OverturnKind = Literal[
    "downstream_needs_fix",
    "red_main",
    "vacuous_test",
    "false_fail",
]


def record_overturn(
    pr: int,
    prior_role: str,
    prior_verdict: str,
    contradicting_source: str,
    kind: OverturnKind,
    evidence_ref: str,
    ts: datetime | None = None,
) -> None:
    """Write one verdict_overturn metric row via stats_writer.record().

    Tags include role, kind, and pr so the reader can aggregate per-role.
    No schema migration required — uses the existing metric_event table.

    Args:
        pr:                   PR number where the overturn was detected.
        prior_role:           Role whose earlier pass/done was overturned.
        prior_verdict:        The overturned verdict ("pass" or "done").
        contradicting_source: Role or agent-id that issued the contradicting verdict.
        kind:                 Overturn kind (see module docstring).
        evidence_ref:         Path to pr-artifacts JSONL that captures the evidence.
        ts:                   Timestamp (defaults to now UTC).
    """
    now = ts or datetime.now(timezone.utc)
    _record(
        metric="verdict_overturn",
        value=1.0,
        unit="count",
        tags={
            "role": prior_role,
            "kind": kind,
            "pr": str(pr),
            "prior_verdict": prior_verdict,
            "contradicting_source": contradicting_source,
            "evidence_ref": evidence_ref,
        },
        source="verdict-overturn-hook",
        ts=now,
    )


def overturn_rate_by_role_24h() -> list[dict]:
    """Return per-role overturn rate over the last 24 hours.

    Returns a list of dicts:
        [{role, overturns, total_pass, overturn_rate, sample_size}, ...]

    Rules:
    - overturn_rate = overturns / total_pass for that role
    - Roles with sample_size < 5 have overturn_rate = None (shown as N/A in UI)
    - sample_size = total_pass verdicts recorded for the role in 24h (from role_verdict metric)
    - Sorted: highest overturn_rate first, None rows last
    """
    from pathlib import Path  # noqa: PLC0415

    try:
        from backend.stats_writer import _db_path  # noqa: PLC0415
    except ImportError:
        return []

    db = _db_path()
    if not Path(str(db)).exists():
        return []

    try:
        from backend.stats_connection import get_read_connection  # noqa: PLC0415
        conn = get_read_connection()
        try:
            # Count overturn events per prior_role in the last 24h
            overturn_rows = conn.execute(
                """
                SELECT
                    json_extract_string(tags, '$.role') AS role,
                    COUNT(*)                            AS overturns
                FROM metric_event
                WHERE metric = 'verdict_overturn'
                  AND ts >= NOW() - INTERVAL 24 HOURS
                  AND json_extract_string(tags, '$.role') IS NOT NULL
                GROUP BY json_extract_string(tags, '$.role')
                """,
            ).fetchall()

            # Count total pass/done verdicts per role in the last 24h (the denominator)
            pass_rows = conn.execute(
                """
                SELECT
                    json_extract_string(tags, '$.role')   AS role,
                    COUNT(*)                              AS total_pass
                FROM metric_event
                WHERE metric = 'role_verdict'
                  AND ts >= NOW() - INTERVAL 24 HOURS
                  AND json_extract_string(tags, '$.verdict') IN ('pass', 'done')
                  AND json_extract_string(tags, '$.role') IS NOT NULL
                GROUP BY json_extract_string(tags, '$.role')
                """,
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []

    # Build lookup dicts
    overturn_map: dict[str, int] = {r[0]: int(r[1]) for r in overturn_rows if r[0]}
    pass_map: dict[str, int] = {r[0]: int(r[1]) for r in pass_rows if r[0]}

    # Union of all roles seen in either query
    all_roles = set(overturn_map) | set(pass_map)

    result = []
    for role in all_roles:
        overturns = overturn_map.get(role, 0)
        total_pass = pass_map.get(role, 0)
        # sample_size is total_pass (the denominator / exposure count)
        sample_size = total_pass
        overturn_rate = (overturns / total_pass) if sample_size >= 5 else None
        result.append(
            {
                "role": role,
                "overturns": overturns,
                "total_pass": total_pass,
                "overturn_rate": overturn_rate,
                "sample_size": sample_size,
            }
        )

    # Sort: highest overturn_rate first, None rows last
    result.sort(
        key=lambda r: (
            r["overturn_rate"] is None,
            -(r["overturn_rate"] if r["overturn_rate"] is not None else 0),
        )
    )
    return result
