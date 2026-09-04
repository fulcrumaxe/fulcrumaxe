import React from 'react'
import type { PrDetailQuality } from '../../api/types'

interface Props {
  quality: PrDetailQuality | null
}

const DIMS = ['complexity', 'test_coverage', 'review_rounds', 'size'] as const
type Dim = typeof DIMS[number]

export default function QualityScoreCard({ quality }: Props) {
  return (
    <div data-testid="quality-score-card" style={cardStyle}>
      <h3 style={headingStyle}>Quality Score</h3>
      {quality ? (
        <>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 16 }}>
            <span style={{ fontSize: 36, fontWeight: 700, color: '#f9fafb' }}>
              {quality.total}
            </span>
            <span style={{ fontSize: 22, color: '#818cf8', fontWeight: 700 }}>
              {quality.grade}
            </span>
          </div>
          {DIMS.map((dim: Dim) => {
            const d = quality[dim]
            if (!d) return null
            const pct = d.max > 0 ? (d.score / d.max) * 100 : 0
            const barColor = pct >= 70 ? '#4ade80' : pct >= 40 ? '#facc15' : '#f87171'
            return (
              <div key={dim} style={{ marginBottom: 12 }}>
                <div style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  fontSize: 13,
                  color: '#d1d5db',
                  marginBottom: 4,
                }}>
                  <span>{dim.replace(/_/g, ' ')}</span>
                  <span style={{ color: '#9ca3af' }}>{d.score}/{d.max}</span>
                </div>
                <div style={{ height: 6, background: '#374151', borderRadius: 3 }}>
                  <div style={{
                    height: '100%',
                    background: barColor,
                    borderRadius: 3,
                    width: `${Math.min(pct, 100)}%`,
                    transition: 'width 0.3s ease',
                  }} />
                </div>
                {d.notes && (
                  <p style={{ margin: '3px 0 0', fontSize: 11, color: '#6b7280' }}>{d.notes}</p>
                )}
              </div>
            )
          })}
        </>
      ) : (
        <p style={{ margin: 0, color: '#6b7280', fontSize: 14 }}>
          No quality score recorded for this PR
        </p>
      )}
    </div>
  )
}

const cardStyle: React.CSSProperties = {
  background: '#1e293b',
  borderRadius: 8,
  padding: 20,
  marginBottom: 16,
}

const headingStyle: React.CSSProperties = {
  margin: '0 0 12px',
  fontSize: 15,
  color: '#9ca3af',
  textTransform: 'uppercase',
  letterSpacing: 1,
  fontWeight: 600,
}
