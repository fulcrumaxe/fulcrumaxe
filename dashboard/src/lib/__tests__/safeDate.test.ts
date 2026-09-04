import { describe, it, expect } from 'vitest'
import {
  formatRelative,
  formatAbsolute,
  formatDate,
  formatTime,
  formatLocaleString,
} from '../safeDate'

// Six required AC-7 cases: valid ISO, valid epoch ms, null, undefined, NaN, malformed string
describe('safeDate — six required AC cases', () => {
  const VALID_ISO = '2026-05-12T08:00:00Z'
  const VALID_EPOCH_MS = new Date(VALID_ISO).getTime() // number

  it('valid ISO string — returns defined non-empty string, never "Invalid Date"', () => {
    const r = formatAbsolute(VALID_ISO)
    expect(r).toBeTruthy()
    expect(r).not.toBe('Invalid Date')
    expect(r).not.toBe('—')
  })

  it('valid epoch ms — returns defined non-empty string, never "Invalid Date"', () => {
    const r = formatAbsolute(VALID_EPOCH_MS)
    expect(r).toBeTruthy()
    expect(r).not.toBe('Invalid Date')
    expect(r).not.toBe('—')
  })

  it('null — returns "—"', () => {
    expect(formatAbsolute(null)).toBe('—')
    expect(formatDate(null)).toBe('—')
    expect(formatTime(null)).toBe('—')
    expect(formatLocaleString(null)).toBe('—')
    expect(formatRelative(null)).toBe('—')
  })

  it('undefined — returns "—"', () => {
    expect(formatAbsolute(undefined)).toBe('—')
    expect(formatDate(undefined)).toBe('—')
    expect(formatTime(undefined)).toBe('—')
    expect(formatLocaleString(undefined)).toBe('—')
    expect(formatRelative(undefined)).toBe('—')
  })

  it('NaN — returns "—"', () => {
    expect(formatAbsolute(NaN)).toBe('—')
    expect(formatDate(NaN)).toBe('—')
    expect(formatTime(NaN)).toBe('—')
    expect(formatLocaleString(NaN)).toBe('—')
    expect(formatRelative(NaN)).toBe('—')
  })

  it('malformed string — returns "—"', () => {
    expect(formatAbsolute('not-a-date')).toBe('—')
    expect(formatDate('not-a-date')).toBe('—')
    expect(formatTime('not-a-date')).toBe('—')
    expect(formatLocaleString('not-a-date')).toBe('—')
    expect(formatRelative('not-a-date')).toBe('—')
  })

  it('empty string — returns "—"', () => {
    expect(formatAbsolute('')).toBe('—')
    expect(formatRelative('')).toBe('—')
  })
})

describe('formatRelative', () => {
  it('seconds ago for timestamps < 60s old', () => {
    const now = Date.now()
    const r = formatRelative(now - 30_000)
    expect(r).toMatch(/^\d+s ago$/)
  })

  it('minutes ago for 2-minute-old timestamps', () => {
    const r = formatRelative(Date.now() - 2 * 60_000)
    expect(r).toMatch(/^\d+m ago$/)
  })

  it('hours ago for 3-hour-old timestamps', () => {
    const r = formatRelative(Date.now() - 3 * 3600_000)
    expect(r).toMatch(/^\d+h ago$/)
  })

  it('days ago for 2-day-old timestamps', () => {
    const r = formatRelative(Date.now() - 2 * 86400_000)
    expect(r).toMatch(/^\d+d ago$/)
  })

  it('falls back to absolute format for timestamps older than 7 days', () => {
    const r = formatRelative(Date.now() - 10 * 86400_000)
    // Should not say "Xd ago" for 10 days
    expect(r).not.toMatch(/^\d+d ago$/)
    expect(r).not.toBe('—')
    expect(r).not.toBe('Invalid Date')
  })

  it('accepts a Date object', () => {
    const r = formatRelative(new Date(Date.now() - 5 * 60_000))
    expect(r).toMatch(/^\d+m ago$/)
  })
})

describe('formatTime', () => {
  it('accepts options', () => {
    const r = formatTime('2026-05-12T08:30:45Z', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
    expect(r).toBeTruthy()
    expect(r).not.toBe('Invalid Date')
    expect(r).not.toBe('—')
  })
})

describe('formatDate', () => {
  it('returns a non-empty string for a valid ISO date', () => {
    const r = formatDate('2026-05-12T08:00:00Z')
    expect(r).toBeTruthy()
    expect(r).not.toBe('Invalid Date')
  })
})
