/**
 * DocumentTitleSync.test.tsx
 *
 * D#2316 finding 5: dashboard/index.html hardcoded <title>Autonomous
 * Forever</title> regardless of which project the instance served. This
 * component derives document.title from the same source the nav-bar
 * ProjectBadge already reads (useActiveProjectName()).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { DocumentTitleSync, FALLBACK_TITLE } from '../DocumentTitleSync'

vi.mock('../../context/ActiveProjectContext', () => ({
  useActiveProjectName: vi.fn(),
}))

import { useActiveProjectName } from '../../context/ActiveProjectContext'

const mockUseActiveProjectName = vi.mocked(useActiveProjectName)

describe('DocumentTitleSync', () => {
  const originalTitle = document.title

  beforeEach(() => {
    document.title = 'unset'
  })

  afterEach(() => {
    document.title = originalTitle
  })

  it('never leaves the hardcoded pre-rename product name on the tab', () => {
    mockUseActiveProjectName.mockReturnValue('fulcrumaxe')
    render(<DocumentTitleSync />)
    expect(document.title).not.toContain('Autonomous Forever')
  })

  it('uses the static fallback while no project has resolved yet', () => {
    mockUseActiveProjectName.mockReturnValue(null)
    render(<DocumentTitleSync />)
    expect(document.title).toBe(FALLBACK_TITLE)
  })

  it('reflects a non-default project name once resolved', () => {
    mockUseActiveProjectName.mockReturnValue('fulcrumaxe')
    render(<DocumentTitleSync />)
    expect(document.title).toContain('fulcrumaxe')
    expect(document.title).not.toBe(FALLBACK_TITLE)
  })

  it('updates the title when the active project changes', () => {
    mockUseActiveProjectName.mockReturnValue('project-a')
    const { rerender } = render(<DocumentTitleSync />)
    expect(document.title).toContain('project-a')

    mockUseActiveProjectName.mockReturnValue('project-b')
    rerender(<DocumentTitleSync />)
    expect(document.title).toContain('project-b')
  })

  it('renders nothing', () => {
    mockUseActiveProjectName.mockReturnValue('fulcrumaxe')
    const { container } = render(<DocumentTitleSync />)
    expect(container.firstChild).toBeNull()
  })
})
