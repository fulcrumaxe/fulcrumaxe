/**
 * WeeklyVelocityTile — PRs merged in the last 7 days.
 *
 * Headline number + 7-bar inline sparkline + trend indicator vs prior 7 days.
 * Calls stats.weekly_velocity RPC, refreshes every 60 s.
 * Mirrors RoleSuccessRateTile ergonomics.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

export interface VelocityByDay {
  date: string  // "YYYY-MM-DD"
  count: number
}

export interface WeeklyVelocityResponse {
  applicable: boolean  // false → no PRs in 14-day window; show empty-state
  total: number
  by_day: VelocityByDay[]
  window_start: string
  window_end: string
  prev_total: number
  trend_pct: number
}

interface Props {
  refreshSignal?: number
}

const BAR_MAX_HEIGHT = 32  // px — max height of sparkline bars
const BAR_WIDTH = 14       // px — width of each bar
const BAR_GAP = 3          // px — gap between bars

function TrendArrow({ pct }: { pct: number }) {
  if (pct > 5) {
    return <span style={{ color: '#22c55e', fontSize: 13, marginLeft: 6 }}>&#8593; {pct}%</span>
  }
  if (pct < -5) {
    return <span style={{ color: '#ef4444', fontSize: 13, marginLeft: 6 }}>&#8595; {Math.abs(pct)}%</span>
  }
  return <span style={{ color: '#9ca3af', fontSize: 13, marginLeft: 6 }}>&#8594; {pct}%</span>
}

function Sparkline({ days }: { days: VelocityByDay[] }) {
  if (!days.length) return null
  const maxCount = Math.max(...days.map(d => d.count), 1)
  const totalWidth = days.length * (BAR_WIDTH + BAR_GAP) - BAR_GAP

  return (
    <svg
      width={totalWidth}
      height={BAR_MAX_HEIGHT + 4}
      style={{ display: 'block', marginTop: 8 }}
      aria-label="Daily PR merge sparkline"
    >
      {days.map((day, i) => {
        const barHeight = Math.max(2, Math.round((day.count / maxCount) * BAR_MAX_HEIGHT))
        const x = i * (BAR_WIDTH + BAR_GAP)
        const y = BAR_MAX_HEIGHT - barHeight + 2
        const color = day.count === 0 ? '#374151' : '#3b82f6'
        return (
          <g key={day.date}>
            <rect
              x={x}
              y={y}
              width={BAR_WIDTH}
              height={barHeight}
              fill={color}
              rx={2}
            >
              <title>{day.date}: {day.count} PR{day.count !== 1 ? 's' : ''}</title>
            </rect>
          </g>
        )
      })}
    </svg>
  )
}

export default function WeeklyVelocityTile({ refreshSignal }: Props) {
  const [data, setData] = useState<WeeklyVelocityResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<WeeklyVelocityResponse>('stats.weekly_velocity', {})
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

  const styles: Record<string, React.CSSProperties> = {
    ...sharedStyles,
    tileCard: {
      background: '#111827',
      border: '1px solid #1f2937',
      borderRadius: 8,
      padding: '16px 20px',
      marginTop: 16,
    },
    headline: {
      fontSize: 42,
      fontWeight: 700,
      color: '#f9fafb',
      lineHeight: 1.1,
      margin: 0,
    },
    subLabel: {
      color: '#6b7280',
      fontSize: 12,
      marginTop: 4,
    },
    headlineRow: {
      display: 'flex',
      alignItems: 'baseline',
      gap: 4,
      flexWrap: 'wrap' as const,
    },
  }

  return (
    <section style={styles.section} aria-label="Weekly Velocity">
      <h2 style={styles.sectionHeading}>Weekly Velocity</h2>
      <div style={styles.tileCard}>
        {loading && !data && (
          <div style={styles.state} role="status">Loading velocity data…</div>
        )}
        {error && !data && (
          <div style={{ ...styles.state, color: '#ef4444' }} role="alert">{error}</div>
        )}
        {data && data.applicable === false && (
          <div style={styles.state} data-testid="weekly-velocity-empty">No PRs in last 14 days</div>
        )}
        {data && data.applicable !== false && data.total > 0 && (
          <div data-testid="weekly-velocity-tile">
            <div style={styles.headlineRow}>
              <span role="status" style={styles.headline}>{data.total}</span>
              <TrendArrow pct={data.trend_pct} />
            </div>
            <p style={styles.subLabel}>PRs merged in the last 7 days</p>
            <Sparkline days={data.by_day} />
            <p style={{ ...styles.subLabel, marginTop: 6 }}>
              Prior 7 days: {data.prev_total} PR{data.prev_total !== 1 ? 's' : ''}
            </p>
          </div>
        )}
      </div>
    </section>
  )
}
