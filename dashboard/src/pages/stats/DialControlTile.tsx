/**
 * DialControlTile — Autonomy Dial Controls.
 *
 * Renders each dial class as a row with a level stepper (1 to ceiling),
 * a TTL selector, and a Set button that POSTs dial.set. Shows success/error
 * feedback inline and reflects the new level immediately after a successful set.
 *
 * Auth: dial.set is a mutating RPC gated by Bearer token (all POST /rpc calls
 * require auth). The backend additionally checks the dashboard-rpc allowlist
 * entry — ceiling violations are caught server-side and shown as an error.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { jsonRpc } from '../../api/client'
import { sharedStyles } from './styles'

// -------------------------------------------------------------------
// Types
// -------------------------------------------------------------------

export interface DialEntry {
  name: string
  level: number
  ceiling: number
  active_directives: number
  ttl_revert_at: string | null
}

export interface DialListResponse {
  dials: DialEntry[]
}

interface DialSetResponse {
  name: string
  level: number
  ceiling: number
}

interface Props {
  refreshSignal?: number
}

// -------------------------------------------------------------------
// TTL options
// -------------------------------------------------------------------

const TTL_OPTIONS: { label: string; value: string | null }[] = [
  { label: 'Permanent', value: null },
  { label: 'Until end of today', value: 'for-today' },
]

// -------------------------------------------------------------------
// Sub-components
// -------------------------------------------------------------------

function StatusBadge({ message, isError }: { message: string; isError: boolean }) {
  return (
    <span
      style={{
        fontSize: 11,
        color: isError ? '#ef4444' : '#22c55e',
        marginLeft: 8,
        fontStyle: 'italic',
      }}
    >
      {message}
    </span>
  )
}

interface DialRowProps {
  dial: DialEntry
  onSet: (name: string, level: number, ttl: string | null) => Promise<{ ok: boolean; message: string }>
}

function DialRow({ dial, onSet }: DialRowProps) {
  const [selectedLevel, setSelectedLevel] = useState(dial.level)
  const [selectedTtl, setSelectedTtl] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [status, setStatus] = useState<{ message: string; isError: boolean } | null>(null)

  // Keep selectedLevel in sync when tile refreshes from parent
  useEffect(() => {
    setSelectedLevel(dial.level)
  }, [dial.level])

  async function handleSet() {
    setPending(true)
    setStatus(null)
    const result = await onSet(dial.name, selectedLevel, selectedTtl)
    setPending(false)
    setStatus({ message: result.message, isError: !result.ok })
    // Clear status after 4 seconds
    setTimeout(() => setStatus(null), 4000)
  }

  const levelOptions = Array.from({ length: dial.ceiling }, (_, i) => i + 1)

  return (
    <tr style={sharedStyles.tr} data-testid={`dial-row-${dial.name}`}>
      <td style={{ ...sharedStyles.td, fontFamily: 'monospace', fontSize: 12 }}>
        {dial.name}
      </td>
      <td style={{ ...sharedStyles.td, textAlign: 'center' }}>
        <span style={{ color: '#9ca3af', fontSize: 12 }}>
          {dial.level}/{dial.ceiling}
        </span>
      </td>
      <td style={{ ...sharedStyles.td }}>
        <select
          value={selectedLevel}
          onChange={e => setSelectedLevel(Number(e.target.value))}
          disabled={pending}
          data-testid={`dial-level-select-${dial.name}`}
          style={{
            background: '#1f2937',
            color: '#f9fafb',
            border: '1px solid #374151',
            borderRadius: 4,
            padding: '2px 6px',
            fontSize: 12,
          }}
        >
          {levelOptions.map(l => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      </td>
      <td style={{ ...sharedStyles.td }}>
        <select
          value={selectedTtl ?? ''}
          onChange={e => setSelectedTtl(e.target.value === '' ? null : e.target.value)}
          disabled={pending}
          data-testid={`dial-ttl-select-${dial.name}`}
          style={{
            background: '#1f2937',
            color: '#f9fafb',
            border: '1px solid #374151',
            borderRadius: 4,
            padding: '2px 6px',
            fontSize: 12,
          }}
        >
          {TTL_OPTIONS.map(opt => (
            <option key={opt.value ?? 'permanent'} value={opt.value ?? ''}>
              {opt.label}
            </option>
          ))}
        </select>
      </td>
      <td style={{ ...sharedStyles.td }}>
        <button
          onClick={handleSet}
          disabled={pending}
          data-testid={`dial-set-btn-${dial.name}`}
          style={{
            background: pending ? '#374151' : '#2563eb',
            color: '#f9fafb',
            border: 'none',
            borderRadius: 4,
            padding: '3px 10px',
            fontSize: 12,
            cursor: pending ? 'not-allowed' : 'pointer',
          }}
        >
          {pending ? 'Setting…' : 'Set'}
        </button>
        {status && <StatusBadge message={status.message} isError={status.isError} />}
      </td>
    </tr>
  )
}

// -------------------------------------------------------------------
// Main component
// -------------------------------------------------------------------

export default function DialControlTile({ refreshSignal }: Props) {
  const [dials, setDials] = useState<DialEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchDials = useCallback(async () => {
    try {
      const resp = await jsonRpc<DialListResponse>('dial.list', {})
      setDials(resp.dials)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchDials()
    intervalRef.current = setInterval(fetchDials, 60_000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [fetchDials, refreshSignal])

  const handleSet = useCallback(
    async (
      name: string,
      level: number,
      ttl: string | null,
    ): Promise<{ ok: boolean; message: string }> => {
      try {
        const result = await jsonRpc<DialSetResponse>('dial.set', { name, level, ttl })
        // Update the local dial list to reflect the new level immediately
        setDials(prev =>
          prev.map(d =>
            d.name === name ? { ...d, level: result.level } : d,
          ),
        )
        return { ok: true, message: `Set to ${result.level}` }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        return { ok: false, message: msg }
      }
    },
    [],
  )

  return (
    <section style={sharedStyles.section} aria-label="Dial Controls (L6 Autonomy)">
      <h2 style={sharedStyles.sectionHeading}>Dial Controls (L6 Autonomy)</h2>
      <p style={{ margin: '0 0 12px', fontSize: 12, color: '#6b7280' }}>
        Set autonomy levels per operation class. Changes are auth-gated and audit-logged.
        Ceiling limits are enforced server-side.
      </p>

      {loading && !dials.length && (
        <div style={sharedStyles.state} role="status">Loading dials…</div>
      )}

      {error && (
        <div style={{ ...sharedStyles.state, color: '#ef4444' }} role="alert" data-testid="dial-control-error">
          {error}
        </div>
      )}

      {dials.length > 0 && (
        <div
          data-testid="dial-control-tile"
          style={{
            background: '#111827',
            border: '1px solid #1f2937',
            borderRadius: 8,
            padding: '8px 0',
          }}
        >
          <table style={{ ...sharedStyles.table, border: 'none' }}>
            <thead>
              <tr>
                <th style={sharedStyles.th} scope="col">Class</th>
                <th style={{ ...sharedStyles.th, textAlign: 'center' }} scope="col">Current</th>
                <th style={sharedStyles.th} scope="col">New Level</th>
                <th style={sharedStyles.th} scope="col">TTL</th>
                <th style={sharedStyles.th} scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {dials.map(dial => (
                <DialRow key={dial.name} dial={dial} onSet={handleSet} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
