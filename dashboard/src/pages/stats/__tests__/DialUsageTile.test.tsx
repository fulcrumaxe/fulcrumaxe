/**
 * Component tests for DialUsageTile.
 *
 * Focuses on threshold-band classification:
 *   levelColor: ratio = level/ceiling
 *     >= 0.8 → green (#22c55e)
 *     >= 0.5 → amber (#f59e0b)
 *     <  0.5 → red   (#ef4444)
 *
 * Also tests: loading/error states, 24h activity chip display,
 * empty-activity message, TTL formatting, and last-ceiling-exceeded banner.
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import type { DialUsageResponse, DialClass } from '../DialUsageTile'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import DialUsageTile from '../DialUsageTile'

const mockJsonRpc = vi.mocked(jsonRpc)

// Minimal 24h block with no activity
const noActivity: DialUsageResponse['last_24h'] = {
  accepted: 0,
  rejected_by_reason: {
    ceiling_violation: 0,
    unauthenticated_source: 0,
    invalid_level: 0,
  },
  ceiling_violations: 0,
  last_ceiling_exceeded: null,
}

// Build a minimal DialUsageResponse with a single dial class
function makeResponse(
  level: number,
  ceiling: number,
  overrides: Partial<DialClass> = {},
  last24hOverrides: Partial<DialUsageResponse['last_24h']> = {},
): DialUsageResponse {
  return {
    current_dials: [
      {
        name: 'agent.spawn',
        level,
        ceiling,
        verb_label: 'spawn agents',
        active_directives: 0,
        ttl_revert_at: null,
        ...overrides,
      },
    ],
    last_24h: { ...noActivity, ...last24hOverrides },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

// -------------------------------------------------------------------
// Loading / error states
// -------------------------------------------------------------------

describe('DialUsageTile — loading/error states', () => {
  it('shows loading message while data is pending', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {})) // never resolves
    render(<DialUsageTile />)
    expect(screen.getByText('Loading dial state…')).toBeInTheDocument()
  })

  it('shows error message when RPC rejects', async () => {
    mockJsonRpc.mockRejectedValue(new Error('backend unavailable'))
    render(<DialUsageTile />)
    await screen.findByText('backend unavailable')
  })

  it('renders the tile container once data loads', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5))
    render(<DialUsageTile />)
    // dial-usage-tile now lives on the data-gated wrapper (D#1896), so this
    // testid resolving is itself proof data has loaded. Still wait on
    // data-dependent text first so the assertion doesn't depend on timing
    // assumptions about which resolves before the other.
    await screen.findByText('agent.spawn')
    expect(screen.getByTestId('dial-usage-tile')).toBeInTheDocument()
  })
})

// -------------------------------------------------------------------
// levelColor threshold bands (via rendered dot color)
//
// The colored dot is a <span> with background = levelColor(level, ceiling).
// It appears inside the table row as the level indicator.
// -------------------------------------------------------------------

describe('DialUsageTile — levelColor threshold bands', () => {
  async function getDotAndLevelColor(level: number, ceiling: number) {
    mockJsonRpc.mockResolvedValue(makeResponse(level, ceiling))
    render(<DialUsageTile />)
    // The dot span has border-radius: 50% inside the table row.
    // Wait directly for the row itself instead of the always-present
    // container testid — the container renders before data (and the
    // table) exist, so that testid resolving proves nothing about rows.
    const rows = await screen.findAllByRole('row')
    // rows[0] = thead, rows[1] = first data row
    const dot = rows[1].querySelector('span[style*="border-radius"]') as HTMLElement
    // The level text span immediately follows the dot
    const levelSpan = dot.nextElementSibling as HTMLElement
    return { dotBackground: dot.style.background, levelColor: levelSpan.style.color }
  }

  it('green at ratio 1.0 (level=5, ceiling=5)', async () => {
    const { dotBackground, levelColor } = await getDotAndLevelColor(5, 5)
    expect(dotBackground).toBe('rgb(34, 197, 94)') // #22c55e
    expect(levelColor).toBe('rgb(34, 197, 94)')
  })

  it('green at ratio = exactly 0.8 (level=4, ceiling=5)', async () => {
    const { dotBackground } = await getDotAndLevelColor(4, 5)
    expect(dotBackground).toBe('rgb(34, 197, 94)')
  })

  it('amber at ratio just below 0.8 — level=3, ceiling=4 (ratio=0.75)', async () => {
    const { dotBackground } = await getDotAndLevelColor(3, 4)
    expect(dotBackground).toBe('rgb(245, 158, 11)') // #f59e0b
  })

  it('amber at ratio = exactly 0.5 (level=1, ceiling=2)', async () => {
    const { dotBackground } = await getDotAndLevelColor(1, 2)
    expect(dotBackground).toBe('rgb(245, 158, 11)')
  })

  it('red at ratio just below 0.5 — level=1, ceiling=3 (ratio=0.333)', async () => {
    const { dotBackground } = await getDotAndLevelColor(1, 3)
    expect(dotBackground).toBe('rgb(239, 68, 68)') // #ef4444
  })

  it('red at ratio 0 (level=0, ceiling=5)', async () => {
    const { dotBackground } = await getDotAndLevelColor(0, 5)
    expect(dotBackground).toBe('rgb(239, 68, 68)')
  })
})

// -------------------------------------------------------------------
// Level / ceiling text rendering
// -------------------------------------------------------------------

describe('DialUsageTile — level/ceiling text', () => {
  it('renders level and ceiling values in the table', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5))
    render(<DialUsageTile />)
    const rows = await screen.findAllByRole('row')
    const levelCell = rows[1].querySelectorAll('td')[2]
    expect(levelCell.textContent).toContain('5')
    expect(levelCell.textContent).toContain('/5')
  })

  it('renders class name and verb label', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(3, 5, { name: 'pr.merge', verb_label: 'merge PRs' }))
    render(<DialUsageTile />)
    await screen.findByText('pr.merge')
    expect(screen.getByText('merge PRs')).toBeInTheDocument()
  })
})

// -------------------------------------------------------------------
// 24h activity chips
// -------------------------------------------------------------------

describe('DialUsageTile — 24h activity display', () => {
  it('shows "No directive activity in 24h" when all counters are zero', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5))
    render(<DialUsageTile />)
    await screen.findByTestId('dial-usage-empty-activity')
    expect(screen.getByTestId('dial-usage-empty-activity')).toBeInTheDocument()
  })

  it('shows Accepted chip when accepted > 0', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5, {}, { accepted: 7 }))
    render(<DialUsageTile />)
    await screen.findByText(/Accepted/)
    expect(screen.queryByTestId('dial-usage-empty-activity')).toBeNull()
    expect(screen.getByText('7')).toBeInTheDocument()
  })

  it('shows Ceiling violations chip when ceiling_violation > 0', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse(5, 5, {}, {
        accepted: 0,
        ceiling_violations: 2,
        rejected_by_reason: { ceiling_violation: 2, unauthenticated_source: 0, invalid_level: 0 },
      }),
    )
    render(<DialUsageTile />)
    await screen.findByText(/Ceiling violations/)
  })

  it('shows Unauthenticated chip when unauthenticated_source > 0', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse(5, 5, {}, {
        accepted: 0,
        rejected_by_reason: { ceiling_violation: 0, unauthenticated_source: 3, invalid_level: 0 },
      }),
    )
    render(<DialUsageTile />)
    await screen.findByText(/Unauthenticated/)
  })

  it('shows Invalid level chip when invalid_level > 0', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse(5, 5, {}, {
        accepted: 0,
        rejected_by_reason: { ceiling_violation: 0, unauthenticated_source: 0, invalid_level: 1 },
      }),
    )
    render(<DialUsageTile />)
    await screen.findByText(/Invalid level/)
  })

  it('shows last_ceiling_exceeded banner when present', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse(5, 5, {}, {
        accepted: 1,
        last_ceiling_exceeded: { class: 'pr.merge', timestamp: '2026-05-20T10:00:00Z' },
      }),
    )
    render(<DialUsageTile />)
    await screen.findByText(/Last ceiling violation/)
    expect(screen.getByText('pr.merge')).toBeInTheDocument()
  })

  it('does not show ceiling exceeded banner when null', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5, {}, { accepted: 1 }))
    render(<DialUsageTile />)
    // accepted: 1 guarantees the Accepted chip renders — use it as the
    // wait target so the negative assertion below runs after data has
    // actually loaded, not just after the always-present container.
    await screen.findByText(/Accepted/)
    expect(screen.queryByText(/Last ceiling violation/)).toBeNull()
  })
})

// -------------------------------------------------------------------
// TTL formatting (formatTtl function behaviour)
// -------------------------------------------------------------------

describe('DialUsageTile — TTL revert display', () => {
  it('shows "—" when ttl_revert_at is null', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5, { ttl_revert_at: null }))
    render(<DialUsageTile />)
    await screen.findByText('—')
  })

  it('shows "expiring" when TTL timestamp is in the past', async () => {
    const pastTs = new Date(Date.now() - 1000).toISOString()
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5, { ttl_revert_at: pastTs }))
    render(<DialUsageTile />)
    await screen.findByText('expiring')
  })

  it('shows "in Xh Ym" when TTL is within 24 hours', async () => {
    // 3 hours in the future — use a whole-hour value to avoid sub-minute drift
    const futureTs = new Date(Date.now() + 3 * 3_600_000).toISOString()
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5, { ttl_revert_at: futureTs }))
    render(<DialUsageTile />)
    // Match the pattern — exact minutes can drift by 1 due to test execution time
    await screen.findByText(/^in \d+h \d+m$/)
  })

  it('shows locale date string when TTL >= 24h away', async () => {
    // 2 days in the future
    const futureTs = new Date(Date.now() + 48 * 3_600_000).toISOString()
    const expectedDate = new Date(futureTs).toLocaleDateString()
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5, { ttl_revert_at: futureTs }))
    render(<DialUsageTile />)
    await screen.findByText(expectedDate)
  })
})

// -------------------------------------------------------------------
// Active directives display
// -------------------------------------------------------------------

describe('DialUsageTile — active directives', () => {
  it('renders active_directives count in the table', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse(5, 5, { active_directives: 3 }))
    render(<DialUsageTile />)
    const rows = await screen.findAllByRole('row')
    const directivesCell = rows[1].querySelectorAll('td')[3]
    expect(directivesCell.textContent).toBe('3')
  })
})

// -------------------------------------------------------------------
// Polling (fake timers — separate describe so they don't bleed)
// -------------------------------------------------------------------

describe('DialUsageTile — polling', () => {
  it('polls every 60s', async () => {
    vi.useFakeTimers()
    try {
      mockJsonRpc.mockResolvedValue(makeResponse(5, 5))
      const { unmount } = render(<DialUsageTile />)
      // Let the initial fetch complete
      await act(async () => { await Promise.resolve() })
      expect(mockJsonRpc).toHaveBeenCalledTimes(1)
      // Advance timer by 60s to trigger the interval
      await act(async () => { vi.advanceTimersByTime(60_000) })
      await act(async () => { await Promise.resolve() })
      expect(mockJsonRpc).toHaveBeenCalledTimes(2)
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})
