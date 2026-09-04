import { Suspense, lazy, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, NavLink } from 'react-router-dom'
import BackendUnavailableBanner from './components/BackendUnavailableBanner'
import StaleBanner from './components/StaleBanner'
import OnboardingTour from './components/OnboardingTour'
import { DocumentTitleSync } from './components/DocumentTitleSync'
import { Tooltip } from './components/Tooltip'
import { ProjectBadge } from './components/ProjectBadge'
import { BackendTargetIndicator } from './components/BackendTargetIndicator'
import { BackendTargetProvider } from './context/BackendTargetContext'
import { ActiveProjectProvider, useActiveProject } from './context/ActiveProjectContext'
import { setActiveProjectGetter } from './api/client'
import ProjectListPage from './pages/ProjectListPage'
import ProjectDashboardPage from './pages/ProjectDashboardPage'
import AgentFeedPage from './pages/AgentFeedPage'
import SettingsPage from './pages/SettingsPage'
import IdeasPage from './pages/IdeasPage'
import LoopController from './pages/LoopController'
import DiscussionExplorer from './pages/DiscussionExplorer'

// Route-level code split — Recharts (~200KB) loads only when /kpi is visited
const KpiDetailPage = lazy(() => import('./pages/KpiDetailPage'))
// PR detail page — lazy loaded to keep main bundle small
const PRDetailPage = lazy(() => import('./pages/PRDetailPage'))
// Route-level code split — PR Inspector lazy-loaded chunk
const PrInspectorPage = lazy(() => import('./pages/PrInspectorPage'))
// Loop Timeline page — Recharts charts for loop iteration history
const LoopTimeline = lazy(() => import('./pages/LoopTimeline'))
// Stats page — per-merge metric cards with 7-day sparklines (Discussion #549)
const StatsPage = lazy(() => import('./pages/StatsPage'))
// Runs page — agent run monitoring with duration percentiles and stuck-run alerts
const RunsPage = lazy(() => import('./pages/RunsPage'))
// Fleet page — multi-project observability tab
const FleetPage = lazy(() => import('./pages/FleetPage'))

const navStyle: React.CSSProperties = {
  display: 'flex',
  gap: 16,
  padding: '8px 16px',
  background: '#1f2937',
  fontFamily: 'system-ui, sans-serif',
  fontSize: 13,
}

const linkStyle: React.CSSProperties = {
  color: '#d1d5db',
  textDecoration: 'none',
}

const activeLinkStyle: React.CSSProperties = {
  color: '#fff',
  fontWeight: 600,
}

/**
 * Inner app shell — rendered inside both BrowserRouter and ActiveProjectProvider
 * so the project picker can use routing hooks.
 */
function AppShell() {
  const { activeName } = useActiveProject()

  // Wire the module-level getter so jsonRpc() can read the active project
  // without importing React hooks. This runs once on mount and after every
  // activeName change — the getter captures the latest value via closure.
  useEffect(() => {
    const _latest = activeName
    setActiveProjectGetter(() => _latest)
    // No teardown: getter holds a primitive, no memory leak.
    // The next activeName change will install a fresh getter.
  }, [activeName])

  return (
    <>
      <DocumentTitleSync />
      <OnboardingTour />
      <BackendUnavailableBanner />
      <StaleBanner />
      <nav style={navStyle}>
        <NavLink to="/" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })} end>
          Projects
        </NavLink>
        <NavLink to="/ideas" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          Ideas
        </NavLink>
        <NavLink to="/loop-controller" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          Loop Controller
        </NavLink>
        <NavLink to="/discussions" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          Discussions
        </NavLink>
        <NavLink to="/kpi" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          KPIs
        </NavLink>
        <NavLink to="/prs" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          PRs
        </NavLink>
        <NavLink to="/loop-timeline" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          Loop Timeline
        </NavLink>
        <NavLink to="/stats" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          Stats
        </NavLink>
        <NavLink to="/runs" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          Runs
        </NavLink>
        <NavLink to="/fleet" style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}>
          Fleet
        </NavLink>
        <Tooltip label="Configure dashboard, restart tour" placement="bottom">
          <NavLink
            to="/settings"
            data-tour="settings-nav"
            style={({ isActive }) => ({ ...linkStyle, ...(isActive ? activeLinkStyle : {}) })}
          >
            Settings
          </NavLink>
        </Tooltip>
        {/* Instance identity badge — names the project this dashboard was started against */}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          <BackendTargetIndicator />
          <ProjectBadge />
        </div>
      </nav>
      <Routes>
        <Route path="/" element={<ProjectListPage />} />
        <Route path="/projects" element={<ProjectListPage />} />
        <Route path="/project/:id" element={<ProjectDashboardPage />} />
        <Route path="/project/:id/agents" element={<AgentFeedPage />} />
        <Route path="/project/:id/settings" element={<SettingsPage />} />
        <Route path="/project/:id/kpi" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading KPIs…</div>}>
            <KpiDetailPage />
          </Suspense>
        } />
        <Route path="/kpi" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading KPIs…</div>}>
            <KpiDetailPage />
          </Suspense>
        } />
        <Route path="/ideas" element={<IdeasPage />} />
        <Route path="/loop-controller" element={<LoopController />} />
        <Route path="/discussions" element={<DiscussionExplorer />} />
        <Route path="/prs" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading PRs…</div>}>
            <PrInspectorPage />
          </Suspense>
        } />
        <Route path="/pr/:number" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading PR…</div>}>
            <PRDetailPage />
          </Suspense>
        } />
        <Route path="/loop-timeline" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading Loop Timeline…</div>}>
            <LoopTimeline />
          </Suspense>
        } />
        <Route path="/stats" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading Stats…</div>}>
            <StatsPage />
          </Suspense>
        } />
        <Route path="/runs" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading Runs…</div>}>
            <RunsPage />
          </Suspense>
        } />
        <Route path="/fleet" element={
          <Suspense fallback={<div style={{ color: '#9ca3af', padding: 24 }}>Loading Fleet…</div>}>
            <FleetPage />
          </Suspense>
        } />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <BackendTargetProvider>
        <ActiveProjectProvider>
          <AppShell />
        </ActiveProjectProvider>
      </BackendTargetProvider>
    </BrowserRouter>
  )
}
