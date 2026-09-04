interface Props {
  value: number
  max?: number
  label?: string
  showPercent?: boolean
}

export function ProgressBar({ value, max = 100, label, showPercent = true }: Props) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100))
  const over = pct > 80

  return (
    <div className="progress-bar-wrapper">
      {label && <span className="progress-bar-label">{label}</span>}
      <div className="progress-bar-track" role="progressbar" aria-valuenow={value} aria-valuemax={max}>
        <div
          className={`progress-bar-fill ${over ? 'progress-bar-fill--warn' : ''}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {showPercent && <span className="progress-bar-pct">{Math.round(pct)}%</span>}
    </div>
  )
}
