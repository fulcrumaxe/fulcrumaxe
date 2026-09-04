/**
 * BackendTargetIndicator — toggle + active-backend badge + health check.
 *
 * Shows which backend the dashboard is currently talking to, lets the user
 * switch between Python and TypeScript backends, and shows whether the
 * selected backend's /health endpoint is reachable.
 *
 * Displayed in the nav bar (App.tsx) so it's visible on every page.
 */

import { useEffect, useRef, useState } from 'react'
import { useBackendTarget } from '../context/BackendTargetContext'
import { resolveRestBaseUrl, getTsBackendOrigin } from '../lib/backendTarget'

type HealthStatus = 'unknown' | 'up' | 'down'

const HEALTH_INTERVAL_MS = 15_000

/** Probe the selected backend's /health endpoint and return up/down. */
async function probeHealth(baseUrl: string): Promise<HealthStatus> {
  try {
    // Health probe uses raw fetch intentionally: cross-origin reachability check with
    // AbortSignal.timeout; auth-retry path doesn't apply to a simple /health ping.
    // eslint-disable-next-line no-restricted-syntax
    const res = await fetch(`${baseUrl}/health`, {
      signal: AbortSignal.timeout(4000),
    })
    return res.ok ? 'up' : 'down'
  } catch {
    return 'down'
  }
}

const healthDot: Record<HealthStatus, { color: string; label: string }> = {
  unknown: { color: '#6b7280', label: 'checking...' },
  up:      { color: '#22c55e', label: 'up' },
  down:    { color: '#ef4444', label: 'down' },
}

export function BackendTargetIndicator() {
  const { backend, setBackend } = useBackendTarget()
  const [health, setHealth] = useState<HealthStatus>('unknown')
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Re-probe whenever the selected backend changes, and on a periodic timer.
  useEffect(() => {
    let cancelled = false

    async function check() {
      const base = resolveRestBaseUrl()
      const status = await probeHealth(base)
      if (!cancelled) setHealth(status)
    }

    setHealth('unknown')
    check()

    timerRef.current = setInterval(check, HEALTH_INTERVAL_MS)
    return () => {
      cancelled = true
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [backend])

  const dot = healthDot[health]
  const isPython = backend === 'python'

  // Build a human-readable label for the health section.
  const backendLabel = isPython ? 'Python' : 'TypeScript'
  const tsOrigin = getTsBackendOrigin()

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        fontSize: 12,
        color: '#d1d5db',
        padding: '0 4px',
        whiteSpace: 'nowrap',
      }}
      title={`Active backend: ${backendLabel}${backend === 'typescript' ? ` (${tsOrigin})` : ''} — health: ${dot.label}`}
    >
      {/* Active-backend badge */}
      <span
        style={{
          background: isPython ? '#1d4ed8' : '#7c3aed',
          color: '#fff',
          borderRadius: 4,
          padding: '2px 7px',
          fontWeight: 600,
          letterSpacing: 0.3,
        }}
        data-testid="backend-badge"
      >
        {isPython ? 'Backend: Python' : 'Backend: TypeScript'}
      </span>

      {/* Health dot */}
      <span
        style={{
          display: 'inline-block',
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: dot.color,
          flexShrink: 0,
        }}
        aria-label={`health: ${dot.label}`}
        data-testid="health-dot"
        title={`/health: ${dot.label}`}
      />

      {/* Toggle */}
      <button
        type="button"
        onClick={() => setBackend(isPython ? 'typescript' : 'python')}
        style={{
          background: 'transparent',
          border: '1px solid #374151',
          borderRadius: 4,
          color: '#9ca3af',
          cursor: 'pointer',
          fontSize: 11,
          padding: '2px 8px',
          lineHeight: 1.4,
        }}
        data-testid="backend-toggle"
        title={`Switch to ${isPython ? 'TypeScript' : 'Python'} backend`}
      >
        {isPython ? 'Switch to TS' : 'Switch to Python'}
      </button>
    </div>
  )
}
