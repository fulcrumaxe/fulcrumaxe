/**
 * DuckDbWritersTile — Processes holding an open FD on stats.duckdb.
 *
 * A stale write-lock on stats.duckdb blocked all dashboard writes for 30+h
 * in PR #927. This tile makes the lock-holder visible at a glance so we
 * never need to diagnose it manually again.
 *
 * Calls stats_duckdb_writers RPC, refreshes every 60s.
 * Shows PID, truncated cmd, humanized age, and FD-mode badge per writer.
 *
 * Four distinct states, deliberately kept apart (D#2326). The tile used to
 * render the empty state and the warning together, so a host where the
 * backend could not look at all still headlined a confident "no active
 * writers" with the reason demoted to a footnote:
 *
 *   determined  — writers empty, nothing unlooked-at → "no active writers"
 *   partial     — some pids could not be inspected; the rows found are shown
 *                 alongside the count that was not looked at. Not a zero.
 *   undetermined— no source could answer; the reason is shown INSTEAD of an
 *                 empty state, never beside it.
 *   populated   — the writer table.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

export interface DuckDbWriter {
  pid: number
  cmd: string
  age_seconds: number | null
  /** null when the source could not determine the mode — render as unknown. */
  fd_mode: string | null
}

export interface DuckDbWritersResponse {
  writers: DuckDbWriter[]
  checked_at: string
  /** Non-null ONLY when no source could answer at all. */
  warning: string | null
  source?: string | null
  inspected_pids?: number | null
  /**
   * Pids the scan could not inspect — another user's processes, mostly.
   * `null`/absent means the source cannot count them, which is not the same
   * as zero and must not be rendered as a confirmed complete answer.
   */
  uninspected_pids?: number | null
  capped?: boolean
}

interface Props {
  refreshSignal?: number
}

function humanAge(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return 'unknown'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  return `${(seconds / 3600).toFixed(1)}h`
}

function fdModeBadge(mode: string | null): React.ReactNode {
  // An unknown mode is rendered as unknown. Defaulting it to 'r' would
  // understate a write lock, which is the thing this tile exists to surface.
  if (mode === null || mode === undefined || mode === '') {
    return (
      <span style={{
        display: 'inline-block',
        padding: '1px 6px',
        borderRadius: 4,
        fontSize: 11,
        fontWeight: 600,
        border: '1px dashed #6b7280',
        color: '#9ca3af',
        fontFamily: 'monospace',
      }}>
        not reported
      </span>
    )
  }
  const color = mode === 'w' || mode === 'rw' ? '#ef4444' : '#6b7280'
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 6px',
      borderRadius: 4,
      fontSize: 11,
      fontWeight: 600,
      background: color,
      color: '#fff',
      fontFamily: 'monospace',
    }}>
      {mode}
    </span>
  )
}

const styles: Record<string, React.CSSProperties> = {
  ...sharedStyles,
  mono: {
    fontFamily: 'monospace',
    fontSize: 11,
    color: '#9ca3af',
  },
  warn: {
    color: '#f59e0b',
    fontSize: 12,
    marginTop: 4,
  },
}

export default function DuckDbWritersTile({ refreshSignal }: Props) {
  const [data, setData] = useState<DuckDbWritersResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<DuckDbWritersResponse>('stats_duckdb_writers', {})
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

  // A null/absent uninspected_pids means the source cannot count what it
  // missed (the lsof path). That is "not reported", not "nothing missed" —
  // it leaves the tile reading exactly as it did before this field existed.
  const partial = !!data && !data.warning && (data.uninspected_pids ?? 0) > 0

  return (
    <section style={styles.section} aria-label="DuckDB Writers">
      <h2 style={styles.sectionHeading}>DuckDB Writers</h2>

      {loading && !data && (
        <div style={styles.state} role="status">Loading DuckDB writer data…</div>
      )}
      {error && !data && (
        <div style={{ ...styles.state, color: '#ef4444' }} role="alert">{error}</div>
      )}

      {/* Undetermined — no source could answer. The reason replaces the
          empty state rather than sitting under it. */}
      {data && data.warning && (
        <div
          style={{ ...styles.state, color: '#f59e0b' }}
          role="status"
          data-testid="duckdb-writers-undetermined"
        >
          could not determine writers — {data.warning}
        </div>
      )}

      {/* Determined and genuinely empty. */}
      {data && !data.warning && data.writers.length === 0 && !partial && (
        <div style={styles.state} role="status" data-testid="duckdb-writers-empty">
          no active writers
        </div>
      )}

      {/* Partial and empty — looked, found none, but did not see everything. */}
      {data && !data.warning && data.writers.length === 0 && partial && (
        <div style={styles.state} role="status" data-testid="duckdb-writers-partial-empty">
          no writers among the {data.inspected_pids ?? 0} processes visible here
        </div>
      )}

      {data && data.writers.length > 0 && (
        <table style={styles.table} data-testid="duckdb-writers-tile">
          <thead>
            <tr>
              <th style={styles.th} scope="col">PID</th>
              <th style={styles.th} scope="col">Command</th>
              <th style={styles.th} scope="col">Age</th>
              <th style={styles.th} scope="col">Mode</th>
            </tr>
          </thead>
          <tbody>
            {data.writers.map((w, i) => (
              <tr key={`${w.pid}-${i}`} style={styles.tr}>
                <td style={{ ...styles.td, ...styles.mono }}>{w.pid}</td>
                <td style={{ ...styles.td, ...styles.mono }}>
                  {w.cmd.length > 40 ? w.cmd.slice(0, 40) + '…' : w.cmd}
                </td>
                <td style={styles.td}>{humanAge(w.age_seconds)}</td>
                <td style={styles.td}>{fdModeBadge(w.fd_mode)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Partial-answer note. Shown whether or not rows were found, because
          "here is what I found" and "here is what I did not look at" are
          both part of the same answer. */}
      {data && partial && (
        <div style={styles.warn} data-testid="duckdb-writers-partial">
          {data.uninspected_pids} of {(data.inspected_pids ?? 0) + (data.uninspected_pids ?? 0)}
          {' '}processes could not be inspected
          {data.capped ? ' (scan capped)' : ' (owned by another user, or exited mid-scan)'}
          {' '}— this is not a confirmed complete list.
        </div>
      )}
    </section>
  )
}
