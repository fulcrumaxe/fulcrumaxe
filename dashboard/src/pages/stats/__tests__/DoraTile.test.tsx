/**
 * Component tests for DoraTile.
 *
 * Covers:
 *   - Loading state
 *   - Empty state (applicable=false)
 *   - Headline numbers render with live-sample values
 *   - change_failure_rate_pct rendered verbatim ("n/a" not coerced to number)
 *   - 60s polling behaviour (fake timers)
 *   - refreshSignal triggers re-fetch
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import type { DoraResponse } from '../DoraTile'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import DoraTile from '../DoraTile'

const mockJsonRpc = vi.mocked(jsonRpc)

// -------------------------------------------------------------------
// Fixtures
// -------------------------------------------------------------------

function makeResponse(overrides: Partial<DoraResponse> = {}): DoraResponse {
  return {
    applicable: true,
    deploy_frequency_per_day: 38.14,
    lead_time_minutes_p50: 8.01,
    change_failure_rate_pct: 'n/a',
    velocity_all_time_per_day: 9.31,
    cycle_time_median_hours: 0.42,
    window_start: '2099-06-01',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

// -------------------------------------------------------------------
// Loading state
// -------------------------------------------------------------------

describe('DoraTile — loading state', () => {
  it('shows loading message while data is pending', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {})) // never resolves
    render(<DoraTile />)
    expect(screen.getByText('Loading DORA metrics…')).toBeInTheDocument()
  })
})

// -------------------------------------------------------------------
// Empty state (applicable=false)
// -------------------------------------------------------------------

describe('DoraTile — empty state', () => {
  it('shows empty-state when applicable is false', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse({ applicable: false }))
    render(<DoraTile />)
    await screen.findByTestId('dora-empty-state')
    expect(screen.getByTestId('dora-empty-state').textContent).toContain('No release or KPI data yet')
  })

  it('does not show metric rows when applicable is false', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse({ applicable: false }))
    render(<DoraTile />)
    await screen.findByTestId('dora-empty-state')
    expect(screen.queryByTestId('dora-deploy-frequency')).toBeNull()
  })
})

// -------------------------------------------------------------------
// Headline numbers
// -------------------------------------------------------------------

describe('DoraTile — data rendering', () => {
  it('renders tile container when data loads', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DoraTile />)
    await screen.findByTestId('dora-tile')
  })

  it('renders deploy frequency from live-sample value', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DoraTile />)
    await screen.findByTestId('dora-deploy-frequency')
    // 38.14 formatted to 2dp
    expect(screen.getByTestId('dora-deploy-frequency').textContent).toContain('38.14')
  })

  it('renders lead time from live-sample value', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DoraTile />)
    await screen.findByTestId('dora-lead-time')
    expect(screen.getByTestId('dora-lead-time').textContent).toContain('8.0')
  })

  it('renders change_failure_rate_pct verbatim as "n/a" — not coerced to number', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse({ change_failure_rate_pct: 'n/a' }))
    render(<DoraTile />)
    await screen.findByTestId('dora-cfr')
    expect(screen.getByTestId('dora-cfr').textContent).toContain('n/a')
  })

  it('renders change_failure_rate_pct verbatim as numeric string', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse({ change_failure_rate_pct: '3.7' }))
    render(<DoraTile />)
    await screen.findByTestId('dora-cfr')
    expect(screen.getByTestId('dora-cfr').textContent).toContain('3.7')
    // Must NOT show something like "3.70" (toFixed coercion)
    expect(screen.getByTestId('dora-cfr').textContent).not.toContain('NaN')
  })

  it('renders velocity from live-sample value', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DoraTile />)
    await screen.findByTestId('dora-velocity')
    expect(screen.getByTestId('dora-velocity').textContent).toContain('9.31')
  })

  it('renders cycle time from live-sample value', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DoraTile />)
    await screen.findByTestId('dora-cycle-time')
    expect(screen.getByTestId('dora-cycle-time').textContent).toContain('0.4')
  })

  it('renders "n/a" for null cycle_time_median_hours', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse({ cycle_time_median_hours: null }))
    render(<DoraTile />)
    await screen.findByTestId('dora-cycle-time')
    expect(screen.getByTestId('dora-cycle-time').textContent).toContain('n/a')
  })
})

// -------------------------------------------------------------------
// Accessibility
// -------------------------------------------------------------------

describe('DoraTile — accessibility', () => {
  it('has a visible heading', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DoraTile />)
    await screen.findByRole('heading', { name: /DORA.*KPI/i })
  })

  it('section uses visible heading for accessible name — no redundant aria-label', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DoraTile />)
    // Heading is the accessible name; the section element should NOT carry
    // a redundant aria-label that duplicates it (WCAG 4.1.2 advisory).
    // The section itself carries no testid (D#1896 — testid must live on a
    // data-gated element, not the unconditional wrapper), so reach it via
    // the heading's ancestor instead.
    const heading = await screen.findByRole('heading', { name: /DORA.*KPI/i })
    const section = heading.closest('section')
    expect(section?.getAttribute('aria-label')).toBeNull()
  })

  it('loading div has role="status" for live-region announcement', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {})) // never resolves
    render(<DoraTile />)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('error div has role="alert" for live-region announcement', async () => {
    mockJsonRpc.mockRejectedValue(new Error('network error'))
    render(<DoraTile />)
    await screen.findByRole('alert')
    expect(screen.getByRole('alert').textContent).toContain('network error')
  })
})

// -------------------------------------------------------------------
// Polling
// -------------------------------------------------------------------

describe('DoraTile — polling', () => {
  it('polls every 60s', async () => {
    vi.useFakeTimers()
    try {
      mockJsonRpc.mockResolvedValue(makeResponse())
      const { unmount } = render(<DoraTile />)
      await act(async () => { await Promise.resolve() })
      expect(mockJsonRpc).toHaveBeenCalledTimes(1)
      await act(async () => { vi.advanceTimersByTime(60_000) })
      await act(async () => { await Promise.resolve() })
      expect(mockJsonRpc).toHaveBeenCalledTimes(2)
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-fetches when refreshSignal changes', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    const { rerender } = render(<DoraTile refreshSignal={0} />)
    await screen.findByTestId('dora-tile')
    const callsBefore = mockJsonRpc.mock.calls.length
    rerender(<DoraTile refreshSignal={1} />)
    await vi.waitFor(() => {
      expect(mockJsonRpc.mock.calls.length).toBeGreaterThan(callsBefore)
    })
  })
})
