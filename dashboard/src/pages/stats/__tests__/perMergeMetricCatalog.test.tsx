/**
 * Unit tests for perMergeMetricCatalog.
 *
 * Covers D#2526's acceptance criteria:
 *   - the denominator is the catalog size, not a hardcoded literal (item 2/3)
 *   - mutation check: the denominator moves when the catalog shrinks (item 4)
 *   - a numerator above the denominator is impossible by construction —
 *     "17 of 12" cannot render (item 5)
 *   - the missing-metric list names exactly the absent catalog entries (item 3)
 */
import { describe, it, expect } from 'vitest'
import {
  PER_MERGE_METRIC_CATALOG,
  computeMetricCoverage,
  formatMetricCoverageLabel,
} from '../perMergeMetricCatalog'

describe('perMergeMetricCatalog', () => {
  it('denominator is the catalog size, not a hardcoded literal', () => {
    // Measured on host nixos, 2026-09-11, against stats.summary: 17 distinct
    // metric names — not 12. A literal 12 would fail this assertion.
    expect(PER_MERGE_METRIC_CATALOG.length).toBe(17)
    const metrics = PER_MERGE_METRIC_CATALOG.map(name => ({ name }))
    expect(formatMetricCoverageLabel(metrics)).toBe(
      'Per-merge metrics — 17 of 17 populated'
    )
  })

  it('mutation check: the denominator moves when the catalog shrinks', () => {
    const shrunkenCatalog = ['a', 'b', 'c']
    const metrics = [{ name: 'a' }, { name: 'b' }]
    expect(formatMetricCoverageLabel(metrics, shrunkenCatalog)).toBe(
      'Per-merge metrics — 2 of 3 populated'
    )
  })

  it('populated never exceeds expected, even with extra unlisted metrics — "17 of 12" cannot render', () => {
    const smallCatalog = ['a', 'b']
    // Simulates the historical bug shape: the API returns MORE distinct
    // metric names than a small, stale catalog.
    const metrics = [
      { name: 'a' },
      { name: 'b' },
      { name: 'c' },
      { name: 'd' },
      { name: 'e' },
    ]
    const { populated, expected } = computeMetricCoverage(metrics, smallCatalog)
    expect(populated).toBeLessThanOrEqual(expected)
    expect(formatMetricCoverageLabel(metrics, smallCatalog)).toBe(
      'Per-merge metrics — 2 of 2 populated'
    )
  })

  it('reports which catalog entries are missing', () => {
    const catalog = ['a', 'b', 'c']
    const metrics = [{ name: 'a' }]
    const { missing, populated, expected } = computeMetricCoverage(metrics, catalog)
    expect(missing).toEqual(['b', 'c'])
    expect(populated).toBe(1)
    expect(expected).toBe(3)
  })

  it('handles an empty metrics response without dividing by zero or crashing', () => {
    const { populated, expected, missing } = computeMetricCoverage([], ['a', 'b'])
    expect(populated).toBe(0)
    expect(expected).toBe(2)
    expect(missing).toEqual(['a', 'b'])
  })
})
