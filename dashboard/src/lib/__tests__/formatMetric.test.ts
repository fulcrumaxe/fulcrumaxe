import { describe, it, expect } from 'vitest'
import { formatUnit } from '../formatMetric'

describe('formatUnit — non-seconds units', () => {
  it('returns the unit unchanged for arbitrary units', () => {
    expect(formatUnit(42, 'ms')).toBe('ms')
    expect(formatUnit(100, 'tokens')).toBe('tokens')
    expect(formatUnit(0, 'requests')).toBe('requests')
  })

  it('returns empty string for empty string unit', () => {
    expect(formatUnit(42, '')).toBe('')
  })

  it('returns unit unchanged for null value with non-seconds unit', () => {
    expect(formatUnit(null, 'ms')).toBe('ms')
    expect(formatUnit(null, 'tokens')).toBe('tokens')
    expect(formatUnit(null, 'dollars')).toBe('dollars')
  })
})

describe('formatUnit — seconds unit', () => {
  it('returns "" when value is null', () => {
    expect(formatUnit(null, 'seconds')).toBe('')
  })

  it('returns "" when value is negative', () => {
    expect(formatUnit(-1, 'seconds')).toBe('')
    expect(formatUnit(-3600, 'seconds')).toBe('')
  })

  it('returns "seconds" for 0 seconds', () => {
    expect(formatUnit(0, 'seconds')).toBe('seconds')
  })

  it('returns "seconds" for values 1–59', () => {
    expect(formatUnit(1, 'seconds')).toBe('seconds')
    expect(formatUnit(30, 'seconds')).toBe('seconds')
    expect(formatUnit(59, 'seconds')).toBe('seconds')
  })

  it('returns "minutes" for values 60–3599', () => {
    expect(formatUnit(60, 'seconds')).toBe('minutes')
    expect(formatUnit(90, 'seconds')).toBe('minutes')
    expect(formatUnit(3599, 'seconds')).toBe('minutes')
  })

  it('returns "hours" for values >= 3600', () => {
    expect(formatUnit(3600, 'seconds')).toBe('hours')
    expect(formatUnit(7200, 'seconds')).toBe('hours')
    expect(formatUnit(86400, 'seconds')).toBe('hours')
  })

  it('boundary: exactly 60 seconds → "minutes"', () => {
    expect(formatUnit(60, 'seconds')).toBe('minutes')
  })

  it('boundary: exactly 3600 seconds → "hours"', () => {
    expect(formatUnit(3600, 'seconds')).toBe('hours')
  })

  it('handles large numbers of seconds', () => {
    expect(formatUnit(1_000_000, 'seconds')).toBe('hours')
  })
})
