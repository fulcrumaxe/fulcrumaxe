import { useEffect, useRef, useState } from 'react'
import { circuitBreakerApi } from '../api/client'
import type { CircuitBreakerTransition } from '../api/types'
import { formatLocaleString } from '../lib/safeDate'

interface Props {
  role: string
  onClose: () => void
}

type SortDir = 'asc' | 'desc'

function formatTs(ts: string): string {
  return formatLocaleString(ts, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function CircuitBreakerHistoryDrawer({ role, onClose }: Props) {
  const [entries, setEntries] = useState<CircuitBreakerTransition[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const drawerRef = useRef<HTMLElement>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    circuitBreakerApi
      .history(role, 20)
      .then(data => {
        if (!cancelled) setEntries(data)
      })
      .catch(err => {
        if (!cancelled) setError(String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [role])

  // Close on backdrop click (outside drawer panel)
  function handleBackdropClick(e: React.MouseEvent<HTMLDivElement>) {
    if (drawerRef.current && !drawerRef.current.contains(e.target as Node)) {
      onClose()
    }
  }

  // Close on Escape key
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const sorted = [...entries].sort((a, b) => {
    const diff = a.timestamp.localeCompare(b.timestamp)
    return sortDir === 'asc' ? diff : -diff
  })

  return (
    // Backdrop
    <div
      className="cb-drawer-backdrop"
      onClick={handleBackdropClick}
    >
      {/* Drawer panel — aside element for E2E selectability */}
      <aside
        className="cb-drawer-panel"
        ref={drawerRef}
        aria-label="Circuit breaker history"
        aria-modal="true"
        role="complementary"
      >
        <div className="cb-drawer-header">
          <h2 className="cb-drawer-title">
            Circuit breaker history — <code>{role}</code>
          </h2>
          <button
            className="cb-drawer-close"
            onClick={onClose}
            aria-label="Close drawer"
          >
            ✕
          </button>
        </div>

        <div className="cb-drawer-body">
          {loading && <p className="cb-drawer-loading">Loading…</p>}
          {error && <p className="cb-drawer-error">Error: {error}</p>}
          {!loading && !error && entries.length === 0 && (
            <p className="cb-drawer-empty">No transitions recorded for role <code>{role}</code>.</p>
          )}
          {!loading && !error && entries.length > 0 && (
            <table className="cb-drawer-table">
              <thead>
                <tr>
                  <th>
                    <button
                      className="cb-drawer-sort-btn"
                      onClick={() => setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))}
                      aria-label={`Sort by time ${sortDir === 'asc' ? 'descending' : 'ascending'}`}
                    >
                      Time {sortDir === 'asc' ? '↑' : '↓'}
                    </button>
                  </th>
                  <th>Transition</th>
                  <th>Reason</th>
                  <th>PR</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((e, i) => (
                  <tr
                    key={i}
                    className={`cb-drawer-row cb-drawer-row--${e.to_state}`}
                  >
                    <td className="cb-drawer-cell cb-drawer-cell--time">
                      {formatTs(e.timestamp)}
                    </td>
                    <td className="cb-drawer-cell cb-drawer-cell--transition">
                      <span className={`cb-state cb-state--${e.from_state}`}>{e.from_state}</span>
                      {' → '}
                      <span className={`cb-state cb-state--${e.to_state}`}>{e.to_state}</span>
                    </td>
                    <td className="cb-drawer-cell cb-drawer-cell--reason" title={e.reason}>
                      {e.reason}
                    </td>
                    <td className="cb-drawer-cell cb-drawer-cell--pr">
                      {e.last_pr != null ? `#${e.last_pr}` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </aside>
    </div>
  )
}
