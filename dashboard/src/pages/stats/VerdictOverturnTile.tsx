/**
 * VerdictOverturnTile — Verdict Overturn Rate (24h) table.
 *
 * Fetches stats.verdict_overturns RPC.
 * Shows per-role overturn rate, sorted worst-first.
 * N/A for roles with fewer than 5 pass/done verdicts in the window.
 * Color coded: red >20%, amber >5%, green otherwise.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

export interface VerdictOverturnRow {
  role: string
  overturns: number
  total_pass: number
  overturn_rate: number | null
  sample_size: number
}

interface VerdictOverturnResponse {
  rows: VerdictOverturnRow[]
}

interface Props {
  refreshSignal?: number
}

export default function VerdictOverturnTile({ refreshSignal }: Props) {
  const [rows, setRows] = useState<VerdictOverturnRow[]>([])
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    const resp = await jsonRpc<VerdictOverturnResponse>('stats.verdict_overturns', {}).catch(
      () => ({ rows: [] }),
    )
    setRows(resp.rows ?? [])
  }, [])

  useEffect(() => {
    loadData()
    intervalRef.current = setInterval(loadData, 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [loadData, refreshSignal])

  return (
    <section style={sharedStyles.section} aria-label="Verdict Overturn Rate (24h)">
      <h2 style={sharedStyles.sectionHeading}>Verdict Overturn Rate (24h)</h2>
      {rows.length === 0 ? (
        <div style={sharedStyles.state} role="status">
          No overturn data yet. Emitted when a later agent contradicts an earlier pass/done on the
          same PR.
        </div>
      ) : (
        <table style={sharedStyles.table} data-testid="verdict-overturn-table">
          <thead>
            <tr>
              <th style={sharedStyles.th} scope="col">Role</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }} scope="col">Overturn Rate</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }} scope="col">Overturns</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }} scope="col">Passes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const color =
                row.overturn_rate === null
                  ? '#6b7280'
                  : row.overturn_rate > 0.2
                    ? '#ef4444'
                    : row.overturn_rate > 0.05
                      ? '#f59e0b'
                      : '#22c55e'

              const rateText =
                row.overturn_rate === null ? 'N/A' : `${(row.overturn_rate * 100).toFixed(1)}%`

              return (
                <tr key={row.role} style={sharedStyles.tr}>
                  <td style={sharedStyles.td}>{row.role}</td>
                  <td
                    style={{ ...sharedStyles.td, textAlign: 'right', color, fontWeight: 600 }}
                    data-testid={`overturn-rate-${row.role}`}
                  >
                    {rateText}
                  </td>
                  <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#9ca3af' }}>
                    {row.overturns}
                  </td>
                  <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#9ca3af' }}>
                    {row.total_pass}
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
