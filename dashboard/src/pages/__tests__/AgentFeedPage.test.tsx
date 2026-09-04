import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import AgentFeedPage from '../AgentFeedPage'
import { useWebSocket } from '../../hooks/useWebSocket'
import { useAuth } from '../../auth/AuthContext'
import type { WsEvent } from '../../api/types'

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
  Header: ({ connected }: { connected?: boolean }) => (
    <header data-testid="header" data-connected={connected} />
  ),
}))

const mockEvents: WsEvent[] = [
  {
    event: 'agent.started',
    timestamp: '2026-04-10T09:00:00Z',
    role: 'executor',
    content: 'Starting implementation',
    projectId: 'proj-1',
  },
  {
    event: 'agent.output',
    timestamp: '2026-04-10T09:01:00Z',
    role: 'code-reviewer',
    content: 'Reviewing PR #42',
    projectId: 'proj-1',
  },
  {
    event: 'agent.done',
    timestamp: '2026-04-10T09:02:00Z',
    role: 'executor',
    content: 'PR created',
    projectId: 'proj-1',
  },
]

describe('AgentFeedPage', () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReturnValue({
      session: { id: 's1', userId: 'u1', username: 'testuser', avatarUrl: '', createdAt: '', expiresAt: '' },
      token: 'tok',
      loading: false,
      login: vi.fn(),
      logout: vi.fn(),
    })
  })

  function renderPage(events: WsEvent[] = mockEvents, projectId = 'proj-1') {
    vi.mocked(useWebSocket).mockReturnValue({ events, connected: true })
    return render(
      <MemoryRouter initialEntries={[`/project/${projectId}/agents`]}>
        <Routes>
          <Route path="/project/:id/agents" element={<AgentFeedPage />} />
        </Routes>
      </MemoryRouter>
    )
  }

  it('renders events from WebSocket', () => {
    renderPage()
    expect(screen.getByText('Starting implementation')).toBeInTheDocument()
    expect(screen.getByText('Reviewing PR #42')).toBeInTheDocument()
    expect(screen.getByText('PR created')).toBeInTheDocument()
  })

  it('shows empty state with no events', () => {
    renderPage([])
    expect(screen.getByTestId('agent-feed-empty')).toBeInTheDocument()
  })

  it('shows role filter dropdown', () => {
    renderPage()
    expect(screen.getByLabelText(/Role/i)).toBeInTheDocument()
  })

  it('filters events by role', () => {
    renderPage()
    const select = screen.getByRole('combobox')
    fireEvent.change(select, { target: { value: 'executor' } })
    // Only executor events should show
    expect(screen.getByText('Starting implementation')).toBeInTheDocument()
    expect(screen.getByText('PR created')).toBeInTheDocument()
    expect(screen.queryByText('Reviewing PR #42')).not.toBeInTheDocument()
  })

  it('shows all events when filter is "all"', () => {
    renderPage()
    const select = screen.getByRole('combobox')
    fireEvent.change(select, { target: { value: 'executor' } })
    fireEvent.change(select, { target: { value: 'all' } })
    expect(screen.getByText('Reviewing PR #42')).toBeInTheDocument()
  })

  it('renders feed as role=log for accessibility', () => {
    renderPage()
    expect(screen.getByRole('log')).toBeInTheDocument()
  })
})
