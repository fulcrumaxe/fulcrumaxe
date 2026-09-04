/**
 * ProjectListTile tests — D#2317 PR-a item 10.
 *
 * Covers the three UI checks the Spec calls out explicitly:
 *   - a "unknown" status renders the shared amber token and its visible
 *     text is not "ok"
 *   - the dashboard link renders ONLY when status === "ok" (this is what
 *     stops a dead/unprobed row from linking to a live, unrelated app)
 *   - an absent agents_running renders "—", never "0"
 *
 * All RPC calls are mocked. No backend or network access.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import ProjectListTile from '../ProjectListTile'

const mockJsonRpc = vi.mocked(jsonRpc)

function respond(projects: unknown[]) {
  mockJsonRpc.mockImplementation(() => Promise.resolve({ projects, etag: 'x' }))
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ProjectListTile — status rendering', () => {
  it('renders the amber "unknown" token, not "ok"', async () => {
    respond([{ name: 'gatekeep', status: 'unknown' }])

    render(<ProjectListTile />)

    await waitFor(() => {
      const badge = screen.getByText('unknown')
      expect(badge).toBeTruthy()
      expect((badge as HTMLElement).style.color).toBe('rgb(217, 119, 6)') // #d97706
    })
    expect(screen.queryByText('ok')).toBeNull()
  })

  it('renders a dashboard link only when status is "ok"', async () => {
    respond([
      { name: 'up', dashboard_port: 5100, status: 'ok' },
      { name: 'down', dashboard_port: 5101, status: 'down' },
      { name: 'unmeasured', dashboard_port: 5102, status: 'unknown' },
      { name: 'broken', status: 'error', error: 'boom' },
    ])

    render(<ProjectListTile />)

    await waitFor(() => {
      expect(screen.getByText('up')).toBeTruthy()
    })

    const links = screen.getAllByRole('link')
    expect(links).toHaveLength(1)
    expect(links[0].getAttribute('href')).toBe('http://localhost:5100')
  })

  it('renders no link at all when every project is non-ok', async () => {
    respond([{ name: 'flaky-project', dashboard_port: 5101, status: 'down' }])

    render(<ProjectListTile />)

    await waitFor(() => {
      expect(screen.getByText('flaky-project')).toBeTruthy()
    })
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('renders "—" for agents_running when the field is absent, never "0"', async () => {
    respond([{ name: 'no-signal', status: 'unknown' }])

    render(<ProjectListTile />)

    await waitFor(() => {
      expect(screen.getByText('—', { selector: 'td' })).toBeTruthy()
    })
    expect(screen.queryByText('0')).toBeNull()
  })

  it('renders the real number when agents_running is present', async () => {
    respond([{ name: 'busy', status: 'ok', dashboard_port: 5100, agents_running: 3 }])

    render(<ProjectListTile />)

    await waitFor(() => {
      expect(screen.getByText('3')).toBeTruthy()
    })
  })
})
