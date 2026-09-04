/**
 * SdkLaneTile — SDK Orchestrator Status.
 *
 * Surfaces the state of the dual-path SDK orchestrator: dispatcher readiness,
 * which backend would be selected (credential presence only — no secret values),
 * credit state, billing regime, and routing counts (sdk vs cc, backed by the
 * routed_via column).
 *
 * Shows a clean "dispatcher off — 0 SDK runs" state when the dispatcher is
 * disabled (the current reality until ROUTE_VIA_DISPATCHER=1 is set).
 *
 * Calls stats.sdk_lane RPC, refreshes every 60s via the shared poll signal.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

// -------------------------------------------------------------------
// Types — mirrors sdk_status() dict shape
// -------------------------------------------------------------------

export interface SdkReadiness {
  dispatcher_live: boolean
  ROUTE_VIA_DISPATCHER: string
  SHADOW_MODE: string
  SDK_BACKEND: string
}

export interface SdkBackendSelection {
  would_select: 'subscription' | 'apikey' | 'none' | string
  reason: string
  CLAUDE_CODE_OAUTH_TOKEN: 'present' | 'absent'
  ANTHROPIC_API_KEY: 'present' | 'absent'
}

export interface SdkCredit {
  remaining_usd: number | null
  used_usd: number | null
  soft_cap_breached: boolean | null
  exhausted: boolean | null
  billing_regime: string | null
  regime_note: string | null
  error?: string
}

export interface SdkRoutingCounts {
  total_runs_all_time: number
  total_runs_last_30d: number
  sdk_runs: number
  cc_runs: number
  null_route_runs: number
  sdk_runs_estimate: string
  db_available: boolean
  note: string
}

export interface SdkLaneResponse {
  generated_at: string
  readiness: SdkReadiness
  backend_selection: SdkBackendSelection
  credit: SdkCredit
  routing_counts: SdkRoutingCounts
}

interface Props {
  refreshSignal?: number
}

// -------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------

function fmt(n: number | null, decimals = 2): string {
  if (n == null) return 'n/a'
  return `$${n.toFixed(decimals)}`
}

function credBadge(presence: 'present' | 'absent') {
  const color = presence === 'present' ? '#22c55e' : '#6b7280'
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '1px 7px',
        borderRadius: 10,
        fontSize: 11,
        background: '#1f2937',
        border: `1px solid ${color}`,
        color,
        marginLeft: 6,
      }}
    >
      {presence}
    </span>
  )
}

function backendColor(b: string): string {
  if (b === 'subscription') return '#22c55e'
  if (b === 'apikey') return '#f59e0b'
  return '#6b7280'
}

// -------------------------------------------------------------------
// Component
// -------------------------------------------------------------------

export default function SdkLaneTile({ refreshSignal }: Props) {
  const [data, setData] = useState<SdkLaneResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<SdkLaneResponse>('stats.sdk_lane', {})
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

  return (
    <section style={sharedStyles.section} aria-label="SDK Orchestrator">
      <h2 style={sharedStyles.sectionHeading}>SDK Orchestrator</h2>

      {loading && !data && (
        <div style={sharedStyles.state} role="status">Loading SDK status…</div>
      )}
      {error && !data && (
        <div style={{ ...sharedStyles.state, color: '#ef4444' }} role="alert">{error}</div>
      )}

      {data && (
        <div
          data-testid="sdk-lane-tile"
          style={{
            background: '#111827',
            border: '1px solid #1f2937',
            borderRadius: 8,
            padding: '16px 20px',
            marginTop: 8,
          }}
        >
          {/* Dispatcher off banner */}
          {!data.readiness.dispatcher_live && (
            <p
              role="status"
              style={{
                fontSize: 12,
                color: '#6b7280',
                fontStyle: 'italic',
                marginBottom: 16,
                marginTop: 0,
              }}
              data-testid="sdk-lane-dispatcher-off"
            >
              Dispatcher off — {data.routing_counts.sdk_runs} SDK run
              {data.routing_counts.sdk_runs !== 1 ? 's' : ''}
            </p>
          )}

          {/* Readiness row */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
              gap: 12,
              marginBottom: 16,
            }}
          >
            <Cell
              label="Dispatcher"
              value={data.readiness.dispatcher_live ? 'LIVE' : 'OFF'}
              valueColor={data.readiness.dispatcher_live ? '#22c55e' : '#6b7280'}
              data-testid="sdk-lane-dispatcher-status"
            />
            <Cell
              label="Shadow mode"
              value={data.readiness.SHADOW_MODE}
            />
            <Cell
              label="SDK_BACKEND override"
              value={data.readiness.SDK_BACKEND}
            />
          </div>

          {/* Backend selection */}
          <div style={{ marginBottom: 16 }}>
            <Label>Backend would select</Label>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
              <span
                style={{
                  fontWeight: 600,
                  fontSize: 14,
                  color: backendColor(data.backend_selection.would_select),
                }}
                data-testid="sdk-lane-would-select"
              >
                {data.backend_selection.would_select}
              </span>
              <span style={{ fontSize: 12, color: '#9ca3af' }}>{data.backend_selection.reason}</span>
            </div>
            <div style={{ display: 'flex', gap: 12, marginTop: 6, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 12, color: '#9ca3af' }}>
                CLAUDE_CODE_OAUTH_TOKEN
                {credBadge(data.backend_selection.CLAUDE_CODE_OAUTH_TOKEN)}
              </span>
              <span style={{ fontSize: 12, color: '#9ca3af' }}>
                ANTHROPIC_API_KEY
                {credBadge(data.backend_selection.ANTHROPIC_API_KEY)}
              </span>
            </div>
          </div>

          {/* Credit state */}
          <div style={{ marginBottom: 16 }}>
            <Label>Credit</Label>
            {data.credit.error ? (
              <p style={{ fontSize: 12, color: '#6b7280', fontStyle: 'italic', marginTop: 4 }}>
                {data.credit.error}
              </p>
            ) : (
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
                  gap: 10,
                  marginTop: 6,
                }}
              >
                <Cell label="Remaining" value={fmt(data.credit.remaining_usd)} />
                <Cell label="Used" value={fmt(data.credit.used_usd, 4)} />
                <Cell
                  label="Regime"
                  value={data.credit.billing_regime ?? 'n/a'}
                  valueColor={data.credit.billing_regime === 'subscription' ? '#22c55e' : undefined}
                />
                <Cell
                  label="Soft cap"
                  value={
                    data.credit.soft_cap_breached == null
                      ? 'n/a'
                      : data.credit.soft_cap_breached
                      ? 'breached'
                      : 'ok'
                  }
                  valueColor={data.credit.soft_cap_breached ? '#ef4444' : undefined}
                />
              </div>
            )}
          </div>

          {/* Routing counts */}
          <div>
            <Label>Routing counts</Label>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))',
                gap: 10,
                marginTop: 6,
              }}
            >
              <Cell label="SDK runs" value={String(data.routing_counts.sdk_runs)} valueColor={data.routing_counts.sdk_runs > 0 ? '#22c55e' : undefined} />
              <Cell label="CC runs" value={String(data.routing_counts.cc_runs)} />
              <Cell label="Pre-D#1331 rows" value={String(data.routing_counts.null_route_runs)} />
              <Cell label="All-time total" value={String(data.routing_counts.total_runs_all_time)} />
              <Cell label="Last 30d" value={String(data.routing_counts.total_runs_last_30d)} />
            </div>
            {data.routing_counts.note && (
              <p
                style={{ fontSize: 11, color: '#6b7280', marginTop: 8, marginBottom: 0 }}
                data-testid="sdk-lane-routing-note"
              >
                {data.routing_counts.note}
              </p>
            )}
          </div>
        </div>
      )}
    </section>
  )
}

// -------------------------------------------------------------------
// Small sub-components
// -------------------------------------------------------------------

function Label({ children }: { children: React.ReactNode }) {
  return (
    <p
      style={{
        fontSize: 11,
        fontWeight: 600,
        color: '#9ca3af',
        textTransform: 'uppercase',
        letterSpacing: '0.05em',
        margin: 0,
      }}
    >
      {children}
    </p>
  )
}

function Cell({
  label,
  value,
  valueColor,
  ...rest
}: {
  label: string
  value: string
  valueColor?: string
  [k: string]: unknown
}) {
  return (
    <div {...rest}>
      <p style={{ fontSize: 11, color: '#6b7280', margin: '0 0 2px' }}>{label}</p>
      <p
        style={{
          fontSize: 14,
          fontWeight: 600,
          color: valueColor ?? '#f9fafb',
          margin: 0,
          fontFamily: 'monospace',
        }}
      >
        {value}
      </p>
    </div>
  )
}
