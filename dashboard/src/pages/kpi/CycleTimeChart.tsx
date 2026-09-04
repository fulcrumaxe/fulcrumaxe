/**
 * CycleTimeChart — histogram of PR cycle times bucketed into 0-2h, 2-6h, 6-24h, 24h+.
 *
 * Renders one of four mutually exclusive states:
 *   Loading…  |  chart SVG  |  empty-state copy  |  error copy
 */

import { useEffect, useState } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { getCycleTime, CycleTimeBucket } from '../../api/kpi'

interface Props {
  /** Range window in days — re-fetches when changed. */
  days?: number
}

export default function CycleTimeChart({ days = 90 }: Props) {
  const [data, setData] = useState<CycleTimeBucket[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getCycleTime(days)
      .then(d => { if (!cancelled) { setData(d); setLoading(false) } })
      .catch(err => { if (!cancelled) { setError(String(err?.message ?? err)); setLoading(false) } })
    return () => { cancelled = true }
  }, [days])

  const hasData = data !== null && data.some(b => b.count > 0)

  return (
    <section data-testid="cycle-time-chart" style={styles.section}>
      <h3 style={styles.title}>Cycle Time Distribution</h3>

      {loading && <p style={styles.state}>Loading...</p>}

      {!loading && error && (
        <p style={styles.state}>Failed to load — see console</p>
      )}

      {!loading && !error && data !== null && !hasData && (
        <p data-testid="kpi-empty" style={styles.state}>No data yet — first completed PR will appear here.</p>
      )}

      {!loading && !error && data !== null && hasData && (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="bucket" tick={{ fill: '#9ca3af', fontSize: 12 }} />
            <YAxis allowDecimals={false} tick={{ fill: '#9ca3af', fontSize: 11 }} />
            <Tooltip
              contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f9fafb' }}
            />
            <Bar dataKey="count" fill="#34d399" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </section>
  )
}

const styles: Record<string, React.CSSProperties> = {
  section: { background: '#111827', borderRadius: 8, padding: '16px 20px', marginBottom: 20 },
  title: { margin: '0 0 12px', fontSize: 15, color: '#f9fafb', fontWeight: 600 },
  state: { color: '#6b7280', fontSize: 13, textAlign: 'center', padding: '24px 0' },
}
