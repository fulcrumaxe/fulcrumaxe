/**
 * FleetConcurrencyTile — fleet-wide agent concurrency view.
 *
 * The headline is the count the fleet cap actually governs (the spawn-agent.sh
 * lane) over that cap. Agent()-tool spawns are registered too but no cap
 * governs them, so they get their own labelled line instead of being folded
 * into the headline — the tile used to add both populations together and
 * render the sum against the cap, which on the operator host read "21 of 8"
 * (D#2323).
 *
 * When the backend cannot read fleet state it says so. A zero here means an
 * empty agents table, never a failed read.
 *
 * Polls fleet.concurrency every 10s with ETag/304.
 */

import { jsonRpc } from '../../api/client'
import { useEtaggedPoll } from './lib/poll'

interface ProjectConcurrency {
  name: string
  capped_agents: number
  uncapped_agents: number
  cap: number
  ok: boolean
  error?: string
}

interface FleetConcurrencyResponse {
  available: boolean
  unavailable_reason?: string
  fleet_cap: number | null
  capped_agents: number | null
  uncapped_agents: number | null
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
  subLabel: { color: '#9ca3af', fontSize: 12, marginBottom: 10 },
  uncapped: { color: '#9ca3af', fontSize: 12, marginBottom: 16, lineHeight: 1.5 },
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
  count: { color: '#9ca3af', width: 96, textAlign: 'right' as const, flexShrink: 0 },
  state: { color: '#6b7280', fontSize: 14, padding: '24px 0', textAlign: 'center' as const },
}

export default function FleetConcurrencyTile() {
  const { data, loading, error } = useEtaggedPoll<FleetConcurrencyResponse>(
    (etag) => jsonRpc<FleetConcurrencyResponse>('fleet.concurrency', { if_none_match: etag }),
    10_000,
  )

  const unavailable = data != null && data.available === false

  return (
    <section style={styles.section} data-testid="fleet-concurrency-tile">
      <h3 style={styles.heading}>Fleet Agent Concurrency</h3>
      {loading && !data && (
        <div style={styles.state}>Loading concurrency data…</div>
      )}
      {error && !data && (
        <div style={{ ...styles.state, color: '#ef4444' }}>{error}</div>
      )}
      {unavailable && (
        <div
          style={{ ...styles.state, color: '#ef4444' }}
          data-testid="fleet-concurrency-unavailable"
        >
          Fleet concurrency unavailable — {data.unavailable_reason ?? 'reason not reported'}
        </div>
      )}
      {data && !unavailable && (
        <div style={styles.card}>
          <div style={styles.headline} data-testid="fleet-concurrency-headline">
            {data.capped_agents} <span style={{ fontSize: 18, color: '#6b7280' }}>of {data.fleet_cap}</span>
          </div>
          <div style={styles.subLabel}>spawn-lane agents running fleet-wide — the cap covers these</div>

          <div style={styles.uncapped} data-testid="fleet-concurrency-uncapped">
            + {data.uncapped_agents} Agent()-tool agents running. No cap covers this lane, so
            they are not counted against the {data.fleet_cap} above.
          </div>

          {data.per_project.map((project) => {
            const pct = project.cap > 0 ? (project.capped_agents / project.cap) * 100 : 0
            return (
              <div key={project.name} style={styles.row}>
                <div style={styles.projectName}>{project.name}</div>
                {project.ok ? (
                  <>
                    <div style={styles.barTrack}>
                      <div style={{ ...styles.barFill, width: `${Math.min(pct, 100)}%` }} />
                    </div>
                    <div style={styles.count}>
                      {project.capped_agents}/{project.cap}
                      {project.uncapped_agents > 0 && (
                        <span style={{ color: '#6b7280' }}> +{project.uncapped_agents} uncapped</span>
                      )}
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
