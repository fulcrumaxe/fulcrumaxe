import { useEffect, useState } from 'react'
import { healthApi } from '../api/client'

const bannerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '10px 20px',
  background: '#7f1d1d',
  color: '#fecaca',
  fontFamily: 'system-ui, sans-serif',
  fontSize: 13,
  borderBottom: '1px solid #991b1b',
}

const codeStyle: React.CSSProperties = {
  background: '#450a0a',
  color: '#fca5a5',
  padding: '2px 6px',
  borderRadius: 3,
  fontFamily: 'monospace',
  fontSize: 12,
}

/**
 * BackendUnavailableBanner — shown when /health is unreachable.
 *
 * Checks backend health on mount and every 10 seconds. Hides itself
 * automatically once the backend comes back online.
 */
export default function BackendUnavailableBanner() {
  const [backendDown, setBackendDown] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function check() {
      try {
        await healthApi.status()
        if (!cancelled) setBackendDown(false)
      } catch {
        if (!cancelled) setBackendDown(true)
      }
    }

    check()
    const id = setInterval(check, 10_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (!backendDown) return null

  return (
    <div style={bannerStyle} role="alert">
      <span>⚠</span>
      <span>
        Backend not running. Start with:{' '}
        <code style={codeStyle}>bash scripts/start-dashboard.sh</code>
      </span>
    </div>
  )
}
