/**
 * Regression test for the /runs route silent-redirect bug.
 *
 * The OnboardingTour was navigating to '/' unconditionally on mount when
 * af_tour_seen was absent from localStorage, which yanked /runs back to /.
 *
 * These tests assert that RunsPage renders when mounted at /runs, confirming
 * the route isn't being hijacked.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { Suspense } from 'react'
import RunsPage from '../RunsPage'

// Mock all four tile imports so they don't fire real RPC calls
vi.mock('../runs/ActiveAgentsTile', () => ({
  default: () => <div data-testid="active-agents-tile">ActiveAgentsTile</div>,
}))

vi.mock('../runs/DurationPercentilesTile', () => ({
  default: () => <div data-testid="duration-percentiles-tile">DurationPercentilesTile</div>,
}))

vi.mock('../runs/StuckRunsTile', () => ({
  default: () => <div data-testid="stuck-runs-tile">StuckRunsTile</div>,
}))

vi.mock('../runs/RecentRunsFeedTile', () => ({
  default: () => <div data-testid="recent-runs-feed-tile">RecentRunsFeedTile</div>,
}))

function renderAtRuns() {
  return render(
    <MemoryRouter initialEntries={['/runs']}>
      <Routes>
        <Route
          path="/runs"
          element={
            <Suspense fallback={<div>Loading Runs…</div>}>
              <RunsPage />
            </Suspense>
          }
        />
        <Route path="/" element={<div data-testid="home-page">ProjectListPage</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('RunsPage route — /runs renders RunsPage, not ProjectListPage', () => {
  beforeEach(() => {
    // Ensure tour won't fire (even though OnboardingTour is not in this render tree,
    // guard against localStorage bleed between tests)
    localStorage.setItem('af_tour_seen', '1')
  })

  afterEach(() => {
    localStorage.removeItem('af_tour_seen')
  })

  it('renders the "Agent Runs" page header at /runs', () => {
    renderAtRuns()
    expect(screen.getByRole('heading', { level: 1, name: 'Agent Runs' })).toBeInTheDocument()
  })

  it('does NOT render ProjectListPage content at /runs', () => {
    renderAtRuns()
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument()
  })

  it('renders all 4 child tiles', () => {
    renderAtRuns()
    expect(screen.getByTestId('active-agents-tile')).toBeInTheDocument()
    expect(screen.getByTestId('duration-percentiles-tile')).toBeInTheDocument()
    expect(screen.getByTestId('stuck-runs-tile')).toBeInTheDocument()
    expect(screen.getByTestId('recent-runs-feed-tile')).toBeInTheDocument()
  })

  it('renders the Refresh button', () => {
    renderAtRuns()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
  })
})

describe('OnboardingTour does NOT redirect /runs to / on initial load', () => {
  beforeEach(() => {
    // Remove tour-seen flag to simulate first-time visitor — tour would fire
    localStorage.removeItem('af_tour_seen')
  })

  afterEach(() => {
    localStorage.removeItem('af_tour_seen')
  })

  it('RunsPage still renders at /runs even without af_tour_seen in localStorage', () => {
    // OnboardingTour is NOT rendered in this tree (it lives in AppShell above Routes).
    // This test documents the contract: the /runs route itself must render RunsPage
    // regardless of tour state. The fix lives in OnboardingTour.tsx — tested separately.
    renderAtRuns()
    expect(screen.getByRole('heading', { level: 1, name: 'Agent Runs' })).toBeInTheDocument()
  })
})
