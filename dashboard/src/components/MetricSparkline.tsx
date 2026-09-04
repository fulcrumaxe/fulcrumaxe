/**
 * MetricSparkline — a compact sparkline card for a single metric.
 *
 * Renders: metric label, current value + unit, a small 7-day line chart,
 * and a "last updated" timestamp.
 *
 * Shared between StatsPage and any future page that needs per-metric charts.
 */

import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip,
} from 'recharts'
import { formatDate } from '../lib/safeDate'
import { formatUnit } from '../lib/formatMetric'

interface SeriesPoint {
  ts_iso: string
  value: number
}

interface Props {
  label: string
  value: number | null
  unit: string
  series: SeriesPoint[]
  updatedAt: string | null
}

function formatValue(value: number | null, unit: string): string {
  if (value === null) return '—'
  if (value < 0) return 'n/a'
  if (unit === 'seconds') {
    if (value >= 3600) return `${(value / 3600).toFixed(1)}h`
    if (value >= 60) return `${(value / 60).toFixed(1)}m`
    return `${value.toFixed(0)}s`
  }
  if (unit === 'usd') return `$${value.toFixed(3)}`
  if (unit === 'ratio') {
    // Guard: a ratio value above this threshold is implausibly large (> 999.9%) —
    // treat it as a mis-tagged count and render as a plain integer instead.
    // Legitimate ratios on this dashboard are 0–1 (rates) or small multiples;
    // nothing legitimately exceeds ~10x, so value > 9.999 is an unambiguous mis-tag.
    if (value > 9.999) return String(Math.round(value))
    return `${(value * 100).toFixed(1)}%`
  }
  if (unit === 'count') return String(Math.round(value))
  return value.toFixed(2)
}

function formatAge(isoTimestamp: string | null): string {
  if (!isoTimestamp) return ''
  const diff = Math.floor((Date.now() - new Date(isoTimestamp).getTime()) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

// Map recharts data to {t, v} for simple display.
// Filters out negative values — backend uses -1 as a "missing data" sentinel.
function toChartData(series: SeriesPoint[]): { t: string; v: number }[] {
  return series
    .filter(pt => pt.value >= 0)
    .map(pt => ({
      t: formatDate(pt.ts_iso),
      v: pt.value,
    }))
}

export default function MetricSparkline({ label, value, unit, series, updatedAt }: Props) {
  const chartData = toChartData(series)
  const hasData = chartData.length > 0
  const displayUnit = formatUnit(value, unit)

  return (
    <div style={styles.card} data-testid={`metric-card-${label}`}>
      <div style={styles.labelRow}>
        <span style={styles.label} title={label}>{label}</span>
        {updatedAt && (
          <span style={styles.age}>{formatAge(updatedAt)}</span>
        )}
      </div>
      <div style={styles.valueRow}>
        <span style={styles.value}>{formatValue(value, unit)}</span>
        <span style={styles.unit}>{displayUnit}</span>
      </div>
      <div style={styles.chart}>
        {hasData ? (
          <ResponsiveContainer width="100%" height={56}>
            <LineChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
              <Line
                type="monotone"
                dataKey="v"
                stroke="#6366f1"
                dot={false}
                strokeWidth={1.5}
              />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f9fafb', fontSize: 11 }}
                labelStyle={{ display: 'none' }}
                formatter={(v: number) => [formatValue(v, unit), label]}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div style={styles.noData}>no data yet</div>
        )}
      </div>
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  card: {
    background: '#111827',
    border: '1px solid #1f2937',
    borderRadius: 8,
    padding: '12px 14px',
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
    minWidth: 0,
  },
  labelRow: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    gap: 8,
  },
  label: {
    color: '#9ca3af',
    fontSize: 11,
    fontFamily: 'monospace',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    flexShrink: 1,
  },
  age: {
    color: '#6b7280',
    fontSize: 10,
    whiteSpace: 'nowrap',
    flexShrink: 0,
  },
  valueRow: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 4,
  },
  value: {
    color: '#f9fafb',
    fontSize: 20,
    fontWeight: 700,
    fontVariantNumeric: 'tabular-nums',
  },
  unit: {
    color: '#6b7280',
    fontSize: 11,
  },
  chart: {
    marginTop: 4,
  },
  noData: {
    color: '#4b5563',
    fontSize: 11,
    textAlign: 'center',
    padding: '10px 0',
  },
}
