/**
 * PrInspectorPage — /prs route.
 *
 * Triage view: all open PRs, gate-label state, fix-cycle count, age, quality score.
 * Row click navigates to /pr/:number (PRDetailPage from Discussion #389).
 * Complementary to PRDetailPage — this is the entry point, that's the deep-dive.
 *
 * Changes vs original:
 * - Auto-refreshes every 30s; manual Refresh button still available
 * - "Last updated Xm ago" in header
 * - Friendly empty-state when no PRs match filter
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchPrList } from '../api/pr'
import type { PrListEntry } from '../api/types'
import FilterChips, { type PrFilter } from './pr-inspector/FilterChips'
import PrTable from './pr-inspector/PrTable'
import { LastUpdated } from '../components/LastUpdated'
import { Tooltip } from '../components/Tooltip'

const ONE_DAY_SECONDS = 86400
const AUTO_REFRESH_MS = 30_000

function isStuck(pr: PrListEntry): boolean {
  return (
    pr.labels.includes('code-review-needs-fix') ||
    pr.age_seconds > ONE_DAY_SECONDS
  )
}

function isReady(pr: PrListEntry): boolean {
  return (
    pr.labels.includes('code-review-passed') &&
    !pr.labels.includes('code-review-needs-fix')
  )
}

export default function PrInspectorPage() {
  const [prs, setPrs] = useState<PrListEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<PrFilter>('all')
  const [fetchedAt, setFetchedAt] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchPrList()
      setPrs(data)
      setFetchedAt(new Date().toISOString())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    intervalRef.current = setInterval(() => { void load() }, AUTO_REFRESH_MS)
    return () => {
      if (intervalRef.current !== null) clearInterval(intervalRef.current)
    }
  }, [load])

  const filtered = prs.filter(pr => {
    if (filter === 'stuck') return isStuck(pr)
    if (filter === 'ready') return isReady(pr)
    return true
  })

  const counts = {
    all: prs.length,
    stuck: prs.filter(isStuck).length,
    ready: prs.filter(isReady).length,
  }

  const isEmptyAfterLoad = !loading && !error && filtered.length === 0

  return (
    <div style={{ padding: 24, fontFamily: 'system-ui, sans-serif', color: '#e5e7eb', background: '#111827', minHeight: '100vh', boxSizing: 'border-box' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 20, gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: '#f9fafb' }}>
            PR Inspector
          </h1>
          <p style={{ margin: '4px 0 0', fontSize: 13, color: '#6b7280' }}>
            All open pull requests — gate state, fix cycles, quality score at a glance
          </p>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
          <LastUpdated fetchedAt={fetchedAt} />
          <Tooltip label="Reload PR list from GitHub" placement="left">
            <button
              onClick={() => void load()}
              disabled={loading}
              style={{
                background: '#1f2937',
                color: '#9ca3af',
                border: '1px solid #374151',
                borderRadius: 6,
                padding: '6px 14px',
                cursor: loading ? 'not-allowed' : 'pointer',
                fontSize: 13,
              }}
            >
              {loading ? 'Loading…' : 'Refresh'}
            </button>
          </Tooltip>
        </div>
      </div>

      {/* Filters */}
      <div style={{ marginBottom: 16 }}>
        <FilterChips active={filter} onChange={setFilter} counts={counts} />
      </div>

      {/* Content */}
      {error ? (
        <div
          style={{
            background: '#7f1d1d',
            color: '#fca5a5',
            borderRadius: 8,
            padding: '12px 16px',
            fontSize: 13,
          }}
        >
          Failed to load PRs: {error}
        </div>
      ) : isEmptyAfterLoad ? (
        <div
          style={{
            background: '#1f2937',
            borderRadius: 10,
            border: '1px solid #374151',
            padding: '48px 24px',
            textAlign: 'center',
          }}
        >
          <p style={{ margin: 0, fontSize: 16, color: '#6b7280', fontWeight: 500 }}>
            All caught up — 0 stuck, 0 ready to merge
          </p>
          <p style={{ margin: '8px 0 0', fontSize: 13, color: '#4b5563' }}>
            No open PRs match the current filter.
          </p>
        </div>
      ) : (
        <div
          data-tour="pr-inspector"
          style={{
            background: '#1f2937',
            borderRadius: 10,
            border: '1px solid #374151',
            overflow: 'hidden',
          }}
        >
          <PrTable prs={filtered} />
        </div>
      )}
    </div>
  )
}
