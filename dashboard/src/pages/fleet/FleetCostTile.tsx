/**
 * FleetCostTile — fleet-wide token spend summary.
 *
 * Shows tokens spent today (UTC) / over the last 7 UTC days / projected EOD.
 * Per-project stacked bar proportional to each project's spend today.
 * Polls fleet.cost every 10s with ETag/304.
 *
 * Two things this tile deliberately does NOT do (D#2317 PR-b):
 *
 *  - It does not label anything "24h". cost_summary.json stores one entry
 *    per UTC calendar date, so a rolling 24-hour figure is not computable
 *    from it; the value is a calendar-day-to-date total and the label now
 *    says so. See backend/fleet/cost_window.py.
 *  - It does not print `0` for a project the backend gave no number for.
 *    An absent token field means "no observation", which is what the
 *    original report was about: this tile read 0 for the last 24h on the
 *    busiest day on record. No signal renders as `—` plus an explicit
 *    caption; a real measured zero still renders as `0`.
 */

import { jsonRpc } from '../../api/client'
import { useEtaggedPoll } from './lib/poll'

interface ProjectCost {
  name: string
  // Absent when the backend had no in-window observation for this project.
  tokens_today_utc?: number
  tokens_7d?: number
  projected_eod_tokens?: number
  ok: boolean
  error?: string
}

interface FleetCostResponse {
  // Absent when no project in the fleet reported spend inside the window.
  total_today_utc?: number
  total_7d?: number
  projected_eod?: number
  per_project: ProjectCost[]
  etag?: string
  not_modified?: boolean
  [key: string]: unknown
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`
  return String(n)
}

/** `—` for no signal; a real number (including 0) for a measurement. */
function formatMetric(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : formatTokens(n)
}

const PROJECT_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444', '#06b6d4']

const styles: Record<string, React.CSSProperties> = {
  section: { marginBottom: 24 },
  heading: { fontSize: 16, fontWeight: 600, color: '#f9fafb', margin: '0 0 12px' },
  card: {
    background: '#111827',
    border: '1px solid #1f2937',
    borderRadius: 8,
    padding: '16px 20px',
  },
  metricsRow: { display: 'flex', gap: 32, marginBottom: 16, flexWrap: 'wrap' as const },
  metric: {},
  metricLabel: { color: '#9ca3af', fontSize: 11, textTransform: 'uppercase' as const, marginBottom: 4 },
  metricValue: { fontSize: 24, fontWeight: 700, color: '#f9fafb' },
  barContainer: { display: 'flex', borderRadius: 4, overflow: 'hidden', height: 20, marginBottom: 12 },
  legend: { display: 'flex', flexWrap: 'wrap' as const, gap: '4px 12px', marginTop: 8 },
  legendItem: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#9ca3af' },
  legendDot: { width: 10, height: 10, borderRadius: '50%' },
  state: { color: '#6b7280', fontSize: 14, padding: '24px 0', textAlign: 'center' as const },
  caption: { color: '#6b7280', fontSize: 13, marginTop: 8 },
}

export default function FleetCostTile() {
  const { data, loading, error } = useEtaggedPoll<FleetCostResponse>(
    (etag) => jsonRpc<FleetCostResponse>('fleet.cost', { if_none_match: etag }),
    10_000,
  )

  const spentToday = data?.total_today_utc

  return (
    <section style={styles.section} data-testid="fleet-cost-tile">
      <h3 style={styles.heading}>Fleet Token Spend</h3>
      {loading && !data && (
        <div style={styles.state}>Loading cost data…</div>
      )}
      {error && !data && (
        <div style={{ ...styles.state, color: '#ef4444' }}>{error}</div>
      )}
      {data && (
        <div style={styles.card}>
          <div style={styles.metricsRow}>
            <div style={styles.metric}>
              <div style={styles.metricLabel}>Today (UTC)</div>
              <div style={styles.metricValue}>{formatMetric(spentToday)}</div>
            </div>
            <div style={styles.metric}>
              <div style={styles.metricLabel}>Last 7 days (UTC)</div>
              <div style={styles.metricValue}>{formatMetric(data.total_7d)}</div>
            </div>
            <div style={styles.metric}>
              <div style={styles.metricLabel}>Projected EOD (UTC)</div>
              <div style={styles.metricValue}>{formatMetric(data.projected_eod)}</div>
            </div>
          </div>

          {/* Stacked bar — per-project proportional share of today's tokens */}
          {data.per_project.length > 0 && (spentToday ?? 0) > 0 && (
            <>
              <div style={styles.barContainer}>
                {data.per_project
                  .filter((p) => p.ok && (p.tokens_today_utc ?? 0) > 0)
                  .map((project, i) => {
                    const width = ((project.tokens_today_utc ?? 0) / (spentToday || 1)) * 100
                    return (
                      <div
                        key={project.name}
                        title={`${project.name}: ${formatMetric(project.tokens_today_utc)} tokens`}
                        style={{
                          width: `${width}%`,
                          background: PROJECT_COLORS[i % PROJECT_COLORS.length],
                          minWidth: width > 0 ? 2 : 0,
                        }}
                      />
                    )
                  })}
              </div>
              <div style={styles.legend}>
                {data.per_project
                  .filter((p) => p.ok)
                  .map((project, i) => (
                    <div key={project.name} style={styles.legendItem}>
                      <div
                        style={{
                          ...styles.legendDot,
                          background: PROJECT_COLORS[i % PROJECT_COLORS.length],
                        }}
                      />
                      <span>{project.name} — {formatMetric(project.tokens_today_utc)}</span>
                    </div>
                  ))}
              </div>
            </>
          )}

          {spentToday === null || spentToday === undefined ? (
            <div style={styles.caption}>
              No token spend measured — no project reported spend in the last 7 days
            </div>
          ) : spentToday === 0 ? (
            <div style={styles.caption}>No token spend recorded today (UTC)</div>
          ) : null}
        </div>
      )}
    </section>
  )
}
