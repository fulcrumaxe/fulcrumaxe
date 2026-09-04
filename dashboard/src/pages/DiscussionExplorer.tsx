/**
 * DiscussionExplorer.tsx — List, filter, and drill into GitHub Discussions.
 *
 * Route: /discussions
 * URL state: ?selected=<number> opens the detail drawer for that discussion.
 *
 * Data flows through JSON-RPC methods discussions.list and discussions.get on
 * backend/server.py — no direct GitHub API calls from the browser.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { costApi, discussionsApi } from '../api/client'
import { DiscussionStatusBadge } from '../components/StatusBadge'
import { LastUpdated } from '../components/LastUpdated'
import { formatTime } from '../lib/safeDate'
import { useActiveRepo } from '../hooks/useActiveRepo'
import { discussionUrl, pullUrl } from '../lib/repoUrls'
import type {
  DiscussionSummary,
  DiscussionGetResult,
  DiscussionStatus,
  PerDiscussionCost,
} from '../api/types'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ALL_STATUSES: DiscussionStatus[] = [
  'DISCUSSING',
  'SPEC_READY',
  'IMPLEMENTING',
  'REVIEWING',
  'DONE',
  'CLOSED',
]

const AGE_OPTIONS = [
  { label: '1d', days: 1 },
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
  { label: 'All', days: 0 },
]

type SortField = 'status' | 'title' | 'age' | 'cost'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a dollar cost for the list cell. null/0 → "$0.00"; tiny values use 4dp. */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper, unit tested directly
export function formatCostUsd(value: number | null | undefined): string {
  if (value == null || value === 0) return '$0.00'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

function relativeAge(isoDate: string | null): string {
  if (!isoDate) return '—'
  const diffMs = Date.now() - new Date(isoDate).getTime()
  const diffDays = Math.floor(diffMs / 86_400_000)
  if (diffDays === 0) return 'today'
  if (diffDays === 1) return '1 day ago'
  if (diffDays < 30) return `${diffDays} days ago`
  const months = Math.floor(diffDays / 30)
  return months === 1 ? '1 month ago' : `${months} months ago`
}

function sortItems(
  items: DiscussionSummary[],
  field: SortField,
  asc: boolean
): DiscussionSummary[] {
  const sorted = [...items].sort((a, b) => {
    if (field === 'status') {
      return a.status.localeCompare(b.status)
    }
    if (field === 'title') {
      return a.title.localeCompare(b.title)
    }
    if (field === 'cost') {
      return (a.costUsd ?? 0) - (b.costUsd ?? 0)
    }
    // age: sort by updatedAt descending by default
    const ta = a.updatedAt ? new Date(a.updatedAt).getTime() : 0
    const tb = b.updatedAt ? new Date(b.updatedAt).getTime() : 0
    return tb - ta
  })
  return asc ? sorted : sorted.reverse()
}

// ---------------------------------------------------------------------------
// Inline markdown renderer (no dependency — supports headings, bold, code, links)
// ---------------------------------------------------------------------------

function SimpleMarkdown({ text }: { text: string }) {
  // Very lightweight renderer: headings, bold, inline code, paragraphs.
  // Full react-markdown would require a new dependency — spec says add if not present;
  // to keep bundle impact minimal we use this for the body and comments.
  const lines = text.split('\n')
  const elements: JSX.Element[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (/^#{1,6}\s/.test(line)) {
      const level = line.match(/^(#{1,6})\s/)![1].length as 1 | 2 | 3 | 4 | 5 | 6
      const content = line.replace(/^#{1,6}\s/, '')
      const Tag = `h${level}` as keyof JSX.IntrinsicElements
      elements.push(
        <Tag key={i} style={{ margin: '12px 0 4px', color: '#e5e7eb' }}>
          {content}
        </Tag>
      )
    } else if (line.trim() === '') {
      elements.push(<br key={i} />)
    } else {
      // Inline bold + code
      const parts = line.split(/(`[^`]+`|\*\*[^*]+\*\*)/)
      elements.push(
        <p key={i} style={{ margin: '4px 0', color: '#d1d5db', lineHeight: 1.6 }}>
          {parts.map((part, j) => {
            if (part.startsWith('`') && part.endsWith('`')) {
              return (
                <code
                  key={j}
                  style={{
                    background: '#374151',
                    borderRadius: 3,
                    padding: '1px 4px',
                    fontFamily: 'monospace',
                    fontSize: 12,
                    color: '#93c5fd',
                  }}
                >
                  {part.slice(1, -1)}
                </code>
              )
            }
            if (part.startsWith('**') && part.endsWith('**')) {
              return <strong key={j}>{part.slice(2, -2)}</strong>
            }
            return part
          })}
        </p>
      )
    }
    i++
  }
  return <>{elements}</>
}

// ---------------------------------------------------------------------------
// Detail drawer
// ---------------------------------------------------------------------------

interface DrawerProps {
  number: number
  onClose: () => void
}

function DiscussionDrawer({ number, onClose }: DrawerProps) {
  const [data, setData] = useState<DiscussionGetResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [costData, setCostData] = useState<PerDiscussionCost | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    discussionsApi
      .get(number)
      .then(result => {
        if (!cancelled) {
          setData(result)
          setLoading(false)
        }
      })
      .catch(err => {
        if (!cancelled) {
          setError((err as Error).message)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [number])

  useEffect(() => {
    let cancelled = false
    setCostData(null)
    costApi
      .perDiscussion(number)
      .then(result => {
        if (!cancelled) setCostData(result ?? null)
      })
      .catch(() => {
        // non-fatal — cost section just stays empty
      })
    return () => {
      cancelled = true
    }
  }, [number])

  // Esc key closes the drawer
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const drawerStyle: React.CSSProperties = {
    position: 'fixed',
    top: 0,
    right: 0,
    bottom: 0,
    width: 520,
    background: '#1f2937',
    borderLeft: '1px solid #374151',
    display: 'flex',
    flexDirection: 'column',
    zIndex: 1000,
    overflowY: 'auto',
  }

  const headerStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '12px 16px',
    borderBottom: '1px solid #374151',
    position: 'sticky',
    top: 0,
    background: '#1f2937',
    zIndex: 1,
  }

  return (
    <aside style={drawerStyle} aria-label="Discussion detail">
      <div style={headerStyle}>
        <span style={{ color: '#9ca3af', fontSize: 12 }}>Discussion #{number}</span>
        <button
          onClick={onClose}
          aria-label="Close drawer"
          style={{
            background: 'none',
            border: 'none',
            color: '#9ca3af',
            cursor: 'pointer',
            fontSize: 18,
            lineHeight: 1,
            padding: 4,
          }}
        >
          ✕
        </button>
      </div>

      <div style={{ padding: 16, flex: 1 }}>
        {loading && (
          <div style={{ color: '#6b7280', textAlign: 'center', marginTop: 48 }}>
            Loading…
          </div>
        )}
        {error && (
          <div style={{ color: '#f87171', padding: 12, background: '#1f1f1f', borderRadius: 6 }}>
            Error: {error}
          </div>
        )}
        {data && (
          <>
            <h2 style={{ margin: '0 0 8px', color: '#f3f4f6', fontSize: 16 }}>
              {data.discussion.title}
            </h2>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
              <DiscussionStatusBadge status={data.discussion.status} />
              {data.discussion.author && (
                <span style={{ color: '#9ca3af', fontSize: 12 }}>
                  by {data.discussion.author}
                </span>
              )}
              {data.discussion.updatedAt && (
                <span style={{ color: '#6b7280', fontSize: 12 }}>
                  {relativeAge(data.discussion.updatedAt)}
                </span>
              )}
            </div>

            {/* Body */}
            <section
              style={{
                background: '#111827',
                borderRadius: 6,
                padding: 12,
                marginBottom: 16,
                fontSize: 13,
              }}
            >
              <SimpleMarkdown text={data.discussion.body} />
            </section>

            {/* Linked PR */}
            {data.linked_pr && (
              <section style={{ marginBottom: 16 }}>
                <h3 style={{ color: '#9ca3af', fontSize: 12, textTransform: 'uppercase', margin: '0 0 6px' }}>
                  Linked PR
                </h3>
                <div
                  style={{
                    background: '#111827',
                    borderRadius: 6,
                    padding: 10,
                    display: 'flex',
                    gap: 8,
                    alignItems: 'center',
                    flexWrap: 'wrap',
                  }}
                >
                  <a
                    href={data.linked_pr.url}
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: '#60a5fa', fontSize: 13, fontWeight: 600 }}
                  >
                    #{data.linked_pr.number}
                  </a>
                  <span
                    style={{
                      fontSize: 11,
                      padding: '2px 6px',
                      borderRadius: 3,
                      background: data.linked_pr.state === 'MERGED' ? '#16a34a' : data.linked_pr.state === 'OPEN' ? '#2563eb' : '#6b7280',
                      color: '#fff',
                      fontWeight: 600,
                    }}
                  >
                    {data.linked_pr.state}
                  </span>
                  {data.linked_pr.labels.map(label => (
                    <span
                      key={label}
                      style={{
                        fontSize: 11,
                        padding: '2px 6px',
                        borderRadius: 3,
                        background: '#374151',
                        color: '#d1d5db',
                      }}
                    >
                      {label}
                    </span>
                  ))}
                </div>
              </section>
            )}

            {/* Comments */}
            {data.comments.length > 0 && (
              <section style={{ marginBottom: 16 }}>
                <h3 style={{ color: '#9ca3af', fontSize: 12, textTransform: 'uppercase', margin: '0 0 6px' }}>
                  Last {data.comments.length} Comment{data.comments.length !== 1 ? 's' : ''}
                </h3>
                {data.comments.map((c, idx) => (
                  <div
                    key={idx}
                    style={{
                      background: '#111827',
                      borderRadius: 6,
                      padding: 10,
                      marginBottom: 8,
                      fontSize: 13,
                    }}
                  >
                    <div style={{ display: 'flex', gap: 8, marginBottom: 4 }}>
                      <span style={{ color: '#60a5fa', fontWeight: 600 }}>
                        {c.author ?? 'unknown'}
                      </span>
                      <span style={{ color: '#6b7280' }}>{relativeAge(c.createdAt)}</span>
                    </div>
                    <SimpleMarkdown text={c.body} />
                  </div>
                ))}
              </section>
            )}

            {/* Agent runs */}
            {data.agent_runs.length > 0 && (
              <section>
                <h3 style={{ color: '#9ca3af', fontSize: 12, textTransform: 'uppercase', margin: '0 0 6px' }}>
                  Agent Activity
                </h3>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {data.agent_runs.map((run, idx) => (
                    <div
                      key={idx}
                      style={{
                        background: '#111827',
                        borderRadius: 4,
                        padding: '6px 10px',
                        display: 'flex',
                        gap: 10,
                        alignItems: 'center',
                        fontSize: 12,
                      }}
                    >
                      <span style={{ color: '#6b7280', fontFamily: 'monospace' }}>
                        {run.ts ? formatTime(run.ts) : '—'}
                      </span>
                      <span style={{ color: '#93c5fd' }}>{run.role}</span>
                      <span
                        style={{
                          color:
                            run.verdict === 'pass' || run.verdict === 'done'
                              ? '#4ade80'
                              : run.verdict === 'fail' || run.verdict === 'needs-fix'
                              ? '#f87171'
                              : '#d1d5db',
                        }}
                      >
                        {run.verdict || '—'}
                      </span>
                      {run.pr && (
                        <span style={{ color: '#9ca3af' }}>PR #{run.pr}</span>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* Cost breakdown */}
            <section style={{ marginTop: 16 }}>
              <h3 style={{ color: '#9ca3af', fontSize: 12, textTransform: 'uppercase', margin: '0 0 6px' }}>
                Cost
              </h3>
              {costData ? (
                <div style={{ background: '#111827', borderRadius: 6, padding: 12, fontSize: 13 }}>
                  <div style={{ marginBottom: 8, color: '#e5e7eb' }}>
                    <span style={{ fontWeight: 600 }}>{formatCostUsd(costData.total_cost_usd)}</span>
                    <span style={{ color: '#6b7280', marginLeft: 8 }}>
                      {costData.total_input_tokens.toLocaleString()} in / {costData.total_output_tokens.toLocaleString()} out tokens
                    </span>
                  </div>
                  {Object.keys(costData.agent_breakdown).length === 0 && Object.keys(costData.pr_breakdown).length === 0 ? (
                    <div style={{ color: '#6b7280' }}>no recorded spend yet</div>
                  ) : (
                    <>
                      {Object.keys(costData.agent_breakdown).length > 0 && (
                        <div style={{ marginBottom: 8 }}>
                          <div style={{ color: '#6b7280', fontSize: 11, marginBottom: 4 }}>By agent</div>
                          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                            <tbody>
                              {Object.entries(costData.agent_breakdown)
                                .sort((a, b) => b[1] - a[1])
                                .map(([role, usd]) => (
                                  <tr key={role}>
                                    <td style={{ padding: '2px 0', color: '#93c5fd' }}>{role}</td>
                                    <td style={{ padding: '2px 0', textAlign: 'right', color: '#d1d5db' }}>{formatCostUsd(usd)}</td>
                                  </tr>
                                ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                      {Object.keys(costData.pr_breakdown).length > 0 && (
                        <div>
                          <div style={{ color: '#6b7280', fontSize: 11, marginBottom: 4 }}>By PR</div>
                          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                            <tbody>
                              {Object.entries(costData.pr_breakdown)
                                .sort((a, b) => b[1] - a[1])
                                .map(([pr, usd]) => (
                                  <tr key={pr}>
                                    <td style={{ padding: '2px 0', color: '#60a5fa' }}>PR #{pr}</td>
                                    <td style={{ padding: '2px 0', textAlign: 'right', color: '#d1d5db' }}>{formatCostUsd(usd)}</td>
                                  </tr>
                                ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </>
                  )}
                </div>
              ) : (
                <div style={{ background: '#111827', borderRadius: 6, padding: 12, color: '#6b7280', fontSize: 13 }}>
                  no recorded spend yet
                </div>
              )}
            </section>
          </>
        )}
      </div>
    </aside>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function DiscussionExplorer() {
  const repo = useActiveRepo()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedNumber = searchParams.get('selected')
    ? parseInt(searchParams.get('selected')!, 10)
    : null

  // Filter state
  const [activeStatuses, setActiveStatuses] = useState<Set<DiscussionStatus>>(
    new Set(['DISCUSSING', 'SPEC_READY'])
  )
  const [searchInput, setSearchInput] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [ageDays, setAgeDays] = useState(0) // 0 = all

  // Data state
  const [items, setItems] = useState<DiscussionSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [fetchedAt, setFetchedAt] = useState<string | null>(null)

  // Sort state
  const [sortField, setSortField] = useState<SortField>('age')
  const [sortAsc, setSortAsc] = useState(false)

  // Manual refresh
  const [refreshKey, setRefreshKey] = useState(0)

  // Preserve list scroll position when drawer opens/closes
  const listRef = useRef<HTMLDivElement>(null)

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchInput), 250)
    return () => clearTimeout(timer)
  }, [searchInput])

  // Fetch data whenever relevant filters change
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    // For multi-status we fetch all and filter client-side
    discussionsApi
      .list({
        status: '*',
        q: debouncedSearch || undefined,
        max_age_days: ageDays > 0 ? ageDays : undefined,
        limit: 200,
      })
      .then(result => {
        if (!cancelled) {
          const filtered = result.items.filter(item =>
            activeStatuses.size === 0 || activeStatuses.has(item.status)
          )
          setItems(filtered)
          setError(null)
          setFetchedAt(new Date().toISOString())
          setLoading(false)
        }
      })
      .catch(err => {
        if (!cancelled) {
          setError((err as Error).message)
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [debouncedSearch, ageDays, activeStatuses, refreshKey])

  const sortedItems = sortItems(items, sortField, sortAsc)

  function toggleStatus(status: DiscussionStatus) {
    setActiveStatuses(prev => {
      const next = new Set(prev)
      if (next.has(status)) {
        next.delete(status)
      } else {
        next.add(status)
      }
      return next
    })
  }

  function openDrawer(number: number) {
    setSearchParams(params => {
      params.set('selected', String(number))
      return params
    })
  }

  const closeDrawer = useCallback(() => {
    setSearchParams(params => {
      params.delete('selected')
      return params
    })
  }, [setSearchParams])

  function handleSort(field: SortField) {
    if (sortField === field) {
      setSortAsc(a => !a)
    } else {
      setSortField(field)
      setSortAsc(true)
    }
  }

  function clearFilters() {
    setActiveStatuses(new Set(['DISCUSSING', 'SPEC_READY']))
    setSearchInput('')
    setAgeDays(0)
  }

  // ---------------------------------------------------------------------------
  // Styles
  // ---------------------------------------------------------------------------

  const pageStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    height: '100vh',
    background: '#111827',
    color: '#d1d5db',
    fontFamily: 'system-ui, sans-serif',
    fontSize: 13,
  }

  const filterBarStyle: React.CSSProperties = {
    padding: '10px 16px',
    borderBottom: '1px solid #374151',
    display: 'flex',
    gap: 12,
    alignItems: 'center',
    flexWrap: 'wrap',
    background: '#1f2937',
  }

  const chipStyle = (active: boolean): React.CSSProperties => ({
    padding: '3px 10px',
    borderRadius: 12,
    border: `1px solid ${active ? '#3b82f6' : '#374151'}`,
    background: active ? '#1d4ed8' : 'transparent',
    color: active ? '#fff' : '#9ca3af',
    cursor: 'pointer',
    fontSize: 12,
    fontWeight: active ? 600 : 400,
  })

  const thStyle = (field: SortField): React.CSSProperties => ({
    textAlign: 'left',
    padding: '8px 12px',
    color: '#6b7280',
    fontSize: 11,
    fontWeight: 600,
    textTransform: 'uppercase',
    cursor: 'pointer',
    userSelect: 'none',
    borderBottom: '1px solid #374151',
    whiteSpace: 'nowrap',
    background: sortField === field ? '#1f2937' : 'transparent',
  })

  const rowStyle: React.CSSProperties = {
    display: 'table-row',
    cursor: 'pointer',
  }

  return (
    <div style={pageStyle}>
      {/* Filter bar */}
      <div style={filterBarStyle}>
        {/* Status chips — multiple selections combine with OR logic */}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          {ALL_STATUSES.map(status => (
            <button
              key={status}
              style={chipStyle(activeStatuses.has(status))}
              onClick={() => toggleStatus(status)}
              aria-pressed={activeStatuses.has(status)}
            >
              {status}
            </button>
          ))}
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 16,
              height: 16,
              borderRadius: '50%',
              background: '#374151',
              color: '#9ca3af',
              fontSize: 10,
              cursor: 'default',
              flexShrink: 0,
            }}
            title="Multiple chips selected = OR filter: shows discussions matching ANY selected status"
          >
            ?
          </span>
        </div>

        {/* Title search */}
        <input
          type="search"
          placeholder="Search titles…"
          value={searchInput}
          onChange={e => setSearchInput(e.target.value)}
          style={{
            background: '#111827',
            border: '1px solid #374151',
            borderRadius: 6,
            padding: '4px 10px',
            color: '#d1d5db',
            fontSize: 13,
            outline: 'none',
            width: 180,
          }}
        />

        {/* Age filter */}
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          <span style={{ color: '#6b7280', fontSize: 11 }}>Age:</span>
          {AGE_OPTIONS.map(opt => (
            <button
              key={opt.label}
              style={chipStyle(ageDays === opt.days)}
              onClick={() => setAgeDays(opt.days)}
            >
              {opt.label}
            </button>
          ))}
        </div>

        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
          <LastUpdated fetchedAt={fetchedAt} />
          <button
            onClick={() => setRefreshKey(k => k + 1)}
            style={{
              background: '#1f2937',
              border: '1px solid #374151',
              borderRadius: 6,
              color: '#d1d5db',
              cursor: 'pointer',
              fontSize: 12,
              padding: '3px 10px',
            }}
          >
            Refresh
          </button>
          <button
            onClick={clearFilters}
            style={{
              background: 'none',
              border: '1px solid #374151',
              borderRadius: 6,
              color: '#9ca3af',
              cursor: 'pointer',
              fontSize: 12,
              padding: '3px 10px',
            }}
          >
            Clear filters
          </button>
        </div>
      </div>

      {/* List */}
      <div ref={listRef} style={{ flex: 1, overflowY: 'auto', paddingRight: selectedNumber ? 520 : 0 }}>
        {error && (
          <div
            role="alert"
            style={{
              margin: 16,
              padding: 12,
              background: '#450a0a',
              borderRadius: 6,
              color: '#f87171',
              display: 'flex',
              gap: 12,
              alignItems: 'center',
            }}
          >
            <span>{error}</span>
            <button
              onClick={clearFilters}
              style={{ background: 'none', border: '1px solid #f87171', borderRadius: 4, color: '#f87171', cursor: 'pointer', padding: '2px 8px', fontSize: 12 }}
            >
              Retry
            </button>
          </div>
        )}

        {loading && (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <tbody>
              {Array.from({ length: 8 }).map((_, i) => (
                <tr key={i}>
                  <td style={{ padding: '10px 12px' }}>
                    <div
                      style={{
                        height: 16,
                        borderRadius: 4,
                        background: '#1f2937',
                        width: `${60 + Math.random() * 30}%`,
                        animation: 'pulse 1.5s ease-in-out infinite',
                      }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {!loading && !error && sortedItems.length === 0 && (
          <div
            style={{
              textAlign: 'center',
              marginTop: 80,
              color: '#6b7280',
            }}
          >
            <p style={{ fontSize: 16, marginBottom: 8 }}>No discussions match your filters.</p>
            <button
              onClick={clearFilters}
              style={{
                background: '#1f2937',
                border: '1px solid #374151',
                borderRadius: 6,
                color: '#9ca3af',
                cursor: 'pointer',
                padding: '6px 16px',
                fontSize: 13,
              }}
            >
              Clear filters
            </button>
          </div>
        )}

        {!loading && sortedItems.length > 0 && (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={thStyle('status')} onClick={() => handleSort('status')}>
                  Status {sortField === 'status' ? (sortAsc ? '↑' : '↓') : ''}
                </th>
                <th style={thStyle('title')} onClick={() => handleSort('title')}>
                  Title {sortField === 'title' ? (sortAsc ? '↑' : '↓') : ''}
                </th>
                <th style={thStyle('age')} onClick={() => handleSort('age')}>
                  Updated {sortField === 'age' ? (sortAsc ? '↑' : '↓') : ''}
                </th>
                <th style={{ ...thStyle('cost'), textAlign: 'right' }} onClick={() => handleSort('cost')}>
                  $ {sortField === 'cost' ? (sortAsc ? '↑' : '↓') : ''}
                </th>
                <th
                  style={{
                    ...thStyle('status'),
                    cursor: 'default',
                    background: 'transparent',
                  }}
                >
                  PR
                </th>
              </tr>
            </thead>
            <tbody>
              {sortedItems.map(item => {
                const discHref = discussionUrl(repo, item.number)
                const prHref = item.linkedPr != null ? pullUrl(repo, item.linkedPr) : null
                return (
                <tr
                  key={item.number}
                  style={{
                    ...rowStyle,
                    background:
                      selectedNumber === item.number ? '#1e3a5f' : 'transparent',
                  }}
                  onClick={() => openDrawer(item.number)}
                  onMouseEnter={e =>
                    selectedNumber !== item.number &&
                    ((e.currentTarget as HTMLTableRowElement).style.background = '#1f2937')
                  }
                  onMouseLeave={e =>
                    selectedNumber !== item.number &&
                    ((e.currentTarget as HTMLTableRowElement).style.background = 'transparent')
                  }
                >
                  <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }}>
                    <DiscussionStatusBadge status={item.status} />
                  </td>
                  <td style={{ padding: '10px 12px', color: '#e5e7eb' }}>
                    <span style={{ fontWeight: 500, color: '#9ca3af', marginRight: 4 }}>
                      #{item.number}
                    </span>
                    {discHref ? (
                      <a
                        href={discHref}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={e => e.stopPropagation()}
                        style={{ color: '#e5e7eb', textDecoration: 'none' }}
                        onMouseEnter={e => { (e.currentTarget as HTMLAnchorElement).style.color = '#60a5fa' }}
                        onMouseLeave={e => { (e.currentTarget as HTMLAnchorElement).style.color = '#e5e7eb' }}
                      >
                        {item.title}
                      </a>
                    ) : (
                      <span style={{ color: '#e5e7eb' }}>{item.title}</span>
                    )}
                  </td>
                  <td style={{ padding: '10px 12px', color: '#9ca3af', whiteSpace: 'nowrap' }}>
                    {relativeAge(item.updatedAt)}
                  </td>
                  <td
                    style={{ padding: '10px 12px', whiteSpace: 'nowrap', textAlign: 'right', color: item.costUsd ? '#34d399' : '#374151' }}
                    title={item.costUsd ? `${item.costUsd.toFixed(6)} USD` : 'no recorded spend'}
                  >
                    {item.costUsd ? formatCostUsd(item.costUsd) : '—'}
                  </td>
                  <td style={{ padding: '10px 12px', whiteSpace: 'nowrap' }}>
                    {item.linkedPr ? (
                      prHref ? (
                        <a
                          href={prHref}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={e => e.stopPropagation()}
                          style={{
                            display: 'inline-block',
                            background: '#1d4ed8',
                            color: '#fff',
                            borderRadius: 4,
                            padding: '2px 6px',
                            fontSize: 11,
                            fontWeight: 600,
                            textDecoration: 'none',
                          }}
                        >
                          PR #{item.linkedPr}
                        </a>
                      ) : (
                        <span
                          style={{
                            display: 'inline-block',
                            background: '#1d4ed8',
                            color: '#fff',
                            borderRadius: 4,
                            padding: '2px 6px',
                            fontSize: 11,
                            fontWeight: 600,
                          }}
                        >
                          PR #{item.linkedPr}
                        </span>
                      )
                    ) : (
                      <span style={{ color: '#374151' }}>—</span>
                    )}
                  </td>
                </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Detail drawer */}
      {selectedNumber !== null && (
        <DiscussionDrawer number={selectedNumber} onClose={closeDrawer} />
      )}
    </div>
  )
}
