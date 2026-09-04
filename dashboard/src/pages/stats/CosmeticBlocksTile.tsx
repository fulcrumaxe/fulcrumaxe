/**
 * CosmeticBlocksTile — Cosmetic Block Events (7d) section.
 *
 * Shows total cosmetic-retry blocks in the last 24h and a 7-day hourly
 * sparkline sourced from the cosmetic-blocks JSONL log.
 *
 * Color-coded: green = 0 blocks, amber = 1-4, red = 5+.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import TileFetchError, { isTransportError, TileBackendError } from './TileFetchError'

interface HourlyBucket {
  hour_iso: string
  count: number
}

export interface CosmeticBlocksResponse {
  total_24h: number
  hourly_7d: HourlyBucket[]
}

const styles: Record<string, React.CSSProperties> = {
  section: {
    marginTop: 32,
  },
  sectionHeading: {
    margin: '0 0 12px',
    fontSize: 18,
    fontWeight: 600,
    color: '#f9fafb',
  },
  tile: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: 6,
    padding: '16px 20px',
    background: '#111827',
    border: '1px solid #374151',
    borderRadius: 8,
    maxWidth: 420,
  },
  countRow: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 8,
  },
  count: {
    fontSize: 36,
    fontWeight: 700,
    lineHeight: 1.1,
  },
  label: {
    fontSize: 14,
    color: '#9ca3af',
  },
  detail: {
    fontSize: 12,
    color: '#6b7280',
    marginTop: 2,
  },
  sparklineWrap: {
    display: 'flex',
    alignItems: 'flex-end',
    gap: 2,
    height: 40,
    marginTop: 8,
  },
  state: {
    color: '#6b7280',
    fontSize: 14,
    padding: '40px 0',
    textAlign: 'center',
  },
}

function sparkColor(total_24h: number): string {
  if (total_24h === 0) return '#22c55e'
  if (total_24h < 5) return '#f59e0b'
  return '#ef4444'
}

interface SparklineProps {
  buckets: HourlyBucket[]
  color: string
}

function Sparkline({ buckets, color }: SparklineProps) {
  if (buckets.length === 0) {
    return <div style={{ ...styles.detail, fontStyle: 'italic' }}>No blocks in 7d</div>
  }
  const max = Math.max(...buckets.map(b => b.count), 1)
  return (
    <div style={styles.sparklineWrap} title="Hourly blocks over 7d">
      {buckets.map((b, i) => {
        const heightPct = (b.count / max) * 100
        return (
          <div
            key={i}
            title={`${b.hour_iso}: ${b.count} block${b.count !== 1 ? 's' : ''}`}
            style={{
              flex: '0 0 4px',
              height: `${Math.max(heightPct, 4)}%`,
              background: color,
              borderRadius: 1,
              opacity: 0.85,
            }}
          />
        )
      })}
    </div>
  )
}

interface Props {
  refreshSignal?: number
}

export default function CosmeticBlocksTile({ refreshSignal }: Props) {
  const [data, setData] = useState<CosmeticBlocksResponse | null>(null)
  const [fetchError, setFetchError] = useState<unknown>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadData = useCallback(async () => {
    try {
      const resp = await jsonRpc<CosmeticBlocksResponse>('stats.cosmetic_blocks', {})
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

  const color = data ? sparkColor(data.total_24h) : '#6b7280'

  return (
    <section style={styles.section} aria-label="Cosmetic Retry Blocks (7d)">
      <h2 style={styles.sectionHeading}>Cosmetic Retry Blocks (7d)</h2>
      {fetchError ? (
        isTransportError(fetchError) ? <TileFetchError error={fetchError} /> : <TileBackendError error={fetchError} />
      ) : data === null ? (
        <div style={styles.state} role="status">Loading…</div>
      ) : (
        <div
          style={{ ...styles.tile, borderColor: color }}
          data-testid="cosmetic-blocks-tile"
        >
          <div style={styles.countRow}>
            <span role="status" style={{ ...styles.count, color }}>{data.total_24h}</span>
            <span style={styles.label}>block{data.total_24h !== 1 ? 's' : ''} in 24h</span>
          </div>
          {data.total_24h === 0 ? (
            <span style={{ ...styles.detail, color: '#22c55e' }}>
              No cosmetic-retry loops detected
            </span>
          ) : (
            <span style={styles.detail}>
              {data.total_24h >= 5 ? 'High block rate — check agent transcripts' : 'Some blocks detected'}
            </span>
          )}
          <Sparkline buckets={data.hourly_7d} color={color} />
        </div>
      )}
    </section>
  )
}
