/**
 * ActiveAgentsTile — concurrent active agents over the last 24h.
 *
 * Fetches runs.active_over_time (1-min buckets) and renders a simple
 * bar chart so you can see at a glance how busy the team has been.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { formatAbsolute } from '../../lib/safeDate'
import { sharedStyles } from './styles'

interface ActivePoint {
  ts: string
  count: number
}

interface ActiveOverTimeResponse {
  points: ActivePoint[]
}

interface Props {
  refreshSignal?: number
}

export default function ActiveAgentsTile({ refreshSignal }: Props) {
  const [points, setPoints] = useState<ActivePoint[]>([])
  const [loading, setLoading] = useState(true)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<ActiveOverTimeResponse>('runs.active_over_time', {
        bucket_seconds: 300, // 5-min buckets to keep the chart readable
      })
      setPoints(resp.points ?? [])
    } catch {
      // non-fatal — display empty state
      setPoints([])
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

  const visiblePoints = points.slice(-288) // last 24h at 5-min buckets
  const peakCount = visiblePoints.length ? Math.max(...visiblePoints.map(p => p.count)) : 0
  // Y-axis ceiling: at least 2× the peak and at least the fleet cap (8).
  // Without this, a peak of 1 fills every bucket to 100% — a solid blue block.
  const FLEET_CAP = 8
  const yMax = Math.max(peakCount * 2, FLEET_CAP, 1)
  // The backend always generates all 288 time buckets even when no runs exist, so
  // checking visiblePoints.length would show a phantom chart with all-zero bars.
  // Only show the chart when at least one bucket has a non-zero count.
  const hasActivity = visiblePoints.some(p => p.count > 0)
  // Count how many currently-in-flight runs (no end_ts) inflate this chart.
  // These are runs the backend counts as active until end_ts is written.
  const inFlightCount = visiblePoints.length > 0
    ? visiblePoints[visiblePoints.length - 1]?.count ?? 0
    : 0
  const ariaLabel = `Active Agents last 24h — peak ${peakCount} concurrent, ${visiblePoints.length} 5-min buckets`

  return (
    <section style={sharedStyles.section} data-testid="active-agents-tile">
      <h2 style={sharedStyles.sectionHeading}>Active Agents (last 24h)</h2>

      {loading ? (
        <div style={sharedStyles.state}>Loading…</div>
      ) : !hasActivity ? (
        <div style={sharedStyles.state}>
          No agent run data yet. Populated after the first spawn via spawn-agent.sh.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <div
            aria-label={ariaLabel}
            role="img"
            style={{
              display: 'flex',
              alignItems: 'flex-end',
              gap: 2,
              height: 120,
              padding: '8px 0',
              minWidth: visiblePoints.length * 4,
            }}
            title={`Peak: ${peakCount} concurrent agents`}
          >
            {visiblePoints.map((p, i) => {
              const heightPct = (p.count / yMax) * 100
              const color = p.count === 0 ? '#1f2937' : p.count >= 4 ? '#22c55e' : '#3b82f6'
              return (
                <div
                  key={i}
                  title={`${formatAbsolute(p.ts)}: ${p.count} active`}
                  style={{
                    flex: '1 0 4px',
                    height: `${Math.max(heightPct, p.count > 0 ? 4 : 1)}%`,
                    background: color,
                    borderRadius: 2,
                    minWidth: 4,
                    transition: 'height 0.2s',
                  }}
                />
              )
            })}
          </div>
          <div style={{ fontSize: 11, color: '#6b7280', marginTop: 4 }}>
            Peak: {peakCount} concurrent · {visiblePoints.length} 5-min buckets
            {inFlightCount > 0 && (
              <span
                style={{ color: '#f59e0b', marginLeft: 8 }}
                title="In-flight runs (no end_ts yet) are counted as active. These appear here but not in Recent Runs until post-agent-hook records their completion."
              >
                · {inFlightCount} in-flight (no end_ts)
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
