import { useEffect, useState } from 'react'
import { jsonRpc } from '../api/client'

interface FreshnessRow {
  metric_name: string
  last_ts: string
  age_seconds: number
  /** False for a one-shot metric with no live writer — see backend/stats/freshness.py. */
  monitored?: boolean
}

interface FreshnessResponse {
  rows: FreshnessRow[]
  warn_age_seconds: number
  bug_age_seconds: number
}

const bannerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 12,
  padding: '10px 20px',
  background: '#78350f',
  color: '#fde68a',
  fontFamily: 'system-ui, sans-serif',
  fontSize: 13,
  borderBottom: '1px solid #92400e',
}

const metricListStyle: React.CSSProperties = {
  margin: '4px 0 0 0',
  padding: 0,
  listStyle: 'none',
  display: 'flex',
  flexWrap: 'wrap',
  gap: '6px 16px',
}

const metricTagStyle: React.CSSProperties = {
  background: '#451a03',
  color: '#fcd34d',
  padding: '2px 8px',
  borderRadius: 3,
  fontFamily: 'monospace',
  fontSize: 12,
}

function humanAge(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

/**
 * StaleBanner — shown when any metric_event row is older than the warn threshold.
 *
 * Polls stats.freshness_list every 60 seconds. Hidden when no stale metrics.
 */
export default function StaleBanner() {
  const [staleRows, setStaleRows] = useState<FreshnessRow[]>([])

  useEffect(() => {
    let cancelled = false

    async function poll() {
      try {
        const data = await jsonRpc<FreshnessResponse>('stats.freshness_list')
        if (!cancelled) {
          // A metric nobody writes any more can't be "stale" in a way anyone
          // can act on. bootstrap_ping sat here asserting 1243h for 51 days,
          // which is how you train people to ignore the banner.
          const stale = data.rows.filter(
            r => r.monitored !== false && r.age_seconds >= data.warn_age_seconds,
          )
          setStaleRows(stale)
        }
      } catch {
        // Backend may not be running — fail silently, don't clutter with extra errors
      }
    }

    poll()
    const id = setInterval(poll, 60_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (staleRows.length === 0) return null

  return (
    <div style={bannerStyle} role="alert">
      <span>&#9888;</span>
      <div>
        <span>Stats stale — the following metrics have not been updated recently:</span>
        <ul style={metricListStyle}>
          {staleRows.map(r => (
            <li key={r.metric_name}>
              <span style={metricTagStyle}>
                {r.metric_name}: {humanAge(r.age_seconds)} stale
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
