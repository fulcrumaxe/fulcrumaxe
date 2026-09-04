"""backend/stats/anomaly_config.py — per-metric anomaly thresholds.

Defaults follow the Discussion #1087 spec:
  - rate metrics    : flag at 10x swing
  - cost metrics    : flag at 5x swing  (tighter — cost spikes are expensive)
  - duration metrics: flag at 20x swing (looser — durations vary more naturally)
  - fallback        : 10x

Add an entry here when a specific metric needs a non-default threshold.
Values are *ratios*: 10 means "flag when current/prev > 10 or prev/current > 10".
"""

from __future__ import annotations

# Default threshold applied to any metric not listed below.
DEFAULT_THRESHOLD: float = 10.0

# Per-metric overrides.  metric_name → ratio threshold.
METRIC_THRESHOLDS: dict[str, float] = {
    # ── rate metrics (10x) ──────────────────────────────────────────────
    "orphan_worktree_rate": 10.0,
    "wasted_tokens_ratio": 10.0,
    "impersonation_rate": 10.0,
    "fail_rate": 10.0,
    # ── cost metrics (5x) ───────────────────────────────────────────────
    "total_cost_usd": 5.0,
    "budget_used_usd": 5.0,
    "tokens_per_iter": 5.0,
    "team_lead_tokens_per_iter": 5.0,
    # ── duration metrics (20x) ──────────────────────────────────────────
    "duration_s": 20.0,
    "loop_duration_s": 20.0,
    "agent_duration_s": 20.0,
    # ── count metrics (10x) ─────────────────────────────────────────────
    "agents_spawned": 10.0,
    "prs_merged": 10.0,
    "hard_rule_violation_count": 10.0,
}


def threshold_for(metric_name: str) -> float:
    """Return the configured ratio threshold for *metric_name*.

    Falls back to DEFAULT_THRESHOLD for any unlisted metric.
    """
    return METRIC_THRESHOLDS.get(metric_name, DEFAULT_THRESHOLD)
