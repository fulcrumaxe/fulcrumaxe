/**
 * TileFetchError — shared "the request never reached the backend" state for
 * stats tiles.
 *
 * D#2251: a CORS-rejected preflight (adopter's AUTONOMOUS_TEAM_STATE_DIR
 * outside $HOME, before the dashboard_origins fix — or any other transport
 * failure) makes fetch() reject with `TypeError: Failed to fetch`. Several
 * tiles swallowed that into their empty/zero state ("No data yet"), which
 * pointed operators at the wrong subsystem. isTransportError() tells a
 * transport failure apart from an ApiError (an HTTP/RPC-level failure the
 * backend actually answered), so a tile can render this instead of its
 * empty state.
 */

import type React from 'react'
import type { ApiError } from '../../api/client'

const TRANSPORT_ERROR_MESSAGE = /failed to fetch|networkerror|load failed/i

/**
 * True when *err* looks like a failed network request rather than an
 * HTTP/RPC-level error the backend actually responded to.
 *
 * `fetch()` rejects with a raw `TypeError` (message varies by browser —
 * "Failed to fetch", "NetworkError when attempting to fetch resource",
 * "Load failed") when the request never completes: DNS failure, connection
 * refused, or a CORS-blocked preflight. `ApiError` is explicitly excluded —
 * that's a real HTTP response the tile should keep treating as backend data
 * (e.g. a 500), not a transport failure.
 *
 * Checked by `.name` rather than `instanceof ApiError`: tile tests mock
 * `../../api/client` wholesale (`{ jsonRpc: vi.fn() }`), which drops the real
 * `ApiError` export, so an `instanceof` check against the imported value
 * would throw in every test that doesn't also stub `ApiError`.
 */
// eslint-disable-next-line react-refresh/only-export-components -- pure helper, unit tested directly
export function isTransportError(err: unknown): boolean {
  if (err instanceof Error && (err as ApiError).name === 'ApiError') return false
  if (err instanceof TypeError) return true
  const message = err instanceof Error ? err.message : String(err)
  return TRANSPORT_ERROR_MESSAGE.test(message)
}

const styles: Record<string, React.CSSProperties> = {
  state: {
    color: '#ef4444',
    fontSize: 13,
    padding: '40px 20px',
    textAlign: 'center',
    background: '#111827',
    border: '1px solid #7f1d1d',
    borderRadius: 8,
  },
  backendState: {
    color: '#fbbf24',
    fontSize: 13,
    padding: '40px 20px',
    textAlign: 'center',
    background: '#111827',
    border: '1px solid #92400e',
    borderRadius: 8,
  },
}

interface Props {
  error: unknown
}

export default function TileFetchError({ error }: Props) {
  void error // not rendered verbatim — a transport error's message isn't actionable to a viewer
  return (
    <div style={styles.state} role="alert" data-testid="tile-fetch-error">
      Could not reach the backend — request failed or was blocked (check the server log for a
      rejected-origin warning).
    </div>
  )
}

/**
 * TileBackendError — the request reached the backend and the backend
 * answered with a JSON-RPC error (an `ApiError`, the shape
 * `dashboard/src/api/client.ts:428` throws for an `{"error": {...}}` body).
 *
 * D#2315: every tile that imports `TileFetchError` used to do
 * `isTransportError(err) ? err : null` in its catch block, which discarded
 * a non-transport error entirely — a crashed handler's JSON-RPC -32000 was
 * indistinguishable from "no data yet". This renders instead: distinct
 * copy, distinct color, distinct data-testid from both `TileFetchError`
 * and any tile's own empty state.
 *
 * Unlike `TileFetchError`, the message IS shown here — a backend error's
 * message (e.g. the exception text that crashed the handler) is actionable
 * to a viewer, where a transport error's message is not.
 */
export function TileBackendError({ error }: Props) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div style={styles.backendState} role="alert" data-testid="tile-backend-error">
      Backend error — the server returned a failure instead of data: {message}
    </div>
  )
}
