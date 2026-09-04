/**
 * AvgFixRoundsTile — Avg Fix Rounds per PR (24h).
 *
 * Fetches stats.avg_fix_rounds_per_pr RPC.
 * Shows average rounds and distribution breakdown.
 * Color coded: red >2, amber >=1, green otherwise.
 * N/A shown when sample_size < 5.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'
import TileFetchError, { isTransportError, TileBackendError } from './TileFetchError'

export interface FixRoundsResponse {
  avg_last_24h: number | null
  sample_size: number
  distribution: Record<string, number>
}

const styles: Record<string, React.CSSProperties> = {
  ...sharedStyles,
  fixRoundsCard: {
    background: '#111827',
    border: '1px solid #1f2937',
    borderRadius: 8,
    padding: '20px 24px',
    display: 'inline-flex',
    flexDirection: 'column' as const,
    gap: 12,
    minWidth: 240,
  },
  fixRoundsMain: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 8,
  },
  fixRoundsValue: {
    fontSize: 36,
    fontWeight: 700,
    fontVariantNumeric: 'tabular-nums',
  },
  fixRoundsLabel: {
    fontSize: 14,
    color: '#9ca3af',
  },
  fixRoundsSample: {
    fontSize: 12,
    color: '#6b7280',
    marginLeft: 4,
  },
  fixRoundsDist: {
    display: 'flex',
    flexWrap: 'wrap' as const,
    gap: 8,
  },
  fixRoundsDistItem: {
    background: '#1f2937',
    borderRadius: 4,
    padding: '2px 8px',
    fontSize: 12,
    color: '#d1d5db',
  },
}

interface Props {
  refreshSignal?: number
}

export default function AvgFixRoundsTile({ refreshSignal }: Props) {
  const [data, setData] = useState<FixRoundsResponse | null>(null)
  const [fetchError, setFetchError] = useState<unknown>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    try {
      const resp = await jsonRpc<FixRoundsResponse>('stats.avg_fix_rounds_per_pr', {})
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
    <section style={styles.section} aria-label="Avg Fix Rounds per PR (24h)">
      <h2 style={styles.sectionHeading}>Avg Fix Rounds per PR (24h)</h2>
      {fetchError ? (
        isTransportError(fetchError) ? <TileFetchError error={fetchError} /> : <TileBackendError error={fetchError} />
      ) : data === null || data.sample_size === 0 ? (
        <div style={styles.state} role="status" data-testid="fix-rounds-empty">
          No merge data yet. Written on each PR merge by post-merge-hook.
        </div>
      ) : (
        <div style={styles.fixRoundsCard} data-testid="fix-rounds-card">
          <div style={styles.fixRoundsMain}>
            <span
              role="status"
              style={{
                ...styles.fixRoundsValue,
                color: (() => {
                  if (data.sample_size < 5) return '#6b7280'
                  const avg = data.avg_last_24h ?? 0
                  if (avg > 2) return '#ef4444'
                  if (avg >= 1) return '#f59e0b'
                  return '#22c55e'
                })(),
              }}
              data-testid="fix-rounds-avg"
            >
              {data.sample_size < 5
                ? 'N/A'
                : data.avg_last_24h !== null
                ? data.avg_last_24h.toFixed(2)
                : 'N/A'}
            </span>
            <span style={styles.fixRoundsLabel}>avg rounds</span>
            <span style={styles.fixRoundsSample} data-testid="fix-rounds-sample">
              n={data.sample_size} PRs
              {data.sample_size < 5 && ' (need ≥5 for avg)'}
            </span>
          </div>
          {Object.keys(data.distribution).length > 0 && (
            <div style={styles.fixRoundsDist} data-testid="fix-rounds-distribution">
              {Object.entries(data.distribution)
                .sort(([a], [b]) => Number(a) - Number(b))
                .map(([rounds, count]) => (
                  <span key={rounds} style={styles.fixRoundsDistItem}>
                    {rounds} round{rounds === '1' ? '' : 's'}: {count}
                  </span>
                ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
