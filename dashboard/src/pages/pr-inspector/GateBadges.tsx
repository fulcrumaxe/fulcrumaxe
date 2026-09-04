/**
 * GateBadges — renders gate label chips for a PR row.
 * Shows colored badges for code-review-passed, code-review-needs-fix,
 * security-review-triggered, and security-review-passed.
 */

import type { CSSProperties } from 'react'

interface GateBadgesProps {
  labels: string[]
}

const BADGE_CONFIG: Record<string, { bg: string; text: string; label: string }> = {
  'code-review-passed': { bg: '#166534', text: '#86efac', label: 'code ✓' },
  'code-review-needs-fix': { bg: '#7f1d1d', text: '#fca5a5', label: 'needs-fix' },
  'security-review-triggered': { bg: '#78350f', text: '#fcd34d', label: 'sec-triggered' },
  'security-review-passed': { bg: '#14532d', text: '#6ee7b7', label: 'sec ✓' },
  'security-issue': { bg: '#7c2d12', text: '#fb923c', label: 'sec-issue' },
}

const containerStyle: CSSProperties = {
  display: 'flex',
  gap: 4,
  flexWrap: 'wrap',
}

export default function GateBadges({ labels }: GateBadgesProps) {
  const gateLabels = labels.filter(l => l in BADGE_CONFIG)

  if (gateLabels.length === 0) {
    return <span style={{ color: '#6b7280', fontSize: 11 }}>no gates</span>
  }

  return (
    <span style={containerStyle}>
      {gateLabels.map(lbl => {
        const cfg = BADGE_CONFIG[lbl]
        return (
          <span
            key={lbl}
            title={lbl}
            style={{
              background: cfg.bg,
              color: cfg.text,
              borderRadius: 4,
              padding: '1px 6px',
              fontSize: 11,
              fontWeight: 500,
              whiteSpace: 'nowrap',
            }}
          >
            {cfg.label}
          </span>
        )
      })}
    </span>
  )
}
