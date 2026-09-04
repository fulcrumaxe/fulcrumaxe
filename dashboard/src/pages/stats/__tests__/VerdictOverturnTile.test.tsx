/**
 * Component tests for VerdictOverturnTile.
 *
 * Covers:
 *   - empty/error state
 *   - table renders with data
 *   - N/A for null overturn_rate
 *   - color threshold bands: red >20%, amber >5%, green otherwise
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import VerdictOverturnTile from '../VerdictOverturnTile'
import type { VerdictOverturnRow } from '../VerdictOverturnTile'

const mockJsonRpc = vi.mocked(jsonRpc)

function mockRows(rows: VerdictOverturnRow[]) {
  mockJsonRpc.mockResolvedValueOnce({ rows })
}

beforeEach(() => {
  vi.clearAllMocks()
})

// -------------------------------------------------------------------
// Empty / error state
// -------------------------------------------------------------------

describe('VerdictOverturnTile — empty state', () => {
  it('shows empty message when rows array is empty', async () => {
    mockRows([])
    render(<VerdictOverturnTile />)
    await screen.findByText(
      /No overturn data yet/,
    )
    expect(screen.queryByTestId('verdict-overturn-table')).toBeNull()
  })

  it('shows empty message when RPC rejects', async () => {
    mockJsonRpc.mockRejectedValue(new Error('network error'))
    render(<VerdictOverturnTile />)
    await screen.findByText(/No overturn data yet/)
  })
})

// -------------------------------------------------------------------
// Table renders with data
// -------------------------------------------------------------------

describe('VerdictOverturnTile — table rendering', () => {
  it('renders table when rows are present', async () => {
    mockRows([
      { role: 'acceptance-tester', overturns: 2, total_pass: 10, overturn_rate: 0.2, sample_size: 10 },
    ])
    render(<VerdictOverturnTile />)
    await screen.findByTestId('verdict-overturn-table')
    expect(screen.getByText('acceptance-tester')).toBeInTheDocument()
    expect(screen.getByText('20.0%')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('10')).toBeInTheDocument()
  })

  it('renders multiple roles', async () => {
    mockRows([
      { role: 'acceptance-tester', overturns: 1, total_pass: 5, overturn_rate: 0.2, sample_size: 5 },
      { role: 'code-reviewer', overturns: 0, total_pass: 8, overturn_rate: 0.0, sample_size: 8 },
    ])
    render(<VerdictOverturnTile />)
    await screen.findByTestId('verdict-overturn-table')
    expect(screen.getByText('acceptance-tester')).toBeInTheDocument()
    expect(screen.getByText('code-reviewer')).toBeInTheDocument()
  })

  it('shows N/A for null overturn_rate', async () => {
    mockRows([
      { role: 'executor', overturns: 0, total_pass: 3, overturn_rate: null, sample_size: 3 },
    ])
    render(<VerdictOverturnTile />)
    await screen.findByTestId('verdict-overturn-table')
    const cell = screen.getByTestId('overturn-rate-executor')
    expect(cell.textContent).toBe('N/A')
  })
})

// -------------------------------------------------------------------
// Threshold band colors
// -------------------------------------------------------------------

describe('VerdictOverturnTile — color threshold bands', () => {
  async function renderSingleRole(overturn_rate: number | null) {
    mockRows([
      { role: 'test-role', overturns: 1, total_pass: 10, overturn_rate, sample_size: 10 },
    ])
    render(<VerdictOverturnTile />)
    await screen.findByTestId('verdict-overturn-table')
    return screen.getByTestId('overturn-rate-test-role')
  }

  it('red (#ef4444) at overturn_rate = 0.21 — above 20% threshold', async () => {
    const cell = await renderSingleRole(0.21)
    expect(cell.style.color).toBe('rgb(239, 68, 68)') // #ef4444
    expect(cell.textContent).toBe('21.0%')
  })

  it('red (#ef4444) at overturn_rate = 1.0 — max value', async () => {
    const cell = await renderSingleRole(1.0)
    expect(cell.style.color).toBe('rgb(239, 68, 68)')
  })

  it('amber (#f59e0b) at overturn_rate = 0.20 — exactly at 20% (not above)', async () => {
    const cell = await renderSingleRole(0.20)
    expect(cell.style.color).toBe('rgb(245, 158, 11)') // #f59e0b
    expect(cell.textContent).toBe('20.0%')
  })

  it('amber (#f59e0b) at overturn_rate = 0.06 — above 5% but not above 20%', async () => {
    const cell = await renderSingleRole(0.06)
    expect(cell.style.color).toBe('rgb(245, 158, 11)')
  })

  it('green (#22c55e) at overturn_rate = 0.05 — exactly at 5% (not above)', async () => {
    const cell = await renderSingleRole(0.05)
    expect(cell.style.color).toBe('rgb(34, 197, 94)') // #22c55e
    expect(cell.textContent).toBe('5.0%')
  })

  it('green (#22c55e) at overturn_rate = 0.0 — no overturns', async () => {
    const cell = await renderSingleRole(0.0)
    expect(cell.style.color).toBe('rgb(34, 197, 94)')
    expect(cell.textContent).toBe('0.0%')
  })

  it('gray (#6b7280) for null overturn_rate', async () => {
    const cell = await renderSingleRole(null)
    expect(cell.style.color).toBe('rgb(107, 114, 128)') // #6b7280
    expect(cell.textContent).toBe('N/A')
  })
})

// -------------------------------------------------------------------
// Polling
// -------------------------------------------------------------------

describe('VerdictOverturnTile — polling', () => {
  it('polls every 60s', async () => {
    vi.useFakeTimers()
    try {
      mockJsonRpc.mockResolvedValue({ rows: [] })
      const { unmount } = render(<VerdictOverturnTile />)
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
