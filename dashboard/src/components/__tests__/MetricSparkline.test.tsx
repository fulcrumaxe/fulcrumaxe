import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import MetricSparkline from '../MetricSparkline'
import { formatUnit } from '../../lib/formatMetric'

// ----------------------------------------------------------------------------
// formatUnit — unit tests (pure function, no render needed)
// ----------------------------------------------------------------------------

describe('formatUnit', () => {
  it('returns "minutes" for a seconds metric whose value is >= 60', () => {
    expect(formatUnit(204, 'seconds')).toBe('minutes')
  })

  it('returns "hours" for a seconds metric whose value is >= 3600', () => {
    expect(formatUnit(7200, 'seconds')).toBe('hours')
  })

  it('returns "seconds" for a seconds metric whose value is < 60', () => {
    expect(formatUnit(45, 'seconds')).toBe('seconds')
  })

  it('returns "seconds" for a zero value seconds metric', () => {
    expect(formatUnit(0, 'seconds')).toBe('seconds')
  })

  it('returns the raw unit unchanged for non-seconds units', () => {
    expect(formatUnit(0.85, 'ratio')).toBe('ratio')
    expect(formatUnit(1.23, 'usd')).toBe('usd')
    expect(formatUnit(42, 'count')).toBe('count')
  })

  it('returns "" when value is null (no meaningful unit for a missing value)', () => {
    expect(formatUnit(null, 'seconds')).toBe('')
  })

  it('returns "" when value is negative (sentinel for missing data)', () => {
    expect(formatUnit(-1, 'seconds')).toBe('')
  })

  it('returns "minutes" for value=300 seconds (5 minutes)', () => {
    expect(formatUnit(300, 'seconds')).toBe('minutes')
  })
})

// ----------------------------------------------------------------------------
// MetricSparkline — integration: rendered unit label matches humanised value
// ----------------------------------------------------------------------------

describe('MetricSparkline unit label', () => {
  it('shows "minutes" not "seconds" when value=204 seconds', () => {
    render(
      <MetricSparkline
        label="time_to_merge_seconds"
        value={204}
        unit="seconds"
        series={[]}
        updatedAt={null}
      />
    )
    // The humanised value should be "3.4m" and the unit label "minutes"
    expect(screen.getByText('3.4m')).toBeInTheDocument()
    expect(screen.getByText('minutes')).toBeInTheDocument()
    expect(screen.queryByText('seconds')).not.toBeInTheDocument()
  })

  it('shows "hours" not "seconds" when value=7200 seconds', () => {
    render(
      <MetricSparkline
        label="loop_iteration_duration_seconds"
        value={7200}
        unit="seconds"
        series={[]}
        updatedAt={null}
      />
    )
    expect(screen.getByText('2.0h')).toBeInTheDocument()
    expect(screen.getByText('hours')).toBeInTheDocument()
    expect(screen.queryByText('seconds')).not.toBeInTheDocument()
  })

  it('keeps "seconds" label when value is < 60', () => {
    render(
      <MetricSparkline
        label="fast_metric"
        value={45}
        unit="seconds"
        series={[]}
        updatedAt={null}
      />
    )
    expect(screen.getByText('45s')).toBeInTheDocument()
    expect(screen.getByText('seconds')).toBeInTheDocument()
  })

  it('leaves non-seconds units unchanged', () => {
    render(
      <MetricSparkline
        label="cost_per_merged_pr_usd"
        value={0.123}
        unit="usd"
        series={[]}
        updatedAt={null}
      />
    )
    expect(screen.getByText('usd')).toBeInTheDocument()
  })

  it('shows "5.0m minutes" not "5.0m seconds" when value=300 unit="seconds"', () => {
    render(
      <MetricSparkline
        label="time_to_merge_seconds"
        value={300}
        unit="seconds"
        series={[]}
        updatedAt={null}
      />
    )
    expect(screen.getByText('5.0m')).toBeInTheDocument()
    expect(screen.getByText('minutes')).toBeInTheDocument()
    expect(screen.queryByText('seconds')).not.toBeInTheDocument()
  })

  it('shows no unit label when value is null for a seconds metric', () => {
    render(
      <MetricSparkline
        label="spec_to_first_pr_latency_seconds"
        value={null}
        unit="seconds"
        series={[]}
        updatedAt={null}
      />
    )
    // value is null → formatValue returns "—", formatUnit returns ""
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.queryByText('seconds')).not.toBeInTheDocument()
  })

  it('renders unit=ratio with a large value (mis-tagged count) as a plain integer, not a percentage', () => {
    // AC3 regression: orphan_worktree_rate arriving via series() path may still carry
    // unit='ratio' with a raw count value like 18000. The formatter must not produce
    // "1800000.0%" — it must fall back to a plain integer.
    render(
      <MetricSparkline
        label="orphan_worktree_rate"
        value={18000}
        unit="ratio"
        series={[]}
        updatedAt={null}
      />
    )
    expect(screen.getByText('18000')).toBeInTheDocument()
    expect(screen.queryByText(/1800000/)).not.toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('renders a genuine ratio value (0–1) as a percentage', () => {
    // AC2: normal ratio metrics must still render as NN.N%
    render(
      <MetricSparkline
        label="acceptance_criteria_pass_rate"
        value={0.873}
        unit="ratio"
        series={[]}
        updatedAt={null}
      />
    )
    expect(screen.getByText('87.3%')).toBeInTheDocument()
    expect(screen.queryByText(/0\.873/)).not.toBeInTheDocument()
  })

  it('renders unit=count as a plain integer, not a percentage', () => {
    // Regression for D#1090: orphan_worktree_rate was displaying as "2160000.0%"
    // because stale DB rows had unit='ratio'. Once the API returns unit='count',
    // MetricSparkline must show a plain integer, never a percentage.
    render(
      <MetricSparkline
        label="orphan_worktree_rate"
        value={18000}
        unit="count"
        series={[]}
        updatedAt={null}
      />
    )
    expect(screen.getByText('18000')).toBeInTheDocument()
    expect(screen.getByText('count')).toBeInTheDocument()
    expect(screen.queryByText(/1800000/)).not.toBeInTheDocument()
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })
})
