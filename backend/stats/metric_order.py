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
    "acceptance_criteria_pass_rate",
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
