/**
 * Smoke tests for stats tile components.
 * Each test verifies the tile renders its empty/loading state
 * without crashing, using a mocked jsonRpc.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import CostSpikesTile from '../CostSpikesTile'
import TeamLeadTokensTile from '../TeamLeadTokensTile'
import RoleSuccessRateTile from '../RoleSuccessRateTile'
import LoopIdleRatioTile from '../LoopIdleRatioTile'
import AvgFixRoundsTile from '../AvgFixRoundsTile'
import WeeklyVelocityTile from '../WeeklyVelocityTile'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
const mockJsonRpc = vi.mocked(jsonRpc)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('CostSpikesTile', () => {
  it('renders loading state initially (promise pending)', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {}))
    render(<CostSpikesTile />)
    expect(screen.getByText('Loading spike data…')).toBeInTheDocument()
  })

  it('shows zero-spikes state when count is 0', async () => {
    mockJsonRpc.mockResolvedValue({ spikes: [], count: 0, last_spike_iso: null })
    render(<CostSpikesTile />)
    await screen.findByTestId('cost-spikes-tile')
    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.getByText('spikes detected')).toBeInTheDocument()
  })

  it('shows spike count when spikes present', async () => {
    mockJsonRpc.mockResolvedValue({
      spikes: [{ ts_iso: '2026-05-12T10:00:00Z', value: 0.05, mu: 0.01, sigma: 0.01 }],
      count: 1,
      last_spike_iso: '2026-05-12T10:00:00Z',
    })
    render(<CostSpikesTile />)
    await screen.findByTestId('cost-spikes-tile')
    expect(screen.getByText('1')).toBeInTheDocument()
  })
})

describe('TeamLeadTokensTile', () => {
  it('shows empty state when no data', async () => {
    mockJsonRpc.mockResolvedValue({ avg: null, p50: null, p95: null, sample_size: 0 })
    render(<TeamLeadTokensTile />)
    await screen.findByTestId('tl-tokens-empty')
    expect(screen.getByTestId('tl-tokens-empty')).toBeInTheDocument()
  })

  it('shows table when sample data exists', async () => {
    mockJsonRpc.mockResolvedValue({ avg: 15000, p50: 14000, p95: 22000, sample_size: 10 })
    render(<TeamLeadTokensTile />)
    await screen.findByTestId('tl-tokens-table')
    expect(screen.getByText('Average')).toBeInTheDocument()
    expect(screen.getByText('Median (p50)')).toBeInTheDocument()
    expect(screen.getByText('p95')).toBeInTheDocument()
  })
})

describe('RoleSuccessRateTile', () => {
  it('shows empty state when no data', async () => {
    mockJsonRpc.mockResolvedValue({ rows: [] })
    render(<RoleSuccessRateTile />)
    await screen.findByText('No verdict data yet. Emitted by post-agent-hook after each agent run.')
  })

  it('shows role table when data present', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [{ role: 'executor', success_rate: 0.95, sample_size: 20 }],
    })
    render(<RoleSuccessRateTile />)
    await screen.findByTestId('role-success-rate-table')
    expect(screen.getByText('executor')).toBeInTheDocument()
    expect(screen.getByText('95.0%')).toBeInTheDocument()
  })
})

describe('LoopIdleRatioTile', () => {
  it('shows N/A when insufficient data', async () => {
    mockJsonRpc.mockResolvedValue({ ratio: 0.1, idle_count: 1, sample_size: 3 })
    render(<LoopIdleRatioTile />)
    await screen.findByTestId('idle-ratio-na')
    expect(screen.getByTestId('idle-ratio-na')).toBeInTheDocument()
  })

  it('shows idle ratio when enough data', async () => {
    mockJsonRpc.mockResolvedValue({ ratio: 0.2, idle_count: 5, sample_size: 25 })
    render(<LoopIdleRatioTile />)
    await screen.findByTestId('idle-ratio-tile')
    expect(screen.getByTestId('idle-ratio-value')).toBeInTheDocument()
    expect(screen.getByText('20.0%')).toBeInTheDocument()
  })
})

describe('WeeklyVelocityTile', () => {
  it('shows empty-state when applicable is false', async () => {
    mockJsonRpc.mockResolvedValue({
      applicable: false,
      total: 0,
      by_day: [],
      window_start: '2026-05-04T00:00:00Z',
      window_end: '2026-05-18T00:00:00Z',
      prev_total: 0,
      trend_pct: 0,
    })
    render(<WeeklyVelocityTile />)
    await screen.findByTestId('weekly-velocity-empty')
    expect(screen.getByText('No PRs in last 14 days')).toBeInTheDocument()
  })

  it('shows headline count when applicable is true and total > 0', async () => {
    mockJsonRpc.mockResolvedValue({
      applicable: true,
      total: 5,
      by_day: [
        { date: '2026-05-12', count: 1 },
        { date: '2026-05-13', count: 2 },
        { date: '2026-05-14', count: 0 },
        { date: '2026-05-15', count: 1 },
        { date: '2026-05-16', count: 0 },
        { date: '2026-05-17', count: 1 },
        { date: '2026-05-18', count: 0 },
      ],
      window_start: '2026-05-12T00:00:00Z',
      window_end: '2026-05-18T00:00:00Z',
      prev_total: 3,
      trend_pct: 67,
    })
    render(<WeeklyVelocityTile />)
    await screen.findByTestId('weekly-velocity-tile')
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.queryByTestId('weekly-velocity-empty')).not.toBeInTheDocument()
  })
})

describe('AvgFixRoundsTile', () => {
  it('shows empty state when no data', async () => {
    mockJsonRpc.mockResolvedValue({ avg_last_24h: null, sample_size: 0, distribution: {} })
    render(<AvgFixRoundsTile />)
    await screen.findByTestId('fix-rounds-empty')
    expect(screen.getByTestId('fix-rounds-empty')).toBeInTheDocument()
  })

  it('shows N/A card when sample_size < 5', async () => {
    mockJsonRpc.mockResolvedValue({
      avg_last_24h: 1.5,
      sample_size: 3,
      distribution: { '1': 2, '2': 1 },
    })
    render(<AvgFixRoundsTile />)
    await screen.findByTestId('fix-rounds-card')
    expect(screen.getByTestId('fix-rounds-avg')).toHaveTextContent('N/A')
    expect(screen.getByTestId('fix-rounds-sample')).toHaveTextContent('need ≥5 for avg')
  })

  it('shows avg when sample_size >= 5', async () => {
    mockJsonRpc.mockResolvedValue({
      avg_last_24h: 1.25,
      sample_size: 8,
      distribution: { '1': 6, '2': 2 },
    })
    render(<AvgFixRoundsTile />)
    await screen.findByTestId('fix-rounds-card')
    expect(screen.getByTestId('fix-rounds-avg')).toHaveTextContent('1.25')
    expect(screen.getByTestId('fix-rounds-distribution')).toBeInTheDocument()
  })
})
