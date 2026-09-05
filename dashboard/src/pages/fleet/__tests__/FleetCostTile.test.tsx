/**
 * FleetCostTile tests — D#2317 PR-b items 5 and 6.
 *
 *   - a fleet with no in-window observation renders the no-signal caption
 *     and `—`, and never the digit 0 (item 5)
 *   - a measured zero still renders 0, and says so (item 5, the other half)
 *   - nothing on the tile is labelled "24h" while the value behind it is a
 *     UTC calendar-day-to-date total (item 6)
 *
 * All RPC calls are mocked. No backend or network access.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import FleetCostTile from '../FleetCostTile'

const mockJsonRpc = vi.mocked(jsonRpc)

function respond(payload: Record<string, unknown>) {
  mockJsonRpc.mockImplementation(() => Promise.resolve({ etag: 'x', ...payload }))
}

async function renderTile() {
  render(<FleetCostTile />)
  await waitFor(() => {
    expect(screen.getByText('Fleet Token Spend')).toBeTruthy()
  })
  return screen.getByTestId('fleet-cost-tile')
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('FleetCostTile — no signal vs measured zero', () => {
  it('renders the no-signal caption and no 0 when no project reported spend', async () => {
    // The backend omits every total when nothing landed inside the window
    // — that is what "no observation" looks like on the wire.
    respond({ per_project: [{ name: 'gatekeep', ok: true }] })

    const tile = await renderTile()

    await waitFor(() => {
      expect(tile.textContent).toContain('No token spend measured')
    })
    expect(tile.textContent).toContain('—')
    // The bug this replaces printed "0" on the busiest day on record.
    expect(tile.textContent).not.toMatch(/\b0\b/)
  })

  it('renders a real 0 when the fleet genuinely spent nothing today', async () => {
    respond({
      total_today_utc: 0,
      total_7d: 12000,
      projected_eod: 0,
      per_project: [
        { name: 'gatekeep', ok: true, tokens_today_utc: 0, tokens_7d: 12000, projected_eod_tokens: 0 },
      ],
    })

    const tile = await renderTile()

    await waitFor(() => {
      expect(tile.textContent).toContain('No token spend recorded today (UTC)')
    })
    expect(tile.textContent).toContain('0')
    expect(tile.textContent).toContain('12K')
    expect(tile.textContent).not.toContain('No token spend measured')
  })

  it('renders per-project totals when there is spend', async () => {
    respond({
      total_today_utc: 1_500_000,
      total_7d: 4_000_000,
      projected_eod: 2_000_000,
      per_project: [
        { name: 'fulcrumaxe', ok: true, tokens_today_utc: 1_500_000, tokens_7d: 4_000_000, projected_eod_tokens: 2_000_000 },
      ],
    })

    const tile = await renderTile()

    await waitFor(() => {
      expect(tile.textContent).toContain('1.5M')
    })
    expect(tile.textContent).toContain('4.0M')
    expect(tile.textContent).not.toContain('No token spend')
  })
})

describe('FleetCostTile — labels match the computation', () => {
  it('never labels a UTC calendar-day-to-date total "24h"', async () => {
    // fleet.cost carries `total_today_utc`, which backend/fleet/cost_window.py
    // computes as the sum of the entry dated today (UTC) — a
    // calendar-day-to-date figure that resets at 00:00 UTC, not a rolling
    // 24 hours. This test fails the moment a "24h" label is put back on
    // top of that value.
    respond({
      total_today_utc: 15000,
      total_7d: 60000,
      projected_eod: 20000,
      per_project: [{ name: 'fulcrumaxe', ok: true, tokens_today_utc: 15000, tokens_7d: 60000, projected_eod_tokens: 20000 }],
    })

    const tile = await renderTile()

    await waitFor(() => {
      expect(tile.textContent).toContain('15K')
    })
    expect(tile.textContent).not.toMatch(/24\s*h/i)
    expect(tile.textContent).toContain('Today (UTC)')
  })
})
