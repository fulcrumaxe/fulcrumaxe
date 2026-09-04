import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import AnalystFindingsTile from '../runs/AnalystFindingsTile'
import { jsonRpc } from '../../api/client'

vi.mock('../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

const mockJsonRpc = vi.mocked(jsonRpc)

const emptyResponse = {
  report_at: null,
  window: null,
  runs_analyzed: 0,
  by_severity: { high: [], medium: [], low: [] },
  total: 0,
  generated_at: '2026-05-20T15:00:00Z',
  error: null,
}

const fixtureResponse = {
  report_at: '2026-05-20T14:44:44Z',
  window: { since: '2026-05-20T02:44:44Z', until: '2026-05-20T14:44:44Z' },
  runs_analyzed: 5,
  by_severity: {
    high: [
      {
        category: 'failure_cluster',
        severity: 'high',
        title: "Pattern 'OOM' hit 6 times in recent runs",
        evidence: ['executor/run-1', 'executor/run-2'],
        suggested_discussion_title: '[Bug] Recurring OOM errors',
        suggested_tag: '[Bug]',
      },
    ],
    medium: [
      {
        category: 'cost_outlier',
        severity: 'medium',
        title: "Role 'code-reviewer' uses 120,000 tokens/pass",
        evidence: ['code-reviewer'],
        suggested_discussion_title: '[Small] Reduce token usage',
        suggested_tag: '[Small]',
      },
    ],
    low: [],
  },
  total: 2,
  generated_at: '2026-05-20T15:00:00Z',
  error: null,
}

describe('AnalystFindingsTile', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders loading state initially', () => {
    mockJsonRpc.mockReturnValue(new Promise(() => {}))
    render(<AnalystFindingsTile />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('renders empty state when no reports exist', async () => {
    mockJsonRpc.mockResolvedValue(emptyResponse)
    await act(async () => {
      render(<AnalystFindingsTile />)
    })
    expect(screen.getByText(/No analyst findings yet/)).toBeInTheDocument()
  })

  it('renders findings grouped by severity', async () => {
    mockJsonRpc.mockResolvedValue(fixtureResponse)
    await act(async () => {
      render(<AnalystFindingsTile />)
    })

    // Heading section labels
    expect(screen.getByText(/high/i)).toBeInTheDocument()
    expect(screen.getByText(/medium/i)).toBeInTheDocument()

    // Finding titles
    expect(screen.getByText(/Pattern 'OOM' hit 6 times/)).toBeInTheDocument()
    expect(screen.getByText(/Role 'code-reviewer'/)).toBeInTheDocument()

    // Evidence refs
    expect(screen.getByText('executor/run-1')).toBeInTheDocument()
    expect(screen.getByText('code-reviewer')).toBeInTheDocument()
  })

  it('renders total count in heading', async () => {
    mockJsonRpc.mockResolvedValue(fixtureResponse)
    await act(async () => {
      render(<AnalystFindingsTile />)
    })
    expect(screen.getByText(/2 findings/)).toBeInTheDocument()
  })

  it('shows "none" label when total is 0', async () => {
    mockJsonRpc.mockResolvedValue(emptyResponse)
    await act(async () => {
      render(<AnalystFindingsTile />)
    })
    expect(screen.getByText('none')).toBeInTheDocument()
  })

  it('renders tile with correct data-testid', async () => {
    mockJsonRpc.mockResolvedValue(emptyResponse)
    await act(async () => {
      render(<AnalystFindingsTile />)
    })
    expect(screen.getByTestId('analyst-findings-tile')).toBeInTheDocument()
  })

  it('renders error state when fetch fails', async () => {
    mockJsonRpc.mockRejectedValue(new Error('network error'))
    await act(async () => {
      render(<AnalystFindingsTile />)
    })
    expect(screen.getByText(/Unable to load analyst findings/)).toBeInTheDocument()
  })

  it('shows category badge for each finding', async () => {
    mockJsonRpc.mockResolvedValue(fixtureResponse)
    await act(async () => {
      render(<AnalystFindingsTile />)
    })
    expect(screen.getByText('failure_cluster')).toBeInTheDocument()
    expect(screen.getByText('cost_outlier')).toBeInTheDocument()
  })

  // D#2316 finding 3: a transport failure (thrown error, e.g. a 401) and a
  // genuinely empty report (resolved data, total: 0, error: null) used to
  // both collapse to "Unable to load analyst findings." — the tile could not
  // tell an operator "the request never even reached the loader" from "the
  // loader ran and found nothing." These three tests key each render off the
  // shape of the response, not off a thrown/not-thrown boolean, and assert
  // the three rendered texts are pairwise different.
  describe('three distinguishable states (transport failure vs empty vs findings)', () => {
    function tileText(): string {
      return screen.getByTestId('analyst-findings-tile').textContent ?? ''
    }

    it('names the failure on a transport error, distinct from the empty-state text', async () => {
      mockJsonRpc.mockRejectedValue(new Error('401 Unauthorized'))
      await act(async () => {
        render(<AnalystFindingsTile />)
      })
      const text = tileText()
      expect(text).toContain('401 Unauthorized')
      expect(text).not.toContain('No analyst findings yet')
    })

    it('renders the empty state for a resolved, genuinely empty report', async () => {
      mockJsonRpc.mockResolvedValue(emptyResponse)
      await act(async () => {
        render(<AnalystFindingsTile />)
      })
      const text = tileText()
      expect(text).toContain('No analyst findings yet')
      expect(text).not.toContain('Unable to load')
    })

    it('renders findings when present', async () => {
      mockJsonRpc.mockResolvedValue(fixtureResponse)
      await act(async () => {
        render(<AnalystFindingsTile />)
      })
      const text = tileText()
      expect(text).toContain("Pattern 'OOM' hit 6 times")
      expect(text).not.toContain('Unable to load')
      expect(text).not.toContain('No analyst findings yet')
    })

    it('the three rendered texts are pairwise different', async () => {
      mockJsonRpc.mockRejectedValueOnce(new Error('network error: fetch failed'))
      const { unmount: unmountA } = render(<AnalystFindingsTile />)
      await act(async () => {})
      const transportFailureText = tileText()
      unmountA()

      vi.clearAllMocks()
      mockJsonRpc.mockResolvedValueOnce(emptyResponse)
      const { unmount: unmountB } = render(<AnalystFindingsTile />)
      await act(async () => {})
      const emptyText = tileText()
      unmountB()

      vi.clearAllMocks()
      mockJsonRpc.mockResolvedValueOnce(fixtureResponse)
      render(<AnalystFindingsTile />)
      await act(async () => {})
      const findingsText = tileText()

      expect(transportFailureText).not.toBe(emptyText)
      expect(transportFailureText).not.toBe(findingsText)
      expect(emptyText).not.toBe(findingsText)
    })
  })
})
