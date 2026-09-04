/**
 * StuckRunsTile — agent runs with no end_ts older than 30 minutes.
 *
 * Fetches runs.stuck (threshold_seconds=1800) and lists them.
 * Kill/investigate buttons are UI-only — actions are a follow-up.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { formatRelative, formatAbsolute } from '../../lib/safeDate'
import { sharedStyles } from './styles'

interface StuckRun {
  agent_id: string
  role: string
  discussion: number | null
  pr: number | null
  start_ts: string | null
  model: string | null
  event_id: string | null
}

interface StuckResponse {
  runs: StuckRun[]
}

function stuckDuration(startTs: string | null): string {
  if (!startTs) return '—'
  const start = new Date(startTs)
  if (isNaN(start.getTime())) return '—'
  const mins = Math.floor((Date.now() - start.getTime()) / 60_000)
  if (mins < 60) return `${mins}m`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

interface Props {
  refreshSignal?: number
}

export default function StuckRunsTile({ refreshSignal }: Props) {
  const [runs, setRuns] = useState<StuckRun[]>([])
  const [loading, setLoading] = useState(true)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<StuckResponse>('runs.stuck', { threshold_seconds: 1800 })
      setRuns(resp.runs ?? [])
    } catch {
      setRuns([])
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

  const countLabel = runs.length === 0 ? '0 stuck' : `${runs.length} stuck`
  const countColor = runs.length === 0 ? '#22c55e' : runs.length > 2 ? '#ef4444' : '#f59e0b'

  return (
    <section style={sharedStyles.section} data-testid="stuck-runs-tile">
      <h2 style={sharedStyles.sectionHeading}>
        Stuck Runs{' '}
        <span
          style={{
            fontSize: 14,
            fontWeight: 500,
            color: countColor,
            marginLeft: 8,
          }}
        >
          {countLabel}
        </span>
      </h2>

      {loading ? (
        <div style={sharedStyles.state}>Loading…</div>
      ) : runs.length === 0 ? (
        <div style={sharedStyles.state} data-testid="stuck-runs-empty">
          No stuck agents — all runs completed within 30 minutes.
        </div>
      ) : (
        <table style={sharedStyles.table}>
          <thead>
            <tr>
              <th style={sharedStyles.th}>Role</th>
              <th style={sharedStyles.th}>Started</th>
              <th style={sharedStyles.th}>Duration</th>
              <th style={sharedStyles.th}>Discussion / PR</th>
              <th style={sharedStyles.th}>Model</th>
              <th style={sharedStyles.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {runs.map(run => (
              <tr key={run.agent_id} style={sharedStyles.tr}>
                <td style={sharedStyles.td}>
                  <span
                    style={{
                      ...sharedStyles.badge,
                      background: '#1e3a5f',
                      color: '#93c5fd',
                    }}
                  >
                    {run.role}
                  </span>
                </td>
                <td
                  style={{ ...sharedStyles.td, color: '#9ca3af', fontSize: 12 }}
                  title={formatAbsolute(run.start_ts)}
                >
                  {formatRelative(run.start_ts)}
                </td>
                <td style={{ ...sharedStyles.td, color: '#ef4444', fontWeight: 600 }}>
                  {stuckDuration(run.start_ts)}
                </td>
                <td style={{ ...sharedStyles.td, color: '#9ca3af', fontSize: 12 }}>
                  {run.discussion ? `D#${run.discussion}` : '—'}
                  {run.pr ? ` / PR#${run.pr}` : ''}
                </td>
                <td style={{ ...sharedStyles.td, color: '#6b7280', fontSize: 11 }}>
                  {run.model ?? '—'}
                </td>
                <td style={sharedStyles.td}>
                  <button
                    disabled
                    title="Kill action available in a follow-up"
                    style={{
                      padding: '2px 8px',
                      background: '#374151',
                      color: '#9ca3af',
                      border: '1px solid #4b5563',
                      borderRadius: 4,
                      fontSize: 12,
                      cursor: 'not-allowed',
                      marginRight: 4,
                    }}
                  >
                    Kill
                  </button>
                  <button
                    disabled
                    title="Investigate action available in a follow-up"
                    style={{
                      padding: '2px 8px',
                      background: '#374151',
                      color: '#9ca3af',
                      border: '1px solid #4b5563',
                      borderRadius: 4,
                      fontSize: 12,
                      cursor: 'not-allowed',
                    }}
                  >
                    Investigate
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
