import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Sidebar } from '../components/Sidebar'
import { Header } from '../components/Header'
import { Card } from '../components/Card'
import { ProgressBar } from '../components/ProgressBar'
import { StatusBadge } from '../components/StatusBadge'
import { AgentCard } from '../components/AgentCard'
import { LoadingSpinner } from '../components/LoadingSpinner'
import { useApi } from '../hooks/useApi'
import { budgetApi, kpiApi, spawnQueueApi, healthApi, agentsApi, apiSpawnBlocks } from '../api/client'
import { useWebSocket } from '../hooks/useWebSocket'
import type { SpawnBlockEvent, SpawnBlockReason } from '../api/types'
import { formatLocaleString, formatRelative } from '../lib/safeDate'

// ── Helpers ──────────────────────────────────────────────────────────────────

function relativeTime(ts: string): string {
  const result = formatRelative(ts)
  return result === '—' ? ts : result
}

const REASON_COLORS: Record<SpawnBlockReason, string> = {
  budget_exceeded: '#dc2626',
  circuit_breaker_open: '#dc2626',
  subscription_throttled: '#d97706',
  worktree_cap_reached: '#d97706',
  concurrency_cap_reached: '#d97706',
}

function RecentSpawnBlocksTile() {
  const [blocks, setBlocks] = useState<SpawnBlockEvent[]>([])
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false

    const fetch_ = () => {
      apiSpawnBlocks(10)
        .then(data => {
          if (!cancelled) {
            setBlocks(data)
            setError(false)
          }
        })
        .catch(() => {
          if (!cancelled) setError(true)
        })
    }

    fetch_()
    const id = window.setInterval(fetch_, 30_000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [])

  return (
    <Card title="Recent Spawn Blocks" subtitle="Last 10 blocked spawn attempts">
      {error && <p className="spawn-blocks-error">Could not load spawn blocks</p>}
      {!error && blocks.length === 0 && (
        <p className="spawn-blocks-empty">No recent blocks</p>
      )}
      {!error && blocks.length > 0 && (
        <ul className="spawn-blocks-list">
          {blocks.map((b, i) => (
            <li key={i} className="spawn-blocks-row">
              <span className="spawn-blocks-role">{b.role}</span>
              <span
                className="spawn-blocks-reason"
                style={{ color: REASON_COLORS[b.reason] ?? '#6b7280' }}
              >
                {b.reason}
              </span>
              <span className="spawn-blocks-time">{relativeTime(b.ts)}</span>
              {b.discussion != null && (
                <Link
                  to={`/discussions/${b.discussion}`}
                  className="spawn-blocks-disc"
                >
                  D#{b.discussion}
                </Link>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

export default function ProjectDashboardPage() {
  const { id = '' } = useParams<{ id: string }>()
  const { connected } = useWebSocket()

  const { data: budget, loading: budgetLoading } = useApi(() => budgetApi.status(id), [id])
  const { data: kpi, loading: kpiLoading } = useApi(() => kpiApi.summary(id), [id])
  const { data: queue, loading: queueLoading } = useApi(() => spawnQueueApi.status(id), [id])
  const { data: loopHealth, loading: loopLoading } = useApi(() => healthApi.loop(), [])
  const { data: agents, loading: agentsLoading } = useApi(() => agentsApi.list(id), [id])

  const loading = budgetLoading || kpiLoading || queueLoading || loopLoading || agentsLoading

  return (
    <div className="layout">
      <Sidebar />
      <div className="layout-main">
        <Header projectName={id} connected={connected} />
        <main className="main-content">
          {loading && <LoadingSpinner />}
          <div className="dashboard-grid">
            {budget && (
              <Card title="Budget" subtitle="Daily / Monthly spend">
                <ProgressBar
                  value={budget.dailySpend}
                  max={budget.dailyLimit}
                  label={`Daily: $${budget.dailySpend.toFixed(2)} / $${budget.dailyLimit.toFixed(2)}`}
                />
                <ProgressBar
                  value={budget.monthlySpend}
                  max={budget.monthlyLimit}
                  label={`Monthly: $${budget.monthlySpend.toFixed(2)} / $${budget.monthlyLimit.toFixed(2)}`}
                />
              </Card>
            )}

            {kpi && (
              <Card title="KPI" subtitle="Current period">
                <div className="kpi-stats">
                  <div className="kpi-stat">
                    <span className="kpi-stat-value">{kpi.velocity}</span>
                    <span className="kpi-stat-label">Velocity</span>
                  </div>
                  <div className="kpi-stat">
                    <span className="kpi-stat-value">{kpi.cycleTimeMean}h</span>
                    <span className="kpi-stat-label">Cycle Time</span>
                  </div>
                  <div
                    className="kpi-stat"
                    title={
                      kpi.estimationAccuracy === null
                        ? `Not enough data — need ${kpi.estimationAccuracyMinSamples ?? 5}+ measured Discussions`
                        : 'Estimation accuracy: mean score across Discussions with both estimated and actual hours'
                    }
                  >
                    {kpi.estimationAccuracy === null ? (
                      <>
                        <span className="kpi-stat-value">N/A</span>
                        <span className="kpi-stat-label">Accuracy</span>
                        <span className="kpi-stat-subtext">
                          Need {kpi.estimationAccuracyMinSamples ?? 5}+ measured Discussions (have {kpi.estimationAccuracySampleCount ?? 0})
                        </span>
                      </>
                    ) : (
                      <>
                        <span className="kpi-stat-value">{Math.round(kpi.estimationAccuracy * 100)}%</span>
                        <span className="kpi-stat-label">Accuracy</span>
                      </>
                    )}
                  </div>
                </div>
              </Card>
            )}

            {queue && (
              <Card title="Spawn Queue">
                <div className="queue-stats">
                  <div className="queue-stat">
                    <span className="queue-stat-value">{queue.pending.length}</span>
                    <span className="queue-stat-label">Pending</span>
                  </div>
                  <div className="queue-stat">
                    <span className="queue-stat-value">{queue.active.length}</span>
                    <span className="queue-stat-label">Active</span>
                  </div>
                  <div className="queue-stat">
                    <span className="queue-stat-value">{queue.totalToday}</span>
                    <span className="queue-stat-label">Today</span>
                  </div>
                </div>
              </Card>
            )}

            {loopHealth !== undefined && loopHealth && (
              <Card title="Loop Health">
                <StatusBadge
                  status={loopHealth.status === 'ok' ? 'success' : 'error'}
                  label={loopHealth.status}
                />
                <p className="loop-last-run">
                  Last run: {formatLocaleString(loopHealth.lastRun ?? null)}
                </p>
                <p className="loop-duration">Duration: {loopHealth.duration}s</p>
              </Card>
            )}
          </div>

          {agents && agents.length > 0 && (
            <Card title="Recent Agent Activity" className="agents-card">
              <div className="agents-list">
                {agents.slice(0, 10).map(a => (
                  <AgentCard key={a.id} agent={a} />
                ))}
              </div>
            </Card>
          )}

          <RecentSpawnBlocksTile />
        </main>
      </div>
    </div>
  )
}
