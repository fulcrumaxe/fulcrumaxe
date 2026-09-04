/**
 * LoopIdleRatioTile — Loop Idle Ratio (24h).
 *
 * Fetches stats.loop_idle_ratio RPC.
 * Shows idle% as a large number, color coded:
 *   red >40%, amber >20%, green otherwise.
 * N/A shown when sample_size < 5.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'
import TileFetchError, { isTransportError, TileBackendError } from './TileFetchError'

export interface IdleRatioResponse {
  ratio: number | null
  idle_count: number
  sample_size: number
}

const styles: Record<string, React.CSSProperties> = {
  ...sharedStyles,
  idleTile: {
    display: 'flex',
    alignItems: 'center',
    padding: '16px 20px',
    background: '#111827',
    border: '1px solid #1f2937',
    borderRadius: 6,
    gap: 8,
  },
}

interface Props {
  refreshSignal?: number
}

export default function LoopIdleRatioTile({ refreshSignal }: Props) {
  const [data, setData] = useState<IdleRatioResponse | null>(null)
  const [fetchError, setFetchError] = useState<unknown>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    try {
      const resp = await jsonRpc<IdleRatioResponse>('stats.loop_idle_ratio', {})
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
    <section style={styles.section} aria-label="Loop Idle Ratio (24h)">
      <h2 style={styles.sectionHeading}>Loop Idle Ratio (24h)</h2>
      {fetchError ? (
        isTransportError(fetchError) ? <TileFetchError error={fetchError} /> : <TileBackendError error={fetchError} />
      ) : !data || data.sample_size < 5 ? (
        <div style={styles.state} role="status" data-testid="idle-ratio-na">
          N/A — {data ? `${data.sample_size} iteration(s) recorded` : 'no data yet'} (need ≥5).
        </div>
      ) : (
        <div style={styles.idleTile} data-testid="idle-ratio-tile">
          <span
            role="status"
            style={{
              fontSize: 32,
              fontWeight: 700,
              color:
                data.ratio! > 0.4
                  ? '#ef4444'
                  : data.ratio! > 0.2
                  ? '#f59e0b'
                  : '#22c55e',
            }}
            data-testid="idle-ratio-value"
          >
            {(data.ratio! * 100).toFixed(1)}%
          </span>
          <span style={{ color: '#9ca3af', fontSize: 13, marginLeft: 12 }}>
            {data.idle_count} idle / {data.sample_size} iterations
          </span>
        </div>
      )}
    </section>
  )
}
