/**
 * DurationPercentilesTile — p50 / p95 duration by role table.
 *
 * Fetches runs.percentiles once per role then merges rows into a table.
 * Outlier coloring: p95 > 600s = red, > 300s = amber, otherwise default.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

const ROLES = [
  'executor',
  'code-reviewer',
  'security-reviewer',
  'project-manager',
  'impl-coordinator',
  'acceptance-tester',
  'browser-tester',
]

interface PercentilesResponse {
  p50: number | null
  p95: number | null
  p99: number | null
  sample_size: number
}

interface RoleRow {
  role: string
  p50: number | null
  p95: number | null
  p99: number | null
  count: number
}

function fmtSecs(v: number | null): string {
  if (v === null || v === undefined || isNaN(v)) return '—'
  if (v < 60) return `${v.toFixed(0)}s`
  return `${(v / 60).toFixed(1)}m`
}

function p95Color(v: number | null): string {
  if (v === null) return '#9ca3af'
  if (v > 600) return '#ef4444'
  if (v > 300) return '#f59e0b'
  return '#22c55e'
}

interface Props {
  refreshSignal?: number
}

export default function DurationPercentilesTile({ refreshSignal }: Props) {
  const [rows, setRows] = useState<RoleRow[]>([])
  const [loading, setLoading] = useState(true)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const results = await Promise.all(
        ROLES.map(role =>
          jsonRpc<PercentilesResponse>('runs.percentiles', { role }).catch(
            () => ({ p50: null, p95: null, p99: null, sample_size: 0 }),
          ),
        ),
      )
      const merged: RoleRow[] = ROLES.map((role, i) => ({
        role,
        p50: results[i].p50 ?? null,
        p95: results[i].p95 ?? null,
        p99: results[i].p99 ?? null,
        count: results[i].sample_size ?? 0,
      })).filter(r => r.count > 0)
      setRows(merged)
    } catch {
      setRows([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    intervalRef.current = setInterval(fetchData, 120_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [fetchData, refreshSignal])

  return (
    <section style={sharedStyles.section} data-testid="duration-percentiles-tile">
      <h2 style={sharedStyles.sectionHeading}>Duration Percentiles by Role (7d)</h2>

      {loading ? (
        <div style={sharedStyles.state}>Loading…</div>
      ) : rows.length === 0 ? (
        <div style={sharedStyles.state}>
          No completed runs in the last 7 days. Percentiles require duration_s, which is
          only recorded when post-agent-hook writes end_ts. In-flight runs are excluded.
        </div>
      ) : (
        <table style={sharedStyles.table}>
          <thead>
            <tr>
              <th style={sharedStyles.th}>Role</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>p50</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>p95</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>p99</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>n</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={row.role} style={sharedStyles.tr}>
                <td style={sharedStyles.td}>{row.role}</td>
                <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#d1d5db' }}>
                  {fmtSecs(row.p50)}
                </td>
                <td
                  style={{
                    ...sharedStyles.td,
                    textAlign: 'right',
                    color: p95Color(row.p95),
                    fontWeight: 600,
                  }}
                >
                  {fmtSecs(row.p95)}
                </td>
                <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#9ca3af' }}>
                  {fmtSecs(row.p99)}
                </td>
                <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#6b7280' }}>
                  {row.count}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
