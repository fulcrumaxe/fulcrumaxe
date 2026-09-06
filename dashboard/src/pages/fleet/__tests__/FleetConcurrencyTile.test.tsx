/**
 * FleetConcurrencyTile tests — D#2323.
 *
 * The tile used to render an unfiltered fleet-wide row count against the fleet
 * cap, which on the operator host read "21 of 8". It also rendered a handler
 * that had caught its own exception as "0 of 8", so an unreadable fleet.db and
 * an idle fleet looked identical.
 *
 * These assert the pair of numbers, not either one alone: what went wrong was
 * what the two meant together.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
  getRpcBaseUrl: vi.fn(() => 'http://localhost:8765'),
  getRpcToken: vi.fn(() => 'test-token'),
}))

import { jsonRpc } from '../../../api/client'
import FleetConcurrencyTile from '../FleetConcurrencyTile'

const mockJsonRpc = vi.mocked(jsonRpc)

/** The shape measured on the operator host: 21 rows, exactly 1 cap-governed. */
function twentyOneRowsOneCapped() {
  return {
    available: true,
    fleet_cap: 8,
    capped_agents: 1,
    uncapped_agents: 20,
    per_project: [
      { name: 'autonomous-forever', capped_agents: 1, uncapped_agents: 20, cap: 8, ok: true },
    ],
    etag: 'ghi789',
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.clearAllTimers()
})

describe('FleetConcurrencyTile — the headline is the cap-governed count', () => {
  it('renders 1 of 8, never 21 of 8, for 21 rows of which 1 is capped', async () => {
    mockJsonRpc.mockResolvedValue(twentyOneRowsOneCapped())
    render(<FleetConcurrencyTile />)

    await waitFor(() => {
      expect(screen.getByTestId('fleet-concurrency-headline').textContent)
        .toMatch(/^\s*1\s*of 8\s*$/)
    })
    expect(screen.getByTestId('fleet-concurrency-tile').textContent).not.toContain('21 of 8')
  })

  it('shows the uncapped lane on its own line, labelled as not cap-governed', async () => {
    mockJsonRpc.mockResolvedValue(twentyOneRowsOneCapped())
    render(<FleetConcurrencyTile />)

    await waitFor(() => {
      const line = screen.getByTestId('fleet-concurrency-uncapped').textContent ?? ''
      expect(line).toContain('20')
      expect(line).toMatch(/No cap covers this lane/i)
    })
  })

  it('sizes the per-project bar and count from the capped lane', async () => {
    mockJsonRpc.mockResolvedValue(twentyOneRowsOneCapped())
    render(<FleetConcurrencyTile />)

    await waitFor(() => {
      const tile = screen.getByTestId('fleet-concurrency-tile')
      expect(tile.textContent).toContain('1/8')
      expect(tile.textContent).not.toContain('21/8')
      expect(tile.textContent).toContain('+20 uncapped')
    })
  })
})

describe('FleetConcurrencyTile — unreadable state is not a zero', () => {
  it('renders the reason when the handler reports unavailable', async () => {
    mockJsonRpc.mockResolvedValue({
      available: false,
      unavailable_reason: 'fleet state not readable: fleet.db not found in the fleet state directory',
      fleet_cap: 8,
      capped_agents: null,
      uncapped_agents: null,
      per_project: [],
      etag: 'x',
    })
    render(<FleetConcurrencyTile />)

    await waitFor(() => {
      expect(screen.getByTestId('fleet-concurrency-unavailable').textContent)
        .toContain('fleet.db not found')
    })
    expect(screen.queryByTestId('fleet-concurrency-headline')).toBeNull()
    expect(screen.getByTestId('fleet-concurrency-tile').textContent).not.toContain('0 of 8')
  })

  it('renders 0 of 8 for a genuinely empty fleet', async () => {
    mockJsonRpc.mockResolvedValue({
      available: true,
      fleet_cap: 8,
      capped_agents: 0,
      uncapped_agents: 0,
      per_project: [],
      etag: 'y',
    })
    render(<FleetConcurrencyTile />)

    await waitFor(() => {
      expect(screen.getByTestId('fleet-concurrency-headline').textContent)
        .toMatch(/^\s*0\s*of 8\s*$/)
    })
    expect(screen.queryByTestId('fleet-concurrency-unavailable')).toBeNull()
    expect(screen.getByText('No projects discovered')).toBeTruthy()
  })
})
