/**
 * FleetPage tests — verifies all 3 tiles render, error states, and new-project toast.
 *
 * All RPC calls and localStorage are mocked. No backend or network access.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

// Mock the jsonRpc client
vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
  getRpcBaseUrl: vi.fn(() => 'http://localhost:8765'),
  getRpcToken: vi.fn(() => 'test-token'),
}))

// Mock localStorage
const localStorageMock = (() => {
  let store: Record<string, string> = {}
  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => { store[key] = value },
    removeItem: (key: string) => { delete store[key] },
    clear: () => { store = {} },
  }
})()
Object.defineProperty(window, 'localStorage', { value: localStorageMock })

import { jsonRpc } from '../../../api/client'
import FleetPage from '../../FleetPage'

const mockJsonRpc = vi.mocked(jsonRpc)

function makeFleetProjectsResponse(projects = [
  { name: 'autonomous-forever', dashboard_port: 5173, status: 'ok' },
  { name: 'projectb', dashboard_port: 5100, status: 'ok' },
]) {
  return { projects, etag: 'abc123' }
}

function makeFleetCostResponse() {
  return {
    total_today_utc: 15000,
    total_7d: 60000,
    projected_eod: 20000,
    per_project: [
      { name: 'autonomous-forever', tokens_today_utc: 10000, tokens_7d: 40000, projected_eod_tokens: 14000, ok: true },
      { name: 'projectb', tokens_today_utc: 5000, tokens_7d: 20000, projected_eod_tokens: 6000, ok: true },
    ],
    etag: 'def456',
  }
}

function makeFleetConcurrencyResponse() {
  return {
    fleet_total: 3,
    fleet_cap: 8,
    per_project: [
      { name: 'autonomous-forever', agents_running: 2, cap: 4, ok: true },
      { name: 'projectb', agents_running: 1, cap: 4, ok: true },
    ],
    etag: 'ghi789',
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorageMock.clear()
  // Default: all RPCs return valid data
  mockJsonRpc.mockImplementation((method: string) => {
    if (method === 'fleet.projects') return Promise.resolve(makeFleetProjectsResponse())
    if (method === 'fleet.cost') return Promise.resolve(makeFleetCostResponse())
    if (method === 'fleet.concurrency') return Promise.resolve(makeFleetConcurrencyResponse())
    if (method === 'fleet.discovery_ack') return Promise.resolve({ ok: true, known: [] })
    return Promise.resolve({})
  })
})

afterEach(() => {
  vi.clearAllTimers()
})

// ---------------------------------------------------------------------------
// AC: All 3 tiles render
// ---------------------------------------------------------------------------

describe('FleetPage — tile rendering', () => {
  it('renders all 3 tiles', async () => {
    render(<FleetPage />)

    await waitFor(() => {
      expect(screen.getByTestId('project-list-tile')).toBeTruthy()
      expect(screen.getByTestId('fleet-cost-tile')).toBeTruthy()
      expect(screen.getByTestId('fleet-concurrency-tile')).toBeTruthy()
    })
  })

  it('ProjectListTile shows 2 projects', async () => {
    render(<FleetPage />)

    await waitFor(() => {
      // getAllByText handles multiple matches (project name appears in toast + table)
      expect(screen.getAllByText('autonomous-forever').length).toBeGreaterThan(0)
      expect(screen.getAllByText('projectb').length).toBeGreaterThan(0)
    })
  })

  it('FleetCostTile shows today, 7d, projected EOD numbers', async () => {
    render(<FleetPage />)

    await waitFor(() => {
      // 15000 tokens → "15K"
      const costTile = screen.getByTestId('fleet-cost-tile')
      expect(costTile.textContent).toContain('15K')
      expect(costTile.textContent).toContain('60K')
    })
  })

  it('FleetConcurrencyTile shows N of 8', async () => {
    render(<FleetPage />)

    await waitFor(() => {
      const concTile = screen.getByTestId('fleet-concurrency-tile')
      expect(concTile.textContent).toContain('3')
      expect(concTile.textContent).toContain('8')
    })
  })
})

// ---------------------------------------------------------------------------
// AC: status "error" project renders inline error
// ---------------------------------------------------------------------------

describe('ProjectListTile — error state', () => {
  it('shows broken project with error message in red', async () => {
    const projectsWithError = [
      { name: 'autonomous-forever', dashboard_port: 5173, status: 'ok' },
      { name: 'broken', status: 'error', error: 'JSON parse error: unexpected token' },
    ]
    mockJsonRpc.mockImplementation((method: string) => {
      if (method === 'fleet.projects') return Promise.resolve({ projects: projectsWithError, etag: 'x' })
      if (method === 'fleet.cost') return Promise.resolve(makeFleetCostResponse())
      if (method === 'fleet.concurrency') return Promise.resolve(makeFleetConcurrencyResponse())
      return Promise.resolve({})
    })

    render(<FleetPage />)

    await waitFor(() => {
      expect(screen.getByText('broken')).toBeTruthy()
      expect(screen.getByText('JSON parse error: unexpected token')).toBeTruthy()
    })
  })

  it('good projects still visible when one is broken', async () => {
    const projectsWithError = [
      { name: 'good', dashboard_port: 5173, status: 'ok' },
      { name: 'broken', status: 'error', error: 'IO error' },
    ]
    mockJsonRpc.mockImplementation((method: string) => {
      if (method === 'fleet.projects') return Promise.resolve({ projects: projectsWithError, etag: 'x' })
      if (method === 'fleet.cost') return Promise.resolve(makeFleetCostResponse())
      if (method === 'fleet.concurrency') return Promise.resolve(makeFleetConcurrencyResponse())
      return Promise.resolve({})
    })

    render(<FleetPage />)

    await waitFor(() => {
      expect(screen.getByText('good')).toBeTruthy()
      expect(screen.getByText('broken')).toBeTruthy()
    })
  })
})

// ---------------------------------------------------------------------------
// AC: new-project toast
// ---------------------------------------------------------------------------

describe('NewProjectToast', () => {
  it('shows banner for unseen project when known list is empty', async () => {
    // Empty localStorage = no known projects
    localStorageMock.clear()

    render(<FleetPage />)

    await waitFor(() => {
      // The toast should appear since autonomous-forever and projectb are new
      expect(screen.getByTestId('new-project-toast')).toBeTruthy()
    })
  })

  it('does not show banner when all projects are already known', async () => {
    // Pre-populate localStorage with all projects
    localStorageMock.setItem(
      'fleet_known_projects',
      JSON.stringify(['autonomous-forever', 'projectb']),
    )

    render(<FleetPage />)

    // Wait for tiles to load
    await waitFor(() => {
      expect(screen.getByTestId('project-list-tile')).toBeTruthy()
    })

    // Banner should not appear
    expect(screen.queryByTestId('new-project-toast')).toBeNull()
  })
})
