"""backend/stats/metric_order.py — single source of truth for KPI display order.

This list is the canonical ordering for metric display across all surfaces.

The dashboard web UI (dashboard/src/pages/StatsPage.tsx) duplicates this list
in its METRIC_ORDER const. If you add or reorder metrics here, update
StatsPage.tsx to match (and vice versa). A comment in StatsPage.tsx points
back here.
"""

from __future__ import annotations

# Preferred display order for known metrics.
# Metrics not in this list are appended alphabetically after the ordered set.
METRIC_ORDER: list[str] = [
    "loop_iteration_duration_seconds",
    "time_to_merge_seconds",
    "fix_cycle_count",
    "spec_to_first_pr_latency_seconds",
    "reviewer_acceptance_latency_seconds",
    # "acceptance_criteria_pass_rate" intentionally removed (D#2476): its
    # writer was retired, not repaired. Left out of the explicit order so it
    # doesn't claim a permanent display slot; its 336 historical rows still
    # surface, just sorted alphabetically after this list like any other
    # unordered metric.
    "cost_per_merged_pr_usd",
    "cost_attribution_unresolved_count",
    "pr_file_conflict_score",
    "scan_to_spawn_ratio",
    "orphan_worktree_rate",
    "interventions_per_agent_avg",
    "interventions_per_classifier",
    "intervention_to_self_correction_rate",
]


def sort_metrics(metrics: list[dict]) -> list[dict]:
    """Sort a list of metric dicts by METRIC_ORDER, appending unknowns alphabetically.

    Each dict must have a 'name' key.
    """
    by_name = {m["name"]: m for m in metrics}
    ordered: list[dict] = []
    seen: set[str] = set()

    for name in METRIC_ORDER:
        if name in by_name:
            ordered.append(by_name[name])
            seen.add(name)

    # Append any metric not in the preferred list, sorted alphabetically
    for name in sorted(by_name):
        if name not in seen:
            ordered.append(by_name[name])

    return ordered


# ---------------------------------------------------------------------------
# Retired metrics (D#2539)
# ---------------------------------------------------------------------------
#
# A metric with no active writer isn't automatically "retired" — it might
# just be new and not wired up yet, or paused. "Retired" is a stronger,
# explicit claim: someone deliberately stopped producing it and said so in a
# PR. That claim needs a name, a reason and a date attached to it, which a
# bare boolean (see backend/stats/freshness.is_monitored) can't carry.
#
# This is the single source of truth for that claim. The dashboard mirrors
# it in dashboard/src/pages/stats/retiredMetrics.ts (parity note there points
# back here) — same pattern as METRIC_ORDER above.
RETIRED_METRICS: dict[str, dict[str, str]] = {
    "acceptance_criteria_pass_rate": {
        "retired_by_pr": "134",
        "retired_date": "2026-09-10",
        "reason": (
            "its scorer counted a criterion as passed whenever any word "
            "longer than three characters from it appeared anywhere in the "
            "PR body or comments — near-guaranteed to match, since the PR "
            "body is written by the same person working from that same "
            "criterion text. It never measured anything."
        ),
    },
}


def is_retired(name: str) -> bool:
    """True when *name* has an explicit retirement record in RETIRED_METRICS."""
    return name in RETIRED_METRICS
