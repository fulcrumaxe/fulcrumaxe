/**
 * FleetCostTile — fleet-wide token spend summary.
 *
 * Shows total tokens for last 24h / last 7d / projected EOD.
 * Per-project stacked bar proportional to each project's 24h spend.
 * Polls fleet.cost every 10s with ETag/304.
 */

import { jsonRpc } from '../../api/client'
import { useEtaggedPoll } from './lib/poll'

interface ProjectCost {
  name: string
  tokens_24h: number
  tokens_7d: number
  projected_eod_tokens: number
  ok: boolean
  error?: string
}

interface FleetCostResponse {
  total_24h: number
  total_7d: number
  projected_eod: number
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
}

export default function FleetCostTile() {
  const { data, loading, error } = useEtaggedPoll<FleetCostResponse>(
    (etag) => jsonRpc<FleetCostResponse>('fleet.cost', { if_none_match: etag }),
    10_000,
  )

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
              <div style={styles.metricLabel}>Last 24h</div>
              <div style={styles.metricValue}>{formatTokens(data.total_24h)}</div>
            </div>
            <div style={styles.metric}>
              <div style={styles.metricLabel}>Last 7d</div>
              <div style={styles.metricValue}>{formatTokens(data.total_7d)}</div>
            </div>
            <div style={styles.metric}>
              <div style={styles.metricLabel}>Projected EOD</div>
              <div style={styles.metricValue}>{formatTokens(data.projected_eod)}</div>
            </div>
          </div>

          {/* Stacked bar — per-project proportional share of 24h tokens */}
          {data.per_project.length > 0 && data.total_24h > 0 && (
            <>
              <div style={styles.barContainer}>
                {data.per_project
                  .filter((p) => p.ok && p.tokens_24h > 0)
                  .map((project, i) => {
                    const width = (project.tokens_24h / data.total_24h) * 100
                    return (
                      <div
                        key={project.name}
                        title={`${project.name}: ${formatTokens(project.tokens_24h)} tokens`}
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
                      <span>{project.name} — {formatTokens(project.tokens_24h)}</span>
                    </div>
                  ))}
              </div>
            </>
          )}
          {data.total_24h === 0 && (
            <div style={{ color: '#6b7280', fontSize: 13, marginTop: 8 }}>
              No token spend recorded in last 24h
            </div>
          )}
        </div>
      )}
    </section>
  )
}
