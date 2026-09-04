import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

vi.mock('../../api/client', () => ({
  projectsApi: {
    list: vi.fn(),
  },
}))

vi.mock('../../context/ActiveProjectContext', () => ({
  useActiveProject: vi.fn(),
}))

import { projectsApi } from '../../api/client'
import { useActiveProject } from '../../context/ActiveProjectContext'
import { useActiveRepo } from '../useActiveRepo'

const mockList = vi.mocked(projectsApi.list)
const mockUseActiveProject = vi.mocked(useActiveProject)

const PROJECTS = [
  { id: 'gatekeep', name: 'gatekeep', repo: 'fulcrumaxe/gatekeep' },
  { id: 'other', name: 'other', repo: 'x/y' },
]

function mockActiveName(activeName: string | null) {
  mockUseActiveProject.mockReturnValue({
    activeName,
    setActive: vi.fn(),
    loading: false,
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useActiveRepo', () => {
  it('resolves the active project\'s repo slug', async () => {
    mockList.mockResolvedValue(PROJECTS as never)
    mockActiveName('gatekeep')

    const { result } = renderHook(() => useActiveRepo())

    await waitFor(() => expect(result.current).toBe('fulcrumaxe/gatekeep'))
  })

  it('resolves a different active project\'s repo slug', async () => {
    mockList.mockResolvedValue(PROJECTS as never)
    mockActiveName('other')

    const { result } = renderHook(() => useActiveRepo())

    await waitFor(() => expect(result.current).toBe('x/y'))
  })

  it('returns null when the project list fetch rejects', async () => {
    mockList.mockRejectedValue(new Error('network error'))
    mockActiveName('gatekeep')

    const { result } = renderHook(() => useActiveRepo())

    await waitFor(() => expect(mockList).toHaveBeenCalled())
    expect(result.current).toBeNull()
  })
})
