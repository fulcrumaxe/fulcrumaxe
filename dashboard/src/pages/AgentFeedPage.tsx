/**
 * AgentFeedPage — live agent event stream for a project.
 *
 * Added in Discussion #495:
 * - Date-range filter (today / 7d / 30d)
 * - Event-type filter beyond "All" role dropdown
 * - Status pills color-coded by verdict (pass/done=green, fail/needs-fix=red, others=gray)
 */
import { useState, useRef, useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { Sidebar } from '../components/Sidebar'
import { Header } from '../components/Header'
import { useWebSocket } from '../hooks/useWebSocket'
import type { WsEvent } from '../api/types'
import { formatTime } from '../lib/safeDate'

const ROLE_OPTIONS = ['all', 'executor', 'code-reviewer', 'security-reviewer', 'impl-coordinator', 'project-manager', 'team-lead']

type DateRange = 'today' | '7d' | '30d' | 'all'
const DATE_RANGE_OPTIONS: { label: string; value: DateRange }[] = [
  { label: 'Today', value: 'today' },
  { label: '7d', value: '7d' },
  { label: '30d', value: '30d' },
  { label: 'All', value: 'all' },
]

type EventTypeFilter = 'all' | 'agent' | 'loop' | 'pr' | 'discussion'
const EVENT_TYPE_OPTIONS: { label: string; value: EventTypeFilter }[] = [
  { label: 'All', value: 'all' },
  { label: 'Agent', value: 'agent' },
  { label: 'Loop', value: 'loop' },
  { label: 'PR', value: 'pr' },
  { label: 'Discussion', value: 'discussion' },
]

function getStartOfToday(): number {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

function getCutoffMs(range: DateRange): number {
  if (range === 'today') return getStartOfToday()
  if (range === '7d') return Date.now() - 7 * 86400_000
  if (range === '30d') return Date.now() - 30 * 86400_000
  return 0
}

/** Return a color for a status-pill based on content keywords. */
function verdictColor(content: string | undefined): string | undefined {
  if (!content) return undefined
  const lower = content.toLowerCase()
  if (lower.includes('pass') || lower.includes('done') || lower.includes('merged') || lower.includes('success')) {
    return '#4ade80'  // green
  }
  if (lower.includes('fail') || lower.includes('error') || lower.includes('needs-fix') || lower.includes('blocked')) {
    return '#f87171'  // red
  }
  return undefined  // default gray
}

function EventLine({ event }: { event: WsEvent }) {
  const color = verdictColor(event.content)
  return (
    <div
      style={{
        display: 'flex',
        gap: 10,
        padding: '5px 12px',
        borderBottom: '1px solid #1f2937',
        fontSize: 12,
        alignItems: 'flex-start',
      }}
    >
      <span style={{ color: '#4b5563', fontFamily: 'monospace', flexShrink: 0, minWidth: 70 }}>
        {formatTime(event.timestamp, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
      </span>
      <span
        style={{
          background: '#374151',
          color: '#93c5fd',
          borderRadius: 3,
          padding: '1px 5px',
          fontSize: 11,
          flexShrink: 0,
          minWidth: 80,
        }}
      >
        {event.event}
      </span>
      {event.role && (
        <span
          style={{
            background: '#1e3a5f',
            color: '#60a5fa',
            borderRadius: 3,
            padding: '1px 5px',
            fontSize: 11,
            flexShrink: 0,
          }}
        >
          {event.role}
        </span>
      )}
      <span
        style={{
          color: color ?? '#d1d5db',
          flex: 1,
          wordBreak: 'break-word',
        }}
      >
        {event.content ?? event.event}
      </span>
    </div>
  )
}

export default function AgentFeedPage() {
  const { id = '' } = useParams<{ id: string }>()
  const { events, connected } = useWebSocket()
  const [roleFilter, setRoleFilter] = useState<string>('all')
  const [dateRange, setDateRange] = useState<DateRange>('all')
  const [eventTypeFilter, setEventTypeFilter] = useState<EventTypeFilter>('all')
  const [autoScroll, setAutoScroll] = useState(true)
  const feedRef = useRef<HTMLDivElement>(null)

  const cutoffMs = getCutoffMs(dateRange)

  const filtered: WsEvent[] = events.filter(e => {
    if (id && e.projectId && e.projectId !== id) return false
    if (roleFilter !== 'all' && e.role !== roleFilter) return false
    if (eventTypeFilter !== 'all') {
      const prefix = e.event.split('.')[0]
      if (prefix !== eventTypeFilter) return false
    }
    if (cutoffMs > 0) {
      const ts = new Date(e.timestamp).getTime()
      if (ts < cutoffMs) return false
    }
    return true
  })

  useEffect(() => {
    if (autoScroll && feedRef.current) {
      feedRef.current.scrollTop = feedRef.current.scrollHeight
    }
  }, [filtered.length, autoScroll])

  const handleScroll = () => {
    if (!feedRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = feedRef.current
    const atBottom = scrollHeight - scrollTop - clientHeight < 40
    setAutoScroll(atBottom)
  }

  const selectStyle: React.CSSProperties = {
    background: '#1f2937',
    color: '#d1d5db',
    border: '1px solid #374151',
    borderRadius: 4,
    padding: '3px 8px',
    fontSize: 12,
    cursor: 'pointer',
  }

  const chipStyle = (active: boolean): React.CSSProperties => ({
    padding: '3px 9px',
    borderRadius: 10,
    border: `1px solid ${active ? '#3b82f6' : '#374151'}`,
    background: active ? '#1d4ed8' : 'transparent',
    color: active ? '#fff' : '#9ca3af',
    cursor: 'pointer',
    fontSize: 11,
    fontWeight: active ? 600 : 400,
  })

  return (
    <div className="layout">
      <Sidebar />
      <div className="layout-main">
        <Header projectName={id} connected={connected} />
        <main className="main-content main-content--feed">
          <div
            className="feed-toolbar"
            style={{ display: 'flex', gap: 12, padding: '10px 12px', alignItems: 'center', flexWrap: 'wrap', borderBottom: '1px solid #374151', background: '#1f2937' }}
          >
            {/* Role filter */}
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <label htmlFor="role-filter" style={{ color: '#6b7280', fontSize: 11 }}>Role:</label>
              <select
                id="role-filter"
                style={selectStyle}
                value={roleFilter}
                onChange={e => setRoleFilter(e.target.value)}
              >
                {ROLE_OPTIONS.map(r => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
            </div>

            {/* Event type filter */}
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              <span style={{ color: '#6b7280', fontSize: 11 }}>Type:</span>
              {EVENT_TYPE_OPTIONS.map(opt => (
                <button
                  key={opt.value}
                  style={chipStyle(eventTypeFilter === opt.value)}
                  onClick={() => setEventTypeFilter(opt.value)}
                >
                  {opt.label}
                </button>
              ))}
            </div>

            {/* Date range filter */}
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              <span style={{ color: '#6b7280', fontSize: 11 }}>Range:</span>
              {DATE_RANGE_OPTIONS.map(opt => (
                <button
                  key={opt.value}
                  style={chipStyle(dateRange === opt.value)}
                  onClick={() => setDateRange(opt.value)}
                >
                  {opt.label}
                </button>
              ))}
            </div>

            <span style={{ color: '#4b5563', fontSize: 11, marginLeft: 'auto' }}>
              {filtered.length} event{filtered.length !== 1 ? 's' : ''}
            </span>

            {!autoScroll && (
              <button
                style={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 4, color: '#9ca3af', cursor: 'pointer', padding: '3px 10px', fontSize: 12 }}
                onClick={() => {
                  setAutoScroll(true)
                  if (feedRef.current) {
                    feedRef.current.scrollTop = feedRef.current.scrollHeight
                  }
                }}
              >
                Jump to latest
              </button>
            )}
          </div>

          <div
            ref={feedRef}
            style={{ flex: 1, overflowY: 'auto', background: '#111827' }}
            onScroll={handleScroll}
            role="log"
            aria-live="polite"
            aria-label="Agent activity feed"
          >
            {filtered.length === 0 && (
              <div data-testid="agent-feed-empty" style={{ color: '#4b5563', padding: 32, textAlign: 'center', fontSize: 13 }}>
                No events match your filters.
              </div>
            )}
            {filtered.map((e, i) => (
              <EventLine key={i} event={e} />
            ))}
          </div>
        </main>
      </div>
    </div>
  )
}
