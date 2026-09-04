/**
 * RecentRunsFeedTile — chronological feed of the last 50 agent runs.
 *
 * Fetches runs.recent and displays role, duration, verdict, and
 * discussion/PR links in reverse-chronological order.
 *
 * Includes both completed and in-flight runs. In-flight rows (no end_ts)
 * are labeled "running" — these are the same rows Active Agents counts as
 * active. Duration and verdict fill in once post-agent-hook writes end_ts.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { jsonRpc } from '../../api/client'
import { formatRelative, formatAbsolute } from '../../lib/safeDate'
import { sharedStyles } from './styles'

interface AgentRun {
  agent_id: string
  role: string
  discussion: number | null
  pr: number | null
  start_ts: string | null
  end_ts: string | null
  duration_s: number | null
  verdict: string | null
  model: string | null
  // Backend DuckDB column names are input_tok / output_tok (not input_tokens / output_tokens)
  input_tok: number | null
  output_tok: number | null
}

interface RecentResponse {
  runs: AgentRun[]
}

function fmtDuration(s: number | null): string {
  if (s === null || s === undefined || isNaN(s)) return '—'
  if (s < 60) return `${s.toFixed(0)}s`
  return `${(s / 60).toFixed(1)}m`
}

function verdictColor(v: string | null): string {
  if (!v) return '#6b7280'
  if (v === 'pass' || v === 'done') return '#22c55e'
  if (v === 'needs-fix' || v === 'fail') return '#ef4444'
  if (v === 'skip') return '#f59e0b'
  return '#9ca3af'
}

interface Props {
  refreshSignal?: number
}

export default function RecentRunsFeedTile({ refreshSignal }: Props) {
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [loading, setLoading] = useState(true)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ?since= and ?until= query params from the "View agent runs" link on LoopTimeline
  const [searchParams] = useSearchParams()
  const sinceParam = searchParams.get('since') ?? undefined
  const untilParam = searchParams.get('until') ?? undefined

  const fetchData = useCallback(async () => {
    try {
      const params: Record<string, unknown> = { limit: 50 }
      if (sinceParam) params.since_iso = sinceParam
      const resp = await jsonRpc<RecentResponse>('runs.recent', params)
      let fetched = resp.runs ?? []
      // Client-side filter for ?until= (the RPC doesn't support until_iso yet)
      if (untilParam) {
        const untilMs = new Date(untilParam).getTime()
        if (!isNaN(untilMs)) {
          fetched = fetched.filter(r => {
            if (!r.start_ts) return false
            const startMs = new Date(r.start_ts).getTime()
            return !isNaN(startMs) && startMs <= untilMs
          })
        }
      }
      setRuns(fetched)
    } catch {
      setRuns([])
    } finally {
      setLoading(false)
    }
  }, [sinceParam, untilParam])

  useEffect(() => {
    fetchData()
    intervalRef.current = setInterval(fetchData, 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [fetchData, refreshSignal])

  const inFlightRuns = runs.filter(r => r.end_ts === null)
  const completedRuns = runs.filter(r => r.end_ts !== null)

  return (
    <section style={sharedStyles.section} data-testid="recent-runs-feed-tile">
      <h2 style={sharedStyles.sectionHeading}>
        {sinceParam ? 'Agent Runs in Window' : 'Recent Runs (last 50)'}
        {inFlightRuns.length > 0 && (
          <span
            style={{ fontSize: 13, fontWeight: 500, color: '#f59e0b', marginLeft: 8 }}
            title="These runs are currently in-flight (no end_ts). They are counted in the Active Agents chart above."
          >
            · {inFlightRuns.length} in-flight
          </span>
        )}
      </h2>

      {loading ? (
        <div style={sharedStyles.state}>Loading…</div>
      ) : runs.length === 0 ? (
        <div style={sharedStyles.state}>
          No agent runs in the last 7 days. Rows appear after the first spawn via spawn-agent.sh.
        </div>
      ) : completedRuns.length === 0 ? (
        <div style={{ marginBottom: 12 }}>
          <div style={{ ...sharedStyles.state, marginBottom: 0 }}>
            No completed runs yet — {inFlightRuns.length} run{inFlightRuns.length !== 1 ? 's' : ''} in-flight.
            Duration and verdict fill in once post-agent-hook records end_ts.
          </div>
        </div>
      ) : null}

      {runs.length > 0 && (
        <table style={sharedStyles.table}>
          <thead>
            <tr>
              <th style={sharedStyles.th}>Role</th>
              <th style={sharedStyles.th}>Finished</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>Duration</th>
              <th style={sharedStyles.th}>Verdict</th>
              <th style={sharedStyles.th}>D / PR</th>
              <th style={{ ...sharedStyles.th, textAlign: 'right' }}>Tokens</th>
            </tr>
          </thead>
          <tbody>
            {runs.map(run => {
              const tokTotal =
                (run.input_tok ?? 0) + (run.output_tok ?? 0)
              const isInFlight = run.end_ts === null
              return (
                <tr
                  key={run.agent_id}
                  style={{
                    ...sharedStyles.tr,
                    opacity: isInFlight ? 0.7 : 1,
                  }}
                >
                  <td style={sharedStyles.td}>
                    <span
                      style={{
                        ...sharedStyles.badge,
                        background: isInFlight ? '#1c2f1a' : '#1e3a5f',
                        color: isInFlight ? '#86efac' : '#93c5fd',
                      }}
                    >
                      {run.role}
                    </span>
                  </td>
                  <td
                    style={{ ...sharedStyles.td, color: '#9ca3af', fontSize: 12 }}
                    title={isInFlight ? 'Still running — no end_ts yet' : formatAbsolute(run.end_ts)}
                  >
                    {isInFlight ? (
                      <span style={{ color: '#f59e0b' }}>running…</span>
                    ) : (
                      formatRelative(run.end_ts)
                    )}
                  </td>
                  <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#d1d5db' }}>
                    {fmtDuration(run.duration_s)}
                  </td>
                  <td style={sharedStyles.td}>
                    <span
                      style={{
                        ...sharedStyles.badge,
                        color: isInFlight ? '#f59e0b' : verdictColor(run.verdict),
                        background: 'transparent',
                        padding: 0,
                        fontWeight: 600,
                      }}
                    >
                      {isInFlight ? 'in-flight' : (run.verdict ?? '—')}
                    </span>
                  </td>
                  <td style={{ ...sharedStyles.td, color: '#9ca3af', fontSize: 12 }}>
                    {run.discussion ? (
                      <span>D#{run.discussion}</span>
                    ) : null}
                    {run.pr ? (
                      <span style={{ marginLeft: run.discussion ? 4 : 0 }}>
                        PR#{run.pr}
                      </span>
                    ) : null}
                    {!run.discussion && !run.pr ? '—' : null}
                  </td>
                  <td style={{ ...sharedStyles.td, textAlign: 'right', color: '#6b7280', fontSize: 12 }}>
                    {tokTotal > 0 ? tokTotal.toLocaleString() : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
