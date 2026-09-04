/**
 * Tests for DialControlTile.
 *
 * Verifies: loading/error states, renders dials from dial.list RPC,
 * Set button fires dial.set with correct params, reflects new level on success,
 * shows error message on failure, ceiling constraint feedback.
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { DialListResponse } from '../DialControlTile'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import DialControlTile from '../DialControlTile'

const mockJsonRpc = vi.mocked(jsonRpc)

function makeDialList(overrides: Partial<DialListResponse['dials'][0]> = {}): DialListResponse {
  return {
    dials: [
      {
        name: 'agent.spawn',
        level: 4,
        ceiling: 5,
        active_directives: 0,
        ttl_revert_at: null,
        ...overrides,
      },
    ],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

// -------------------------------------------------------------------
// Loading / error states
// -------------------------------------------------------------------

describe('DialControlTile — loading/error states', () => {
  it('shows loading message while data is pending', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {}))
    render(<DialControlTile />)
    expect(screen.getByText('Loading dials…')).toBeInTheDocument()
  })

  it('shows error when RPC rejects', async () => {
    mockJsonRpc.mockRejectedValue(new Error('network failure'))
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-error')
    expect(screen.getByText('network failure')).toBeInTheDocument()
  })

  it('renders tile container after data loads', async () => {
    mockJsonRpc.mockResolvedValue(makeDialList())
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')
  })
})

// -------------------------------------------------------------------
// Rendering dials
// -------------------------------------------------------------------

describe('DialControlTile — renders dials from dial.list', () => {
  it('renders a row for each dial class', async () => {
    mockJsonRpc.mockResolvedValue({
      dials: [
        { name: 'agent.spawn', level: 4, ceiling: 5, active_directives: 0, ttl_revert_at: null },
        { name: 'sandbox.modify', level: 1, ceiling: 1, active_directives: 0, ttl_revert_at: null },
      ],
    })
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')
    expect(screen.getByTestId('dial-row-agent.spawn')).toBeInTheDocument()
    expect(screen.getByTestId('dial-row-sandbox.modify')).toBeInTheDocument()
  })

  it('shows current level/ceiling for each dial', async () => {
    mockJsonRpc.mockResolvedValue(makeDialList({ level: 3, ceiling: 5 }))
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')
    expect(screen.getByText('3/5')).toBeInTheDocument()
  })

  it('level select contains options up to ceiling', async () => {
    mockJsonRpc.mockResolvedValue(makeDialList({ ceiling: 3 }))
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')
    const select = screen.getByTestId('dial-level-select-agent.spawn') as HTMLSelectElement
    expect(select.options.length).toBe(3)
    expect(select.options[0].value).toBe('1')
    expect(select.options[2].value).toBe('3')
  })

  it('level select is pre-filled with current level', async () => {
    mockJsonRpc.mockResolvedValue(makeDialList({ level: 4, ceiling: 5 }))
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')
    const select = screen.getByTestId('dial-level-select-agent.spawn') as HTMLSelectElement
    expect(select.value).toBe('4')
  })
})

// -------------------------------------------------------------------
// Set button interactions
// -------------------------------------------------------------------

describe('DialControlTile — Set button fires dial.set', () => {
  it('calls dial.set with correct name and level when Set clicked', async () => {
    mockJsonRpc.mockResolvedValueOnce(makeDialList({ level: 4, ceiling: 5 }))
    mockJsonRpc.mockResolvedValueOnce({ name: 'agent.spawn', level: 2, ceiling: 5 })

    const user = userEvent.setup()
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')

    // userEvent.selectOptions is the Testing-Library-recommended way to
    // drive a <select> — it dispatches the full pointer/focus/change event
    // sequence and awaits each one, unlike raw fireEvent.change which fired
    // synchronously and proved unreliable in CI (the controlled select's
    // value never committed before the following click read it).
    const select = screen.getByTestId('dial-level-select-agent.spawn') as HTMLSelectElement
    await user.selectOptions(select, '2')
    expect(select.value).toBe('2')

    await user.click(screen.getByTestId('dial-set-btn-agent.spawn'))

    // Second call should be dial.set
    expect(mockJsonRpc).toHaveBeenCalledWith('dial.set', {
      name: 'agent.spawn',
      level: 2,
      ttl: null,
    })
  })

  it('reflects new level after successful set', async () => {
    mockJsonRpc.mockResolvedValueOnce(makeDialList({ level: 4, ceiling: 5 }))
    mockJsonRpc.mockResolvedValueOnce({ name: 'agent.spawn', level: 2, ceiling: 5 })

    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')

    // Before: 4/5
    expect(screen.getByText('4/5')).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByTestId('dial-set-btn-agent.spawn'))
    })

    // After set: current level in display updates to 2
    await screen.findByText('2/5')
  })

  it('shows success feedback after set', async () => {
    mockJsonRpc.mockResolvedValueOnce(makeDialList())
    mockJsonRpc.mockResolvedValueOnce({ name: 'agent.spawn', level: 4, ceiling: 5 })

    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')

    await act(async () => {
      fireEvent.click(screen.getByTestId('dial-set-btn-agent.spawn'))
    })

    expect(screen.getByText(/Set to 4/)).toBeInTheDocument()
  })

  it('shows error feedback when dial.set rejects', async () => {
    mockJsonRpc.mockResolvedValueOnce(makeDialList())
    mockJsonRpc.mockRejectedValueOnce(new Error('ceiling_exceeded: level 2 exceeds ceiling 1'))

    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')

    await act(async () => {
      fireEvent.click(screen.getByTestId('dial-set-btn-agent.spawn'))
    })

    expect(screen.getByText(/ceiling_exceeded/)).toBeInTheDocument()
  })

  it('includes ttl in dial.set call when for-today selected', async () => {
    mockJsonRpc.mockResolvedValueOnce(makeDialList())
    mockJsonRpc.mockResolvedValueOnce({ name: 'agent.spawn', level: 4, ceiling: 5 })

    const user = userEvent.setup()
    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')

    const ttlSelect = screen.getByTestId('dial-ttl-select-agent.spawn') as HTMLSelectElement
    await user.selectOptions(ttlSelect, 'for-today')
    expect(ttlSelect.value).toBe('for-today')

    await user.click(screen.getByTestId('dial-set-btn-agent.spawn'))

    expect(mockJsonRpc).toHaveBeenCalledWith('dial.set', {
      name: 'agent.spawn',
      level: 4,
      ttl: 'for-today',
    })
  })

  it('disables button while pending', async () => {
    mockJsonRpc.mockResolvedValueOnce(makeDialList())
    // dial.set never resolves — stays pending
    mockJsonRpc.mockReturnValueOnce(new Promise(() => {}))

    render(<DialControlTile />)
    await screen.findByTestId('dial-control-tile')

    const btn = screen.getByTestId('dial-set-btn-agent.spawn') as HTMLButtonElement
    fireEvent.click(btn)
    expect(btn.disabled).toBe(true)
    expect(btn.textContent).toContain('Setting')
  })
})
