"""backend/stats/debater.py — D#841 debater precision tracker.

Computes a rolling 30-day precision metric for the debater pass and
optionally auto-disables the `gates.debater_pass` gate when precision
falls below `policies.debater.min_precision_30d`.

Definition (per D#841 spec):
    precision_30d = (# debater verdict=needs-fix that were substantive)
                    / (# debater verdict=needs-fix total)

A debater finding is considered "substantive" when the routed-back PR
results in a follow-up commit that changes behavior (i.e. the executor
did not just bounce it back unchanged). We approximate this by counting
the PR as substantive when the resulting merged commit's diff differs
from the diff at the debater verdict's HEAD SHA.

When the precision_30d falls below the configured floor, calling
`maybe_disable_gate()` flips `gates.debater_pass` to False and writes
an audit-log entry.

Data source: .autonomous-team/agent-feed.jsonl entries with role=debater.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_FEED = Path(
    os.environ.get(
        "AF_AGENT_FEED",
        str(_REPO_ROOT / ".autonomous-team" / "agent-feed.jsonl"),
    )
)


def _iter_debater_entries(feed: Path) -> list[dict[str, Any]]:
    if not feed.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with feed.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("role") != "debater":
                    continue
                out.append(row)
    except OSError:
        return []
    return out


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def precision_30d(
    feed: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return rolling-30d debater precision.

    Returns a dict:
        {
          "total_needs_fix": int,
          "substantive": int,
          "precision": float,   # 0.0 - 1.0, or None if total_needs_fix==0
          "window_days": 30,
        }
    """
    feed = feed or _DEFAULT_FEED
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    needs_fix = 0
    substantive = 0
    # Track which PR+sha pairs had a subsequent merge with a different SHA
    # (proxy for "executor pushed a real fix"). We rely on the post-merge
    # event being recorded in agent-feed (event_type=merge).
    debate_routes: list[tuple[int, str, datetime]] = []
    merge_shas: dict[int, set[str]] = defaultdict(set)

    for row in _iter_debater_entries(feed):
        ts = _parse_ts(row.get("ts", ""))
        if not ts or ts < cutoff:
            continue
        verdict = row.get("verdict")
        if verdict != "needs-fix":
            continue
        needs_fix += 1
        pr = row.get("pr")
        details = row.get("details") or {}
        sha = details.get("head_sha", "")
        if isinstance(pr, int) and sha:
            debate_routes.append((pr, sha, ts))

    # Pull merge events from feed for the same PRs (any post-debate merge with a different SHA
    # indicates the executor produced a real fix).
    if debate_routes:
        prs_of_interest = {p for p, _, _ in debate_routes}
        try:
            with (feed or _DEFAULT_FEED).open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line.strip() or "{}")
                    except json.JSONDecodeError:
                        continue
                    if row.get("event_type") != "merge":
                        continue
                    pr = row.get("pr")
                    if pr in prs_of_interest:
                        details = row.get("details") or {}
                        s = details.get("merged_sha") or details.get("head_sha", "")
                        if s:
                            merge_shas[pr].add(s)
        except OSError:
            pass

    for pr, sha, _ts in debate_routes:
        merged = merge_shas.get(pr, set())
        # Substantive iff merged with a different SHA after the debate.
        if merged and sha not in merged:
            substantive += 1

    precision = (substantive / needs_fix) if needs_fix else None
    return {
        "total_needs_fix": needs_fix,
        "substantive": substantive,
        "precision": precision,
        "window_days": 30,
    }


def maybe_disable_gate(
    feed: Path | None = None,
    floor: float | None = None,
) -> dict[str, Any]:
    """If precision_30d is below the floor, flip gates.debater_pass to False.

    Returns a dict describing the action taken:
        {
          "action": "disabled" | "kept_on" | "insufficient_data",
          "precision": float | None,
          "floor": float,
          "total_needs_fix": int,
        }
    """
    # Lazy import — keep this module dependency-light for tests.
    import sys

    sys.path.insert(0, str(_REPO_ROOT))
    from backend.control_plane import ControlPlane  # type: ignore

    cp = ControlPlane()
    cp.load()
    configured_floor = floor
    if configured_floor is None:
        configured_floor = cp.get_policy("debater").get("min_precision_30d", 0.30)

    stats = precision_30d(feed=feed)
    prec = stats["precision"]
    total = stats["total_needs_fix"]

    # Need a minimum sample to act — 5 needs-fix verdicts before we trust the rate.
    if total < 5:
        return {
            "action": "insufficient_data",
            "precision": prec,
            "floor": configured_floor,
            "total_needs_fix": total,
        }

    if prec is not None and prec < configured_floor:
        cp.set("gates.debater_pass", False)
        return {
            "action": "disabled",
            "precision": prec,
            "floor": configured_floor,
            "total_needs_fix": total,
        }

    return {
        "action": "kept_on",
        "precision": prec,
        "floor": configured_floor,
        "total_needs_fix": total,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Debater precision tracker (D#841)")
    parser.add_argument(
        "command",
        choices=["show", "auto-disable"],
        help="show: print current precision; auto-disable: flip gate if below floor",
    )
    args = parser.parse_args()

    if args.command == "show":
        print(json.dumps(precision_30d(), indent=2, default=str))
    elif args.command == "auto-disable":
        print(json.dumps(maybe_disable_gate(), indent=2, default=str))
