/**
 * DialRejectionsTile — Dial Rejections / Sandbox Blocks (24h).
 *
 * Shows last-24h counts of rejected directive events and sandbox-block events,
 * with breakdowns by reason/kind. Proves the dial system is doing real work.
 *
 * Empty state: "No dial rejections in 24h." shown when both totals are zero.
 * No write calls — this tile is strictly read-only.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

// -------------------------------------------------------------------
// Types
// -------------------------------------------------------------------

export interface DialRejectionsDirectives24h {
  total: number
  by_reason: Record<string, number>
  last_at: string | null
}

export interface DialRejectionsSandboxBlocks24h {
  total: number
  by_kind: {
    sandbox_block_agent_spawn: number
    sandbox_block_gh_api_mutation: number
    sandbox_block_untrusted_cwd: number
  }
  last_at: string | null
}

export interface DialRejectionsLastRejection {
  kind: string
  reason_or_class: string
  timestamp: string
  cwd: string | null
}

export interface DialRejectionsResponse {
  rejected_directives_24h: DialRejectionsDirectives24h
  sandbox_blocks_24h: DialRejectionsSandboxBlocks24h
  last_rejection: DialRejectionsLastRejection | null
}

interface Props {
  refreshSignal?: number
}

// -------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------

function relativeTime(isoStr: string): string {
  try {
    const diffMs = Date.now() - new Date(isoStr).getTime()
    if (diffMs < 0) return 'just now'
    const diffS = Math.floor(diffMs / 1000)
    if (diffS < 60) return `${diffS}s ago`
    const diffM = Math.floor(diffS / 60)
    if (diffM < 60) return `${diffM}m ago`
    const diffH = Math.floor(diffM / 60)
    if (diffH < 24) return `${diffH}h ago`
    return new Date(isoStr).toLocaleDateString()
  } catch {
    return isoStr
  }
}

const KIND_LABELS: Record<string, string> = {
  sandbox_block_agent_spawn: 'Agent spawn',
  sandbox_block_gh_api_mutation: 'GH API mutation',
  sandbox_block_untrusted_cwd: 'Untrusted cwd',
}

function Chip({
  label,
  count,
  color,
}: {
  label: string
  count: number
  color: string
  // React uses key as a special internal prop; declare it here so TypeScript
  // doesn't reject <Chip key={...}> when React types are absent from the env.
  key?: string
}) {
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

export default function DialRejectionsTile({ refreshSignal }: Props) {
  const [data, setData] = useState<DialRejectionsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<DialRejectionsResponse>('stats.dial_rejections', {})
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

  const isEmpty =
    data !== null &&
    data.rejected_directives_24h.total === 0 &&
    data.sandbox_blocks_24h.total === 0

  const byReason: [string, number][] = data
    ? (Object.entries(data.rejected_directives_24h.by_reason) as [string, number][])
    : []
  const sortedReasons = byReason.sort(([, a], [, b]) => b - a)

  return (
    <section style={sharedStyles.section} aria-label="Dial Rejections (24h)">
      <h2 style={sharedStyles.sectionHeading}>Dial Rejections (24h)</h2>

      {loading && !data && (
        <div style={sharedStyles.state} role="status">Loading rejection data…</div>
      )}
      {error && !data && (
        <div style={{ ...sharedStyles.state, color: '#ef4444' }} role="alert">{error}</div>
      )}

      {data && isEmpty && (
        <div
          style={{ ...sharedStyles.state, fontStyle: 'italic' }}
          data-testid="dial-rejections-empty"
        >
          No dial rejections in 24h.
        </div>
      )}

      {data && !isEmpty && (
        <div
          data-testid="dial-rejections-tile"
          style={{
            background: '#111827',
            border: '1px solid #1f2937',
            borderRadius: 8,
            padding: '16px 20px',
            marginTop: 8,
            display: 'flex',
            flexDirection: 'column',
            gap: 16,
          }}
        >
          {/* Headline counts */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }} role="status">
            <span style={{ fontSize: 14, color: '#f9fafb' }}>
              Rejected directives:{' '}
              <strong style={{ color: '#f59e0b' }}>
                {data.rejected_directives_24h.total}
              </strong>
            </span>
            <span style={{ fontSize: 14, color: '#9ca3af' }}>·</span>
            <span style={{ fontSize: 14, color: '#f9fafb' }}>
              Sandbox blocks:{' '}
              <strong style={{ color: '#ef4444' }}>
                {data.sandbox_blocks_24h.total}
              </strong>
            </span>
          </div>

          {/* by_reason chips for directive rejections */}
          {sortedReasons.length > 0 && (
            <div>
              <p style={{ margin: '0 0 6px', fontSize: 12, color: '#6b7280' }}>
                Rejection reasons
              </p>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {sortedReasons.map(([reason, count]) => (
                  <Chip
                    key={reason}
                    label={reason === 'other' ? 'other' : reason}
                    count={count}
                    color={reason === 'other' ? '#6b7280' : '#f59e0b'}
                  />
                ))}
              </div>
            </div>
          )}

          {/* by_kind chips for sandbox blocks — always show all 3 */}
          <div>
            <p style={{ margin: '0 0 6px', fontSize: 12, color: '#6b7280' }}>
              Sandbox block kinds
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {(
                Object.entries(data.sandbox_blocks_24h.by_kind) as [string, number][]
              ).map(([kind, count]) => (
                <Chip
                  key={kind}
                  label={KIND_LABELS[kind] ?? kind}
                  count={count as number}
                  color={count > 0 ? '#ef4444' : '#374151'}
                />
              ))}
            </div>
          </div>

          {/* Last rejection row */}
          {data.last_rejection && (
            <div
              style={{
                borderTop: '1px solid #1f2937',
                paddingTop: 10,
                fontSize: 12,
                color: '#9ca3af',
                display: 'flex',
                flexWrap: 'wrap',
                gap: 6,
                alignItems: 'center',
              }}
              data-testid="dial-rejections-last-row"
            >
              <span style={{ color: '#f9fafb', fontFamily: 'monospace' }}>
                {data.last_rejection.kind}
              </span>
              <span>·</span>
              <span>{data.last_rejection.reason_or_class}</span>
              <span>·</span>
              <span>{relativeTime(data.last_rejection.timestamp)}</span>
              {data.last_rejection.cwd && (
                <>
                  <span>·</span>
                  <span
                    style={{ fontFamily: 'monospace', fontSize: 11, color: '#6b7280' }}
                    title={data.last_rejection.cwd}
                  >
                    {data.last_rejection.cwd.length > 40
                      ? '…' + data.last_rejection.cwd.slice(-40)
                      : data.last_rejection.cwd}
                  </span>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  )
}
