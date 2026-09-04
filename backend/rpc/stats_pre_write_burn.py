"""RPC handler: stats.pre_write_burn

Return the most-recent N executor agent_run rows where the pre-Write turn
ratio (first_write_turn / total_turns) exceeds 10%. This is the human-visible
evidence channel for the spawn-template cache-hit work.

Only rows where both first_write_turn and total_turns are non-NULL and
total_turns > 0 are considered.
"""
from __future__ import annotations

from typing import Any


def _db_path() -> str:
    """Return the DuckDB stats path — see backend/state_paths.py."""
    from backend import state_paths  # noqa: PLC0415
    return str(state_paths.STATS_DB)


def pre_write_burn_rows(limit: int = 20) -> list[dict[str, Any]]:
    """Return up to *limit* executor runs where pre-Write ratio > 10%.

    Returns rows sorted by ratio DESC so the worst offenders appear first.
    Returns an empty list when the table is empty or no rows exceed the threshold.
    """
    import duckdb  # local import — not available in all test environments

    db = _db_path()
    try:
        conn = duckdb.connect(db, read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT
                    agent_id,
                    role,
                    discussion,
                    pr,
                    first_write_turn,
                    total_turns,
                    ROUND(first_write_turn * 1.0 / total_turns * 100, 1) AS ratio_pct,
                    input_tok,
                    event_id
                FROM agent_run
                WHERE role = 'executor'
                  AND first_write_turn IS NOT NULL
                  AND total_turns IS NOT NULL
                  AND total_turns > 0
                  AND (first_write_turn * 1.0 / total_turns) > 0.10
                ORDER BY ratio_pct DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
            cols = [
                "agent_id", "role", "discussion", "pr",
                "first_write_turn", "total_turns", "ratio_pct",
                "input_tok", "event_id",
            ]
            return [dict(zip(cols, row)) for row in rows]
        finally:
            conn.close()
    except Exception:
        return []


def handle(params: dict) -> dict:
    """Return pre-Write burn rows.

    Response: {
        "rows": [
            {
                "agent_id": str,
                "role": "executor",
                "discussion": int | null,
                "pr": int | null,
                "first_write_turn": int,
                "total_turns": int,
                "ratio_pct": float,   -- e.g. 15.0 means 15%
                "input_tok": int | null,
                "event_id": str | null
            },
            ...
        ]
    }
    """
    limit = int(params.get("limit", 20))
    return {"rows": pre_write_burn_rows(limit=limit)}
