import { useState, useEffect, useRef, useCallback } from 'react'
import { AgentEvent, JsonRpcError, LoopEntry, TeamStatusSnapshot } from '../types/loopController'
import { JsonRpcClient } from '../lib/jsonrpcClient'
import { tailAgentFeed, TailHandle } from '../lib/agentFeedTail'
import { jsonRpc, getRpcBaseUrl, getRpcToken } from '../api/client'

const SNAPSHOT_POLL_MS = 10_000

// ---------------------------------------------------------------------------
// Error banner
// ---------------------------------------------------------------------------

interface ErrorBannerProps {
  error: string
}

function ErrorBanner({ error }: ErrorBannerProps) {
  return (
    <div
      style={{
        background: '#fee2e2',
        border: '1px solid #fca5a5',
        borderRadius: 4,
        padding: '10px 14px',
        marginBottom: 16,
      }}
    >
      <span style={{ color: '#991b1b', fontSize: 13 }}>{error}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Loop start form
// ---------------------------------------------------------------------------

interface StartFormProps {
  onStart: (prompt: string, cadence: number | null) => Promise<void>
  loading: boolean
}

const CADENCE_OPTIONS: { label: string; value: number | null }[] = [
  { label: 'No cadence (one-shot)', value: null },
  { label: 'Every 5 minutes', value: 300 },
  { label: 'Every 10 minutes', value: 600 },
  { label: 'Every 30 minutes', value: 1800 },
]

function StartForm({ onStart, loading }: StartFormProps) {
  const [prompt, setPrompt] = useState('run /loop iteration')
  const [cadenceIdx, setCadenceIdx] = useState(0)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const cadence = CADENCE_OPTIONS[cadenceIdx].value
    await onStart(prompt, cadence)
  }

  return (
    <form onSubmit={handleSubmit} style={{ marginBottom: 24 }}>
      <h3 style={{ marginTop: 0 }}>Start a Loop</h3>
      <label style={{ display: 'block', marginBottom: 8 }}>
        Prompt
        <textarea
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          rows={3}
          style={{ display: 'block', width: '100%', padding: 6, marginTop: 4, fontFamily: 'monospace', boxSizing: 'border-box' }}
        />
      </label>
      <label style={{ display: 'block', marginBottom: 12 }}>
        Cadence
        <select
          value={cadenceIdx}
          onChange={e => setCadenceIdx(Number(e.target.value))}
          style={{ display: 'block', marginTop: 4, padding: 6 }}
        >
          {CADENCE_OPTIONS.map((opt, i) => (
            <option key={i} value={i}>{opt.label}</option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={loading || !prompt.trim()}>
        {loading ? 'Starting…' : 'Start Loop'}
      </button>
    </form>
  )
}

// ---------------------------------------------------------------------------
// Active loops panel
// ---------------------------------------------------------------------------

interface ActiveLoopsPanelProps {
  loops: LoopEntry[]
  onStop: (loopId: string) => Promise<void>
  stoppingIds: Set<string>
}

function ActiveLoopsPanel({ loops, onStop, stoppingIds }: ActiveLoopsPanelProps) {
  if (loops.length === 0) {
    return <p style={{ color: '#999', fontSize: 13 }}>No active loops.</p>
  }

  return (
    <div>
      {loops.map(loop => (
        <div
          key={loop.loop_id}
          style={{
            border: '1px solid #d1d5db',
            borderRadius: 4,
            padding: 12,
            marginBottom: 8,
            fontFamily: 'monospace',
            fontSize: 12,
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div>
              <strong>{loop.loop_id}</strong>
              <div style={{ color: '#555', marginTop: 4, maxWidth: 480, wordBreak: 'break-word' }}>
                {loop.prompt}
              </div>
              <div style={{ color: '#888', marginTop: 4 }}>
                Started: {loop.started_at} | Last event: {loop.last_event_at}
                {loop.cadence_seconds ? ` | Cadence: ${loop.cadence_seconds}s` : ''}
              </div>
            </div>
            <button
              onClick={() => onStop(loop.loop_id)}
              disabled={stoppingIds.has(loop.loop_id)}
              style={{ marginLeft: 12, padding: '4px 10px', fontSize: 12 }}
            >
              {stoppingIds.has(loop.loop_id) ? 'Stopping…' : 'Stop'}
            </button>
          </div>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Agent feed panel
// ---------------------------------------------------------------------------

interface AgentFeedPanelProps {
  events: AgentEvent[]
  autoScroll: boolean
  onToggleAutoScroll: () => void
}

function roleBadgeStyle(role?: string): React.CSSProperties {
  const colors: Record<string, string> = {
    executor: '#3b82f6',
    'code-reviewer': '#8b5cf6',
    'security-reviewer': '#ef4444',
    'project-manager': '#f59e0b',
    'team-lead': '#10b981',
  }
  return {
    background: colors[role ?? ''] ?? '#6b7280',
    color: '#fff',
    borderRadius: 3,
    padding: '1px 5px',
    fontSize: 10,
    marginRight: 4,
  }
}

function verdictBadge(verdict?: string): React.CSSProperties {
  const colors: Record<string, string> = {
    pass: '#10b981',
    done: '#10b981',
    'needs-fix': '#f59e0b',
    fail: '#ef4444',
    skip: '#6b7280',
  }
  return {
    background: colors[verdict ?? ''] ?? '#d1d5db',
    color: '#fff',
    borderRadius: 3,
    padding: '1px 5px',
    fontSize: 10,
    marginRight: 4,
  }
}

function AgentFeedPanel({ events, autoScroll, onToggleAutoScroll }: AgentFeedPanelProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (autoScroll) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [events, autoScroll])

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <strong>Live Agent Feed</strong>
        <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 4 }}>
          <input type="checkbox" checked={autoScroll} onChange={onToggleAutoScroll} />
          Auto-scroll
        </label>
      </div>
      <div
        style={{
          height: 280,
          overflowY: 'auto',
          border: '1px solid #e5e7eb',
          borderRadius: 4,
          background: '#f9fafb',
          padding: 8,
          fontFamily: 'monospace',
          fontSize: 11,
        }}
      >
        {events.length === 0 && (
          <span style={{ color: '#9ca3af' }}>Waiting for events…</span>
        )}
        {events.map((ev, i) => (
          <div key={i} style={{ marginBottom: 4 }}>
            <span style={{ color: '#9ca3af', marginRight: 6 }}>
              {ev.timestamp?.slice(11, 19) ?? ''}
            </span>
            {ev.role && <span style={roleBadgeStyle(ev.role)}>{ev.role}</span>}
            {ev.verdict && <span style={verdictBadge(ev.verdict)}>{ev.verdict}</span>}
            {ev.discussion != null && (
              <span style={{ color: '#6b7280', marginRight: 4 }}>d#{ev.discussion}</span>
            )}
            {ev.pr != null && (
              <span style={{ color: '#6b7280', marginRight: 4 }}>pr#{ev.pr}</span>
            )}
            <span>{ev.message ?? ev.event_type ?? JSON.stringify(ev).slice(0, 80)}</span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Team status panel
// ---------------------------------------------------------------------------

interface TeamStatusPanelProps {
  snapshot: TeamStatusSnapshot | null
  loading: boolean
}

function TeamStatusPanel({ snapshot, loading }: TeamStatusPanelProps) {
  if (loading && !snapshot) {
    return <p style={{ color: '#999', fontSize: 13 }}>Loading status…</p>
  }
  if (!snapshot) return null
  if (snapshot.error) {
    return <p style={{ color: '#ef4444', fontSize: 13 }}>Status error: {snapshot.error}</p>
  }

  const discussions = snapshot.discussions as Record<string, unknown>
  const prs = snapshot.prs as Record<string, unknown>

  return (
    <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
      <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
        <div>
          <strong>Discussions</strong>
          <pre style={{ margin: 0, background: '#f3f4f6', color: '#1f2937', padding: 8, borderRadius: 4, marginTop: 4 }}>
            {JSON.stringify(discussions, null, 2)}
          </pre>
        </div>
        <div>
          <strong>PRs</strong>
          <pre style={{ margin: 0, background: '#f3f4f6', color: '#1f2937', padding: 8, borderRadius: 4, marginTop: 4 }}>
            {JSON.stringify(prs, null, 2)}
          </pre>
        </div>
        <div>
          <strong>Budget</strong>
          {(snapshot.budget as Record<string, unknown>)?.no_agents_recorded === true && (
            <p style={{ margin: '4px 0 0', fontSize: 11, color: '#6b7280', fontStyle: 'italic' }}>
              No agents recorded yet — spent will appear once agents complete work.
            </p>
          )}
          <pre style={{ margin: 0, background: '#f3f4f6', color: '#1f2937', padding: 8, borderRadius: 4, marginTop: 4 }}>
            {JSON.stringify(snapshot.budget, null, 2)}
          </pre>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main LoopController page
// ---------------------------------------------------------------------------

export default function LoopController() {
  // Per-source error state: each RPC source owns its own error slot so that
  // a success on one source never wipes an unrelated error on another.
  const [listError, setListError] = useState('')
  const [snapshotError, setSnapshotError] = useState('')
  const [feedError, setFeedError] = useState('')
  const [actionError, setActionError] = useState('')

  // Derive the banner message: first non-empty wins.
  const error = listError || snapshotError || feedError || actionError

  const [starting, setStarting] = useState(false)
  const [loops, setLoops] = useState<LoopEntry[]>([])
  const [stoppingIds, setStoppingIds] = useState<Set<string>>(new Set())
  const [feedEvents, setFeedEvents] = useState<AgentEvent[]>([])
  const [autoScroll, setAutoScroll] = useState(true)
  const [snapshot, setSnapshot] = useState<TeamStatusSnapshot | null>(null)
  const [snapshotLoading, setSnapshotLoading] = useState(false)
  // gates.loop_start: false by default until the RPC response arrives
  const [loopStartEnabled, setLoopStartEnabled] = useState(false)

  // SSE tail uses JsonRpcClient directly (needs baseUrl for EventSource URL construction).
  // Credentials come from the shared auto-discovery layer in api/client.ts.
  const tailClientRef = useRef<JsonRpcClient | null>(null)
  const tailRef = useRef<TailHandle | null>(null)
  const snapshotTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  function getTailClient(): JsonRpcClient {
    const baseUrl = getRpcBaseUrl()
    const token = getRpcToken()
    if (
      !tailClientRef.current ||
      tailClientRef.current.baseUrl !== baseUrl ||
      tailClientRef.current.token !== token
    ) {
      tailClientRef.current = new JsonRpcClient(baseUrl, token)
    }
    return tailClientRef.current
  }

  function formatError(err: unknown): string {
    if (err instanceof JsonRpcError) return `RPC error ${err.code}: ${err.message}`
    if (err instanceof Error) return err.message
    return String(err)
  }

  // Fetch loop list
  const refreshLoops = useCallback(async () => {
    try {
      const result = await jsonRpc<{ loops: LoopEntry[] }>('loop.list', {})
      setLoops(result.loops)
      setListError('')
    } catch (err) {
      setListError(formatError(err))
    }
  }, [])

  // Fetch snapshot
  const refreshSnapshot = useCallback(async () => {
    setSnapshotLoading(true)
    try {
      const result = await jsonRpc<TeamStatusSnapshot>('team_status.snapshot', {})
      setSnapshot(result)
      setSnapshotError('')
    } catch (err) {
      setSnapshotError(formatError(err))
    } finally {
      setSnapshotLoading(false)
    }
  }, [])

  // Start SSE tail — uses JsonRpcClient for EventSource URL construction
  const startTail = useCallback(() => {
    tailRef.current?.close()
    tailRef.current = tailAgentFeed(getTailClient, {
      onEvent: ev => setFeedEvents(prev => [...prev.slice(-500), ev]),
      onError: err => setFeedError(`Feed error: ${err.message}`),
    })
  }, [])

  // Initialize on mount
  useEffect(() => {
    refreshLoops()
    refreshSnapshot()
    startTail()

    // Read gates snapshot to check whether loop.start is enabled
    jsonRpc<{ gates: Record<string, boolean | string> }>('dashboard.gates_snapshot', {})
      .then(res => setLoopStartEnabled(res.gates['loop_start'] === true))
      .catch(() => setLoopStartEnabled(false))

    snapshotTimerRef.current = setInterval(refreshSnapshot, SNAPSHOT_POLL_MS)

    return () => {
      tailRef.current?.close()
      if (snapshotTimerRef.current) clearInterval(snapshotTimerRef.current)
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleStart(prompt: string, cadence: number | null) {
    setStarting(true)
    setActionError('')
    try {
      await jsonRpc('loop.start', { prompt, cadence_seconds: cadence } as Record<string, unknown>)
      await refreshLoops()
    } catch (err) {
      setActionError(formatError(err))
    } finally {
      setStarting(false)
    }
  }

  async function handleStop(loopId: string) {
    setStoppingIds(prev => new Set([...prev, loopId]))
    setActionError('')
    try {
      await jsonRpc('loop.stop', { loop_id: loopId })
      await refreshLoops()
    } catch (err) {
      setActionError(formatError(err))
    } finally {
      setStoppingIds(prev => {
        const next = new Set(prev)
        next.delete(loopId)
        return next
      })
    }
  }

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: 24, fontFamily: 'system-ui, sans-serif' }}>
      <h2 style={{ marginTop: 0 }}>Loop Controller</h2>

      {error && <ErrorBanner error={error} />}

      <section style={{ marginBottom: 32 }}>
        {loopStartEnabled ? (
          <StartForm onStart={handleStart} loading={starting} />
        ) : (
          <div
            style={{
              background: '#f3f4f6',
              border: '1px solid #d1d5db',
              borderRadius: 4,
              padding: '12px 16px',
              color: '#6b7280',
              fontSize: 13,
            }}
          >
            <strong style={{ color: '#374151' }}>Dashboard loop spawning disabled</strong>
            <p style={{ margin: '4px 0 0' }}>
              Start loops from the CLI: <code>bash run-loop-iteration.sh</code> or via the cron
              trigger. The dashboard is observer-only by default to prevent competing writes.
              Enable with: <code>python3 backend/control_plane.py set gates.loop_start true</code>
            </p>
          </div>
        )}
      </section>

      <section style={{ marginBottom: 32 }}>
        <h3>Active Loops</h3>
        <ActiveLoopsPanel loops={loops} onStop={handleStop} stoppingIds={stoppingIds} />
      </section>

      <section style={{ marginBottom: 32 }}>
        <AgentFeedPanel
          events={feedEvents}
          autoScroll={autoScroll}
          onToggleAutoScroll={() => setAutoScroll(v => !v)}
        />
      </section>

      <section>
        <h3>Team Status {snapshotLoading && <span style={{ fontSize: 12, color: '#9ca3af' }}>refreshing…</span>}</h3>
        <TeamStatusPanel snapshot={snapshot} loading={snapshotLoading} />
      </section>
    </div>
  )
}
