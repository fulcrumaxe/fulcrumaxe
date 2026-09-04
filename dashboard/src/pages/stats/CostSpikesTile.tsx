/**
 * CostSpikesTile — Cost Spikes (24h) section.
 *
 * Fetches stats.cost_spike_history RPC, shows spike count and severity.
 * Color-coded: green = 0 spikes, amber = 1-2, red = 3+.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { formatTime } from '../../lib/safeDate'
import TileFetchError, { isTransportError, TileBackendError } from './TileFetchError'

interface CostSpikeEntry {
  ts_iso: string
  value: number
  mu: number
  sigma: number
}

export interface CostSpikeResponse {
  spikes: CostSpikeEntry[]
  count: number
  last_spike_iso: string | null
}

const styles: Record<string, React.CSSProperties> = {
  state: {
    color: '#6b7280',
    fontSize: 14,
    padding: '40px 0',
    textAlign: 'center',
  },
  section: {
    marginTop: 32,
  },
  sectionHeading: {
    margin: '0 0 12px',
    fontSize: 18,
    fontWeight: 600,
    color: '#f9fafb',
  },
  spikeTile: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: 4,
    padding: '16px 20px',
    background: '#111827',
    border: '1px solid #374151',
    borderRadius: 8,
    maxWidth: 360,
  },
  spikeCount: {
    fontSize: 36,
    fontWeight: 700,
    color: '#f9fafb',
    lineHeight: 1.1,
  },
  spikeLabel: {
    fontSize: 14,
    color: '#9ca3af',
  },
  spikeDetail: {
    fontSize: 12,
    color: '#6b7280',
    marginTop: 2,
  },
}

interface Props {
  /** Interval in ms for auto-refresh. 0 means no auto-refresh (parent controls). */
  refreshSignal?: number
}

export default function CostSpikesTile({ refreshSignal }: Props) {
  const [data, setData] = useState<CostSpikeResponse | null>(null)
  const [fetchError, setFetchError] = useState<unknown>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    try {
      const resp = await jsonRpc<CostSpikeResponse>('stats.cost_spike_history', { hours: 24 })
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
    <section style={styles.section} aria-label="Cost Spikes (24h)">
      <h2 style={styles.sectionHeading}>Cost Spikes (24h)</h2>
      {fetchError ? (
        isTransportError(fetchError) ? <TileFetchError error={fetchError} /> : <TileBackendError error={fetchError} />
      ) : data === null ? (
        <div style={styles.state} role="status">Loading spike data…</div>
      ) : data.count === 0 ? (
        <div style={styles.spikeTile} data-testid="cost-spikes-tile">
          <span role="status" style={styles.spikeCount}>0</span>
          <span style={styles.spikeLabel}>spikes detected</span>
          <span style={{ ...styles.spikeDetail, color: '#22c55e' }}>Normal — no 3σ exceedances in 24h</span>
        </div>
      ) : (
        <div
          style={{ ...styles.spikeTile, borderColor: data.count >= 3 ? '#ef4444' : '#f59e0b' }}
          data-testid="cost-spikes-tile"
        >
          <span role="alert" style={{ ...styles.spikeCount, color: data.count >= 3 ? '#ef4444' : '#f59e0b' }}>
            {data.count}
          </span>
          <span style={styles.spikeLabel}>spike{data.count !== 1 ? 's' : ''} in 24h</span>
          {data.last_spike_iso && (
            <span style={styles.spikeDetail}>
              Last: {formatTime(data.last_spike_iso)}
            </span>
          )}
          {data.count >= 3 && (
            <span style={{ ...styles.spikeDetail, color: '#ef4444', fontWeight: 600 }}>
              Auto-throttle may be active (gates.budget_check)
            </span>
          )}
          {data.spikes.slice(0, 3).map((s, i) => (
            <span key={i} style={{ ...styles.spikeDetail, fontSize: 11 }}>
              ${s.value.toFixed(4)} vs threshold ${(s.mu + 3 * s.sigma).toFixed(4)}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
