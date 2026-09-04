/**
 * CrossFileFindingsTile — summary count of open cross-file-finding Discussions
 * in the last 7 days, with a click-through to the Discussion Explorer.
 *
 * The tile queries discussions.list with q="[cross-file-finding]" and
 * max_age_days=7, then displays the count. Clicking the tile navigates to
 * /discussions?q=[cross-file-finding] so the user can inspect each one.
 */

import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { discussionsApi } from '../../api/client'

interface CrossFileFindingsTileProps {
  /** Width/height styling — defaults to a compact square tile. */
  style?: React.CSSProperties
}

export default function CrossFileFindingsTile({ style }: CrossFileFindingsTileProps) {
  const navigate = useNavigate()
  const [count, setCount] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(false)

    discussionsApi
      .list({ q: '[cross-file-finding]', max_age_days: 7, limit: 100 })
      .then(result => {
        if (!cancelled) {
          setCount(result.items.length)
          setLoading(false)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setError(true)
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [])

  const hasFindings = count !== null && count > 0

  return (
    <div
      onClick={() => navigate('/discussions?q=%5Bcross-file-finding%5D')}
      title="Open cross-file pattern Discussions (last 7 days) — click to inspect"
      style={{
        background: '#1f2937',
        border: `1px solid ${hasFindings ? '#d97706' : '#374151'}`,
        borderRadius: 8,
        padding: '14px 18px',
        cursor: 'pointer',
        userSelect: 'none',
        minWidth: 160,
        ...style,
      }}
    >
      <div style={{ fontSize: 11, color: '#9ca3af', marginBottom: 6, fontWeight: 500 }}>
        Cross-file findings (7d)
      </div>

      {loading ? (
        <div style={{ fontSize: 24, color: '#4b5563', fontWeight: 700 }}>—</div>
      ) : error ? (
        <div style={{ fontSize: 13, color: '#6b7280' }}>unavailable</div>
      ) : (
        <div
          style={{
            fontSize: 28,
            fontWeight: 700,
            color: hasFindings ? '#fbbf24' : '#4ade80',
            lineHeight: 1,
          }}
        >
          {count}
        </div>
      )}

      <div style={{ fontSize: 11, color: '#4b5563', marginTop: 4 }}>
        {loading ? 'loading…' : error ? '' : hasFindings ? 'open — click to review' : 'all clear'}
      </div>
    </div>
  )
}
