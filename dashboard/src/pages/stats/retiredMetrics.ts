/**
 * retiredMetrics.ts — retired-metric registry and sentinel-aware value
 * formatting for the /stats page (D#2539).
 *
 * Two independent things live here because they share one root cause: the
 * page was asserting states the data didn't support.
 *
 *  1. A metric can be *retired* — someone deliberately stopped producing it
 *     and said so in a PR. That's a named state (who, when, why), not the
 *     same thing as "no data" or "unmonitored". A retired metric must never
 *     drive the staleness banner, but must stay visible elsewhere on the
 *     page, labelled as retired.
 *
 *  2. Several writers use `-1` as a sentinel meaning "measured, but not
 *     applicable to this row" — a real, successfully-computed answer. That
 *     must render distinguishably from "no rows have ever been written for
 *     this metric".
 *
 * PARITY NOTE: RETIRED_METRICS mirrors backend/stats/metric_order.py's
 * RETIRED_METRICS dict (Python is the canonical source). Update both
 * together, same convention as METRIC_ORDER in StatsPage.tsx.
 */

export interface RetiredInfo {
  retiredByPr: string
  retiredDate: string
  reason: string
}

export const RETIRED_METRICS: Record<string, RetiredInfo> = {
  acceptance_criteria_pass_rate: {
    retiredByPr: '134',
    retiredDate: '2026-09-10',
    reason:
      'its scorer counted a criterion as passed whenever any word longer ' +
      'than three characters from it appeared anywhere in the PR body or ' +
      'comments — near-guaranteed to match, since the PR body is written ' +
      'by the same person working from that same criterion text. It never ' +
      'measured anything.',
  },
}

/** True when `name` has an explicit retirement record. */
export function isRetiredMetric(name: string): boolean {
  return name in RETIRED_METRICS
}

/** The retirement record for `name`, or undefined when it isn't retired. */
export function getRetiredInfo(name: string): RetiredInfo | undefined {
  return RETIRED_METRICS[name]
}

/**
 * Special-case display text for a metric value, distinguishing "no rows
 * ever written" from "measured, and a -1 sentinel says not applicable".
 *
 * Returns the special-case string for null/undefined/negative values, or
 * `null` when the caller should format the (real, non-sentinel) value
 * itself.
 */
export function formatSentinelAware(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return '—'
  if (value < 0) return 'not applicable'
  return null
}

/** Minimal shape of a stats.freshness_list row — matches StaleBanner's FreshnessRow. */
export interface FreshnessRowLike {
  metric_name: string
  age_seconds: number
  /** False for a one-shot metric with no live writer. */
  monitored?: boolean
}

/**
 * Whether a freshness row should drive the staleness banner.
 *
 * A retired metric never alarms, regardless of age or `monitored` — its
 * retirement record is the authoritative reason it stopped being written,
 * and that reason is not "something is wrong". An unmonitored (but not
 * retired) metric keeps the existing behaviour: no alarm. Everything else
 * alarms once its age crosses `warnAgeSeconds`.
 */
export function isStaleAndAlarming(row: FreshnessRowLike, warnAgeSeconds: number): boolean {
  if (isRetiredMetric(row.metric_name)) return false
  if (row.monitored === false) return false
  return row.age_seconds >= warnAgeSeconds
}
