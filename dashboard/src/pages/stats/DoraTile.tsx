/**
 * DoraTile — DORA metrics + KPI velocity and cycle time.
 *
 * Surfaces the analytics-engineer snapshot on the dashboard:
 *   deploy frequency/day, lead time p50, change failure rate,
 *   velocity (all-time/day), cycle time median.
 *
 * Calls stats.dora RPC, refreshes every 60 s.
 * change_failure_rate_pct is rendered verbatim — it may be "n/a" when
 * no bug-filing data exists.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

export interface DoraResponse {
  applicable: boolean
  deploy_frequency_per_day: number
  lead_time_minutes_p50: number
  change_failure_rate_pct: string  // verbatim — "n/a" or numeric string
  velocity_all_time_per_day: number
  cycle_time_median_hours: number | null
  window_start: string
}

interface Props {
  refreshSignal?: number
}

interface MetricRowProps {
  label: string
  value: string
  unit?: string
  testId?: string
}

function MetricRow({ label, value, unit, testId }: MetricRowProps) {
  const styles: Record<string, React.CSSProperties> = {
    row: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'baseline',
      padding: '6px 0',
      borderBottom: '1px solid #1f2937',
    },
    label: {
      color: '#9ca3af',
      fontSize: 13,
    },
    valueWrap: {
      display: 'flex',
      alignItems: 'baseline',
      gap: 4,
    },
    value: {
      color: '#f9fafb',
      fontSize: 15,
      fontWeight: 600,
    },
    unit: {
      color: '#9ca3af',
      fontSize: 11,
    },
  }

  return (
    <div style={styles.row} data-testid={testId}>
      <span style={styles.label}>{label}</span>
      <span style={styles.valueWrap}>
        <span style={styles.value}>{value}</span>
        {unit && <span style={styles.unit}>{unit}</span>}
      </span>
    </div>
  )
}

function fmt(n: number | null | undefined, decimals = 2): string {
  if (n == null || n < 0) return 'n/a'
  return n.toFixed(decimals)
}

export default function DoraTile({ refreshSignal }: Props) {
  const [data, setData] = useState<DoraResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<DoraResponse>('stats.dora', {})
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

  const cardStyles: Record<string, React.CSSProperties> = {
    ...sharedStyles,
    tileCard: {
      background: '#111827',
      border: '1px solid #1f2937',
      borderRadius: 8,
      padding: '16px 20px',
      marginTop: 16,
    },
    windowNote: {
      color: '#9ca3af',
      fontSize: 11,
      marginTop: 8,
      textAlign: 'right' as const,
    },
  }

  return (
    <section style={cardStyles.section}>
      <h2 style={cardStyles.sectionHeading}>DORA + KPI Metrics</h2>
      <div style={cardStyles.tileCard}>
        {loading && !data && (
          <div style={cardStyles.state} role="status">Loading DORA metrics…</div>
        )}
        {error && !data && (
          <div style={{ ...cardStyles.state, color: '#ef4444' }} role="alert">{error}</div>
        )}
        {data && data.applicable === false && (
          <div style={cardStyles.state} data-testid="dora-empty-state">
            No release or KPI data yet
          </div>
        )}
        {data && data.applicable !== false && (
          <div data-testid="dora-tile">
            <MetricRow
              label="Deploy frequency"
              value={fmt(data.deploy_frequency_per_day)}
              unit="deploys/day"
              testId="dora-deploy-frequency"
            />
            <MetricRow
              label="Lead time (p50)"
              value={fmt(data.lead_time_minutes_p50, 1)}
              unit="min"
              testId="dora-lead-time"
            />
            <MetricRow
              label="Change failure rate"
              value={String(data.change_failure_rate_pct)}
              unit={data.change_failure_rate_pct !== 'n/a' ? '%' : undefined}
              testId="dora-cfr"
            />
            <MetricRow
              label="Velocity (all-time)"
              value={fmt(data.velocity_all_time_per_day)}
              unit="discussions/day"
              testId="dora-velocity"
            />
            <MetricRow
              label="Cycle time (median)"
              value={data.cycle_time_median_hours != null && data.cycle_time_median_hours >= 0
                ? fmt(data.cycle_time_median_hours, 1)
                : 'n/a'}
              unit={data.cycle_time_median_hours != null && data.cycle_time_median_hours >= 0
                ? 'hr'
                : undefined}
              testId="dora-cycle-time"
            />
            {data.window_start && (
              <p style={cardStyles.windowNote}>as of {data.window_start}</p>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
