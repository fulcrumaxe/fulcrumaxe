import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import SettingsPage from '../SettingsPage'
import { controlApi } from '../../api/client'

vi.mock('../../api/client', () => ({
  controlApi: {
    getSettings: vi.fn(),
    getAudit: vi.fn(),
    updateSettings: vi.fn(),
  },
}))

vi.mock('../../components/Sidebar', () => ({
  Sidebar: () => <nav data-testid="sidebar" />,
}))

vi.mock('../../components/Header', () => ({
  Header: () => <header data-testid="header" />,
}))

const mockGetSettings = controlApi.getSettings as ReturnType<typeof vi.fn>
const mockGetAudit = controlApi.getAudit as ReturnType<typeof vi.fn>

describe('SettingsPage', () => {
  beforeEach(() => {
    mockGetSettings.mockReset()
    mockGetAudit.mockReset()
    mockGetSettings.mockResolvedValue({
      autoMerge: true,
      requireSecurityReview: false,
      maxConcurrentAgents: 3,
      qualityGateThreshold: 0.8,
    })
    mockGetAudit.mockResolvedValue([])
  })

  it('project-scoped route (/project/:id/settings) calls controlApi with the project id', async () => {
    render(
      <MemoryRouter initialEntries={['/project/proj-1/settings']}>
        <Routes>
          <Route path="/project/:id/settings" element={<SettingsPage />} />
        </Routes>
      </MemoryRouter>
    )

    await waitFor(() => expect(mockGetSettings).toHaveBeenCalledWith('proj-1'))
    expect(mockGetAudit).toHaveBeenCalledWith('proj-1')
    expect(await screen.findByText('Control Plane')).toBeInTheDocument()
  })

  it('global route (/settings) never calls controlApi — no malformed /control request', async () => {
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </MemoryRouter>
    )

    // Onboarding card (project-independent) still renders.
    expect(await screen.findByText('Onboarding')).toBeInTheDocument()

    expect(mockGetSettings).not.toHaveBeenCalled()
    expect(mockGetAudit).not.toHaveBeenCalled()
  })
})
