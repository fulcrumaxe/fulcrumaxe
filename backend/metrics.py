"""Prometheus metrics exporter for fulcrumaxe.

Converts internal KPI and operational metrics to Prometheus text exposition
format (https://prometheus.io/docs/instrumenting/exposition_formats/).

Usage:
    from backend.metrics import generate_prometheus_metrics
    text = generate_prometheus_metrics()
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _gauge_lines(name: str, help_text: str, value: Any) -> list[str]:
    """Return HELP, TYPE, and value lines for a single gauge metric.

    Returns an empty list if *value* is None (metric omitted gracefully).
    """
    if value is None:
        return []
    return [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
        f"{name} {value}",
    ]


def generate_prometheus_metrics() -> str:
    """Build and return a Prometheus text exposition format string.

    Each subsystem is wrapped in try/except so a single failure does not
    prevent the remaining metrics from being exposed.
    """
    lines: list[str] = []

    # ------------------------------------------------------------------
    # Budget metrics
    # ------------------------------------------------------------------
    try:
        from backend.budget import BudgetTracker
        bt = BudgetTracker()
        status = bt.get_status()
        ceiling = status.get("ceiling")
        spent = status.get("spent")
        remaining = status.get("remaining")
        lines += _gauge_lines(
            "af_budget_ceiling_tokens",
            "Session token ceiling",
            ceiling,
        )
        lines += _gauge_lines(
            "af_budget_spent_tokens",
            "Tokens spent this session",
            spent,
        )
        lines += _gauge_lines(
            "af_budget_remaining_tokens",
            "Tokens remaining this session",
            remaining,
        )
    except Exception:  # noqa: BLE001
        logger.warning("metrics: budget subsystem unavailable — omitting budget metrics")

    # ------------------------------------------------------------------
    # Registry / discussion metrics
    # ------------------------------------------------------------------
    try:
        from backend.registry import DiscussionRegistry
        reg = DiscussionRegistry()
        stats = reg.stats()
        total = stats.get("total")
        done = stats.get("done")
        in_progress = stats.get("in_progress")
        lines += _gauge_lines(
            "af_discussions_total",
            "Total number of discussions",
            total,
        )
        lines += _gauge_lines(
            "af_discussions_done_total",
            "Number of completed discussions",
            done,
        )
        lines += _gauge_lines(
            "af_discussions_in_progress",
            "Number of discussions currently in progress",
            in_progress,
        )
    except Exception:  # noqa: BLE001
        logger.warning("metrics: registry subsystem unavailable — omitting discussion metrics")

    # ------------------------------------------------------------------
    # KPI metrics (velocity, cycle time, idle rate)
    # ------------------------------------------------------------------
    try:
        import backend.kpi_engine as kpi_engine
        kpi = kpi_engine.compute_all()

        velocity = kpi.get("velocity", {})
        lines += _gauge_lines(
            "af_velocity_tasks_per_day",
            "All-time tasks per day completion rate",
            velocity.get("all_time_per_day"),
        )
        lines += _gauge_lines(
            "af_velocity_last_24h",
            "Number of tasks completed in the last 24 hours",
            velocity.get("last_24h"),
        )

        pr_cycle = kpi.get("pr_cycle_time", {})
        lines += _gauge_lines(
            "af_pr_cycle_time_mean_hours",
            "Mean PR cycle time in hours",
            pr_cycle.get("mean_hours"),
        )
        lines += _gauge_lines(
            "af_pr_cycle_time_median_hours",
            "Median PR cycle time in hours",
            pr_cycle.get("median_hours"),
        )

        idle_rate = kpi.get("idle_rate", {})
        lines += _gauge_lines(
            "af_idle_rate_all_time_pct",
            "All-time loop idle rate as a percentage",
            idle_rate.get("all_time_pct"),
        )
    except Exception:  # noqa: BLE001
        logger.warning("metrics: KPI subsystem unavailable — omitting KPI metrics")

    # ------------------------------------------------------------------
    # Loop health metrics
    # ------------------------------------------------------------------
    try:
        from backend.health_monitor import check_loop_health
        health = check_loop_health()
        healthy_val = 1 if health.get("healthy") else 0
        age_minutes = health.get("age_minutes")
        lines += _gauge_lines(
            "af_loop_healthy",
            "1 if the loop has run recently, 0 if stale or unknown",
            healthy_val,
        )
        lines += _gauge_lines(
            "af_loop_age_minutes",
            "Minutes since the last loop run",
            age_minutes,
        )
    except Exception:  # noqa: BLE001
        logger.warning("metrics: health_monitor subsystem unavailable — omitting loop metrics")

    # ------------------------------------------------------------------
    # Cost metrics
    # ------------------------------------------------------------------
    try:
        from backend.cost_tracker import CostTracker
        ct = CostTracker()
        summary = ct.get_session_cost()
        total_cost = summary.get("total_cost_usd")
        lines += _gauge_lines(
            "af_cost_session_usd",
            "Total session cost in USD",
            total_cost,
        )
        model_breakdown = summary.get("model_breakdown", [])
        if model_breakdown:
            lines.append("# HELP af_cost_by_model_usd Session cost in USD broken down by model")
            lines.append("# TYPE af_cost_by_model_usd gauge")
            for entry in model_breakdown:
                model_label = entry["model"].replace('"', '\\"')
                lines.append(f'af_cost_by_model_usd{{model="{model_label}"}} {entry["cost_usd"]}')
    except Exception:  # noqa: BLE001
        logger.warning("metrics: cost_tracker subsystem unavailable — omitting cost metrics")

    # ------------------------------------------------------------------
    # Performance benchmark metrics (p50/p95/p99 per category)
    # ------------------------------------------------------------------
    try:
        from backend.benchmarks import get_recorder as _get_bench  # noqa: PLC0415
        rec = _get_bench()
        for category in ["http", "event_bus", "spawn", "db"]:
            stats = rec.compute_stats(category, window_seconds=300)
            if stats.count == 0:
                continue
            safe_cat = category.replace("_", "")
            lines += _gauge_lines(
                f"af_{safe_cat}_request_duration_ms_p50",
                f"p50 request duration in ms for category {category}",
                stats.p50_ms,
            )
            lines += _gauge_lines(
                f"af_{safe_cat}_request_duration_ms_p95",
                f"p95 request duration in ms for category {category}",
                stats.p95_ms,
            )
            lines += _gauge_lines(
                f"af_{safe_cat}_request_duration_ms_p99",
                f"p99 request duration in ms for category {category}",
                stats.p99_ms,
            )
            lines += _gauge_lines(
                f"af_{safe_cat}_requests_total",
                f"Total samples recorded for category {category} (rolling 5-min window)",
                stats.count,
            )
    except Exception:  # noqa: BLE001
        logger.warning("metrics: benchmarks subsystem unavailable — omitting benchmark metrics")

    # Prometheus exposition format requires a trailing newline.
    return "\n".join(lines) + "\n" if lines else "\n"
