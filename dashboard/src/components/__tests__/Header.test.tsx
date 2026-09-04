import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { Header } from '../Header'

// Mock the circuitBreakerApi so we don't need a running backend
vi.mock('../../api/client', () => ({
  circuitBreakerApi: {
    summary: vi.fn(),
  },
}))

import { circuitBreakerApi } from '../../api/client'

const mockSummary = circuitBreakerApi.summary as ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Header', () => {
  it('renders project name', async () => {
    mockSummary.mockResolvedValue({ tripped: [], warnings: [], threshold: 3 })
    await act(async () => {
      render(<Header projectName="fulcrumaxe" />)
    })
    expect(screen.getByText('fulcrumaxe')).toBeInTheDocument()
  })

  it('renders no badge when tripped count is 0', async () => {
    mockSummary.mockResolvedValue({ tripped: [], warnings: [], threshold: 3 })
    await act(async () => {
      render(<Header />)
    })
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('renders warning badge when tripped count > 0', async () => {
    mockSummary.mockResolvedValue({
      tripped: [
        { discussion: 394, count: 3, agent: 'executor', reason: 'timed out', updated_at: null },
        { discussion: 12, count: 4, agent: 'code-reviewer', reason: 'lint fail', updated_at: null },
      ],
      warnings: [],
      threshold: 3,
    })
    await act(async () => {
      render(<Header />)
    })
    const badge = screen.getByRole('status')
    expect(badge).toBeInTheDocument()
    expect(badge.textContent).toContain('2 tripped')
    expect(badge.className).toContain('warning')
  })

  it('badge has title tooltip listing tripped discussions', async () => {
    mockSummary.mockResolvedValue({
      tripped: [
        { discussion: 394, count: 3, agent: 'executor', reason: 'timed out', updated_at: null },
      ],
      warnings: [],
      threshold: 3,
    })
    await act(async () => {
      render(<Header />)
    })
    const wrapper = screen.getByRole('status').parentElement!
    expect(wrapper.title).toContain('#394')
    expect(wrapper.title).toContain('executor')
    expect(wrapper.title).toContain('timed out')
  })

  it('polls every 30s', async () => {
    mockSummary.mockResolvedValue({ tripped: [], warnings: [], threshold: 3 })
    await act(async () => {
      render(<Header />)
    })
    expect(mockSummary).toHaveBeenCalledTimes(1)
    await act(async () => {
      vi.advanceTimersByTime(30_000)
    })
    expect(mockSummary).toHaveBeenCalledTimes(2)
  })
})
