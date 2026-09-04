import { useEffect, useState } from 'react'
import { circuitBreakerApi, claudeSpawnTrackerApi } from '../api/client'
import { CircuitBreakerHistoryDrawer } from './CircuitBreakerHistoryDrawer'
import type { CircuitBreakerEntry, ClaudeSpawnSummary } from '../api/types'
import { StatusBadge } from './StatusBadge'

interface Props {
  projectName?: string
  connected?: boolean
}

function _buildTooltip(tripped: CircuitBreakerEntry[]): string {
  return tripped
    .map(e => `#${e.discussion} ${e.agent ?? 'unknown'}: ${e.reason ?? 'unknown'}`)
    .join('\n')
}

function _buildSpawnTooltip(summary: ClaudeSpawnSummary): string {
  const meta = summary.tripped_meta
  const lines: string[] = [
    `Spawn breaker: ${summary.tripped ? 'TRIPPED' : 'closed'}`,
    `Spawns 1h: ${summary.spawns_1h} / ${summary.thresholds.spawns_per_hour_max ?? '?'}`,
    `Spawns 24h: ${summary.spawns_24h} / ${summary.thresholds.spawns_24h_max ?? '?'}`,
    `Spend 24h: $${summary.spend_24h_usd.toFixed(4)}`,
  ]
  if (meta) {
    lines.push(`Reason: ${meta.reason}`)
    lines.push(`Tripped at: ${meta.tripped_at}`)
  }
  return lines.join('\n')
}

export function Header({ projectName, connected = false }: Props) {
  const [trippedCount, setTrippedCount] = useState(0)
  const [tooltip, setTooltip] = useState('')
  const [trippedEntries, setTrippedEntries] = useState<CircuitBreakerEntry[]>([])
  const [drawerRole, setDrawerRole] = useState<string | null>(null)
  const [spawnSummary, setSpawnSummary] = useState<ClaudeSpawnSummary | null>(null)

  useEffect(() => {
    let cancelled = false

    async function fetchSummary() {
      try {
        const data = await circuitBreakerApi.summary()
        if (!cancelled) {
          const tripped = data.tripped ?? []
          setTrippedCount(tripped.length)
          setTooltip(_buildTooltip(tripped))
          setTrippedEntries(tripped)
        }
      } catch {
        // Non-critical — silently ignore fetch errors
      }
    }

    fetchSummary()
    const interval = setInterval(fetchSummary, 30_000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    async function fetchSpawnSummary() {
      try {
        const data = await claudeSpawnTrackerApi.summary()
        if (!cancelled) {
          setSpawnSummary(data)
        }
      } catch {
        // Non-critical — silently ignore fetch errors
      }
    }

    fetchSpawnSummary()
    const interval = setInterval(fetchSpawnSummary, 30_000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  function handleBadgeClick() {
    // Open drawer for the first tripped role, or fall back to 'executor'
    const role = trippedEntries[0]?.agent ?? 'executor'
    setDrawerRole(role)
  }

  return (
    <>
      <header className="header">
        <div className="header-project">
          {projectName && <span className="header-project-name">{projectName}</span>}
        </div>
        <div className="header-right">
          {spawnSummary !== null && (
            <span
              title={_buildSpawnTooltip(spawnSummary)}
              style={{ marginRight: 8 }}
              aria-label={spawnSummary.tripped ? 'Spawn breaker tripped' : 'Spawn breaker closed'}
            >
              <StatusBadge
                status={spawnSummary.tripped ? 'error' : 'success'}
                label={spawnSummary.tripped ? 'spawn-breaker tripped' : 'spawns ok'}
              />
            </span>
          )}
          {trippedCount > 0 && (
            <button
              className="cb-badge-btn"
              data-testid="cb-badge"
              title={tooltip}
              onClick={handleBadgeClick}
              aria-label={`${trippedCount} circuit breaker trip${trippedCount !== 1 ? 's' : ''} — click to view history`}
              style={{ marginRight: 8, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
            >
              <StatusBadge status="warning" label={`${trippedCount} tripped`} />
            </button>
          )}
          <span
            className={`header-conn-dot ${connected ? 'header-conn-dot--connected' : 'header-conn-dot--disconnected'}`}
            aria-label={connected ? 'Connected' : 'Disconnected'}
            title={connected ? 'WebSocket connected' : 'WebSocket disconnected'}
          />
        </div>
      </header>

      {drawerRole !== null && (
        <CircuitBreakerHistoryDrawer
          role={drawerRole}
          onClose={() => setDrawerRole(null)}
        />
      )}
    </>
  )
}
