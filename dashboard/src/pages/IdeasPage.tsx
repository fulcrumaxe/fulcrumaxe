import { useEffect, useRef, useState } from 'react'
import { Sidebar } from '../components/Sidebar'
import { Header } from '../components/Header'
import { LoadingSpinner } from '../components/LoadingSpinner'
import { ideasApi } from '../api/client'
import type { Idea } from '../api/types'

const REFRESH_INTERVAL_MS = 30_000

function useSecondsAgo(isoTimestamp: string | null): string {
  const [label, setLabel] = useState<string>('')

  useEffect(() => {
    if (!isoTimestamp) {
      setLabel('')
      return
    }
    function update() {
      const diff = Math.floor((Date.now() - new Date(isoTimestamp!).getTime()) / 1000)
      if (diff < 5) setLabel('just now')
      else if (diff < 60) setLabel(`${diff}s ago`)
      else if (diff < 3600) setLabel(`${Math.floor(diff / 60)}m ago`)
      else setLabel(`${Math.floor(diff / 3600)}h ago`)
    }
    update()
    const t = setInterval(update, 5_000)
    return () => clearInterval(t)
  }, [isoTimestamp])

  return label
}

export default function IdeasPage() {
  const [ideas, setIdeas] = useState<Idea[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sourceEmpty, setSourceEmpty] = useState(false)
  const [fetchedAt, setFetchedAt] = useState<string | null>(null)
  const [acting, setActing] = useState<Record<string, boolean>>({})
  const [refreshKey, setRefreshKey] = useState(0)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const lastUpdatedLabel = useSecondsAgo(fetchedAt)

  async function fetchIdeas() {
    try {
      const data = await ideasApi.list()
      setIdeas(data.ideas)
      setSourceEmpty(data.source_empty)
      setFetchedAt(data.fetched_at)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchIdeas()
    intervalRef.current = setInterval(fetchIdeas, REFRESH_INTERVAL_MS)
    return () => {
      if (intervalRef.current !== null) clearInterval(intervalRef.current)
    }
  }, [refreshKey])

  async function handleUpvote(id: string) {
    setActing(a => ({ ...a, [id]: true }))
    try {
      const updated = await ideasApi.upvote(id)
      setIdeas(prev => prev.map(i => (i.id === id ? updated : i)))
    } catch {
      /* leave existing state */
    } finally {
      setActing(a => ({ ...a, [id]: false }))
    }
  }

  async function handleDismiss(id: string) {
    setActing(a => ({ ...a, [id]: true }))
    try {
      const updated = await ideasApi.dismiss(id)
      setIdeas(prev => prev.map(i => (i.id === id ? updated : i)))
    } catch {
      /* leave existing state */
    } finally {
      setActing(a => ({ ...a, [id]: false }))
    }
  }

  async function handlePromote(id: string) {
    setActing(a => ({ ...a, [id]: true }))
    try {
      const updated = await ideasApi.promote(id)
      setIdeas(prev => prev.map(i => (i.id === id ? updated : i)))
    } catch {
      /* leave existing state */
    } finally {
      setActing(a => ({ ...a, [id]: false }))
    }
  }

  function statusColor(status: Idea['status']): string {
    if (status === 'promoted') return '#3fb950'
    if (status === 'dismissed') return '#8b949e'
    return '#58a6ff'
  }

  return (
    <div className="layout">
      <Sidebar />
      <div className="layout-main">
        <Header />
        <main className="main-content">
          <div
            className="page-header"
            style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
          >
            <h2 className="page-title">Ideas</h2>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              {lastUpdatedLabel && (
                <span style={{ fontSize: 12, color: '#8b949e' }}>Updated {lastUpdatedLabel}</span>
              )}
              <button
                type="button"
                onClick={() => setRefreshKey(k => k + 1)}
                style={{
                  padding: '6px 14px',
                  background: '#1f2937',
                  color: '#d1d5db',
                  border: '1px solid #374151',
                  borderRadius: 6,
                  fontSize: 13,
                  cursor: 'pointer',
                }}
              >
                Refresh
              </button>
            </div>
          </div>

          {loading && <LoadingSpinner />}
          {error && (
            <div className="error-message" role="alert">
              Failed to load ideas: {error}
            </div>
          )}

          {!loading && !error && sourceEmpty && (
            <div data-testid="ideas-empty" style={{ color: '#8b949e', padding: '24px 0' }}>
              No ideas yet. The project-manager generates ideas when the queue is idle.
            </div>
          )}

          {!loading && !error && !sourceEmpty && ideas.length === 0 && (
            <div data-testid="ideas-empty" style={{ color: '#8b949e', padding: '24px 0' }}>No ideas yet.</div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {ideas.map(idea => (
              <div
                key={idea.id}
                style={{
                  background: '#161b22',
                  border: '1px solid #30363d',
                  borderRadius: 8,
                  padding: 16,
                  opacity: idea.status === 'dismissed' ? 0.5 : 1,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    justifyContent: 'space-between',
                    gap: 12,
                  }}
                >
                  <div style={{ flex: 1 }}>
                    <div
                      style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}
                    >
                      <span style={{ fontWeight: 600, color: '#c9d1d9', fontSize: 14 }}>
                        {idea.title}
                      </span>
                      <span
                        style={{
                          fontSize: 11,
                          padding: '2px 6px',
                          borderRadius: 4,
                          background: 'rgba(0,0,0,0.3)',
                          color: statusColor(idea.status),
                          border: `1px solid ${statusColor(idea.status)}44`,
                          textTransform: 'capitalize',
                        }}
                      >
                        {idea.status}
                      </span>
                    </div>
                    <p style={{ color: '#8b949e', fontSize: 13, margin: 0, lineHeight: 1.5 }}>
                      {idea.summary}
                    </p>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                    <span
                      style={{
                        fontSize: 13,
                        color: '#58a6ff',
                        minWidth: 32,
                        textAlign: 'right',
                        fontWeight: 600,
                      }}
                    >
                      {idea.votes}
                    </span>
                    <button
                      type="button"
                      disabled={acting[idea.id] || idea.status !== 'pending'}
                      onClick={() => handleUpvote(idea.id)}
                      title="Upvote"
                      style={{
                        background: 'transparent',
                        color: '#58a6ff',
                        border: '1px solid #30363d',
                        borderRadius: 4,
                        padding: '4px 10px',
                        cursor: idea.status === 'pending' ? 'pointer' : 'default',
                        fontSize: 12,
                        opacity: idea.status !== 'pending' ? 0.4 : 1,
                      }}
                    >
                      +1
                    </button>
                    <button
                      type="button"
                      disabled={acting[idea.id] || idea.status !== 'pending'}
                      onClick={() => handleDismiss(idea.id)}
                      title="Dismiss"
                      style={{
                        background: 'transparent',
                        color: '#8b949e',
                        border: '1px solid #30363d',
                        borderRadius: 4,
                        padding: '4px 10px',
                        cursor: idea.status === 'pending' ? 'pointer' : 'default',
                        fontSize: 12,
                        opacity: idea.status !== 'pending' ? 0.4 : 1,
                      }}
                    >
                      Dismiss
                    </button>
                    <button
                      type="button"
                      disabled={acting[idea.id] || idea.status !== 'pending'}
                      onClick={() => handlePromote(idea.id)}
                      title="Promote to Discussion"
                      style={{
                        background: idea.status === 'pending' ? '#238636' : 'transparent',
                        color: '#c9d1d9',
                        border: '1px solid #30363d',
                        borderRadius: 4,
                        padding: '4px 10px',
                        cursor: idea.status === 'pending' ? 'pointer' : 'default',
                        fontSize: 12,
                        opacity: idea.status !== 'pending' ? 0.4 : 1,
                      }}
                    >
                      Promote
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </main>
      </div>
    </div>
  )
}
