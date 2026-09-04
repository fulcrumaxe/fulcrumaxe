/**
 * DebaterTile — D#841 PR Inspector tile.
 *
 * Read-only surface that shows the latest debater envelope for a PR:
 *   - verdict (pass / needs-fix / skip)
 *   - issues[] (informational; never affects routing)
 *   - reviewer_under_debate (which reviewer's pass was being challenged)
 *
 * The debater label `debater-confirmed` is rendered via GateBadges; this
 * tile shows the underlying reasoning.
 */

import type { CSSProperties } from 'react'

export interface DebaterIssue {
  severity: 'blocker' | 'major' | string
  summary: string
  evidence?: string
}

export interface DebaterEnvelope {
  verdict: 'pass' | 'needs-fix' | 'skip' | string
  reviewer_under_debate?: 'code-reviewer' | 'security-reviewer' | string
  issues?: DebaterIssue[]
  head_sha?: string
}

interface DebaterTileProps {
  envelope: DebaterEnvelope | null
}

const VERDICT_COLOR: Record<string, { bg: string; fg: string }> = {
  pass: { bg: '#166534', fg: '#86efac' },
  'needs-fix': { bg: '#7f1d1d', fg: '#fca5a5' },
  skip: { bg: '#374151', fg: '#d1d5db' },
}

const containerStyle: CSSProperties = {
  border: '1px solid #374151',
  borderRadius: 6,
  padding: 12,
  background: '#111827',
  color: '#e5e7eb',
  fontSize: 12,
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
}

const verdictPillStyle = (verdict: string): CSSProperties => {
  const color = VERDICT_COLOR[verdict] || VERDICT_COLOR.skip
  return {
    display: 'inline-block',
    background: color.bg,
    color: color.fg,
    borderRadius: 4,
    padding: '2px 8px',
    fontWeight: 600,
    fontSize: 11,
  }
}

export default function DebaterTile({ envelope }: DebaterTileProps) {
  if (!envelope) {
    return (
      <div style={containerStyle}>
        <strong>Debater</strong>
        <span style={{ color: '#6b7280' }}>No debater pass recorded for this PR.</span>
      </div>
    )
  }

  const issues = envelope.issues ?? []
  return (
    <div style={containerStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <strong>Debater</strong>
        <span style={verdictPillStyle(envelope.verdict)}>{envelope.verdict}</span>
      </div>
      {envelope.reviewer_under_debate && (
        <div style={{ color: '#9ca3af' }}>
          Challenging: <code>{envelope.reviewer_under_debate}</code>
        </div>
      )}
      {issues.length === 0 ? (
        <div style={{ color: '#6b7280' }}>No substantive objections raised.</div>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {issues.map((iss, idx) => (
            <li key={idx} style={{ marginBottom: 6 }}>
              <span style={{ fontWeight: 600, color: iss.severity === 'blocker' ? '#fca5a5' : '#fcd34d' }}>
                [{iss.severity}]
              </span>{' '}
              {iss.summary}
              {iss.evidence && (
                <pre
                  style={{
                    background: '#0b1220',
                    border: '1px solid #1f2937',
                    borderRadius: 4,
                    padding: 6,
                    marginTop: 4,
                    fontSize: 11,
                    overflow: 'auto',
                  }}
                >
                  {iss.evidence}
                </pre>
              )}
            </li>
          ))}
        </ul>
      )}
      {envelope.head_sha && (
        <div style={{ color: '#6b7280', fontSize: 11 }}>
          HEAD SHA: <code>{envelope.head_sha.slice(0, 7)}</code>
        </div>
      )}
    </div>
  )
}
