interface DataPoint {
  x: number | string
  y: number
}

interface Props {
  data: DataPoint[]
  type?: 'line' | 'bar'
  width?: number
  height?: number
  label?: string
}

export function Chart({ data, type = 'line', width = 400, height = 200, label }: Props) {
  if (data.length === 0) {
    return (
      <div className="chart-empty" style={{ width, height }}>
        No data
      </div>
    )
  }

  const padding = { top: 16, right: 16, bottom: 32, left: 40 }
  const plotW = width - padding.left - padding.right
  const plotH = height - padding.top - padding.bottom

  const ys = data.map(d => d.y)
  const minY = Math.min(...ys)
  const maxY = Math.max(...ys) || 1

  const toX = (i: number) => padding.left + (i / Math.max(data.length - 1, 1)) * plotW
  const toY = (y: number) => padding.top + plotH - ((y - minY) / (maxY - minY || 1)) * plotH

  if (type === 'line') {
    const points = data.map((d, i) => `${toX(i)},${toY(d.y)}`).join(' ')
    return (
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={label ?? 'Line chart'}
        className="chart"
      >
        <polyline
          points={points}
          fill="none"
          stroke="var(--color-accent)"
          strokeWidth="2"
        />
        {data.map((d, i) => (
          <circle
            key={i}
            cx={toX(i)}
            cy={toY(d.y)}
            r="3"
            fill="var(--color-accent)"
            aria-label={`${d.x}: ${d.y}`}
          />
        ))}
      </svg>
    )
  }

  // Bar chart
  const barW = (plotW / data.length) * 0.7
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label ?? 'Bar chart'}
      className="chart"
    >
      {data.map((d, i) => {
        const x = padding.left + (i / data.length) * plotW + barW * 0.2
        const y = toY(d.y)
        const barH = padding.top + plotH - y
        return (
          <rect
            key={i}
            x={x}
            y={y}
            width={barW}
            height={Math.max(barH, 0)}
            fill="var(--color-accent)"
            aria-label={`${d.x}: ${d.y}`}
          />
        )
      })}
    </svg>
  )
}
