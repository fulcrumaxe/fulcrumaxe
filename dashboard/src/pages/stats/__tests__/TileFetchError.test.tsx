/**
 * Tests for TileFetchError — isTransportError() classification and the
 * per-tile "fetch failed" rendering it enables.
 *
 * D#2251: a CORS-rejected preflight makes fetch() reject with a raw
 * TypeError, which five tiles previously swallowed into their empty/zero
 * state. These tests lock in:
 *  - isTransportError() tells a transport failure apart from an ApiError
 *  - each of the five in-scope tiles renders <TileFetchError> (not its
 *    empty-state copy) on a transport failure
 *  - each tile still renders its normal empty state on an empty/zero
 *    payload — a transport failure and "no data yet" stay visually and
 *    textually distinct
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import { isTransportError } from '../TileFetchError'
import { ApiError } from '../../../api/client'

vi.mock('../../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../../api/client')>('../../../api/client')
  return {
    ...actual,
    jsonRpc: vi.fn(),
  }
})

import { jsonRpc } from '../../../api/client'
import AvgFixRoundsTile from '../AvgFixRoundsTile'
import TeamLeadTokensTile from '../TeamLeadTokensTile'
import LoopIdleRatioTile from '../LoopIdleRatioTile'
import CostSpikesTile from '../CostSpikesTile'
import CosmeticBlocksTile from '../CosmeticBlocksTile'

const mockJsonRpc = vi.mocked(jsonRpc)

beforeEach(() => {
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// isTransportError()
// ---------------------------------------------------------------------------

describe('isTransportError', () => {
  it('is false for an ApiError', () => {
    expect(isTransportError(new ApiError(500, 'boom'))).toBe(false)
  })

  it('is true for a raw TypeError (the shape fetch() rejects with)', () => {
    expect(isTransportError(new TypeError('Failed to fetch'))).toBe(true)
  })

  it('is true for an error whose message matches a known transport phrase', () => {
    expect(isTransportError(new Error('NetworkError when attempting to fetch resource'))).toBe(true)
    expect(isTransportError(new Error('Load failed'))).toBe(true)
  })

  it('is false for an unrelated Error', () => {
    expect(isTransportError(new Error('method not found: stats.foo'))).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Per-tile: transport failure renders TileFetchError, not the empty state
// ---------------------------------------------------------------------------

const EMPTY_STATE_COPY = [
  'No merge data yet. Written on each PR merge by post-merge-hook.',
  'No iterations recorded yet. Written by /loop step 7.5.',
]

describe('AvgFixRoundsTile — transport failure vs. empty state', () => {
  it('renders tile-fetch-error on a transport failure, not the empty-state copy', async () => {
    mockJsonRpc.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<AvgFixRoundsTile />)
    await screen.findByTestId('tile-fetch-error')
    for (const copy of EMPTY_STATE_COPY) {
      expect(screen.queryByText(copy)).not.toBeInTheDocument()
    }
    expect(screen.queryByText(/no data yet/i)).not.toBeInTheDocument()
  })

  it('renders the empty state (not tile-fetch-error) on a zero-sample payload', async () => {
    mockJsonRpc.mockResolvedValue({ avg_last_24h: null, sample_size: 0, distribution: {} })
    render(<AvgFixRoundsTile />)
    await screen.findByTestId('fix-rounds-empty')
    expect(screen.queryByTestId('tile-fetch-error')).not.toBeInTheDocument()
  })
})

describe('TeamLeadTokensTile — transport failure vs. empty state', () => {
  it('renders tile-fetch-error on a transport failure, not the empty-state copy', async () => {
    mockJsonRpc.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<TeamLeadTokensTile />)
    await screen.findByTestId('tile-fetch-error')
    for (const copy of EMPTY_STATE_COPY) {
      expect(screen.queryByText(copy)).not.toBeInTheDocument()
    }
    expect(screen.queryByText(/no data yet/i)).not.toBeInTheDocument()
  })

  it('renders the empty state (not tile-fetch-error) on a zero-sample payload', async () => {
    mockJsonRpc.mockResolvedValue({ avg: null, p50: null, p95: null, sample_size: 0 })
    render(<TeamLeadTokensTile />)
    await screen.findByTestId('tl-tokens-empty')
    expect(screen.queryByTestId('tile-fetch-error')).not.toBeInTheDocument()
  })
})

describe('LoopIdleRatioTile — transport failure vs. empty state', () => {
  it('renders tile-fetch-error on a transport failure, not the empty-state copy', async () => {
    mockJsonRpc.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<LoopIdleRatioTile />)
    await screen.findByTestId('tile-fetch-error')
    expect(screen.queryByText(/no data yet/i)).not.toBeInTheDocument()
    expect(screen.queryByTestId('idle-ratio-na')).not.toBeInTheDocument()
  })

  it('renders the empty state (not tile-fetch-error) on a zero-sample payload', async () => {
    mockJsonRpc.mockResolvedValue({ ratio: null, idle_count: 0, sample_size: 0 })
    render(<LoopIdleRatioTile />)
    await screen.findByTestId('idle-ratio-na')
    expect(screen.queryByTestId('tile-fetch-error')).not.toBeInTheDocument()
  })
})

describe('CostSpikesTile — transport failure vs. empty state', () => {
  it('renders tile-fetch-error on a transport failure, not the zero-spikes card', async () => {
    mockJsonRpc.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<CostSpikesTile />)
    await screen.findByTestId('tile-fetch-error')
    expect(screen.queryByTestId('cost-spikes-tile')).not.toBeInTheDocument()
    expect(screen.queryByText(/no data yet/i)).not.toBeInTheDocument()
  })

  it('renders the zero-spikes card (not tile-fetch-error) on an empty payload', async () => {
    mockJsonRpc.mockResolvedValue({ spikes: [], count: 0, last_spike_iso: null })
    render(<CostSpikesTile />)
    await screen.findByTestId('cost-spikes-tile')
    expect(screen.queryByTestId('tile-fetch-error')).not.toBeInTheDocument()
  })
})

describe('CosmeticBlocksTile — transport failure vs. empty state', () => {
  it('renders tile-fetch-error on a transport failure, not the zero-blocks tile', async () => {
    mockJsonRpc.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<CosmeticBlocksTile />)
    await screen.findByTestId('tile-fetch-error')
    expect(screen.queryByTestId('cosmetic-blocks-tile')).not.toBeInTheDocument()
    expect(screen.queryByText(/no data yet/i)).not.toBeInTheDocument()
  })

  it('renders the zero-blocks tile (not tile-fetch-error) on an empty payload', async () => {
    mockJsonRpc.mockResolvedValue({ total_24h: 0, hourly_7d: [] })
    render(<CosmeticBlocksTile />)
    await screen.findByTestId('cosmetic-blocks-tile')
    expect(screen.queryByTestId('tile-fetch-error')).not.toBeInTheDocument()
  })
})
