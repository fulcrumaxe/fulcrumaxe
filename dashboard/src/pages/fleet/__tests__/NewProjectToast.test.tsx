/**
 * NewProjectToast tests — D#2317 PR-a item 11.
 *
 * Covers:
 *   - detectNewProjects consults the backend's persisted known list
 *     (fleet.discovery_known) rather than only localStorage — with the
 *     backend reporting every live name as already known and localStorage
 *     empty, the component renders null.
 *   - the banner is a polite region (role="status"), never role="alert".
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import NewProjectToast from '../NewProjectToast'

const mockJsonRpc = vi.mocked(jsonRpc)

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

beforeEach(() => {
  vi.clearAllMocks()
  localStorageMock.clear()
})

describe('NewProjectToast — backend is the source of truth', () => {
  it('renders null when the backend already knows every live project, even with empty localStorage', async () => {
    mockJsonRpc.mockImplementation((method: string) => {
      if (method === 'fleet.discovery_known') {
        return Promise.resolve({ known: ['fulcrumaxe', 'projectb'] })
      }
      return Promise.resolve({ ok: true, known: [] })
    })

    render(<NewProjectToast projectNames={['fulcrumaxe', 'projectb']} />)

    await waitFor(() => {
      expect(mockJsonRpc).toHaveBeenCalledWith('fleet.discovery_known', {})
    })
    expect(screen.queryByTestId('new-project-toast')).toBeNull()
  })

  it('still announces a project the backend does not know about', async () => {
    mockJsonRpc.mockImplementation((method: string) => {
      if (method === 'fleet.discovery_known') {
        return Promise.resolve({ known: ['fulcrumaxe'] })
      }
      return Promise.resolve({ ok: true, known: [] })
    })

    render(<NewProjectToast projectNames={['fulcrumaxe', 'projectb']} />)

    await waitFor(() => {
      expect(screen.getByTestId('new-project-toast')).toBeTruthy()
    })
  })

  it('uses role="status", never role="alert"', async () => {
    mockJsonRpc.mockImplementation((method: string) => {
      if (method === 'fleet.discovery_known') return Promise.resolve({ known: [] })
      return Promise.resolve({ ok: true, known: [] })
    })

    render(<NewProjectToast projectNames={['new-project']} />)

    await waitFor(() => {
      const banner = screen.getByTestId('new-project-toast')
      expect(banner.getAttribute('role')).toBe('status')
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('falls back to the localStorage cache when the backend call fails', async () => {
    localStorageMock.setItem('fleet_known_projects', JSON.stringify(['fulcrumaxe', 'projectb']))
    mockJsonRpc.mockImplementation((method: string) => {
      if (method === 'fleet.discovery_known') return Promise.reject(new Error('network error'))
      return Promise.resolve({ ok: true, known: [] })
    })

    render(<NewProjectToast projectNames={['fulcrumaxe', 'projectb']} />)

    await waitFor(() => {
      expect(mockJsonRpc).toHaveBeenCalledWith('fleet.discovery_known', {})
    })
    expect(screen.queryByTestId('new-project-toast')).toBeNull()
  })
})
