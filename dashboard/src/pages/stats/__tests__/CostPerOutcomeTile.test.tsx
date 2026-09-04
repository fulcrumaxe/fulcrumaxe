/**
 * Component tests for CostPerOutcomeTile.
 *
 * Covers:
 *   - empty state (rows: [])
 *   - RPC rejection → empty state
 *   - populated rows → table with PR number, USD, top role
 *   - row with empty by_role → top role shows "—" (N/A)
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import CostPerOutcomeTile from '../CostPerOutcomeTile'

const mockJsonRpc = vi.mocked(jsonRpc)

beforeEach(() => {
  vi.clearAllMocks()
})

// -------------------------------------------------------------------
// Empty / error state
// -------------------------------------------------------------------

describe('CostPerOutcomeTile — empty state', () => {
  it('shows empty message when rows array is empty', async () => {
    mockJsonRpc.mockResolvedValueOnce({ rows: [] })
    render(<CostPerOutcomeTile />)
    await screen.findByTestId('cost-per-outcome-empty')
    expect(screen.queryByTestId('cost-per-outcome-table')).toBeNull()
  })

  it('shows empty message when RPC rejects', async () => {
    mockJsonRpc.mockRejectedValueOnce(new Error('network error'))
    render(<CostPerOutcomeTile />)
    await screen.findByTestId('cost-per-outcome-empty')
    expect(screen.queryByTestId('cost-per-outcome-table')).toBeNull()
  })
})

// -------------------------------------------------------------------
// Table renders with populated data
// -------------------------------------------------------------------

describe('CostPerOutcomeTile — populated state', () => {
  it('renders table with PR number, USD, and top role', async () => {
    mockJsonRpc.mockResolvedValueOnce({
      rows: [
        {
          pr: 42,
          usd: 1.23,
          total_tokens: 50000,
          fix_rounds: 2,
          by_role: [
            { role: 'executor', input_tokens: 30000, output_tokens: 20000, usd: 1.23 },
          ],
        },
      ],
    })
    render(<CostPerOutcomeTile />)
    await screen.findByTestId('cost-per-outcome-table')
    expect(screen.getByText('#42')).toBeInTheDocument()
    expect(screen.getByText('$1.23')).toBeInTheDocument()
    expect(screen.getByText('executor')).toBeInTheDocument()
  })

  it('renders multiple rows', async () => {
    mockJsonRpc.mockResolvedValueOnce({
      rows: [
        {
          pr: 100,
          usd: 2.50,
          total_tokens: 80000,
          fix_rounds: 3,
          by_role: [{ role: 'executor', input_tokens: 50000, output_tokens: 30000, usd: 2.50 }],
        },
        {
          pr: 99,
          usd: 0.75,
          total_tokens: 20000,
          fix_rounds: 0,
          by_role: [{ role: 'code-reviewer', input_tokens: 15000, output_tokens: 5000, usd: 0.75 }],
        },
      ],
    })
    render(<CostPerOutcomeTile />)
    await screen.findByTestId('cost-per-outcome-table')
    expect(screen.getByText('#100')).toBeInTheDocument()
    expect(screen.getByText('#99')).toBeInTheDocument()
    expect(screen.getByText('$2.50')).toBeInTheDocument()
    expect(screen.getByText('$0.75')).toBeInTheDocument()
  })

  it('slices to top 10 rows', async () => {
    const rows = Array.from({ length: 15 }, (_, i) => ({
      pr: i + 1,
      usd: 15 - i,
      total_tokens: 10000,
      fix_rounds: 0,
      by_role: [{ role: 'executor', input_tokens: 8000, output_tokens: 2000, usd: 15 - i }],
    }))
    mockJsonRpc.mockResolvedValueOnce({ rows })
    render(<CostPerOutcomeTile />)
    await screen.findByTestId('cost-per-outcome-table')
    // Only top 10 rows rendered — PR #11 through #15 not visible
    expect(screen.queryByText('#11')).toBeNull()
  })
})

// -------------------------------------------------------------------
// N/A state — empty by_role
// -------------------------------------------------------------------

describe('CostPerOutcomeTile — N/A top role', () => {
  it('shows "—" when by_role is empty', async () => {
    mockJsonRpc.mockResolvedValueOnce({
      rows: [
        {
          pr: 55,
          usd: 0.00,
          total_tokens: 0,
          fix_rounds: 0,
          by_role: [],
        },
      ],
    })
    render(<CostPerOutcomeTile />)
    await screen.findByTestId('cost-per-outcome-table')
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
