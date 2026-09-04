/**
 * backendTarget.ts — persisted backend-target setting.
 *
 * Holds which backend the dashboard points at: "python" (the current default,
 * FastAPI on 18099/8765) or "typescript" (the TS-backend on 19099).
 *
 * This is a plain module (no React) so it can be read from client.ts which
 * runs outside the component tree.  The React context in BackendTargetContext.tsx
 * wraps this to drive re-renders.
 */

export type BackendType = 'python' | 'typescript'

const STORAGE_KEY = 'af.backendTarget'
const DEFAULT_BACKEND: BackendType = 'python'

// The TS backend base URL — configurable at build time via VITE_TS_API_URL,
// defaulting to the loopback port declared in ts-backend/src/index.ts.
const TS_API_URL: string =
  ((import.meta as unknown as { env: { VITE_TS_API_URL?: string } }).env.VITE_TS_API_URL) ??
  'http://127.0.0.1:19099'

// Module-level state — kept in sync with localStorage via setBackendTarget().
let _current: BackendType = DEFAULT_BACKEND

/** Listeners notified on every target change (used by BackendTargetContext). */
const _listeners: Array<(t: BackendType) => void> = []

function _readFromStorage(): BackendType {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === 'typescript' ? 'typescript' : DEFAULT_BACKEND
  } catch {
    return DEFAULT_BACKEND
  }
}

// Initialise from storage on module load.
_current = _readFromStorage()

export function getBackendTarget(): BackendType {
  return _current
}

export function setBackendTarget(t: BackendType): void {
  _current = t
  try {
    localStorage.setItem(STORAGE_KEY, t)
  } catch {
    // ignore storage failures
  }
  for (const l of _listeners) l(t)
}

/**
 * Subscribe to backend-target changes.
 * Returns an unsubscribe function.
 */
export function subscribeBackendTarget(listener: (t: BackendType) => void): () => void {
  _listeners.push(listener)
  return () => {
    const idx = _listeners.indexOf(listener)
    if (idx !== -1) _listeners.splice(idx, 1)
  }
}

/**
 * Resolve the REST + SSE base URL for the currently selected backend.
 *
 * "python"     → window.location.origin (Vite proxy / same-origin production)
 *                or VITE_API_URL when set at build time.
 * "typescript" → the TS backend origin (VITE_TS_API_URL or 127.0.0.1:19099)
 */
export function resolveRestBaseUrl(): string {
  if (_current === 'typescript') {
    return TS_API_URL.replace(/\/$/, '')
  }
  // Python: mirror the logic in client.ts (VITE_API_URL → origin)
  const viteUrl = ((import.meta as unknown as { env: { VITE_API_URL?: string } }).env.VITE_API_URL)
  if (viteUrl) return viteUrl.replace(/\/$/, '')
  return typeof window !== 'undefined' ? window.location.origin.replace(/\/$/, '') : ''
}

/**
 * Resolve the JSON-RPC base URL for the selected backend.
 *
 * The TS backend handles /rpc on the same origin as its REST routes.
 * Python delegates /rpc to server.py (port 8765, proxied via Vite or from
 * af_dashboard_base_url / VITE_API_URL / _configCache in client.ts).
 *
 * NOTE: for python we return an empty string to signal "use client.ts's
 * existing getRpcBaseUrl() logic" — the context reads this value.
 */
export function resolveRpcBaseUrl(): string {
  if (_current === 'typescript') {
    return TS_API_URL.replace(/\/$/, '')
  }
  // Signal to client.ts to use its own config-cache / localStorage logic.
  return ''
}

/** The TS backend origin (always, regardless of selection) for health checks. */
export function getTsBackendOrigin(): string {
  return TS_API_URL.replace(/\/$/, '')
}
