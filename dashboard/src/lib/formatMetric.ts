/**
 * formatMetric — pure helpers for metric value / unit display.
 *
 * Extracted from MetricSparkline so they can be tested in isolation and
 * exported without triggering the react-refresh/only-export-components lint rule.
 */

/**
 * Returns the display unit label that matches the humanised value produced by
 * formatValue().
 *
 * When a "seconds" metric is large enough that formatValue() emits "3.4m" or
 * "2.3h", showing the raw "seconds" unit label alongside it produces the
 * confusing "3.4m seconds" mismatch.  This function maps the raw unit to the
 * label that matches the actual suffix used in the formatted value.
 *
 * When value is null the formatted value is "—" or "n/a", so no unit label
 * is meaningful — returns "" to suppress the unit span entirely.
 */
export function formatUnit(value: number | null, unit: string): string {
  if (unit === 'seconds') {
    if (value === null) return ''
    if (value < 0) return ''
    if (value >= 3600) return 'hours'
    if (value >= 60) return 'minutes'
    return 'seconds'
  }
  return unit
}
