import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import ProjectDashboardPage from '../ProjectDashboardPage'
import * as clientModule from '../../api/client'
import { useWebSocket } from '../../hooks/useWebSocket'
import { useAuth } from '../../auth/AuthContext'
import type { SpawnBlockEvent } from '../../api/types'

vi.mock('../../api/client', () => ({
  budgetApi: { status: vi.fn() },
  kpiApi: { summary: vi.fn() },
  spawnQueueApi: { status: vi.fn() },
  healthApi: { loop: vi.fn() },
  agentsApi: { list: vi.fn() },
  apiSpawnBlocks: vi.fn(),
}))

vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(),
}))

vi.mock('../../auth/AuthContext', () => ({
  useAuth: vi.fn(),
}))

vi.mock('../../components/Sidebar', () => ({
  Sidebar: () => <nav data-testid="sidebar" />,
}))

vi.mock('../../components/Header', () => ({
  Header: ({ projectName }: { projectName?: string }) => <header data-testid="header">{projectName}</header>,
}))

const mockBudget = {
  dailySpend: 1.5,
  dailyLimit: 10,
  monthlySpend: 30,
  monthlyLimit: 200,
  currency: 'USD',
  alertThreshold: 0.8,
}

const mockKpi = {
  velocity: 12,
  momentum: 0.9,
  cycleTimeMean: 4.5,
  estimationAccuracy: 0.82,
  estimationAccuracySampleCount: 8,
  estimationAccuracyMinSamples: 5,
  period: '2026-04',
}

const mockKpiNullAccuracy = {
  velocity: 12,
  momentum: 0.9,
  cycleTimeMean: 4.5,
  estimationAccuracy: null,
  estimationAccuracySampleCount: 3,
  estimationAccuracyMinSamples: 5,
  period: '2026-04',
}

const mockQueue = {
  pending: [],
  active: [],
  totalToday: 5,
}

const mockLoop = {
  lastRun: '2026-04-10T10:00:00Z',
  status: 'ok' as const,
  duration: 45,
}

const mockAgents = [
  {
    id: 'a1',
    role: 'executor',
    status: 'done' as const,
    startedAt: '2026-04-10T09:00:00Z',
    duration: 300,
    discussion: 100,
  },
]

describe('ProjectDashboardPage', () => {
  beforeEach(() => {
    vi.mocked(useWebSocket).mockReturnValue({ events: [], connected: true })
    vi.mocked(useAuth).mockReturnValue({
      session: { id: 's1', userId: 'u1', username: 'testuser', avatarUrl: '', createdAt: '', expiresAt: '' },
      token: 'tok',
      loading: false,
      login: vi.fn(),
      logout: vi.fn(),
    })
    vi.mocked(clientModule.budgetApi.status).mockResolvedValue(mockBudget)
    vi.mocked(clientModule.kpiApi.summary).mockResolvedValue(mockKpi)
    vi.mocked(clientModule.spawnQueueApi.status).mockResolvedValue(mockQueue)
    vi.mocked(clientModule.healthApi.loop).mockResolvedValue(mockLoop)
    vi.mocked(clientModule.agentsApi.list).mockResolvedValue(mockAgents)
    vi.mocked(clientModule.apiSpawnBlocks).mockResolvedValue([])
  })

  function renderPage(projectId = 'proj-1') {
    return render(
      <MemoryRouter initialEntries={[`/project/${projectId}`]}>
        <Routes>
          <Route path="/project/:id" element={<ProjectDashboardPage />} />
        </Routes>
      </MemoryRouter>
    )
  }

  it('renders budget card', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Budget')).toBeInTheDocument()
    })
  })

  it('renders KPI card', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('KPI')).toBeInTheDocument()
    })
  })

  it('renders queue card', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Spawn Queue')).toBeInTheDocument()
    })
  })

  it('renders loop health card', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Loop Health')).toBeInTheDocument()
    })
  })

  it('renders agent activity section', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Recent Agent Activity')).toBeInTheDocument()
    })
  })

  it('displays velocity value from KPI', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('12')).toBeInTheDocument()
    })
  })

  it('renders accuracy percentage when sample count is sufficient', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('82%')).toBeInTheDocument()
    })
  })

  it('renders N/A when estimationAccuracy is null', async () => {
    vi.mocked(clientModule.kpiApi.summary).mockResolvedValue(mockKpiNullAccuracy)
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('N/A')).toBeInTheDocument()
    })
  })

  it('renders N/A subtext with have-X count when accuracy is null', async () => {
    vi.mocked(clientModule.kpiApi.summary).mockResolvedValue(mockKpiNullAccuracy)
    renderPage()
    await waitFor(() => {
      // Subtext: "Need 5+ measured Discussions (have 3)"
      expect(screen.getByText(/Need 5\+ measured Discussions \(have 3\)/)).toBeInTheDocument()
    })
  })

  it('does not render N/A when accuracy is a real number', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.queryByText('N/A')).not.toBeInTheDocument()
    })
  })

  it('renders spawn blocks tile with empty state when no blocks', async () => {
    vi.mocked(clientModule.apiSpawnBlocks).mockResolvedValue([])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Recent Spawn Blocks')).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByText('No recent blocks')).toBeInTheDocument()
    })
  })

  it('renders spawn blocks tile rows for non-empty data', async () => {
    const mockBlocks: SpawnBlockEvent[] = [
      {
        ts: new Date(Date.now() - 3 * 60 * 1000).toISOString(),
        role: 'executor',
        event_type: 'spawn_blocked',
        reason: 'budget_exceeded',
        message: 'blocked executor: budget exhausted',
        discussion: 42,
        details: { budget_remaining: 0 },
      },
      {
        ts: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
        role: 'code-reviewer',
        event_type: 'spawn_blocked',
        reason: 'circuit_breaker_open',
        message: 'blocked code-reviewer: circuit-breaker open',
        details: { circuit_failures: 3 },
      },
      {
        ts: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
        role: 'impl-coordinator',
        event_type: 'spawn_blocked',
        reason: 'worktree_cap_reached',
        message: 'blocked impl-coordinator: worktree cap reached',
        details: { active_worktrees: 8, cap: 8 },
      },
    ]
    vi.mocked(clientModule.apiSpawnBlocks).mockResolvedValue(mockBlocks)
    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Recent Spawn Blocks')).toBeInTheDocument()
    })
    await waitFor(() => {
      // Use getAllByText since 'executor' also appears in agent activity card
      expect(screen.getAllByText('executor').length).toBeGreaterThanOrEqual(1)
      expect(screen.getByText('budget_exceeded')).toBeInTheDocument()
      expect(screen.getByText('circuit_breaker_open')).toBeInTheDocument()
      expect(screen.getByText('worktree_cap_reached')).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByText('D#42')).toBeInTheDocument()
    })
  })

  it('shows "—" for lastRun when loop health lastRun is null (AC-6: null-guard)', async () => {
    vi.mocked(clientModule.healthApi.loop).mockResolvedValue({
      lastRun: null as unknown as string,
      status: 'ok' as const,
      duration: 0,
    })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Loop Health')).toBeInTheDocument()
    })
    await waitFor(() => {
      expect(screen.getByText(/Last run:/)).toBeInTheDocument()
    })
    expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument()
    expect(screen.getByText(/Last run:/).textContent).toContain('—')
  })
})
