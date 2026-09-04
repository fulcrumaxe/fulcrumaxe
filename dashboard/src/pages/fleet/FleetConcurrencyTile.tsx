/**
 * FleetConcurrencyTile — fleet-wide agent concurrency view.
 *
 * Shows total agents running across all projects vs fleet cap (8).
 * Per-project count with a capacity bar.
 * Polls fleet.concurrency every 10s with ETag/304.
 */

import { jsonRpc } from '../../api/client'
import { useEtaggedPoll } from './lib/poll'

interface ProjectConcurrency {
  name: string
  agents_running: number
  cap: number
  ok: boolean
  error?: string
}

interface FleetConcurrencyResponse {
  fleet_total: number
  fleet_cap: number
  per_project: ProjectConcurrency[]
  etag?: string
  not_modified?: boolean
  [key: string]: unknown
}

const styles: Record<string, React.CSSProperties> = {
  section: { marginBottom: 24 },
  heading: { fontSize: 16, fontWeight: 600, color: '#f9fafb', margin: '0 0 12px' },
  card: {
    background: '#111827',
    border: '1px solid #1f2937',
    borderRadius: 8,
    padding: '16px 20px',
  },
  headline: { fontSize: 32, fontWeight: 700, color: '#f9fafb', marginBottom: 4 },
  subLabel: { color: '#9ca3af', fontSize: 12, marginBottom: 16 },
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    marginBottom: 10,
    fontSize: 13,
  },
  projectName: { color: '#f9fafb', width: 180, flexShrink: 0 },
  barTrack: {
    flex: 1,
    height: 8,
    background: '#1f2937',
    borderRadius: 4,
    overflow: 'hidden',
  },
  barFill: { height: '100%', borderRadius: 4, background: '#3b82f6' },
  count: { color: '#9ca3af', width: 40, textAlign: 'right' as const, flexShrink: 0 },
  state: { color: '#6b7280', fontSize: 14, padding: '24px 0', textAlign: 'center' as const },
}

export default function FleetConcurrencyTile() {
  const { data, loading, error } = useEtaggedPoll<FleetConcurrencyResponse>(
    (etag) => jsonRpc<FleetConcurrencyResponse>('fleet.concurrency', { if_none_match: etag }),
    10_000,
  )

  return (
    <section style={styles.section} data-testid="fleet-concurrency-tile">
      <h3 style={styles.heading}>Fleet Agent Concurrency</h3>
      {loading && !data && (
        <div style={styles.state}>Loading concurrency data…</div>
      )}
      {error && !data && (
        <div style={{ ...styles.state, color: '#ef4444' }}>{error}</div>
      )}
      {data && (
        <div style={styles.card}>
          <div style={styles.headline}>
            {data.fleet_total} <span style={{ fontSize: 18, color: '#6b7280' }}>of {data.fleet_cap}</span>
          </div>
          <div style={styles.subLabel}>agents running fleet-wide</div>

          {data.per_project.map((project) => {
            const pct = project.cap > 0 ? (project.agents_running / project.cap) * 100 : 0
            return (
              <div key={project.name} style={styles.row}>
                <div style={styles.projectName}>{project.name}</div>
                {project.ok ? (
                  <>
                    <div style={styles.barTrack}>
                      <div style={{ ...styles.barFill, width: `${Math.min(pct, 100)}%` }} />
                    </div>
                    <div style={styles.count}>
                      {project.agents_running}/{project.cap}
                    </div>
                  </>
                ) : (
                  <div style={{ color: '#ef4444', fontSize: 12 }}>
                    error: {project.error}
                  </div>
                )}
              </div>
            )
          })}

          {data.per_project.length === 0 && (
            <div style={{ color: '#6b7280', fontSize: 13 }}>No projects discovered</div>
          )}
        </div>
      )}
    </section>
  )
}
