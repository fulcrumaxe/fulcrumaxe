/**
 * LoopTimeline — Dashboard page showing loop iteration history as charts.
 *
 * Route: /loop-timeline
 *
 * Two charts:
 *   - Duration chart (LineChart): iteration duration over time. Idle iterations
 *     shown with a dimmed dot marker. Crashed iterations (≤1 s, non-zero exit)
 *     shown with muted gray dots.
 *   - Activity chart (stacked BarChart): agents_spawned + prs_merged per iteration.
 *     Crashed iterations excluded.
 *
 * Clicking a data point opens a side drawer with the full loop-run log and
 * structured fields for that iteration.
 *
 * Bug fixes shipped (Discussion #466):
 *   Bug 1/2  — Activity chart and Detail panel counters default to 0 (not '—').
 *   Bug 3    — Log file resolved via run_id (backend fix in server.py).
 *   Bug 4    — X-axis is time-scaled (numeric epoch-seconds `ts` field).
 *   Bug 5    — Error reference lines use numeric x={ts} so they render.
 *   Bug 6    — Selected iteration highlighted with amber ReferenceLine.
 *   Bug 7    — Crashed iterations styled gray; Activity chart skips them.
 *   Extra    — Array.isArray guard on detail.metrics.actions.
 *
 * Bug fix (D#1039 item 2):
 *   BarChart + XAxis type="number" scale="time" makes bars zero-width — invisible.
 *   Fixed: BarChart now uses type="category" dataKey="timestamp" for proper band scale.
 */

import { useState, useEffect, useCallback, useRef } from 'react'
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'
import { getLoopTimeline, getIterationDetail } from '../api/loop'
import type { LoopIterationPoint, LoopIterationDetail, LoopRunReferences } from '../api/loop'
import { LastUpdated } from '../components/LastUpdated'
import { useActiveRepo } from '../hooks/useActiveRepo'
import { discussionUrl, pullUrl } from '../lib/repoUrls'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Format a timestamp for axis ticks.
 *
 * Accepts either:
 *   - a number (epoch seconds) — used by the LineChart time-scaled XAxis
 *   - a string (ISO 8601)      — used by the BarChart categorical XAxis
 *
 * Returns "MM/DD HH:MM" in UTC.
 */
function formatTick(ts: number | string): string {
  try {
    const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
    if (isNaN(d.getTime())) return String(ts)
    return `${String(d.getUTCMonth() + 1).padStart(2, '0')}/${String(d.getUTCDate()).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
  } catch {
    return String(ts)
  }
}

// ---------------------------------------------------------------------------
// Types for chart data
// ---------------------------------------------------------------------------

interface ChartRow {
  index: number
  timestamp: string
  /** Epoch seconds — used as the X-axis key for time-scaled charts (Bug 4). */
  ts: number
  duration_seconds: number
  agents_spawned: number
  prs_merged: number
  idle: boolean
  hasError: boolean
  /** True when duration ≤1 s AND exit indicates a crash (Bug 7). */
  crashed: boolean
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function LoadingState() {
  return (
    <div style={styles.state}>
      <p style={styles.stateText}>Loading loop timeline…</p>
    </div>
  )
}

function ErrorState({ message }: { message: string }) {
  return (
    <div style={styles.state}>
      <p style={{ ...styles.stateText, color: '#f87171' }}>Error: {message}</p>
    </div>
  )
}

function EmptyState() {
  return (
    <div style={styles.state}>
      <p style={styles.stateText}>No loop iterations recorded yet.</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Reference pills (D#N → GitHub Discussions, PR #N → GitHub pulls)
// ---------------------------------------------------------------------------

export function ReferencesSection({ references }: { references?: LoopRunReferences }) {
  // useActiveRepo() must be called before the `!references` guard below —
  // calling it conditionally would violate react-hooks/rules-of-hooks
  // (npm run lint --max-warnings 0 is a merge gate).
  const repo = useActiveRepo()
  if (!references) return null
  const { discussions, prs } = references
  if (discussions.length === 0 && prs.length === 0) return null

  return (
    <div style={styles.refsBlock}>
      <h3 style={styles.sectionHeading}>References</h3>
      {discussions.length > 0 && (
        <div style={styles.pillRow}>
          {discussions.map(n => {
            const href = discussionUrl(repo, n)
            return href ? (
              <a
                key={`d${n}`}
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                style={styles.pillDiscussion}
              >
                D#{n}
              </a>
            ) : (
              <span key={`d${n}`} style={styles.pillDiscussion}>
                D#{n}
              </span>
            )
          })}
        </div>
      )}
      {prs.length > 0 && (
        <div style={styles.pillRow}>
          {prs.map(n => {
            const href = pullUrl(repo, n)
            return href ? (
              <a
                key={`pr${n}`}
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                style={styles.pillPR}
              >
                PR #{n}
              </a>
            ) : (
              <span key={`pr${n}`} style={styles.pillPR}>
                PR #{n}
              </span>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// "View agent runs during this window" link
// ---------------------------------------------------------------------------

function AgentRunsLink({ timestamp }: { timestamp: string }) {
  // Loop runs every 10 minutes — compute a 10-minute window around this timestamp
  const since = timestamp
  let until: string
  try {
    const d = new Date(timestamp)
    d.setMinutes(d.getMinutes() + 10)
    until = d.toISOString().replace(/\.\d{3}Z$/, 'Z')
  } catch {
    return null
  }
  const href = `/runs?since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`
  return (
    <div style={styles.agentRunsLink}>
      <a href={href} style={styles.agentRunsAnchor}>
        View agent runs during this window &rarr;
      </a>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Drawer
// ---------------------------------------------------------------------------

interface DrawerProps {
  timestamp: string | null
  detail: LoopIterationDetail | null
  loadingDetail: boolean
  onClose: () => void
}

function IterationDrawer({ timestamp, detail, loadingDetail, onClose }: DrawerProps) {
  if (!timestamp) return null

  return (
    <>
      {/* Backdrop */}
      <div style={styles.backdrop} onClick={onClose} />
      {/* Drawer panel */}
      <aside style={styles.drawer}>
        <div style={styles.drawerHeader}>
          <h2 style={styles.drawerTitle}>Iteration Detail</h2>
          <button onClick={onClose} style={styles.closeBtn} aria-label="Close drawer">✕</button>
        </div>

        {loadingDetail && (
          <p style={styles.drawerMeta}>Loading…</p>
        )}

        {!loadingDetail && detail && (
          <div>
            <dl style={styles.metaGrid}>
              <dt style={styles.metaKey}>Timestamp</dt>
              <dd style={styles.metaVal}>{detail.timestamp}</dd>

              <dt style={styles.metaKey}>Duration</dt>
              <dd style={styles.metaVal}>{detail.metrics.duration_seconds ?? '—'}s</dd>

              {/* Bug 2 fix: show 0 for present-but-missing counters; '—' only when row absent */}
              <dt style={styles.metaKey}>Agents spawned</dt>
              <dd style={styles.metaVal}>{detail.metrics.agents_spawned ?? 0}</dd>

              <dt style={styles.metaKey}>PRs merged</dt>
              <dd style={styles.metaVal}>{detail.metrics.prs_merged ?? 0}</dd>

              <dt style={styles.metaKey}>Discussions scanned</dt>
              <dd style={styles.metaVal}>{detail.metrics.discussions_scanned ?? 0}</dd>

              <dt style={styles.metaKey}>PRs scanned</dt>
              <dd style={styles.metaVal}>{detail.metrics.prs_scanned ?? 0}</dd>

              <dt style={styles.metaKey}>Idle</dt>
              <dd style={styles.metaVal}>{detail.metrics.idle ? 'Yes' : 'No'}</dd>

              {detail.metrics.error && (
                <>
                  <dt style={styles.metaKey}>Error</dt>
                  <dd style={{ ...styles.metaVal, color: '#f87171' }}>{detail.metrics.error}</dd>
                </>
              )}
            </dl>

            {/* Bug Extra: Array.isArray guard so non-array `actions` values don't crash */}
            {Array.isArray(detail.metrics.actions) && detail.metrics.actions.length > 0 && (
              <div style={styles.actionsBlock}>
                <h3 style={styles.sectionHeading}>Actions</h3>
                <ul style={styles.actionList}>
                  {detail.metrics.actions.map((a, i) => (
                    <li key={i} style={styles.actionItem}>{a}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* References section — D#N and PR #N links extracted from the log */}
            <ReferencesSection references={detail.references} />

            {/* Link to agent runs filtered to this iteration's time window */}
            <AgentRunsLink timestamp={detail.timestamp} />

            <div style={styles.logBlock}>
              <h3 style={styles.sectionHeading}>Loop Run Log</h3>
              {detail.log ? (
                <pre style={styles.logPre}>{detail.log}</pre>
              ) : (
                <p style={styles.drawerMeta}>No log file recorded for this iteration</p>
              )}
            </div>
          </div>
        )}
      </aside>
    </>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

const LIMIT_OPTIONS = [50, 100, 250] as const

export default function LoopTimeline() {
  const [limit, setLimit] = useState<number>(100)
  const [rows, setRows] = useState<LoopIterationPoint[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [fetchedAt, setFetchedAt] = useState<string | null>(null)
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Drawer state
  const [selectedTimestamp, setSelectedTimestamp] = useState<string | null>(null)
  const [detail, setDetail] = useState<LoopIterationDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  // Fetch timeline data
  const fetchTimeline = useCallback((lim: number) => {
    setLoading(true)
    setError(null)
    getLoopTimeline(lim)
      .then(data => {
        setRows(data)
        setFetchedAt(new Date().toISOString())
        setLoading(false)
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : String(err))
        setLoading(false)
      })
  }, [])

  useEffect(() => {
    fetchTimeline(limit)
    // Auto-refresh every 30 seconds
    autoRefreshRef.current = setInterval(() => fetchTimeline(limit), 30_000)
    return () => {
      if (autoRefreshRef.current) clearInterval(autoRefreshRef.current)
    }
  }, [limit, fetchTimeline])

  // Fetch detail when a data point is clicked
  const handlePointClick = useCallback((timestamp: string) => {
    setSelectedTimestamp(timestamp)
    setDetail(null)
    setLoadingDetail(true)
    getIterationDetail(timestamp)
      .then(d => {
        setDetail(d)
        setLoadingDetail(false)
      })
      .catch(() => {
        setDetail({ timestamp, metrics: {}, log: null, log_path: null })
        setLoadingDetail(false)
      })
  }, [])

  const handleCloseDrawer = useCallback(() => {
    setSelectedTimestamp(null)
    setDetail(null)
  }, [])

  // Build chart data (Bug 4: add ts field for time-scaled X axis)
  const chartData: ChartRow[] = rows.map((row, i) => {
    const tsNum = Math.floor(new Date(row.timestamp).getTime() / 1000)
    // Bug 7: crashed = very short duration with error indicator
    const crashed = row.duration_seconds <= 1 && (!!row.error || (row as unknown as { exit_code?: number }).exit_code !== undefined && (row as unknown as { exit_code?: number }).exit_code !== 0)
    return {
      index: i,
      timestamp: row.timestamp,
      ts: tsNum,
      duration_seconds: row.duration_seconds,
      agents_spawned: row.agents_spawned,
      prs_merged: row.prs_merged,
      idle: row.idle,
      hasError: !!row.error,
      crashed,
    }
  })

  // Bug 5 fix: error reference lines now use the ts numeric field
  const errorPoints = chartData.filter(r => r.hasError)

  // Bug 7: crashed iterations for footer note
  const crashedCount = chartData.filter(r => r.crashed).length

  // Activity chart excludes crashed iterations (Bug 7)
  const activityData = chartData.filter(r => !r.crashed)

  // Bug 6: selected timestamp's numeric ts for chart highlight
  const selectedTs = selectedTimestamp
    ? Math.floor(new Date(selectedTimestamp).getTime() / 1000)
    : null

  const handleChartClick = (data: { activePayload?: Array<{ payload: ChartRow }> } | null) => {
    if (!data?.activePayload?.[0]) return
    const row = data.activePayload[0].payload
    handlePointClick(row.timestamp)
  }

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <div style={styles.headerTop}>
          <h1 style={styles.heading}>Loop Timeline</h1>
          <LastUpdated fetchedAt={fetchedAt} />
        </div>
        <p style={styles.subtitle}>
          Iteration-over-iteration trends from <code style={styles.code}>loop-metrics.jsonl</code>.
          Click any data point to see the full loop-run log.
        </p>
        <div style={styles.controls}>
          <span style={styles.controlLabel}>Show last:</span>
          {LIMIT_OPTIONS.map(opt => (
            <button
              key={opt}
              onClick={() => setLimit(opt)}
              style={{ ...styles.limitBtn, ...(limit === opt ? styles.limitBtnActive : {}) }}
            >
              {opt}
            </button>
          ))}
        </div>
      </header>

      {loading && <LoadingState />}
      {!loading && error && <ErrorState message={error} />}
      {!loading && !error && rows.length === 0 && <EmptyState />}

      {!loading && !error && rows.length > 0 && (
        <div data-tour="loop-timeline" style={styles.charts}>
          {/* Duration chart */}
          <section style={styles.chartSection}>
            <h2 style={styles.chartTitle}>Iteration Duration (seconds)</h2>
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={chartData} onClick={handleChartClick} style={{ cursor: 'pointer' }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                {/* Bug 4 fix: time-scaled X axis using numeric `ts` field */}
                <XAxis
                  dataKey="ts"
                  type="number"
                  scale="time"
                  domain={['dataMin', 'dataMax']}
                  tickFormatter={formatTick}
                  tick={{ fill: '#9ca3af', fontSize: 11 }}
                />
                <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} unit="s" />
                <Tooltip
                  contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f9fafb' }}
                  formatter={(val: number) => [`${val}s`, 'Duration']}
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  labelFormatter={(_: unknown, payload: any[]) => {
                    const row = payload?.[0]?.payload as ChartRow | undefined
                    return row ? row.timestamp : ''
                  }}
                />
                {/* Bug 5 fix: reference lines use numeric x={ts} */}
                {errorPoints.map(r => (
                  <ReferenceLine key={r.ts} x={r.ts} stroke="#ef4444" strokeDasharray="4 4" />
                ))}
                {/* Bug 6 fix: amber marker for selected iteration */}
                {selectedTs !== null && (
                  <ReferenceLine x={selectedTs} stroke="#fbbf24" strokeWidth={2} />
                )}
                <Line
                  type="monotone"
                  dataKey="duration_seconds"
                  stroke="#60a5fa"
                  dot={(props: { cx: number; cy: number; payload: ChartRow }) => {
                    const { cx, cy, payload } = props
                    // Bug 7: crashed dots are muted gray
                    if (payload.crashed) {
                      return <circle key={`dot-${payload.index}`} cx={cx} cy={cy} r={3} fill="#4b5563" stroke="#6b7280" strokeDasharray="2 2" />
                    }
                    if (payload.idle) {
                      return <circle key={`dot-${payload.index}`} cx={cx} cy={cy} r={3} fill="#4b5563" stroke="#6b7280" />
                    }
                    return <circle key={`dot-${payload.index}`} cx={cx} cy={cy} r={3} fill="#60a5fa" />
                  }}
                  activeDot={{ r: 6, fill: '#93c5fd' }}
                  name="Duration"
                />
              </LineChart>
            </ResponsiveContainer>
            {/* Bug 7: crashed iterations footnote */}
            {crashedCount > 0 && (
              <p style={styles.crashNote}>
                {crashedCount} iteration{crashedCount > 1 ? 's' : ''} crashed early (gray, dashed dots above).
              </p>
            )}
          </section>

          {/* Activity chart */}
          <section style={styles.chartSection}>
            <h2 style={styles.chartTitle}>Activity per Iteration</h2>
            <ResponsiveContainer width="100%" height={280}>
              {/*
                BarChart requires a categorical/band XAxis to render bars with
                visible width. Using type="number" scale="time" (as on the
                LineChart) makes bars zero-width and invisible — that was the
                root cause of the empty chart. We use type="category" +
                dataKey="timestamp" so Recharts computes a proper band scale,
                then tickFormatter shortens the label.  The selected-iteration
                ReferenceLine now uses x={selectedTimestamp} (the ISO string)
                to match the categorical domain.
              */}
              <BarChart data={activityData} onClick={handleChartClick} style={{ cursor: 'pointer' }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis
                  dataKey="timestamp"
                  type="category"
                  tickFormatter={formatTick}
                  tick={{ fill: '#9ca3af', fontSize: 11 }}
                  interval="preserveStartEnd"
                />
                <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} allowDecimals={false} />
                <Tooltip
                  contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f9fafb' }}
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  labelFormatter={(_: unknown, payload: any[]) => {
                    const row = payload?.[0]?.payload as ChartRow | undefined
                    return row ? row.timestamp : ''
                  }}
                />
                <Legend wrapperStyle={{ color: '#9ca3af', fontSize: 12 }} />
                {/* Amber marker for selected iteration — uses ISO timestamp to match categorical domain */}
                {selectedTimestamp !== null && (
                  <ReferenceLine x={selectedTimestamp} stroke="#fbbf24" strokeWidth={2} />
                )}
                <Bar dataKey="agents_spawned" name="Agents Spawned" stackId="a" fill="#818cf8" />
                <Bar dataKey="prs_merged" name="PRs Merged" stackId="a" fill="#34d399" />
              </BarChart>
            </ResponsiveContainer>
            {errorPoints.length > 0 && (
              <p style={styles.errorNote}>
                {errorPoints.length} iteration{errorPoints.length > 1 ? 's' : ''} had errors (shown as red dashed lines in Duration chart).
              </p>
            )}
          </section>
        </div>
      )}

      <IterationDrawer
        timestamp={selectedTimestamp}
        detail={detail}
        loadingDetail={loadingDetail}
        onClose={handleCloseDrawer}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles: Record<string, React.CSSProperties> = {
  page: {
    minHeight: '100vh',
    background: '#111827',
    color: '#f9fafb',
    fontFamily: 'system-ui, sans-serif',
    padding: '24px 32px',
    boxSizing: 'border-box',
  },
  header: {
    marginBottom: 32,
  },
  headerTop: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 12,
    marginBottom: 4,
  },
  heading: {
    fontSize: 24,
    fontWeight: 700,
    margin: 0,
    color: '#f9fafb',
  },
  subtitle: {
    fontSize: 13,
    color: '#9ca3af',
    margin: '0 0 16px',
  },
  code: {
    fontFamily: 'monospace',
    background: '#1f2937',
    padding: '1px 4px',
    borderRadius: 3,
    fontSize: 12,
  },
  controls: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
  },
  controlLabel: {
    fontSize: 13,
    color: '#9ca3af',
  },
  limitBtn: {
    background: '#1f2937',
    border: '1px solid #374151',
    color: '#9ca3af',
    borderRadius: 4,
    padding: '4px 12px',
    fontSize: 12,
    cursor: 'pointer',
  },
  limitBtnActive: {
    background: '#2563eb',
    border: '1px solid #3b82f6',
    color: '#fff',
  },
  state: {
    display: 'flex',
    justifyContent: 'center',
    alignItems: 'center',
    minHeight: 200,
  },
  stateText: {
    color: '#6b7280',
    fontSize: 14,
  },
  charts: {
    display: 'flex',
    flexDirection: 'column',
    gap: 40,
  },
  chartSection: {
    background: '#1f2937',
    borderRadius: 8,
    padding: '20px 24px',
  },
  chartTitle: {
    fontSize: 15,
    fontWeight: 600,
    color: '#e5e7eb',
    margin: '0 0 16px',
  },
  errorNote: {
    fontSize: 12,
    color: '#f87171',
    marginTop: 8,
  },
  crashNote: {
    fontSize: 12,
    color: '#9ca3af',
    marginTop: 8,
  },
  // Drawer
  backdrop: {
    position: 'fixed',
    inset: 0,
    background: 'rgba(0,0,0,0.5)',
    zIndex: 40,
  },
  drawer: {
    position: 'fixed',
    top: 0,
    right: 0,
    bottom: 0,
    width: 480,
    maxWidth: '90vw',
    background: '#1f2937',
    borderLeft: '1px solid #374151',
    zIndex: 50,
    overflowY: 'auto',
    padding: 24,
    boxSizing: 'border-box',
  },
  drawerHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 20,
  },
  drawerTitle: {
    fontSize: 18,
    fontWeight: 600,
    color: '#f9fafb',
    margin: 0,
  },
  closeBtn: {
    background: 'transparent',
    border: 'none',
    color: '#9ca3af',
    fontSize: 18,
    cursor: 'pointer',
    lineHeight: 1,
    padding: 4,
  },
  metaGrid: {
    display: 'grid',
    gridTemplateColumns: 'auto 1fr',
    gap: '6px 16px',
    marginBottom: 20,
  },
  metaKey: {
    fontSize: 12,
    color: '#6b7280',
    fontWeight: 500,
    textAlign: 'right',
  },
  metaVal: {
    fontSize: 12,
    color: '#e5e7eb',
    fontFamily: 'monospace',
  },
  drawerMeta: {
    fontSize: 13,
    color: '#6b7280',
  },
  actionsBlock: {
    marginBottom: 20,
  },
  sectionHeading: {
    fontSize: 13,
    fontWeight: 600,
    color: '#9ca3af',
    margin: '0 0 8px',
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
  },
  actionList: {
    margin: 0,
    padding: '0 0 0 16px',
    listStyle: 'disc',
  },
  actionItem: {
    fontSize: 12,
    color: '#d1d5db',
    marginBottom: 4,
  },
  refsBlock: {
    marginBottom: 16,
  },
  pillRow: {
    display: 'flex',
    flexWrap: 'wrap' as const,
    gap: 6,
    marginBottom: 6,
  },
  pillDiscussion: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: 12,
    background: '#1e3a5f',
    color: '#93c5fd',
    fontSize: 11,
    fontFamily: 'monospace',
    textDecoration: 'none',
    border: '1px solid #2563eb',
  },
  pillPR: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: 12,
    background: '#1c2f1a',
    color: '#86efac',
    fontSize: 11,
    fontFamily: 'monospace',
    textDecoration: 'none',
    border: '1px solid #16a34a',
  },
  agentRunsLink: {
    marginBottom: 16,
  },
  agentRunsAnchor: {
    fontSize: 12,
    color: '#60a5fa',
    textDecoration: 'none',
  },
  logBlock: {
    marginTop: 8,
  },
  logPre: {
    background: '#111827',
    border: '1px solid #374151',
    borderRadius: 6,
    padding: 12,
    fontSize: 11,
    color: '#d1d5db',
    overflowX: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    maxHeight: 400,
    overflowY: 'auto',
    margin: 0,
  },
}
