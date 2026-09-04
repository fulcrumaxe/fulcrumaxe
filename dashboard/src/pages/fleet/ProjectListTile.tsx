/**
 * ProjectListTile — shows all discovered fleet projects.
 *
 * Each row: project name, status badge (ok/down/unknown/error), clickable
 * dashboard URL (only when the status is genuinely "ok" — a project that
 * was never probed, or that failed its probe, must never link to a live,
 * unrelated application listening on a stale port), agents-running count.
 * Broken projects show their error inline in red — they are never
 * silently dropped.
 *
 * `status` is measured (backend/fleet/fleet_set.py), not asserted: "ok"
 * only ever appears when every advertised port was actually probed and
 * answered. "unknown" reuses the same amber convention as
 * `Project.liveness` on the Projects page (dashboard/src/pages/ProjectListPage.tsx)
 * — it means "nothing was probeable", not "broken".
 *
 * Polls fleet.projects every 10s with ETag/304.
 */

import { jsonRpc } from '../../api/client'
import { useEtaggedPoll } from './lib/poll'

type FleetStatus = 'ok' | 'down' | 'unknown' | 'error'

interface FleetProject {
  name: string
  dashboard_port?: number | null
  status: FleetStatus
  error?: string
  agents_running?: number
}

interface FleetProjectsResponse {
  projects: FleetProject[]
  etag?: string
  not_modified?: boolean
  [key: string]: unknown
}

const styles: Record<string, React.CSSProperties> = {
  section: { marginBottom: 24 },
  heading: { fontSize: 16, fontWeight: 600, color: '#f9fafb', margin: '0 0 12px' },
  table: {
    width: '100%',
    borderCollapse: 'collapse' as const,
    fontSize: 13,
    background: '#111827',
    border: '1px solid #1f2937',
    borderRadius: 6,
  },
  th: {
    padding: '8px 12px',
    textAlign: 'left' as const,
    color: '#9ca3af',
    fontWeight: 500,
    borderBottom: '1px solid #1f2937',
    background: '#0f172a',
  },
  tr: { borderBottom: '1px solid #1f2937' },
  td: { padding: '8px 12px', color: '#f9fafb' },
  badgeOk: {
    background: '#052e16',
    color: '#22c55e',
    borderRadius: 4,
    padding: '2px 8px',
    fontSize: 11,
    fontWeight: 600,
  },
  badgeError: {
    background: '#450a0a',
    color: '#ef4444',
    borderRadius: 4,
    padding: '2px 8px',
    fontSize: 11,
    fontWeight: 600,
  },
  // Same amber (#d97706) as ProjectListPage.tsx's LIVENESS_COLORS.unknown —
  // "unknown" means "nothing was measurable", not "broken", and should read
  // the same way everywhere it appears on the dashboard.
  badgeUnknown: {
    background: '#451a03',
    color: '#d97706',
    borderRadius: 4,
    padding: '2px 8px',
    fontSize: 11,
    fontWeight: 600,
  },
  errorMsg: { color: '#ef4444', fontSize: 12, marginTop: 2 },
  link: { color: '#60a5fa', textDecoration: 'none' },
  state: { color: '#6b7280', fontSize: 14, padding: '24px 0', textAlign: 'center' as const },
}

function badgeStyle(status: FleetStatus): React.CSSProperties {
  switch (status) {
    case 'ok':
      return styles.badgeOk
    case 'unknown':
      return styles.badgeUnknown
    case 'down':
    case 'error':
    default:
      return styles.badgeError
  }
}

export default function ProjectListTile() {
  const { data, loading, error } = useEtaggedPoll<FleetProjectsResponse>(
    (etag) => jsonRpc<FleetProjectsResponse>('fleet.projects', { if_none_match: etag }),
    10_000,
  )

  return (
    <section style={styles.section} data-testid="project-list-tile">
      <h3 style={styles.heading}>Fleet Projects</h3>
      {loading && !data && (
        <div style={styles.state}>Discovering projects…</div>
      )}
      {error && !data && (
        <div style={{ ...styles.state, color: '#ef4444' }}>{error}</div>
      )}
      {data && (
        <table style={styles.table}>
          <thead>
            <tr>
              <th style={styles.th}>Project</th>
              <th style={styles.th}>Status</th>
              <th style={styles.th}>Dashboard</th>
              <th style={styles.th}>Agents</th>
            </tr>
          </thead>
          <tbody>
            {data.projects.length === 0 && (
              <tr>
                <td colSpan={4} style={{ ...styles.td, color: '#6b7280', textAlign: 'center' }}>
                  No projects discovered
                </td>
              </tr>
            )}
            {data.projects.map((project) => (
              <tr key={project.name} style={styles.tr}>
                <td style={styles.td}>
                  <div>{project.name}</div>
                  {project.status === 'error' && project.error && (
                    <div style={styles.errorMsg}>{project.error}</div>
                  )}
                </td>
                <td style={styles.td}>
                  <span style={badgeStyle(project.status)}>
                    {project.status}
                  </span>
                </td>
                <td style={styles.td}>
                  {project.status === 'ok' && project.dashboard_port ? (
                    <a
                      href={`http://localhost:${project.dashboard_port}`}
                      target="_blank"
                      rel="noreferrer"
                      style={styles.link}
                    >
                      :{project.dashboard_port}
                    </a>
                  ) : (
                    <span style={{ color: '#6b7280' }}>—</span>
                  )}
                </td>
                <td style={styles.td}>
                  {project.agents_running ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
