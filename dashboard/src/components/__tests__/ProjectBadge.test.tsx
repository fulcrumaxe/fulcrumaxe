/**
 * ProjectBadge.test.tsx
 *
 * ProjectBadge renders the active project's name (sourced from
 * ActiveProjectContext, which itself resolves from /api/projects — see
 * D#2239) and nothing else — no dropdown, no switcher.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ProjectBadge } from '../ProjectBadge'

vi.mock('../../context/ActiveProjectContext', () => ({
  useActiveProjectName: vi.fn(),
}))

import { useActiveProjectName } from '../../context/ActiveProjectContext'

const mockUseActiveProjectName = vi.mocked(useActiveProjectName)

describe('ProjectBadge', () => {
  it('renders the primary project\'s name', () => {
    mockUseActiveProjectName.mockReturnValue('gatekeep')
    render(<ProjectBadge />)
    expect(screen.getByText('gatekeep')).toBeTruthy()
  })

  it('renders nothing while /api/projects is still loading', () => {
    mockUseActiveProjectName.mockReturnValue(null)
    const { container } = render(<ProjectBadge />)
    expect(container.firstChild).toBeNull()
  })
})
