/**
 * RoleSuccessRateTile — Role Performance (24h) table.
 *
 * Fetches stats.role_success_rate and stats.role_retry_rate RPCs.
 * Shows per-role success rate and retry rate in a combined table.
 * Color coded: red <70%, amber 70–90%, green >=90% for success;
 *              red >30%, amber >15%, green otherwise for retry.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

export interface RoleSuccessRow {
  role: string
  success_rate: number | null
  sample_size: number
}

export interface RoleRetryRow {
  role: string
  retry_rate: number | null
  sample_size: number
}

interface RoleSuccessResponse {
  rows: RoleSuccessRow[]
}

interface RoleRetryResponse {
  rows: RoleRetryRow[]
}

const styles: Record<string, React.CSSProperties> = {
  ...sharedStyles,
}

interface Props {
  refreshSignal?: number
}

export default function RoleSuccessRateTile({ refreshSignal }: Props) {
  const [successRates, setSuccessRates] = useState<RoleSuccessRow[]>([])
  const [retryRates, setRetryRates] = useState<RoleRetryRow[]>([])
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    const [successResp, retryResp] = await Promise.all([
      jsonRpc<RoleSuccessResponse>('stats.role_success_rate', {}).catch(() => ({ rows: [] })),
      jsonRpc<RoleRetryResponse>('stats.role_retry_rate', {}).catch(() => ({ rows: [] })),
    ])
    setSuccessRates(successResp.rows ?? [])
    setRetryRates(retryResp.rows ?? [])
  }, [])

  useEffect(() => {
    loadData()
    intervalRef.current = setInterval(loadData, 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [loadData, refreshSignal])

  return (
    <section style={styles.section} aria-label="Role Performance (24h)">
      <h2 style={styles.sectionHeading}>Role Performance (24h)</h2>
      {successRates.length === 0 ? (
        <div style={styles.state} role="status">
          No verdict data yet. Emitted by post-agent-hook after each agent run.
        </div>
      ) : (
        <table style={styles.table} data-testid="role-success-rate-table">
          <thead>
            <tr>
              <th style={styles.th} scope="col">Role</th>
              <th style={{ ...styles.th, textAlign: 'right' }} scope="col">Success Rate</th>
              <th style={{ ...styles.th, textAlign: 'right' }} scope="col">Retry Rate</th>
              <th style={{ ...styles.th, textAlign: 'right' }} scope="col">Sample Size</th>
            </tr>
          </thead>
          <tbody>
            {successRates.map(row => {
              const successColor =
                row.success_rate === null
                  ? '#6b7280'
                  : row.success_rate < 0.70
                  ? '#ef4444'
                  : row.success_rate < 0.90
                  ? '#f59e0b'
                  : '#22c55e'
              const successText =
                row.success_rate === null
                  ? 'N/A'
                  : `${(row.success_rate * 100).toFixed(1)}%`

              const retryRow = retryRates.find(r => r.role === row.role)
              const retryRate = retryRow?.retry_rate ?? null
              const retryColor =
                retryRate === null
                  ? '#6b7280'
                  : retryRate > 0.30
                  ? '#ef4444'
                  : retryRate > 0.15
                  ? '#f59e0b'
                  : '#22c55e'
              const retryText =
                retryRate === null
                  ? 'N/A'
                  : `${(retryRate * 100).toFixed(1)}%`

              return (
                <tr key={row.role} style={styles.tr}>
                  <td style={styles.td}>{row.role}</td>
                  <td style={{ ...styles.td, textAlign: 'right', color: successColor, fontWeight: 600 }}>
                    {successText}
                  </td>
                  <td
                    style={{ ...styles.td, textAlign: 'right', color: retryColor, fontWeight: 600 }}
                    data-testid={`retry-rate-${row.role}`}
                  >
                    {retryText}
                  </td>
                  <td style={{ ...styles.td, textAlign: 'right', color: '#9ca3af' }}>
                    {row.sample_size}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
