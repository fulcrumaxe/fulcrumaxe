/**
 * CostPerOutcomeTile — Cost per merged PR (USD).
 *
 * Fetches stats.cost_per_outcome RPC.
 * Shows top-10 PRs sorted by usd descending with per-row top-role callout.
 * Auto-registers via #1406 tile registry (no index.ts/StatsPage edit needed).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

interface RoleBreakdown {
  role: string
  input_tokens: number
  output_tokens: number
  usd: number
}

interface CostRow {
  pr: number
  usd: number
  total_tokens: number
  fix_rounds: number
  by_role: RoleBreakdown[]
}

interface CostPerOutcomeResponse {
  rows: CostRow[]
}

interface Props {
  refreshSignal?: number
}

export default function CostPerOutcomeTile({ refreshSignal }: Props) {
  const [rows, setRows] = useState<CostRow[]>([])
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    const resp = await jsonRpc<CostPerOutcomeResponse>('stats.cost_per_outcome', {}).catch(
      () => ({ rows: [] }),
    )
    // Server returns sorted desc; slice to top 10 for display
    setRows((resp.rows ?? []).slice(0, 10))
  }, [])

  useEffect(() => {
    loadData()
    intervalRef.current = setInterval(loadData, 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [loadData, refreshSignal])

  return (
    <section style={sharedStyles.section} aria-label="Cost per Outcome (top 10 PRs)">
      <h2 style={sharedStyles.sectionHeading}>Cost per Outcome (top 10 PRs)</h2>
      {rows.length === 0 ? (
        <div style={sharedStyles.state} role="status" data-testid="cost-per-outcome-empty">
          No cost data yet. Rows appear after PRs are merged with agent cost records.
        </div>
      ) : (
        <table style={sharedStyles.table} data-testid="cost-per-outcome-table">
          <thead>
            <tr>
              <th style={sharedStyles.th} scope="col">PR</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }} scope="col">USD</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }} scope="col">Tokens</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }} scope="col">Fix Rounds</th>
              <th style={sharedStyles.th} scope="col">Top Role</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const topRole = row.by_role[0]?.role ?? '—'
              return (
                <tr key={row.pr} style={sharedStyles.tr}>
                  <td style={sharedStyles.td}>#{row.pr}</td>
                  <td style={{ ...sharedStyles.td, textAlign: 'right', fontWeight: 600 }}>
                    ${row.usd.toFixed(2)}
                  </td>
                  <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#9ca3af' }}>
                    {row.total_tokens.toLocaleString()}
                  </td>
                  <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#9ca3af' }}>
                    {row.fix_rounds}
                  </td>
                  <td style={{ ...sharedStyles.td, color: '#9ca3af' }}>{topRole}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
