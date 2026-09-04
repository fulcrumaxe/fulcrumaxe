/**
 * D#2314 PR2 — the Projects page card rework.
 *
 * PR 1 fixed the backend signal (liveness now reads real fleet.db rows
 * instead of a cron trail nobody writes). This suite covers PR 2: the card
 * itself collapses four status-ish fields (liveness badge, health badge,
 * momentum line, activeAgents count) into one honest status line, with a
 * visibly distinct no-signal state and a "what they're doing" role-name line.
 *
 * Carried over from the pre-rework suite: the "no unearned zero" guard
 * (activeAgents must never render for a project that wasn't successfully
 * queried) — still true under the new copy, just asserted against the new
 * markup.
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { Suspense } from 'react'
import type { Project } from '../../api/types'

vi.mock('../../components/Sidebar', () => ({
  Sidebar: () => <div data-testid="sidebar" />,
}))
vi.mock('../../components/Header', () => ({
  Header: () => <div data-testid="header" />,
}))

const mockList = vi.fn()
vi.mock('../../api/client', () => ({
  projectsApi: {
    list: (...args: unknown[]) => mockList(...args),
    create: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}))

function baseProject(overrides: Partial<Project>): Project {
  return {
    id: 'proj-a',
    name: 'proj-a',
    repo: 'owner/proj-a',
    health: 'healthy',
    momentum: 'steady',
    createdAt: '2026-01-01T00:00:00Z',
    liveness: 'idle',
    primary: false,
    ...overrides,
  }
}

async function renderProjectList() {
  const ProjectListPage = (await import('../ProjectListPage')).default
  return render(
    <MemoryRouter initialEntries={['/?picker=1']}>
      <Routes>
        <Route
          path="/"
          element={
            <Suspense fallback={<div>Loading…</div>}>
              <ProjectListPage />
            </Suspense>
          }
        />
        <Route path="/project/:id" element={<div data-testid="project-page" />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ProjectListPage — active state', () => {
  it('renders agent count and "newest started", not "last activity"', async () => {
    mockList.mockResolvedValue([
      baseProject({
        id: 'proj-active',
        name: 'proj-active',
        liveness: 'active',
        activeAgents: 3,
        newestStartedAt: new Date(Date.now() - 4 * 60_000).toISOString(),
        roles: ['executor', 'code-reviewer'],
      }),
    ])

    await renderProjectList()

    expect(await screen.findByText(/3 agents running/)).toBeInTheDocument()
    expect(screen.getByText(/newest started/)).toBeInTheDocument()
    expect(screen.queryByText(/last activity/)).not.toBeInTheDocument()
  })

  it('shows up to two role clauses plus a "+N more" for additional agents', async () => {
    mockList.mockResolvedValue([
      baseProject({
        id: 'proj-busy',
        name: 'proj-busy',
        liveness: 'active',
        activeAgents: 3,
        newestStartedAt: new Date().toISOString(),
        roles: ['executor', 'code-reviewer', 'security-reviewer'],
      }),
    ])

    await renderProjectList()

    expect(await screen.findByText('executor running, code-reviewer running, +1 more')).toBeInTheDocument()
  })

  it('renders no "doing" line when the fleet row has no role data', async () => {
    mockList.mockResolvedValue([
      baseProject({
        id: 'proj-active-noroles',
        name: 'proj-active-noroles',
        liveness: 'active',
        activeAgents: 1,
        newestStartedAt: new Date().toISOString(),
        roles: undefined,
      }),
    ])

    await renderProjectList()

    expect(await screen.findByText(/1 agent running/)).toBeInTheDocument()
    expect(screen.queryByText(/running,/)).not.toBeInTheDocument()
  })

  it('gives the status line a role="status" element with a full aria-label', async () => {
    mockList.mockResolvedValue([
      baseProject({
        id: 'proj-active-a11y',
        name: 'proj-active-a11y',
        liveness: 'active',
        activeAgents: 2,
        newestStartedAt: new Date(Date.now() - 90_000).toISOString(),
      }),
    ])

    await renderProjectList()

    const status = await screen.findByRole('status', { name: /2 agents running, newest started/ })
    expect(status).toBeInTheDocument()
  })
})

describe('ProjectListPage — idle state', () => {
  it('renders "No agents running" with a checked-time stamp, and no bare count', async () => {
    mockList.mockResolvedValue([
      baseProject({ id: 'proj-idle', name: 'proj-idle', liveness: 'idle', activeAgents: 0 }),
    ])

    await renderProjectList()

    expect(await screen.findByText(/No agents running/)).toBeInTheDocument()
    expect(await screen.findByText(/checked/)).toBeInTheDocument()
    // Never the old "0 active agents" phrasing.
    expect(screen.queryByText(/0 active agents/)).not.toBeInTheDocument()
  })
})

describe('ProjectListPage — no-signal state', () => {
  it('renders "No signal" copy and never an agent count for an unqueried project', async () => {
    mockList.mockResolvedValue([
      baseProject({ id: 'proj-unknown', name: 'proj-unknown', liveness: 'unknown', activeAgents: undefined }),
    ])

    await renderProjectList()

    expect(await screen.findByText(/No signal/)).toBeInTheDocument()
    expect(screen.getByText(/can't read this project's state dir/)).toBeInTheDocument()
    // No unearned zero, and no leftover "active agents" phrasing of any kind.
    expect(screen.queryByText(/0 agents/)).not.toBeInTheDocument()
    expect(screen.queryByText(/active agents/)).not.toBeInTheDocument()
  })

  it('is distinguishable from idle by more than color: distinct icon and status word', async () => {
    mockList.mockResolvedValue([
      baseProject({ id: 'proj-idle', name: 'proj-idle', liveness: 'idle', activeAgents: 0 }),
      baseProject({ id: 'proj-unknown', name: 'proj-unknown', liveness: 'unknown', activeAgents: undefined }),
    ])

    await renderProjectList()

    expect(await screen.findByText('○', { exact: false })).toBeInTheDocument()
    expect(await screen.findByText('▲', { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/No agents running/)).toBeInTheDocument()
    expect(screen.getByText(/No signal/)).toBeInTheDocument()
  })
})

describe('ProjectListPage — deleted fields', () => {
  it('never renders a momentum line or a separate health/liveness badge', async () => {
    mockList.mockResolvedValue([
      baseProject({ id: 'proj-a', name: 'proj-a', liveness: 'active', activeAgents: 1 }),
    ])

    await renderProjectList()

    await screen.findByText(/1 agent running/)
    expect(screen.queryByText(/Momentum/)).not.toBeInTheDocument()
    expect(screen.queryByText('healthy')).not.toBeInTheDocument()
  })

  it('does not render the LoopRunner widget on the Projects page', async () => {
    mockList.mockResolvedValue([baseProject({})])

    await renderProjectList()

    expect(await screen.findByText('proj-a')).toBeInTheDocument()
    expect(screen.queryByTestId('loop-runner')).not.toBeInTheDocument()
  })
})

describe('ProjectListPage — single-project auto-routing', () => {
  it('still auto-routes on liveness === "active"', async () => {
    mockList.mockResolvedValue([
      baseProject({ id: 'solo', name: 'solo', liveness: 'active', activeAgents: 1 }),
    ])

    const ProjectListPage = (await import('../ProjectListPage')).default
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route
            path="/"
            element={
              <Suspense fallback={<div>Loading…</div>}>
                <ProjectListPage />
              </Suspense>
            }
          />
          <Route path="/project/:id" element={<div data-testid="project-page" />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(await screen.findByTestId('project-page')).toBeInTheDocument()
  })
})
