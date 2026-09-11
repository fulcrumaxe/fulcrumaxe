/**
 * Unit tests for retiredMetrics (D#2539).
 *
 * Covers the acceptance criteria for the /stats display-layer fix:
 *   - item 2/3: an explicit retired list exists and is queryable
 *   - item 4: a -1 sentinel renders distinguishably from "no rows ever written"
 *   - item 6: mutation checks in both directions
 *   - item 7: the banner still fires on a genuinely stale, non-retired metric
 */
import { describe, it, expect } from 'vitest'
import {
  RETIRED_METRICS,
  isRetiredMetric,
  getRetiredInfo,
  formatSentinelAware,
  isStaleAndAlarming,
} from '../retiredMetrics'

describe('RETIRED_METRICS / isRetiredMetric / getRetiredInfo', () => {
  it('acceptance_criteria_pass_rate is registered as retired', () => {
    expect(isRetiredMetric('acceptance_criteria_pass_rate')).toBe(true)
    const info = getRetiredInfo('acceptance_criteria_pass_rate')
    expect(info).toBeDefined()
    expect(info?.retiredByPr).toBe('134')
    expect(info?.retiredDate).toBe('2026-09-10')
    expect(info?.reason.length).toBeGreaterThan(0)
  })

  it('a metric with no retirement record is not retired', () => {
    expect(isRetiredMetric('spec_to_first_pr_latency_seconds')).toBe(false)
    expect(getRetiredInfo('spec_to_first_pr_latency_seconds')).toBeUndefined()
  })
})

describe('formatSentinelAware', () => {
  it('renders the -1 sentinel distinguishably from absent (item 4)', () => {
    const absent = formatSentinelAware(null)
    const sentinel = formatSentinelAware(-1)
    expect(absent).toBe('—')
    expect(sentinel).toBe('not applicable')
    expect(absent).not.toBe(sentinel)
  })

  it('treats undefined the same as null (no rows ever written)', () => {
    expect(formatSentinelAware(undefined)).toBe('—')
  })

  it('returns null (pass-through) for a real, non-negative value', () => {
    expect(formatSentinelAware(0)).toBeNull()
    expect(formatSentinelAware(219)).toBeNull()
    expect(formatSentinelAware(36545)).toBeNull()
  })
})

describe('isStaleAndAlarming', () => {
  it('does not alarm on a retired metric even if it looks very stale (item 2)', () => {
    const row = { metric_name: 'acceptance_criteria_pass_rate', age_seconds: 999_999, monitored: true }
    expect(isStaleAndAlarming(row, 7200)).toBe(false)
  })

  it('mutation check: removing the retired entry makes the banner alarm again (item 6)', () => {
    const row = { metric_name: 'acceptance_criteria_pass_rate', age_seconds: 999_999, monitored: true }
    expect(isStaleAndAlarming(row, 7200)).toBe(false)

    const saved = RETIRED_METRICS[row.metric_name]
    delete RETIRED_METRICS[row.metric_name]
    try {
      expect(isStaleAndAlarming(row, 7200)).toBe(true)
    } finally {
      RETIRED_METRICS[row.metric_name] = saved
    }
    // Restored — still suppressed for every test that runs after this one.
    expect(isStaleAndAlarming(row, 7200)).toBe(false)
  })

  it('still alarms on a genuinely stale, non-retired, monitored metric (item 7)', () => {
    // orphan_worktree_rate — a live example named in the Discussion of a
    // metric that is neither retired nor sentinel-valued.
    const row = { metric_name: 'orphan_worktree_rate', age_seconds: 999_999, monitored: true }
    expect(isStaleAndAlarming(row, 7200)).toBe(true)
  })

  it('does not alarm on an unmonitored (but not retired) metric — unchanged prior behaviour', () => {
    const row = { metric_name: 'bootstrap_ping', age_seconds: 999_999, monitored: false }
    expect(isStaleAndAlarming(row, 7200)).toBe(false)
  })

  it('does not alarm when age is below the warn threshold', () => {
    const row = { metric_name: 'orphan_worktree_rate', age_seconds: 60, monitored: true }
    expect(isStaleAndAlarming(row, 7200)).toBe(false)
  })
})
