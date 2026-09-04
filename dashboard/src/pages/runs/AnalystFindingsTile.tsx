/**
 * AnalystFindingsTile — latest run-analyst findings grouped by severity.
 *
 * Fetches stats.analyst_findings (read-only RPC) and renders findings
 * in high / medium / low sections, each color-coded. Each finding shows
 * its title and evidence refs (file/PR/discussion strings from the report).
 *
 * Clean empty state when no reports have been written yet.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

interface Finding {
  category: string
  severity: string
  title: string
  evidence: string[]
  suggested_discussion_title: string
  suggested_tag: string
}

interface BySeverity {
  high: Finding[]
  medium: Finding[]
  low: Finding[]
}

interface AnalystFindingsResponse {
  report_at: string | null
  window: { since: string; until: string } | null
  runs_analyzed: number
  by_severity: BySeverity
  total: number
  generated_at: string
  error: string | null
}

const SEVERITY_COLORS: Record<string, string> = {
  high: '#ef4444',
  medium: '#f59e0b',
  low: '#22c55e',
}

const SEVERITY_BG: Record<string, string> = {
  high: '#2d1515',
  medium: '#2d2010',
  low: '#102d1a',
}

function formatReportDate(iso: string | null): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

interface Props {
  refreshSignal?: number
}

export default function AnalystFindingsTile({ refreshSignal }: Props) {
  const [data, setData] = useState<AnalystFindingsResponse | null>(null)
  // Transport failure (network error, 401, JSON-RPC error) is tracked
  // separately from "loader ran and found nothing" (data.total === 0,
  // data.error === null). Collapsing both into "data === null" rendered the
  // same "Unable to load analyst findings." string for two different states
  // (D#2316 finding 3) — a thrown fetch and a genuinely empty report read
  // identically even though they mean different things to an operator.
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const resp = await jsonRpc<AnalystFindingsResponse>('stats.analyst_findings', {})
      setData(resp)
      setFetchError(null)
    } catch (err) {
      setData(null)
      setFetchError(err instanceof Error ? err.message : String(err))
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

  const totalCount = data?.total ?? 0
  const totalColor =
    totalCount === 0
      ? '#22c55e'
      : (data?.by_severity.high.length ?? 0) > 0
        ? '#ef4444'
        : '#f59e0b'

  return (
    <section style={sharedStyles.section} data-testid="analyst-findings-tile">
      <h2 style={sharedStyles.sectionHeading}>
        Run-Analyst Findings
        <span
          style={{
            fontSize: 14,
            fontWeight: 500,
            color: totalColor,
            marginLeft: 8,
          }}
        >
          {loading ? '' : totalCount === 0 ? 'none' : `${totalCount} finding${totalCount !== 1 ? 's' : ''}`}
        </span>
        {data?.report_at && (
          <span style={{ fontSize: 12, color: '#6b7280', marginLeft: 12, fontWeight: 400 }}>
            report from {formatReportDate(data.report_at)}
          </span>
        )}
      </h2>

      {loading ? (
        <div style={sharedStyles.state}>Loading…</div>
      ) : fetchError !== null ? (
        <div style={sharedStyles.state}>Unable to load analyst findings: {fetchError}</div>
      ) : data === null ? (
        <div style={sharedStyles.state}>Unable to load analyst findings.</div>
      ) : totalCount === 0 ? (
        <div style={sharedStyles.state}>No analyst findings yet. Run backend/run_analyst.py to generate a report.</div>
      ) : (
        <div>
          {(['high', 'medium', 'low'] as const).map((sev) => {
            const findings = data.by_severity[sev] ?? []
            if (findings.length === 0) return null
            const color = SEVERITY_COLORS[sev]
            const bg = SEVERITY_BG[sev]
            return (
              <div key={sev} style={{ marginBottom: 20 }}>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color,
                    textTransform: 'uppercase',
                    letterSpacing: '0.05em',
                    marginBottom: 8,
                  }}
                >
                  {sev} ({findings.length})
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {findings.map((f, i) => (
                    <div
                      key={i}
                      style={{
                        background: bg,
                        border: `1px solid ${color}33`,
                        borderLeft: `3px solid ${color}`,
                        borderRadius: 6,
                        padding: '10px 14px',
                      }}
                    >
                      <div
                        style={{
                          fontSize: 13,
                          fontWeight: 500,
                          color: '#f9fafb',
                          marginBottom: f.evidence.length > 0 ? 6 : 0,
                        }}
                      >
                        <span
                          style={{
                            ...sharedStyles.badge,
                            background: '#1f2937',
                            color: '#9ca3af',
                            marginRight: 8,
                            fontSize: 11,
                          }}
                        >
                          {f.category}
                        </span>
                        {f.title}
                      </div>
                      {f.evidence.length > 0 && (
                        <div
                          style={{
                            display: 'flex',
                            flexWrap: 'wrap',
                            gap: 4,
                            marginTop: 4,
                          }}
                        >
                          {f.evidence.slice(0, 8).map((ev, j) => (
                            <span
                              key={j}
                              style={{
                                fontSize: 11,
                                color: '#9ca3af',
                                background: '#0f172a',
                                border: '1px solid #1f2937',
                                borderRadius: 3,
                                padding: '1px 6px',
                                fontFamily: 'monospace',
                              }}
                            >
                              {ev}
                            </span>
                          ))}
                          {f.evidence.length > 8 && (
                            <span style={{ fontSize: 11, color: '#6b7280' }}>
                              +{f.evidence.length - 8} more
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
