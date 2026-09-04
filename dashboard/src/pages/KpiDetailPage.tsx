/**
 * KpiDetailPage — Team KPIs dashboard.
 *
 * This file is route-split via React.lazy in App.tsx so Recharts is not
 * pulled into the main bundle.
 *
 * Three chart sections:
 *   - velocity-chart   : merged-PRs-per-day line chart
 *   - cycle-time-chart : PR cycle-time histogram
 *   - cost-chart       : top-10 discussions by spend
 *
 * Each section is a self-contained component that handles its own
 * loading / empty / error states.
 *
 * The 7d/30d/90d range toggle is a single page-level control that applies
 * to all three sibling charts (Req #3).
 */

import { useState, useEffect } from 'react'
import VelocityChart from './kpi/VelocityChart'
import CycleTimeChart from './kpi/CycleTimeChart'
import CostChart from './kpi/CostChart'
import { LastUpdated } from '../components/LastUpdated'

const WINDOWS = [7, 30, 90] as const
type Window = (typeof WINDOWS)[number]

export default function KpiDetailPage() {
  const [days, setDays] = useState<Window>(30)
  const [fetchedAt, setFetchedAt] = useState<string>(() => new Date().toISOString())

  // Refresh timestamp whenever days changes (charts re-fetch immediately)
  // and every 60s to stay current while the page is open.
  useEffect(() => {
    setFetchedAt(new Date().toISOString())
    const t = setInterval(() => setFetchedAt(new Date().toISOString()), 60_000)
    return () => clearInterval(t)
  }, [days])

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <div style={styles.headerLeft}>
          <h1 style={styles.heading}>Team KPIs</h1>
          <p style={styles.subtitle}>Velocity, cycle time, and cost at a glance.</p>
        </div>
        <div style={styles.headerRight}>
          <LastUpdated fetchedAt={fetchedAt} />
          <div style={styles.windowButtons}>
            {WINDOWS.map(w => (
              <button
                key={w}
                data-window={`${w}d`}
                onClick={() => setDays(w)}
                style={days === w ? styles.activeBtn : styles.btn}
                aria-pressed={days === w}
              >
                {w}d
              </button>
            ))}
          </div>
        </div>
      </header>

      <div style={styles.charts}>
        <VelocityChart defaultDays={days} days={days} />
        <CycleTimeChart days={days} />
        <CostChart days={days} />
      </div>
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    background: '#0f172a',
    minHeight: '100vh',
    padding: '32px 24px',
    fontFamily: 'system-ui, sans-serif',
    color: '#f9fafb',
    boxSizing: 'border-box',
  },
  header: {
    marginBottom: 28,
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    flexWrap: 'wrap',
    gap: 12,
  },
  headerLeft: { flex: 1 },
  headerRight: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'flex-end',
    gap: 8,
  },
  heading: { margin: 0, fontSize: 24, fontWeight: 700, color: '#f9fafb' },
  subtitle: { margin: '6px 0 0', fontSize: 14, color: '#6b7280' },
  charts: { maxWidth: 900 },
  windowButtons: { display: 'flex', gap: 6 },
  btn: {
    background: '#1f2937',
    color: '#9ca3af',
    border: '1px solid #374151',
    borderRadius: 4,
    padding: '3px 10px',
    cursor: 'pointer',
    fontSize: 12,
  },
  activeBtn: {
    background: '#3b82f6',
    color: '#fff',
    border: '1px solid #3b82f6',
    borderRadius: 4,
    padding: '3px 10px',
    cursor: 'pointer',
    fontSize: 12,
    fontWeight: 600,
  },
}
