import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import SdkVsCcTile from '../runs/SdkVsCcTile'
import { jsonRpc } from '../../api/client'

vi.mock('../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

const mockJsonRpc = vi.mocked(jsonRpc)

const SAMPLE_ROWS = [
  {
    role: 'executor',
    route: 'sdk',
    run_count: 42,
    median_input_tok: 15000,
    median_output_tok: 2000,
    pass_rate: 0.857,
  },
  {
    role: 'executor',
    route: 'cc',
    run_count: 8,
    median_input_tok: 12000,
    median_output_tok: 1800,
    pass_rate: 0.625,
  },
  {
    role: 'code-reviewer',
    route: 'sdk',
    run_count: 10,
    median_input_tok: 5000,
    median_output_tok: 800,
    pass_rate: 1.0,
  },
]

describe('SdkVsCcTile', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders loading state initially', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {}))
    render(<SdkVsCcTile />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('renders empty state when no SDK runs recorded', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [],
      has_routed_via: false,
      generated_at: '2026-05-20T00:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })
    expect(screen.getByText(/No SDK runs recorded/)).toBeInTheDocument()
  })

  it('renders empty state when rows is empty but column present', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [],
      has_routed_via: true,
      generated_at: '2026-05-20T00:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })
    expect(screen.getByText(/No SDK runs recorded/)).toBeInTheDocument()
  })

  it('renders per-role SDK vs CC table with correct data', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: SAMPLE_ROWS,
      has_routed_via: true,
      generated_at: '2026-05-20T00:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })

    // Check tile heading
    expect(screen.getByText('SDK vs CC — Per-Role Comparison')).toBeInTheDocument()

    // Role names
    expect(screen.getAllByText('executor').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('code-reviewer').length).toBeGreaterThanOrEqual(1)

    // Route badges
    const sdkBadges = screen.getAllByText('sdk')
    expect(sdkBadges.length).toBeGreaterThanOrEqual(2) // executor+sdk, code-reviewer+sdk
    const ccBadges = screen.getAllByText('cc')
    expect(ccBadges.length).toBeGreaterThanOrEqual(1)

    // Run counts
    expect(screen.getByText('42')).toBeInTheDocument()
    expect(screen.getByText('8')).toBeInTheDocument()
    expect(screen.getByText('10')).toBeInTheDocument()

    // Token formatting (15000 → 15.0k)
    expect(screen.getByText('15.0k')).toBeInTheDocument()
    expect(screen.getByText('12.0k')).toBeInTheDocument()

    // Pass rates (0.857 → 85.7%)
    expect(screen.getByText('85.7%')).toBeInTheDocument()
    expect(screen.getByText('62.5%')).toBeInTheDocument()
    expect(screen.getByText('100.0%')).toBeInTheDocument()
  })

  it('renders tile with data-testid for easy selection', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: SAMPLE_ROWS,
      has_routed_via: true,
      generated_at: '2026-05-20T00:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })
    expect(screen.getByTestId('sdk-vs-cc-tile')).toBeInTheDocument()
  })

  it('handles null pass_rate gracefully', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [
        {
          role: 'executor',
          route: 'sdk',
          run_count: 1,
          median_input_tok: null,
          median_output_tok: null,
          pass_rate: null,
        },
      ],
      has_routed_via: true,
      generated_at: '2026-05-20T00:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })
    // null values render as em-dash
    const dashes = screen.getAllByText('—')
    expect(dashes.length).toBeGreaterThanOrEqual(3) // input_tok, output_tok, pass_rate
  })

  it('calls stats.sdk_vs_cc RPC method', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [],
      has_routed_via: false,
      generated_at: '2026-05-20T00:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })
    expect(mockJsonRpc).toHaveBeenCalledWith('stats.sdk_vs_cc', {})
  })
})
