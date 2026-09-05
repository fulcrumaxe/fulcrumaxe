/**
 * SdkVsCcTileHonesty.test.tsx — the tile must never state a pass rate nobody measured.
 *
 * The backend used to average a CASE expression over COUNT(*), so a group where
 * no run ever recorded a verdict produced 0.0 rather than "no rate exists".
 * Every one of the 22 roles on the live Runs page read "0.0%". The reader now
 * returns null for an empty denominator; these tests pin the rendering half of
 * that — null must reach the screen as an em-dash, and "0.0%" must not appear
 * anywhere unless a group genuinely measured a 0% pass rate.
 *
 * Also covers the excluded-run count: "1,949 runs are not attributed to a
 * route" and "there are no runs" are different facts and must read differently.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, within } from '@testing-library/react'
import SdkVsCcTile from '../runs/SdkVsCcTile'
import { jsonRpc } from '../../api/client'

vi.mock('../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

const mockJsonRpc = vi.mocked(jsonRpc)

function row(overrides: Record<string, unknown> = {}) {
  return {
    role: 'executor',
    route: 'cc',
    run_count: 841,
    median_input_tok: null,
    median_output_tok: null,
    verdict_count: 0,
    pass_rate: null,
    ...overrides,
  }
}

/** The pass-rate cell is the last column of a body row. */
function passRateCellText(role: string): string {
  const cell = screen.getByText(role).closest('tr')!
  const cells = within(cell).getAllByRole('cell')
  return cells[cells.length - 1].textContent ?? ''
}

describe('SdkVsCcTile — pass-rate honesty', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders a null pass_rate as an em-dash, never as 0.0%', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [row()],
      has_routed_via: true,
      excluded_unrouted_runs: 0,
      generated_at: '2026-09-04T04:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })

    expect(passRateCellText('executor')).toBe('—')
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
    expect(screen.getByTestId('sdk-vs-cc-tile').textContent).not.toContain('0.0%')
  })

  it('still shows a real 0.0% when a group actually measured one', async () => {
    // The distinction that matters: 0 passes out of 4 recorded verdicts IS a
    // measurement, and must not be suppressed along with the unmeasured case.
    mockJsonRpc.mockResolvedValue({
      rows: [row({ role: 'code-reviewer', verdict_count: 4, pass_rate: 0 })],
      has_routed_via: true,
      excluded_unrouted_runs: 0,
      generated_at: '2026-09-04T04:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })

    expect(passRateCellText('code-reviewer')).toBe('0.0%')
  })

  it('renders measured and unmeasured rows differently in the same table', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [
        row({ role: 'executor', verdict_count: 0, pass_rate: null }),
        row({ role: 'project-manager', verdict_count: 8, pass_rate: 0.75 }),
      ],
      has_routed_via: true,
      excluded_unrouted_runs: 0,
      generated_at: '2026-09-04T04:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })

    const unmeasured = passRateCellText('executor')
    const measured = passRateCellText('project-manager')
    expect(unmeasured).toBe('—')
    expect(measured).toBe('75.0%')
    expect(unmeasured).not.toBe(measured)
  })
})

describe('SdkVsCcTile — excluded runs are stated, not dropped', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('names the unattributed run count alongside the table', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [row()],
      has_routed_via: true,
      excluded_unrouted_runs: 1949,
      generated_at: '2026-09-04T04:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })

    expect(screen.getByText(/1,949 runs are not attributed to a route/)).toBeInTheDocument()
  })

  it('distinguishes "no runs at all" from "runs exist but none are routed"', async () => {
    mockJsonRpc.mockResolvedValue({
      rows: [],
      has_routed_via: true,
      excluded_unrouted_runs: 0,
      generated_at: '2026-09-04T04:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })
    const trulyEmpty = screen.getByTestId('sdk-vs-cc-tile').textContent ?? ''

    mockJsonRpc.mockResolvedValue({
      rows: [],
      has_routed_via: true,
      excluded_unrouted_runs: 1949,
      generated_at: '2026-09-04T04:00:00Z',
      error: null,
    })
    await act(async () => {
      render(<SdkVsCcTile />)
    })
    const tiles = screen.getAllByTestId('sdk-vs-cc-tile')
    const withExcluded = tiles[tiles.length - 1].textContent ?? ''

    expect(withExcluded).not.toBe(trulyEmpty)
    expect(withExcluded).toContain('1,949')
    expect(trulyEmpty).not.toContain('not attributed')
  })
})
