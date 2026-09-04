/**
 * RunsPage — agent run monitoring at /runs.
 *
 * Thin layout shell. Each tile owns its data fetching.
 * Adding a new metric = add a new file in ./runs/, then import and
 * render it here (registration only, no business logic).
 *
 * Tiles:
 *   ActiveAgentsTile      — concurrent active agents over time
 *   DurationPercentilesTile — p50/p95 by role
 *   StuckRunsTile         — runs >30 min with no end_ts
 *   RecentRunsFeedTile    — last 50 completed runs
 */

import type React from 'react'
import { useState } from 'react'
import ActiveAgentsTile from './runs/ActiveAgentsTile'
import DurationPercentilesTile from './runs/DurationPercentilesTile'
import StuckRunsTile from './runs/StuckRunsTile'
import RecentRunsFeedTile from './runs/RecentRunsFeedTile'
import SdkVsCcTile from './runs/SdkVsCcTile'
import AnalystFindingsTile from './runs/AnalystFindingsTile'

const pageStyles: Record<string, React.CSSProperties> = {
  container: {
    padding: '24px 32px',
    fontFamily: 'system-ui, sans-serif',
    background: '#0d1117',
    minHeight: '100vh',
    color: '#f9fafb',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 24,
  },
  title: {
    margin: 0,
    fontSize: 24,
    fontWeight: 700,
    color: '#f9fafb',
  },
  refreshBtn: {
    padding: '6px 14px',
    background: '#1f2937',
    color: '#d1d5db',
    border: '1px solid #374151',
    borderRadius: 6,
    fontSize: 13,
    cursor: 'pointer',
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 24,
  },
  fullWidth: {
    gridColumn: '1 / -1',
  },
}

export default function RunsPage() {
  const [refreshSignal, setRefreshSignal] = useState(0)

  return (
    <div style={pageStyles.container}>
      <div style={pageStyles.header}>
        <h1 style={pageStyles.title}>Agent Runs</h1>
        <button
          style={pageStyles.refreshBtn}
          onClick={() => setRefreshSignal(s => s + 1)}
        >
          Refresh
        </button>
      </div>

      <div style={pageStyles.grid}>
        {/* Full-width: active agents chart */}
        <div style={pageStyles.fullWidth}>
          <ActiveAgentsTile refreshSignal={refreshSignal} />
        </div>

        {/* Side by side: percentiles and stuck runs */}
        <DurationPercentilesTile refreshSignal={refreshSignal} />
        <StuckRunsTile refreshSignal={refreshSignal} />

        {/* Full-width: SDK vs CC comparison */}
        <div style={pageStyles.fullWidth}>
          <SdkVsCcTile refreshSignal={refreshSignal} />
        </div>

        {/* Full-width: recent runs feed */}
        <div style={pageStyles.fullWidth}>
          <RecentRunsFeedTile refreshSignal={refreshSignal} />
        </div>

        {/* Full-width: analyst findings */}
        <div style={pageStyles.fullWidth}>
          <AnalystFindingsTile refreshSignal={refreshSignal} />
        </div>
      </div>
    </div>
  )
}
