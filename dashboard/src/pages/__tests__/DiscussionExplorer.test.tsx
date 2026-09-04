/**
 * Tests for DiscussionExplorer.tsx — cost formatter and drawer cost section.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { formatCostUsd } from '../DiscussionExplorer'

// ---------------------------------------------------------------------------
// formatCostUsd unit tests (AC 6)
// ---------------------------------------------------------------------------

describe('formatCostUsd', () => {
  it('null → $0.00', () => {
    expect(formatCostUsd(null)).toBe('$0.00')
  })

  it('undefined → $0.00', () => {
    expect(formatCostUsd(undefined)).toBe('$0.00')
  })

  it('0 → $0.00', () => {
    expect(formatCostUsd(0)).toBe('$0.00')
  })

  it('tiny value (< 0.01) → 4 decimals', () => {
    expect(formatCostUsd(0.005)).toBe('$0.0050')
  })

  it('normal value → 2 decimals', () => {
    expect(formatCostUsd(1.234)).toBe('$1.23')
  })

  it('exactly 0.01 → 2 decimals', () => {
    expect(formatCostUsd(0.01)).toBe('$0.01')
  })

  it('large value → 2 decimals', () => {
    expect(formatCostUsd(12.5678)).toBe('$12.57')
  })
})

// ---------------------------------------------------------------------------
// Drawer cost section rendering test (AC 7)
// ---------------------------------------------------------------------------

// Mock all API calls used by the drawer/page
vi.mock('../../api/client', () => ({
  discussionsApi: {
    list: vi.fn().mockResolvedValue({ items: [] }),
    get: vi.fn().mockResolvedValue({
      discussion: {
        number: 401,
        title: 'Test Discussion',
        body: 'body',
        status: 'IMPLEMENTING',
        url: null,
        createdAt: null,
        updatedAt: null,
        author: null,
      },
      comments: [],
      linked_pr: null,
      agent_runs: [],
    }),
  },
  costApi: {
    perDiscussion: vi.fn(),
  },
  // useActiveRepo() (via DiscussionExplorer) reads the project list through
  // projectsApi.list() — default to an empty list so existing tests that
  // don't care about repo links keep resolving null (renders as text).
  projectsApi: {
    list: vi.fn().mockResolvedValue([]),
  },
}))

// DiscussionExplorer isn't wrapped in <ActiveProjectProvider> in these tests,
// so useActiveProject() reads the context's default value (activeName: null)
// — useActiveRepo() falls back to the first entry from projectsApi.list().

vi.mock('../../components/StatusBadge', () => ({
  DiscussionStatusBadge: ({ status }: { status: string }) => (
    <span data-testid="status-badge">{status}</span>
  ),
}))

describe('DiscussionExplorer drawer cost section', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    // test-setup.ts runs vi.restoreAllMocks() after every test, which wipes
    // the mockResolvedValue set once in the vi.mock('../../api/client', ...)
    // factory above. Re-establish it here so discussionsApi.get() resolves
    // instead of returning undefined on the second+ test in this file.
    const { discussionsApi, projectsApi } = await import('../../api/client')
    vi.mocked(discussionsApi.list).mockResolvedValue({ items: [] })
    vi.mocked(discussionsApi.get).mockResolvedValue({
      discussion: {
        number: 401,
        title: 'Test Discussion',
        body: 'body',
        status: 'IMPLEMENTING',
        url: null,
        createdAt: null,
        updatedAt: null,
        author: null,
      },
      comments: [],
      linked_pr: null,
      agent_runs: [],
    })
    // Same restoreAllMocks wipe applies to the projectsApi.list default set
    // in the vi.mock(...) factory above — re-establish it here too.
    vi.mocked(projectsApi.list).mockResolvedValue([])
  })

  it('renders "no recorded spend yet" when costApi returns null', async () => {
    const { costApi } = await import('../../api/client')
    vi.mocked(costApi.perDiscussion).mockResolvedValue(null)

    const { default: DiscussionExplorer } = await import('../DiscussionExplorer')
    render(
      <MemoryRouter initialEntries={['/discussions?selected=401']}>
        <DiscussionExplorer />
      </MemoryRouter>
    )

    await waitFor(() => {
      const noSpend = screen.getAllByText('no recorded spend yet')
      expect(noSpend.length).toBeGreaterThan(0)
    }, { timeout: 3000 })
  })

  it('renders agent_breakdown and pr_breakdown tables when cost data is present', async () => {
    const { costApi } = await import('../../api/client')
    vi.mocked(costApi.perDiscussion).mockResolvedValue({
      discussion: 401,
      cost_usd: 0.045,
      total_cost_usd: 0.045,
      total_input_tokens: 10000,
      total_output_tokens: 3000,
      agent_count: 2,
      agents: ['executor-401-1', 'code-reviewer-401-1'],
      agent_breakdown: {
        executor: 0.03,
        'code-reviewer': 0.015,
      },
      pr_breakdown: {
        '410': 0.045,
      },
    })

    const { default: DiscussionExplorer } = await import('../DiscussionExplorer')
    render(
      <MemoryRouter initialEntries={['/discussions?selected=401']}>
        <DiscussionExplorer />
      </MemoryRouter>
    )

    await waitFor(() => {
      // Cost section header (uppercase: 'COST')
      expect(screen.getAllByText(/cost/i).length).toBeGreaterThan(0)
      // Role names
      expect(screen.getByText('executor')).toBeTruthy()
      // PR entry
      expect(screen.getByText('PR #410')).toBeTruthy()
    }, { timeout: 3000 })
  })
})

// ---------------------------------------------------------------------------
// Discussion / PR links derive from the active project's repo (D#2234)
// ---------------------------------------------------------------------------

function makeSummary(overrides: Partial<{
  number: number
  title: string
  status: string
  linkedPr: number | null
  updatedAt: string | null
  costUsd: number | null
}> = {}) {
  return {
    number: 55,
    title: 'Some discussion',
    status: 'DISCUSSING',
    linkedPr: null,
    url: null,
    createdAt: null,
    updatedAt: null,
    author: null,
    costUsd: null,
    ...overrides,
  }
}

describe('DiscussionExplorer — repo-derived links', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    const { discussionsApi, costApi } = await import('../../api/client')
    vi.mocked(discussionsApi.get).mockResolvedValue({
      discussion: {
        number: 55,
        title: 'Some discussion',
        body: 'body',
        status: 'DISCUSSING',
        url: null,
        createdAt: null,
        updatedAt: null,
        author: null,
      },
      comments: [],
      linked_pr: null,
      agent_runs: [],
    })
    vi.mocked(costApi.perDiscussion).mockResolvedValue(null)
  })

  it('builds Discussion and PR hrefs from the resolved project repo', async () => {
    const { discussionsApi, projectsApi } = await import('../../api/client')
    vi.mocked(discussionsApi.list).mockResolvedValue({
      items: [makeSummary({ number: 55, linkedPr: 99 })],
    })
    vi.mocked(projectsApi.list).mockResolvedValue([
      { id: 'gatekeep', name: 'gatekeep', repo: 'fulcrumaxe/gatekeep' } as never,
    ])

    const { default: DiscussionExplorer } = await import('../DiscussionExplorer')
    render(
      <MemoryRouter initialEntries={['/discussions']}>
        <DiscussionExplorer />
      </MemoryRouter>
    )

    await waitFor(() => {
      const link = screen.getByText('Some discussion').closest('a')
      expect(link).not.toBeNull()
      expect(link?.getAttribute('href')).toBe('https://github.com/fulcrumaxe/gatekeep/discussions/55')
    })

    const prLink = screen.getByText('PR #99').closest('a')
    expect(prLink).not.toBeNull()
    expect(prLink?.getAttribute('href')).toBe('https://github.com/fulcrumaxe/gatekeep/pull/99')
  })

  it('renders the Discussion title as plain text (no anchor) when the repo cannot be resolved', async () => {
    const { discussionsApi, projectsApi } = await import('../../api/client')
    vi.mocked(discussionsApi.list).mockResolvedValue({
      items: [makeSummary({ number: 55, linkedPr: null })],
    })
    // No projects at all — useActiveRepo() resolves null.
    vi.mocked(projectsApi.list).mockResolvedValue([])

    const { default: DiscussionExplorer } = await import('../DiscussionExplorer')
    render(
      <MemoryRouter initialEntries={['/discussions']}>
        <DiscussionExplorer />
      </MemoryRouter>
    )

    await waitFor(() => {
      const title = screen.getByText('Some discussion')
      expect(title.closest('a')).toBeNull()
    })
  })
})
