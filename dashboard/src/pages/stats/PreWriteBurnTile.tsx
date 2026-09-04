/**
 * PreWriteBurnTile — Executor runs with high pre-Write burn ratio.
 *
 * Lists executor runs where (first_write_turn / total_turns) > 10%.
 * High values indicate the agent spent many turns reading/thinking before
 * writing code, which is a signal that spawn-template context could be leaner.
 *
 * Calls stats.pre_write_burn RPC, refreshes every 60s.
 * Sorted by ratio DESC so worst offenders appear first.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

export interface PreWriteBurnRow {
  agent_id: string
  role: string
  discussion: number | null
  pr: number | null
  first_write_turn: number
  total_turns: number
  ratio_pct: number
  input_tok: number | null
  event_id: string | null
}

export interface PreWriteBurnResponse {
  rows: PreWriteBurnRow[]
}

interface Props {
  refreshSignal?: number
}

function ratioColor(pct: number): string {
  if (pct >= 50) return '#ef4444'  // red: over half the run was pre-Write
  if (pct >= 25) return '#f59e0b'  // amber
  return '#d1d5db'                 // neutral: just above 10% threshold
}

const styles: Record<string, React.CSSProperties> = {
  ...sharedStyles,
  tableWrap: {
    overflowX: 'auto' as const,
    marginTop: 8,
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse' as const,
    fontSize: 13,
    color: '#d1d5db',
  },
  th: {
    textAlign: 'left' as const,
    padding: '6px 10px',
    borderBottom: '1px solid #374151',
    color: '#9ca3af',
    fontWeight: 600,
    whiteSpace: 'nowrap' as const,
  },
  td: {
    padding: '6px 10px',
    borderBottom: '1px solid #1f2937',
    verticalAlign: 'middle' as const,
  },
  mono: {
    fontFamily: 'monospace',
    fontSize: 11,
    color: '#9ca3af',
  },
}

export default function PreWriteBurnTile({ refreshSignal }: Props) {
  const [data, setData] = useState<PreWriteBurnResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<PreWriteBurnResponse>('stats.pre_write_burn', {})
      setData(resp)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    intervalRef.current = setInterval(fetchData, 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [fetchData, refreshSignal])

  return (
    <section style={styles.section} aria-label="Pre-Write Burn (executor runs > 10%)">
      <h2 style={styles.sectionHeading}>Pre-Write Burn (executor runs &gt; 10%)</h2>
      {loading && !data && (
        <div style={styles.state} role="status">Loading pre-Write burn data…</div>
      )}
      {error && !data && (
        <div style={{ ...styles.state, color: '#ef4444' }} role="alert">{error}</div>
      )}
      {data && data.rows.length === 0 && (
        <div style={styles.state} role="status" data-testid="pre-write-burn-empty">
          No executor runs with pre-Write burn yet.
        </div>
      )}
      {data && data.rows.length > 0 && (
        <div style={styles.tableWrap} data-testid="pre-write-burn-tile">
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th} scope="col">Agent</th>
                <th style={styles.th} scope="col">Discussion</th>
                <th style={styles.th} scope="col">PR</th>
                <th style={styles.th} scope="col">First Write</th>
                <th style={styles.th} scope="col">Total Turns</th>
                <th style={styles.th} scope="col">Ratio</th>
                <th style={styles.th} scope="col">Input Tok</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.agent_id}>
                  <td style={{ ...styles.td, ...styles.mono }}>
                    {row.agent_id.length > 20
                      ? row.agent_id.slice(0, 20) + '…'
                      : row.agent_id}
                  </td>
                  <td style={styles.td}>
                    {row.discussion != null ? `#${row.discussion}` : '—'}
                  </td>
                  <td style={styles.td}>
                    {row.pr != null ? `#${row.pr}` : '—'}
                  </td>
                  <td style={{ ...styles.td, textAlign: 'center' as const }}>
                    {row.first_write_turn}
                  </td>
                  <td style={{ ...styles.td, textAlign: 'center' as const }}>
                    {row.total_turns}
                  </td>
                  <td style={{ ...styles.td, fontWeight: 600, color: ratioColor(row.ratio_pct) }}>
                    {row.ratio_pct.toFixed(1)}%
                  </td>
                  <td style={{ ...styles.td, color: '#9ca3af' }}>
                    {row.input_tok != null ? row.input_tok.toLocaleString() : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
