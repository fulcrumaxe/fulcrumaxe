/**
 * Component tests for SdkLaneTile.
 *
 * Covers:
 *   - Loading / error states
 *   - "Dispatcher off" state (the current expected reality)
 *   - Dispatcher live state
 *   - Backend selection display (subscription / apikey / none)
 *   - Credential presence badges (never show actual values)
 *   - Routing counts rendering
 *   - Credit display (regime, remaining, error fallback)
 *   - 60s poll behaviour (fake timers)
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import type { SdkLaneResponse } from '../SdkLaneTile'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import SdkLaneTile from '../SdkLaneTile'

const mockJsonRpc = vi.mocked(jsonRpc)

// -------------------------------------------------------------------
// Fixtures
// -------------------------------------------------------------------

function makeResponse(overrides: Partial<SdkLaneResponse> = {}): SdkLaneResponse {
  return {
    generated_at: '2026-05-20T12:00:00Z',
    readiness: {
      dispatcher_live: false,
      ROUTE_VIA_DISPATCHER: '(not set)',
      SHADOW_MODE: 'alternate',
      SDK_BACKEND: '(not set)',
    },
    backend_selection: {
      would_select: 'none',
      reason: 'no SDK credential — routes to Claude Code path',
      CLAUDE_CODE_OAUTH_TOKEN: 'absent',
      ANTHROPIC_API_KEY: 'absent',
    },
    credit: {
      remaining_usd: null,
      used_usd: null,
      soft_cap_breached: null,
      exhausted: null,
      billing_regime: null,
      regime_note: null,
      error: 'credit_tracker unavailable',
    },
    routing_counts: {
      total_runs_all_time: 0,
      total_runs_last_30d: 0,
      sdk_runs: 0,
      cc_runs: 0,
      null_route_runs: 0,
      sdk_runs_estimate: '0 SDK runs (no credit consumed; dispatcher likely off)',
      db_available: false,
      note: 'stats.duckdb not found — no telemetry yet',
    },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

// -------------------------------------------------------------------
// Loading / error states
// -------------------------------------------------------------------

describe('SdkLaneTile — loading/error states', () => {
  it('shows loading message while data is pending', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {})) // never resolves
    render(<SdkLaneTile />)
    expect(screen.getByText('Loading SDK status…')).toBeInTheDocument()
  })

  it('shows error message when RPC rejects', async () => {
    mockJsonRpc.mockRejectedValue(new Error('backend unavailable'))
    render(<SdkLaneTile />)
    await screen.findByText('backend unavailable')
  })

  it('renders tile container once data loads', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-tile')
  })
})

// -------------------------------------------------------------------
// Dispatcher off state (current expected reality)
// -------------------------------------------------------------------

describe('SdkLaneTile — dispatcher off state', () => {
  it('shows "Dispatcher off — 0 SDK runs" banner', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-dispatcher-off')
    expect(screen.getByTestId('sdk-lane-dispatcher-off').textContent).toContain('Dispatcher off')
    expect(screen.getByTestId('sdk-lane-dispatcher-off').textContent).toContain('0 SDK run')
  })

  it('shows singular "run" when sdk_runs is 1', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({ routing_counts: { ...makeResponse().routing_counts, sdk_runs: 1 } }),
    )
    render(<SdkLaneTile />)
    const banner = await screen.findByTestId('sdk-lane-dispatcher-off')
    expect(banner.textContent).toContain('1 SDK run')
    // Should not say "runs" (plural)
    expect(banner.textContent).not.toContain('1 SDK runs')
  })

  it('does not show dispatcher-off banner when dispatcher is live', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        readiness: {
          dispatcher_live: true,
          ROUTE_VIA_DISPATCHER: '1',
          SHADOW_MODE: 'alternate',
          SDK_BACKEND: '(not set)',
        },
      }),
    )
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-tile')
    expect(screen.queryByTestId('sdk-lane-dispatcher-off')).toBeNull()
  })
})

// -------------------------------------------------------------------
// Backend selection
// -------------------------------------------------------------------

describe('SdkLaneTile — backend selection', () => {
  it('shows "none" when no credentials', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<SdkLaneTile />)
    const el = await screen.findByTestId('sdk-lane-would-select')
    expect(el.textContent).toBe('none')
  })

  it('shows "subscription" backend', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        backend_selection: {
          would_select: 'subscription',
          reason: 'CLAUDE_CODE_OAUTH_TOKEN present',
          CLAUDE_CODE_OAUTH_TOKEN: 'present',
          ANTHROPIC_API_KEY: 'absent',
        },
      }),
    )
    render(<SdkLaneTile />)
    const el = await screen.findByTestId('sdk-lane-would-select')
    expect(el.textContent).toBe('subscription')
  })

  it('shows "apikey" backend', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        backend_selection: {
          would_select: 'apikey',
          reason: 'ANTHROPIC_API_KEY present',
          CLAUDE_CODE_OAUTH_TOKEN: 'absent',
          ANTHROPIC_API_KEY: 'present',
        },
      }),
    )
    render(<SdkLaneTile />)
    const el = await screen.findByTestId('sdk-lane-would-select')
    expect(el.textContent).toBe('apikey')
  })

  it('shows credential presence badges — never raw token values', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        backend_selection: {
          would_select: 'subscription',
          reason: 'CLAUDE_CODE_OAUTH_TOKEN present',
          CLAUDE_CODE_OAUTH_TOKEN: 'present',
          ANTHROPIC_API_KEY: 'absent',
        },
      }),
    )
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-tile')
    // Should show the string "present" and "absent" — never a token value
    const badges = screen.getAllByText('present')
    expect(badges.length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('absent')).toBeInTheDocument()
    // Sanity: no "sk-ant-" style prefix should ever appear
    expect(screen.queryByText(/sk-ant-/)).toBeNull()
  })
})

// -------------------------------------------------------------------
// Routing counts
// -------------------------------------------------------------------

describe('SdkLaneTile — routing counts', () => {
  it('renders sdk_runs count', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        routing_counts: {
          ...makeResponse().routing_counts,
          sdk_runs: 7,
          cc_runs: 42,
          total_runs_all_time: 49,
        },
      }),
    )
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-tile')
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
    expect(screen.getByText('49')).toBeInTheDocument()
  })

  it('shows routing note', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-routing-note')
    expect(screen.getByTestId('sdk-lane-routing-note').textContent).toContain('stats.duckdb not found')
  })
})

// -------------------------------------------------------------------
// Credit display
// -------------------------------------------------------------------

describe('SdkLaneTile — credit', () => {
  it('shows credit error message gracefully', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-tile')
    expect(screen.getByText('credit_tracker unavailable')).toBeInTheDocument()
  })

  it('shows remaining_usd when available', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        credit: {
          remaining_usd: 195.50,
          used_usd: 4.50,
          soft_cap_breached: false,
          exhausted: false,
          billing_regime: 'subscription',
          regime_note: 'subscription-covered',
        },
      }),
    )
    render(<SdkLaneTile />)
    await screen.findByTestId('sdk-lane-tile')
    expect(screen.getByText('$195.50')).toBeInTheDocument()
    expect(screen.getByText('subscription')).toBeInTheDocument()
  })
})

// -------------------------------------------------------------------
// Polling
// -------------------------------------------------------------------

describe('SdkLaneTile — polling', () => {
  it('polls every 60s', async () => {
    vi.useFakeTimers()
    try {
      mockJsonRpc.mockResolvedValue(makeResponse())
      const { unmount } = render(<SdkLaneTile />)
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
})
