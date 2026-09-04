/**
 * DialUsageTile — Dial State / Autonomy Level.
 *
 * Shows current level for all 13 dial classes plus 24h activity counters.
 * Calls stats.dial_usage RPC, refreshes every 60s via the shared poll signal.
 *
 * Empty state: "No directive activity in 24h" message shown when counters are
 * all zero, but current dial levels are always rendered.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

// -------------------------------------------------------------------
// Types
// -------------------------------------------------------------------

export interface DialClass {
  name: string
  level: number
  verb_label: string
  ceiling: number
  active_directives: number
  ttl_revert_at: string | null
}

export interface DialRejectedBreakdown {
  ceiling_violation: number
  unauthenticated_source: number
  invalid_level: number
}

export interface DialLast24h {
  accepted: number
  rejected_by_reason: DialRejectedBreakdown
  ceiling_violations: number
  last_ceiling_exceeded: { class: string; timestamp: string } | null
}

export interface DialUsageResponse {
  current_dials: DialClass[]
  last_24h: DialLast24h
}

interface Props {
  refreshSignal?: number
}

// -------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------

function levelColor(level: number, ceiling: number): string {
  const ratio = level / ceiling
  if (ratio >= 0.8) return '#22c55e'   // green — high autonomy
  if (ratio >= 0.5) return '#f59e0b'   // amber — medium
  return '#ef4444'                     // red — restricted
}

function formatTtl(ttl: string | null): string {
  if (!ttl) return '—'
  try {
    const d = new Date(ttl)
    const diffMs = d.getTime() - Date.now()
    if (diffMs <= 0) return 'expiring'
    const diffH = Math.floor(diffMs / 3_600_000)
    const diffM = Math.floor((diffMs % 3_600_000) / 60_000)
    if (diffH < 24) return `in ${diffH}h ${diffM}m`
    return d.toLocaleDateString()
  } catch {
    return ttl
  }
}

function Chip({ label, count, color }: { label: string; count: number; color: string }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '2px 8px',
        borderRadius: 12,
        fontSize: 12,
        background: '#1f2937',
        border: `1px solid ${color}`,
        color,
        whiteSpace: 'nowrap',
      }}
    >
      {label}: <strong>{count}</strong>
    </span>
  )
}

// -------------------------------------------------------------------
// Component
// -------------------------------------------------------------------

export default function DialUsageTile({ refreshSignal }: Props) {
  const [data, setData] = useState<DialUsageResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<DialUsageResponse>('stats.dial_usage', {})
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

  const has24hActivity = data
    ? data.last_24h.accepted > 0 ||
      data.last_24h.ceiling_violations > 0 ||
      (
        data.last_24h.rejected_by_reason.ceiling_violation > 0 ||
        data.last_24h.rejected_by_reason.unauthenticated_source > 0 ||
        data.last_24h.rejected_by_reason.invalid_level > 0
      )
    : false

  return (
    <section style={sharedStyles.section} aria-label="Dial State (Level 6)">
      <h2 style={sharedStyles.sectionHeading}>Dial State (Level 6)</h2>

      {loading && !data && (
        <div style={sharedStyles.state} role="status">Loading dial state…</div>
      )}
      {error && !data && (
        <div style={{ ...sharedStyles.state, color: '#ef4444' }} role="alert">{error}</div>
      )}

      {data && (
        <div
          data-testid="dial-usage-tile"
          style={{
            background: '#111827',
            border: '1px solid #1f2937',
            borderRadius: 8,
            padding: '16px 20px',
            marginTop: 8,
          }}
        >
          {/* 24h activity counters */}
          {has24hActivity ? (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 16 }} role="status">
              <Chip
                label="Accepted"
                count={data.last_24h.accepted}
                color="#22c55e"
              />
              {data.last_24h.rejected_by_reason.ceiling_violation > 0 && (
                <Chip
                  label="Ceiling violations"
                  count={data.last_24h.rejected_by_reason.ceiling_violation}
                  color="#ef4444"
                />
              )}
              {data.last_24h.rejected_by_reason.unauthenticated_source > 0 && (
                <Chip
                  label="Unauthenticated"
                  count={data.last_24h.rejected_by_reason.unauthenticated_source}
                  color="#f59e0b"
                />
              )}
              {data.last_24h.rejected_by_reason.invalid_level > 0 && (
                <Chip
                  label="Invalid level"
                  count={data.last_24h.rejected_by_reason.invalid_level}
                  color="#f59e0b"
                />
              )}
            </div>
          ) : (
            <p
              style={{ fontSize: 12, color: '#6b7280', fontStyle: 'italic', marginBottom: 12 }}
              data-testid="dial-usage-empty-activity"
            >
              No directive activity in 24h
            </p>
          )}

          {/* Dial class table */}
          <table style={sharedStyles.table}>
            <thead>
              <tr>
                <th style={sharedStyles.th} scope="col">Class</th>
                <th style={sharedStyles.th} scope="col">Action</th>
                <th style={{ ...sharedStyles.th, textAlign: 'center' }} scope="col">Level / Ceiling</th>
                <th style={{ ...sharedStyles.th, textAlign: 'center' }} scope="col">Directives</th>
                <th style={sharedStyles.th} scope="col">TTL Revert</th>
              </tr>
            </thead>
            <tbody>
              {data.current_dials.map((cls: DialClass) => {
                const color = levelColor(cls.level, cls.ceiling)
                return (
                  <tr key={cls.name} style={sharedStyles.tr}>
                    <td style={{ ...sharedStyles.td, fontFamily: 'monospace', fontSize: 12 }}>
                      {cls.name}
                    </td>
                    <td style={{ ...sharedStyles.td, color: '#9ca3af', fontSize: 12 }}>
                      {cls.verb_label}
                    </td>
                    <td style={{ ...sharedStyles.td, textAlign: 'center' }}>
                      <span
                        style={{
                          display: 'inline-block',
                          width: 8,
                          height: 8,
                          borderRadius: '50%',
                          marginRight: 4,
                          background: color,
                          verticalAlign: 'middle',
                        }}
                      />
                      <span style={{ color, fontWeight: 600 }}>{cls.level}</span>
                      <span style={{ color: '#6b7280' }}>/{cls.ceiling}</span>
                    </td>
                    <td
                      style={{
                        ...sharedStyles.td,
                        textAlign: 'center',
                        color: cls.active_directives > 0 ? '#f9fafb' : '#4b5563',
                      }}
                    >
                      {cls.active_directives}
                    </td>
                    <td style={{ ...sharedStyles.td, fontSize: 11, color: '#6b7280' }}>
                      {formatTtl(cls.ttl_revert_at)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {/* Last ceiling exceeded */}
          {data.last_24h.last_ceiling_exceeded && (
            <p style={{ marginTop: 8, fontSize: 11, color: '#ef4444' }}>
              Last ceiling violation: <strong>{data.last_24h.last_ceiling_exceeded.class}</strong>{' '}
              at {new Date(data.last_24h.last_ceiling_exceeded.timestamp).toLocaleString()}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
