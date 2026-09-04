/**
 * CostChart — top-10 discussions by token spend (bar chart).
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
import { getCostByDiscussion, CostEntry } from '../../api/kpi'

interface Props {
  /** Range window in days — re-fetches when changed. */
  days?: number
}

export default function CostChart({ days = 90 }: Props) {
  const [data, setData] = useState<CostEntry[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getCostByDiscussion(10, days)
      .then(d => { if (!cancelled) { setData(d); setLoading(false) } })
      .catch(err => { if (!cancelled) { setError(String(err?.message ?? err)); setLoading(false) } })
    return () => { cancelled = true }
  }, [days])

  return (
    <section data-testid="cost-chart" style={styles.section}>
      <h3 style={styles.title}>Cost per Discussion (top 10, USD)</h3>

      {loading && <p style={styles.state}>Loading...</p>}

      {!loading && error && (
        <p style={styles.state}>Failed to load — see console</p>
      )}

      {!loading && !error && data !== null && data.length === 0 && (
        <p data-testid="kpi-empty" style={styles.state}>No cost data yet — first agent run will appear here.</p>
      )}

      {!loading && !error && data !== null && data.length > 0 && (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart
            data={data.map(e => ({ ...e, label: `#${e.discussion}` }))}
            margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="label" tick={{ fill: '#9ca3af', fontSize: 11 }} />
            <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} />
            <Tooltip
              contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f9fafb' }}
              formatter={(val: number) => [`$${val.toFixed(4)}`, 'USD']}
            />
            <Bar dataKey="usd" fill="#f59e0b" radius={[3, 3, 0, 0]} />
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
