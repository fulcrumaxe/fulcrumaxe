"""RPC handler: stats.parity_trend

Return per-role token-delta trend across recent parity-experiment runs.

Reads .autonomous-team/parity-history.jsonl (written by write_parity_history
in parity_experiment.py) and returns the last N run records with per-role
input/output token deltas and verdict match rate.

Graceful when the file is absent or empty — returns an empty runs list.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend import state_paths as _state_paths

_DEFAULT_LIMIT = 20

# ---------------------------------------------------------------------------
# PARITY_HISTORY — resolved at call time (D#1810), with a compatibility shim
# ---------------------------------------------------------------------------
# Same reasoning as backend/orchestrator/parity_experiment.py: __getattr__
# makes external access resolve fresh, `_attr()` lets this module's own
# reference honor a direct patch/assignment from a test.


def __getattr__(name: str):
    if name == "PARITY_HISTORY":
        return _state_paths.PARITY_HISTORY
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _attr(name: str):
    if name in globals():
        return globals()[name]
    return __getattr__(name)


def handle(params: dict) -> dict:
    """Return per-role parity-experiment trend data.

    Params
    ------
    limit : int, optional
        Max number of recent runs to return (default: 20).

    Response
    --------
    {
        "runs": [
            {
                "ts": "2026-05-20T12:00:00+00:00",
                "overall": { ... ParityReport fields ... },
                "per_role": [
                    {
                        "role": "executor",
                        "sdk_verdict": "done",
                        "cc_verdict": "done",
                        "verdict_agree": true,
                        "token_input_delta": 50,
                        "token_output_delta": 10,
                        "output_similarity": 0.95,
                    },
                    ...
                ],
            },
            ...
        ],
        "total_runs": int,
        "history_path": str,
    }
    """
    limit = int(params.get("limit", _DEFAULT_LIMIT))
    history_path: Path = _attr("PARITY_HISTORY")

    if not history_path.exists():
        return {
            "runs": [],
            "total_runs": 0,
            "history_path": str(history_path),
        }

    records: list[dict[str, Any]] = []
    try:
        raw = history_path.read_text(encoding="utf-8")
    except OSError:
        return {
            "runs": [],
            "total_runs": 0,
            "history_path": str(history_path),
        }

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            # Skip malformed lines — never crash the RPC
            continue

    total = len(records)
    # Return the most recent N runs (tail of the append-only log)
    recent = records[-limit:] if limit > 0 else records

    return {
        "runs": recent,
        "total_runs": total,
        "history_path": str(history_path),
    }
