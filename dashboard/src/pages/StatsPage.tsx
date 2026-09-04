/**
 * StatsPage — Team metrics dashboard at /stats.
 *
 * Thin layout shell. Each tile component owns its own data fetching,
 * state, and styles. Adding a new metric = add a new file in ./stats/,
 * then import and render it here (registration only, no business logic).
 *
 * Tile registry: dashboard/src/pages/stats/index.ts
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../api/client'
import MetricSparkline from '../components/MetricSparkline'
import { LastUpdated } from '../components/LastUpdated'
import { tileRegistry } from './stats/registry'

// All known metric names — Phase 1 (4) + Phase 2 (3 confirmed per-merge, plus 4 pre-existing)
// Phase 3 adds loop-health metrics; this list defines sort order for display.
//
// PARITY NOTE: This list is duplicated in backend/stats/metric_order.py (Python).
// If you add or reorder entries here, update that file too (and vice versa).
const METRIC_ORDER = [
  'loop_iteration_duration_seconds',
  'time_to_merge_seconds',
  'fix_cycle_count',
  'spec_to_first_pr_latency_seconds',
  'reviewer_acceptance_latency_seconds',
  'acceptance_criteria_pass_rate',
  'cost_per_merged_pr_usd',
  'cost_attribution_unresolved_count',
  'pr_file_conflict_score',
  'scan_to_spawn_ratio',
  'orphan_worktree_rate',
  'interventions_per_agent_avg',
  'interventions_per_classifier',
  'intervention_to_self_correction_rate',
]

interface MetricEntry {
  name: string
  value: number
  unit: string
  updated_at_iso: string
}

interface SeriesPoint {
  ts_iso: string
  value: number
}

interface SummaryResponse {
  metrics: MetricEntry[]
}

interface SeriesResponse {
  name: string
  points: SeriesPoint[]
}

type SeriesMap = Record<string, SeriesPoint[]>

const SINCE_HOURS = 168 // 7 days

function sortedMetrics(metrics: MetricEntry[]): MetricEntry[] {
  const ordered: MetricEntry[] = []
  const byName: Record<string, MetricEntry> = {}
  for (const m of metrics) byName[m.name] = m
  for (const name of METRIC_ORDER) {
    if (byName[name]) ordered.push(byName[name])
  }
  // Append any metrics not in the preferred order list
  for (const m of metrics) {
    if (!METRIC_ORDER.includes(m.name)) ordered.push(m)
  }
  return ordered
}

export default function StatsPage() {
  const [metrics, setMetrics] = useState<MetricEntry[]>([])
  const [series, setSeries] = useState<SeriesMap>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [fetchedAt, setFetchedAt] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  // Signal tiles to refresh — incremented on manual refresh click
  const [refreshSignal, setRefreshSignal] = useState(0)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchMetricGrid = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true)
    setError(null)
    try {
      const summaryResp = await jsonRpc<SummaryResponse>('stats.summary', {})
      const fetched = summaryResp.metrics ?? []
      setMetrics(fetched)

      // Fetch 7-day series for each metric in parallel
      const seriesResults = await Promise.allSettled(
        fetched.map(m =>
          jsonRpc<SeriesResponse>('stats.series', { name: m.name, since_hours: SINCE_HOURS })
        )
      )
      const newSeries: SeriesMap = {}
      for (let i = 0; i < fetched.length; i++) {
        const r = seriesResults[i]
        newSeries[fetched[i].name] = r.status === 'fulfilled' ? (r.value.points ?? []) : []
      }
      setSeries(newSeries)
      setFetchedAt(new Date().toISOString())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    fetchMetricGrid()
    intervalRef.current = setInterval(() => fetchMetricGrid(), 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [fetchMetricGrid])

  const handleRefresh = useCallback(() => {
    fetchMetricGrid(true)
    setRefreshSignal(n => n + 1)
  }, [fetchMetricGrid])

  const sorted = sortedMetrics(metrics)

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <div style={styles.headerLeft}>
          <h1 style={styles.heading}>Team Stats</h1>
          <p style={styles.subtitle}>
            Per-merge metrics — {sorted.length} of 12 populated
          </p>
        </div>
        <div style={styles.headerRight}>
          <LastUpdated fetchedAt={fetchedAt} />
          <button
            onClick={handleRefresh}
            disabled={refreshing || loading}
            style={refreshing || loading ? styles.btnDisabled : styles.btn}
            data-testid="stats-refresh-button"
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      {loading && !metrics.length && (
        <div style={styles.state}>Loading metrics…</div>
      )}

      {error && (
        <div style={styles.error} data-testid="stats-error">
          {error}
        </div>
      )}

      {!loading && !error && sorted.length === 0 && (
        <div style={styles.state} data-testid="stats-empty">
          No metrics recorded yet. Metrics are written on each PR merge.
        </div>
      )}

      <div data-tour="stats-tile" style={styles.grid} data-testid="stats-grid">
        {sorted.map(m => (
          <MetricSparkline
            key={m.name}
            label={m.name}
            value={m.value}
            unit={m.unit}
            series={series[m.name] ?? []}
            updatedAt={m.updated_at_iso ?? fetchedAt}
          />
        ))}
      </div>

      {/* Self-contained tile components — auto-discovered from stats/*Tile.tsx */}
      {tileRegistry.map(({ name, Component }) => (
        <Component key={name} refreshSignal={refreshSignal} />
      ))}
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    padding: '24px',
    maxWidth: 1100,
    margin: '0 auto',
    color: '#f9fafb',
    fontFamily: 'system-ui, -apple-system, sans-serif',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    marginBottom: 24,
    flexWrap: 'wrap',
    gap: 12,
  },
  headerLeft: {
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },
  headerRight: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
  },
  heading: {
    margin: 0,
    fontSize: 24,
    fontWeight: 700,
    color: '#f9fafb',
  },
  subtitle: {
    margin: 0,
    fontSize: 13,
    color: '#6b7280',
  },
  btn: {
    padding: '6px 14px',
    background: '#374151',
    color: '#f9fafb',
    border: '1px solid #4b5563',
    borderRadius: 6,
    cursor: 'pointer',
    fontSize: 13,
  },
  btnDisabled: {
    padding: '6px 14px',
    background: '#1f2937',
    color: '#4b5563',
    border: '1px solid #374151',
    borderRadius: 6,
    cursor: 'not-allowed',
    fontSize: 13,
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
    gap: 16,
  },
  state: {
    color: '#6b7280',
    fontSize: 14,
    padding: '40px 0',
    textAlign: 'center',
  },
  error: {
    color: '#ef4444',
    fontSize: 13,
    padding: '12px 16px',
    background: '#1f0000',
    border: '1px solid #7f1d1d',
    borderRadius: 6,
    marginBottom: 16,
  },
}
