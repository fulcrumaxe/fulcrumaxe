type Status = 'success' | 'warning' | 'error' | 'neutral' | 'info'

interface Props {
  status: Status
  label: string
}

export function StatusBadge({ status, label }: Props) {
  return (
    <span className={`status-badge status-badge--${status}`} role="status">
      {label}
    </span>
  )
}

// Discussion-specific status badge with inline styles (no extra CSS class needed)
type DiscussionStatus =
  | 'DISCUSSING'
  | 'SPEC_READY'
  | 'IMPLEMENTING'
  | 'REVIEWING'
  | 'DONE'
  | 'CLOSED'
  | 'UNKNOWN'

const DISCUSSION_STATUS_COLORS: Record<DiscussionStatus, { bg: string; color: string }> = {
  DISCUSSING:    { bg: '#1d4ed8', color: '#fff' },
  SPEC_READY:    { bg: '#0891b2', color: '#fff' },
  IMPLEMENTING:  { bg: '#7c3aed', color: '#fff' },
  REVIEWING:     { bg: '#d97706', color: '#fff' },
  DONE:          { bg: '#16a34a', color: '#fff' },
  CLOSED:        { bg: '#6b7280', color: '#fff' },
  UNKNOWN:       { bg: '#374151', color: '#9ca3af' },
}

interface DiscussionStatusBadgeProps {
  status: DiscussionStatus
}

export function DiscussionStatusBadge({ status }: DiscussionStatusBadgeProps) {
  const { bg, color } = DISCUSSION_STATUS_COLORS[status] ?? DISCUSSION_STATUS_COLORS.UNKNOWN
  return (
    <span
      role="status"
      style={{
        background: bg,
        color,
        borderRadius: 4,
        padding: '2px 7px',
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        whiteSpace: 'nowrap',
      }}
    >
      {status}
    </span>
  )
}
