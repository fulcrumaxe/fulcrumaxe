/**
 * Component tests for RoleSuccessRateTile.
 *
 * Focuses on threshold-band classification:
 *   success_rate: red <70%, amber 70–<90%, green >=90%, gray null
 *   retry_rate:   red >30%, amber >15%,  green <=15%,  gray null
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import RoleSuccessRateTile from '../RoleSuccessRateTile'
import type { RoleSuccessRow } from '../RoleSuccessRateTile'

const mockJsonRpc = vi.mocked(jsonRpc)

// Helper: return a standard success-rate response + empty retry-rate response
function mockSuccess(rows: RoleSuccessRow[]) {
  mockJsonRpc
    .mockResolvedValueOnce({ rows })          // stats.role_success_rate
    .mockResolvedValueOnce({ rows: [] })      // stats.role_retry_rate
}

// Helper: resolve both RPCs
function mockBoth(
  successRows: { role: string; success_rate: number | null; sample_size: number }[],
  retryRows: { role: string; retry_rate: number | null; sample_size: number }[],
) {
  mockJsonRpc
    .mockResolvedValueOnce({ rows: successRows })
    .mockResolvedValueOnce({ rows: retryRows })
}

beforeEach(() => {
  vi.clearAllMocks()
})

// -------------------------------------------------------------------
// Empty / error state
// -------------------------------------------------------------------

describe('RoleSuccessRateTile — empty state', () => {
  it('shows empty message when success rows array is empty', async () => {
    mockJsonRpc.mockResolvedValue({ rows: [] })
    render(<RoleSuccessRateTile />)
    await screen.findByText('No verdict data yet. Emitted by post-agent-hook after each agent run.')
    expect(screen.queryByTestId('role-success-rate-table')).toBeNull()
  })

  it('shows empty message when RPC rejects (network error)', async () => {
    mockJsonRpc.mockRejectedValue(new Error('network error'))
    render(<RoleSuccessRateTile />)
    await screen.findByText('No verdict data yet. Emitted by post-agent-hook after each agent run.')
  })
})

// -------------------------------------------------------------------
// Table renders with data
// -------------------------------------------------------------------

describe('RoleSuccessRateTile — table rendering', () => {
  it('renders table when rows are present', async () => {
    mockSuccess([{ role: 'executor', success_rate: 0.95, sample_size: 20 }])
    render(<RoleSuccessRateTile />)
    await screen.findByTestId('role-success-rate-table')
    expect(screen.getByText('executor')).toBeInTheDocument()
    expect(screen.getByText('95.0%')).toBeInTheDocument()
    expect(screen.getByText('20')).toBeInTheDocument()
  })

  it('renders multiple roles', async () => {
    mockSuccess([
      { role: 'executor', success_rate: 0.95, sample_size: 10 },
      { role: 'code-reviewer', success_rate: 0.80, sample_size: 5 },
    ])
    render(<RoleSuccessRateTile />)
    await screen.findByTestId('role-success-rate-table')
    expect(screen.getByText('executor')).toBeInTheDocument()
    expect(screen.getByText('code-reviewer')).toBeInTheDocument()
  })
})

// -------------------------------------------------------------------
// Success-rate threshold bands (color classification)
// -------------------------------------------------------------------

describe('RoleSuccessRateTile — success_rate threshold bands', () => {
  async function renderSingleRole(success_rate: number | null) {
    mockSuccess([{ role: 'test-role', success_rate, sample_size: 10 }])
    render(<RoleSuccessRateTile />)
    await screen.findByTestId('role-success-rate-table')
    // rows[0] = thead row, rows[1] = first data row
    const rows = screen.getAllByRole('row')
    const cells = rows[1].querySelectorAll('td')
    return cells[1] as HTMLElement // success rate cell (index 1)
  }

  it('green (#22c55e) at success_rate = 0.90 — boundary: exactly at green threshold', async () => {
    const cell = await renderSingleRole(0.90)
    expect(cell.style.color).toBe('rgb(34, 197, 94)') // #22c55e
    expect(cell.textContent).toBe('90.0%')
  })

  it('green (#22c55e) at success_rate = 1.0 — max value', async () => {
    const cell = await renderSingleRole(1.0)
    expect(cell.style.color).toBe('rgb(34, 197, 94)')
    expect(cell.textContent).toBe('100.0%')
  })

  it('amber (#f59e0b) at success_rate = 0.89 — just below green boundary', async () => {
    const cell = await renderSingleRole(0.89)
    expect(cell.style.color).toBe('rgb(245, 158, 11)') // #f59e0b
    expect(cell.textContent).toBe('89.0%')
  })

  it('amber (#f59e0b) at success_rate = 0.70 — boundary: exactly at amber threshold', async () => {
    const cell = await renderSingleRole(0.70)
    expect(cell.style.color).toBe('rgb(245, 158, 11)')
    expect(cell.textContent).toBe('70.0%')
  })

  it('red (#ef4444) at success_rate = 0.69 — just below amber boundary', async () => {
    const cell = await renderSingleRole(0.69)
    expect(cell.style.color).toBe('rgb(239, 68, 68)') // #ef4444
    expect(cell.textContent).toBe('69.0%')
  })

  it('red (#ef4444) at success_rate = 0.0 — min value', async () => {
    const cell = await renderSingleRole(0.0)
    expect(cell.style.color).toBe('rgb(239, 68, 68)')
    expect(cell.textContent).toBe('0.0%')
  })

  it('gray (#6b7280) and "N/A" for null success_rate', async () => {
    const cell = await renderSingleRole(null)
    expect(cell.style.color).toBe('rgb(107, 114, 128)') // #6b7280
    expect(cell.textContent).toBe('N/A')
  })
})

// -------------------------------------------------------------------
// Retry-rate threshold bands (color + text)
// -------------------------------------------------------------------

describe('RoleSuccessRateTile — retry_rate threshold bands', () => {
  async function renderWithRetry(retry_rate: number | null) {
    mockBoth(
      [{ role: 'test-role', success_rate: 0.95, sample_size: 10 }],
      [{ role: 'test-role', retry_rate, sample_size: 10 }],
    )
    render(<RoleSuccessRateTile />)
    await screen.findByTestId('role-success-rate-table')
    return screen.getByTestId('retry-rate-test-role')
  }

  it('green (#22c55e) at retry_rate = 0.0 — best case', async () => {
    const cell = await renderWithRetry(0.0)
    expect(cell.style.color).toBe('rgb(34, 197, 94)') // #22c55e
    expect(cell.textContent).toBe('0.0%')
  })

  it('green (#22c55e) at retry_rate = 0.15 — boundary: exactly at green threshold', async () => {
    const cell = await renderWithRetry(0.15)
    expect(cell.style.color).toBe('rgb(34, 197, 94)')
    expect(cell.textContent).toBe('15.0%')
  })

  it('amber (#f59e0b) at retry_rate = 0.16 — just above green boundary', async () => {
    const cell = await renderWithRetry(0.16)
    expect(cell.style.color).toBe('rgb(245, 158, 11)') // #f59e0b
  })

  it('amber (#f59e0b) at retry_rate = 0.30 — boundary: exactly at amber ceiling', async () => {
    const cell = await renderWithRetry(0.30)
    expect(cell.style.color).toBe('rgb(245, 158, 11)')
    expect(cell.textContent).toBe('30.0%')
  })

  it('red (#ef4444) at retry_rate = 0.31 — just above red threshold', async () => {
    const cell = await renderWithRetry(0.31)
    expect(cell.style.color).toBe('rgb(239, 68, 68)') // #ef4444
  })

  it('red (#ef4444) at retry_rate = 1.0 — max value', async () => {
    const cell = await renderWithRetry(1.0)
    expect(cell.style.color).toBe('rgb(239, 68, 68)')
    expect(cell.textContent).toBe('100.0%')
  })

  it('gray (#6b7280) and "N/A" when role missing from retry rows', async () => {
    // Role present in success rows but absent from retry rows → retryRate resolves to null
    mockBoth(
      [{ role: 'test-role', success_rate: 0.95, sample_size: 10 }],
      [], // no retry data
    )
    render(<RoleSuccessRateTile />)
    await screen.findByTestId('role-success-rate-table')
    const cell = screen.getByTestId('retry-rate-test-role')
    expect(cell.style.color).toBe('rgb(107, 114, 128)') // #6b7280
    expect(cell.textContent).toBe('N/A')
  })
})

// -------------------------------------------------------------------
// Polling (fake timers — scoped to not bleed into other tests)
// -------------------------------------------------------------------

describe('RoleSuccessRateTile — polling', () => {
  it('polls every 60s', async () => {
    vi.useFakeTimers()
    try {
      mockJsonRpc.mockResolvedValue({ rows: [] })
      const { unmount } = render(<RoleSuccessRateTile />)
      // Let the initial fetch complete
      await act(async () => { await Promise.resolve() })
      // First load: 2 calls (success + retry RPCs)
      expect(mockJsonRpc).toHaveBeenCalledTimes(2)
      // Advance timer by 60s to trigger the interval
      await act(async () => { vi.advanceTimersByTime(60_000) })
      await act(async () => { await Promise.resolve() })
      expect(mockJsonRpc).toHaveBeenCalledTimes(4)
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-fetches when refreshSignal changes', async () => {
    mockJsonRpc.mockResolvedValue({ rows: [] })
    const { rerender } = render(<RoleSuccessRateTile refreshSignal={0} />)
    await screen.findByText('No verdict data yet. Emitted by post-agent-hook after each agent run.')
    const callsAfterMount = mockJsonRpc.mock.calls.length
    rerender(<RoleSuccessRateTile refreshSignal={1} />)
    await vi.waitFor(() => {
      expect(mockJsonRpc.mock.calls.length).toBeGreaterThan(callsAfterMount)
    })
  })
})
