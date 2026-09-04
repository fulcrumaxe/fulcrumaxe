/**
 * DuckDbWritersTile — Processes holding an open FD on stats.duckdb.
 *
 * A stale write-lock on stats.duckdb blocked all dashboard writes for 30+h
 * in PR #927. This tile makes the lock-holder visible at a glance so we
 * never need to diagnose it manually again.
 *
 * Calls stats_duckdb_writers RPC, refreshes every 60s.
 * Shows PID, truncated cmd, humanized age, and FD-mode badge per writer.
 * Empty state: "no active writers".
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

export interface DuckDbWriter {
  pid: number
  cmd: string
  age_seconds: number | null
  fd_mode: string
}

export interface DuckDbWritersResponse {
  writers: DuckDbWriter[]
  checked_at: string
  warning: string | null
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

function fdModeBadge(mode: string): React.ReactNode {
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

  return (
    <section style={styles.section} aria-label="DuckDB Writers">
      <h2 style={styles.sectionHeading}>DuckDB Writers</h2>

      {loading && !data && (
        <div style={styles.state} role="status">Loading DuckDB writer data…</div>
      )}
      {error && !data && (
        <div style={{ ...styles.state, color: '#ef4444' }} role="alert">{error}</div>
      )}

      {data && data.writers.length === 0 && (
        <div style={styles.state} role="status" data-testid="duckdb-writers-empty">
          no active writers
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

      {data?.warning && (
        <div style={styles.warn} data-testid="duckdb-writers-warning">
          {data.warning}
        </div>
      )}
    </section>
  )
}
