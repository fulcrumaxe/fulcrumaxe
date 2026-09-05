/**
 * SdkVsCcTile — per-role SDK vs CC comparison table.
 *
 * Fetches stats.sdk_vs_cc once on mount and every 2 minutes.
 * Shows run count, median tokens, and pass rate for each (role, route) pair.
 * Empty state when no SDK runs have been recorded yet.
 */

import type React from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

interface SdkVsCcRow {
  role: string
  route: string
  run_count: number
  median_input_tok: number | null
  median_output_tok: number | null
  /** Runs in this group that actually recorded a verdict — pass_rate's denominator. */
  verdict_count?: number
  pass_rate: number | null
}

interface SdkVsCcResponse {
  rows: SdkVsCcRow[]
  has_routed_via: boolean
  /** Runs dropped by the routed_via filter. Not "no runs" — unattributed runs. */
  excluded_unrouted_runs?: number
  generated_at: string
  error: string | null
}

function fmtTok(v: number | null): string {
  if (v === null || v === undefined) return '—'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}

// null pass_rate means no run in the group ever recorded a verdict. That is
// not a 0% pass rate, and rendering it as one is how 22 roles came to report a
// confident "0.0%" nobody had measured.
function fmtRate(v: number | null): string {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function routeBadgeStyle(route: string): React.CSSProperties {
  return {
    ...sharedStyles.badge,
    background: route === 'sdk' ? '#1d4ed8' : '#374151',
    color: route === 'sdk' ? '#93c5fd' : '#d1d5db',
  }
}

interface Props {
  refreshSignal?: number
}

export default function SdkVsCcTile({ refreshSignal }: Props) {
  const [data, setData] = useState<SdkVsCcResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const result = await jsonRpc<SdkVsCcResponse>('stats.sdk_vs_cc', {})
      setData(result)
    } catch {
      setData(null)
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

  const rows: SdkVsCcRow[] = data?.rows ?? []
  const hasData = rows.length > 0
  const excluded = data?.excluded_unrouted_runs ?? 0
  const excludedNote =
    excluded > 0
      ? `${excluded.toLocaleString()} run${excluded === 1 ? '' : 's'} are not attributed ` +
        'to a route and are not counted above.'
      : null

  return (
    <section style={sharedStyles.section} data-testid="sdk-vs-cc-tile">
      <h2 style={sharedStyles.sectionHeading}>SDK vs CC — Per-Role Comparison</h2>

      {loading ? (
        <div style={sharedStyles.state}>Loading…</div>
      ) : !hasData ? (
        <div style={sharedStyles.state}>
          {data?.has_routed_via === false
            ? 'No SDK runs recorded yet — routed_via column absent or all runs pre-date D#1331.'
            : 'No SDK runs recorded yet.'}
          {excludedNote ? <div style={{ marginTop: 6 }}>{excludedNote}</div> : null}
        </div>
      ) : (
        <table style={sharedStyles.table}>
          <thead>
            <tr>
              <th style={sharedStyles.th}>Role</th>
              <th style={sharedStyles.th}>Route</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>Runs</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>Median In</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>Median Out</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>Pass Rate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(row => (
              <tr key={`${row.role}-${row.route}`} style={sharedStyles.tr}>
                <td style={sharedStyles.td}>{row.role}</td>
                <td style={sharedStyles.td}>
                  <span style={routeBadgeStyle(row.route)}>{row.route}</span>
                </td>
                <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#d1d5db' }}>
                  {row.run_count}
                </td>
                <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#9ca3af' }}>
                  {fmtTok(row.median_input_tok)}
                </td>
                <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#9ca3af' }}>
                  {fmtTok(row.median_output_tok)}
                </td>
                <td
                  style={{
                    ...sharedStyles.td,
                    textAlign: 'right',
                    color:
                      row.pass_rate === null
                        ? '#6b7280'
                        : row.pass_rate >= 0.8
                          ? '#22c55e'
                          : row.pass_rate >= 0.6
                            ? '#f59e0b'
                            : '#ef4444',
                    fontWeight: 600,
                  }}
                >
                  {fmtRate(row.pass_rate)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!loading && hasData && excludedNote ? (
        <div style={{ ...sharedStyles.state, marginTop: 8 }}>{excludedNote}</div>
      ) : null}
    </section>
  )
}
