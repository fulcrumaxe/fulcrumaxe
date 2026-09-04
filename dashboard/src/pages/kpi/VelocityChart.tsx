/**
 * VelocityChart — merged-PRs-per-day line chart with a 7/30/90-day window selector.
 *
 * Renders one of four mutually exclusive states:
 *   Loading…  |  chart SVG  |  empty-state copy  |  error copy
 */

import { useEffect, useState } from 'react'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { getVelocity, VelocityPoint } from '../../api/kpi'

const WINDOWS = [7, 30, 90] as const
type Window = (typeof WINDOWS)[number]

interface Props {
  defaultDays?: Window
  /** When provided by parent (page-level toggle), overrides internal state. */
  days?: Window
}

export default function VelocityChart({ defaultDays = 30, days: externalDays }: Props) {
  const [internalDays, setInternalDays] = useState<Window>(defaultDays)
  // If parent supplies days, use it; otherwise manage state internally.
  const days = externalDays ?? internalDays
  const setDays = (w: Window) => { if (!externalDays) setInternalDays(w) }
  const [data, setData] = useState<VelocityPoint[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setData(null)
    getVelocity(days)
      .then(d => { if (!cancelled) { setData(d); setLoading(false) } })
      .catch(err => { if (!cancelled) { setError(String(err?.message ?? err)); setLoading(false) } })
    return () => { cancelled = true }
  }, [days])

  return (
    <section data-testid="velocity-chart" style={styles.section}>
      <div style={styles.header}>
        <h3 style={styles.title}>Velocity — merged PRs/day</h3>
        {/* Hide per-chart toggle when the parent page provides a shared toggle */}
        {!externalDays && (
          <div style={styles.windowButtons}>
            {WINDOWS.map(w => (
              <button
                key={w}
                data-window={`${w}d`}
                onClick={() => setDays(w)}
                style={days === w ? styles.activeBtn : styles.btn}
              >
                {w}d
              </button>
            ))}
          </div>
        )}
      </div>

      {loading && <p style={styles.state}>Loading...</p>}

      {!loading && error && (
        <p style={styles.state}>Failed to load — see console</p>
      )}

      {!loading && !error && data !== null && data.length === 0 && (
        <p data-testid="kpi-empty" style={styles.state}>No data yet — first merged PR will appear here.</p>
      )}

      {!loading && !error && data !== null && data.length > 0 && (
        <ResponsiveContainer width="100%" height={240}>
          <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="date" tick={{ fill: '#9ca3af', fontSize: 11 }} />
            <YAxis allowDecimals={false} tick={{ fill: '#9ca3af', fontSize: 11 }} />
            <Tooltip
              contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f9fafb' }}
            />
            <Line type="monotone" dataKey="count" stroke="#60a5fa" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      )}
    </section>
  )
}

const styles: Record<string, React.CSSProperties> = {
  section: { background: '#111827', borderRadius: 8, padding: '16px 20px', marginBottom: 20 },
  header: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 },
  title: { margin: 0, fontSize: 15, color: '#f9fafb', fontWeight: 600 },
  windowButtons: { display: 'flex', gap: 6 },
  btn: {
    background: '#1f2937', color: '#9ca3af', border: '1px solid #374151',
    borderRadius: 4, padding: '3px 10px', cursor: 'pointer', fontSize: 12,
  },
  activeBtn: {
    background: '#3b82f6', color: '#fff', border: '1px solid #3b82f6',
    borderRadius: 4, padding: '3px 10px', cursor: 'pointer', fontSize: 12, fontWeight: 600,
  },
  state: { color: '#6b7280', fontSize: 13, textAlign: 'center', padding: '24px 0' },
}
