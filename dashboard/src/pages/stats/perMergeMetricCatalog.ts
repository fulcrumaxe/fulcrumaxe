/**
 * perMergeMetricCatalog — the named, maintained metric set the "Per-merge
 * metrics" header on StatsPage measures coverage against.
 *
 * The header used to divide by a hardcoded literal (`12`) that never
 * tracked the actual metric set — it read "17 of 12 populated" the same day
 * it read "0 of 12" (D#2526). This module replaces that literal with a
 * named list a reader can see, plus the derivation that turns it into a
 * coverage count and a missing-metric list.
 *
 * Maintenance: `stats.summary` carries no time window (backend/stats_reader.py
 * `summary()` returns the latest row ever written per metric name, forever),
 * so this list isn't auto-derivable from the API response without the same
 * "N of N" degeneracy the old code had — it needs the same manual upkeep as
 * StatsPage.tsx's METRIC_ORDER. Measured against `stats.summary` on host
 * `nixos`, 2026-09-11 (17 distinct metric names). Add or remove a name here
 * when the backend starts or stops writing it.
 */

export const PER_MERGE_METRIC_CATALOG: readonly string[] = [
  'acceptance_criteria_pass_rate',
  'bootstrap_ping',
  'cost_attribution_unresolved_count',
  'cost_per_merged_pr_usd',
  'fix_cycle_count',
  'fix_rounds_per_pr',
  'hard_rule_violation_count',
  'impersonation_rate',
  'loop_iteration_duration_seconds',
  'orphan_worktree_rate',
  'pr_file_conflict_score',
  'reviewer_acceptance_latency_seconds',
  'role_verdict',
  'scan_to_spawn_ratio',
  'spec_to_first_pr_latency_seconds',
  'time_to_merge_seconds',
  'wasted_tokens_ratio',
]

interface MetricLike {
  name: string
}

export interface MetricCoverage {
  /** Count of catalog entries present in `metrics`. Never exceeds `expected`. */
  populated: number
  /** Catalog size — the denominator. */
  expected: number
  /** Catalog entries with no matching entry in `metrics`, sorted. */
  missing: string[]
}

/**
 * Compute catalog coverage against an actual `stats.summary` response.
 *
 * `populated` counts only catalog members present in `metrics` — a metric
 * name outside the catalog can never inflate the numerator past the
 * denominator, which is what made "17 of 12" renderable before this change.
 */
export function computeMetricCoverage(
  metrics: readonly MetricLike[],
  catalog: readonly string[] = PER_MERGE_METRIC_CATALOG
): MetricCoverage {
  const present = new Set(metrics.map(m => m.name))
  const missing = catalog.filter(name => !present.has(name))
  return {
    populated: catalog.length - missing.length,
    expected: catalog.length,
    missing,
  }
}

/** Render the "Per-merge metrics — N of M populated" header string. */
export function formatMetricCoverageLabel(
  metrics: readonly MetricLike[],
  catalog: readonly string[] = PER_MERGE_METRIC_CATALOG
): string {
  const { populated, expected } = computeMetricCoverage(metrics, catalog)
  return `Per-merge metrics — ${populated} of ${expected} populated`
}
