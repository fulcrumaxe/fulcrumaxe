/**
 * TeamLeadTokensTile — Team Lead Token Usage (24h) table.
 *
 * Fetches stats.team_lead_tokens RPC, shows avg / p50 / p95 per iteration.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'
import TileFetchError, { isTransportError, TileBackendError } from './TileFetchError'

export interface TeamLeadTokensResponse {
  avg: number | null
  p50: number | null
  p95: number | null
  sample_size: number
}

const styles: Record<string, React.CSSProperties> = {
  ...sharedStyles,
}

interface Props {
  refreshSignal?: number
}

export default function TeamLeadTokensTile({ refreshSignal }: Props) {
  const [data, setData] = useState<TeamLeadTokensResponse | null>(null)
  const [fetchError, setFetchError] = useState<unknown>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    try {
      const resp = await jsonRpc<TeamLeadTokensResponse>('stats.team_lead_tokens', { since_hours: 24 })
      setData(resp)
      setFetchError(null)
    } catch (err) {
      setData(null)
      setFetchError(err)
    }
  }, [])

  useEffect(() => {
    loadData()
    intervalRef.current = setInterval(loadData, 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [loadData, refreshSignal])

  return (
    <section style={styles.section} aria-label="Team Lead Token Usage (24h)">
      <h2 style={styles.sectionHeading}>Team Lead Token Usage (24h)</h2>
      {fetchError ? (
        isTransportError(fetchError) ? <TileFetchError error={fetchError} /> : <TileBackendError error={fetchError} />
      ) : !data || data.sample_size === 0 ? (
        <div style={styles.state} role="status" data-testid="tl-tokens-empty">
          No iterations recorded yet. Written by /loop step 7.5.
        </div>
      ) : (
        <table style={styles.table} data-testid="tl-tokens-table">
          <thead>
            <tr>
              <th style={styles.th} scope="col">Metric</th>
              <th style={{ ...styles.th, textAlign: 'right' }} scope="col">Tokens / iteration</th>
            </tr>
          </thead>
          <tbody>
            {(['avg', 'p50', 'p95'] as const).map(stat => {
              const val = data[stat]
              const display =
                val === null
                  ? 'N/A'
                  : val.toLocaleString(undefined, { maximumFractionDigits: 0 })
              return (
                <tr key={stat} style={styles.tr}>
                  <td style={styles.td}>{stat === 'avg' ? 'Average' : stat === 'p50' ? 'Median (p50)' : 'p95'}</td>
                  <td style={{ ...styles.td, textAlign: 'right', color: val === null ? '#6b7280' : '#f9fafb' }}>
                    {display}
                  </td>
                </tr>
              )
            })}
            <tr style={styles.tr}>
              <td style={{ ...styles.td, color: '#9ca3af' }}>Sample size</td>
              <td style={{ ...styles.td, textAlign: 'right', color: '#9ca3af' }}>
                {data.sample_size}
              </td>
            </tr>
          </tbody>
        </table>
      )}
    </section>
  )
}
