import React from 'react'
import type { PrDetailCost } from '../../api/types'

interface Props {
  cost: PrDetailCost | null
}

export default function CostBreakdownCard({ cost }: Props) {
  return (
    <div data-testid="cost-breakdown-card" style={cardStyle}>
      <h3 style={headingStyle}>Cost Breakdown</h3>
      {cost ? (
        <>
          <div style={{ marginBottom: 16 }}>
            <span style={{ fontSize: 28, fontWeight: 700, color: '#f9fafb' }}>
              ${cost.usd.toFixed(4)}
            </span>
            <span style={{ fontSize: 13, color: '#6b7280', marginLeft: 8 }}>
              {cost.total_tokens.toLocaleString()} tokens total
            </span>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr>
                <th style={thStyle}>Role</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>In</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Out</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>USD</th>
              </tr>
            </thead>
            <tbody>
              {cost.by_role.map(r => (
                <tr key={r.role} style={{ borderTop: '1px solid #374151', color: '#d1d5db' }}>
                  <td style={tdStyle}>{r.role}</td>
                  <td style={{ ...tdStyle, textAlign: 'right', color: '#9ca3af' }}>
                    {r.input_tokens.toLocaleString()}
                  </td>
                  <td style={{ ...tdStyle, textAlign: 'right', color: '#9ca3af' }}>
                    {r.output_tokens.toLocaleString()}
                  </td>
                  <td style={{ ...tdStyle, textAlign: 'right' }}>
                    ${r.usd.toFixed(4)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <p style={{ margin: 0, color: '#6b7280', fontSize: 14 }}>
          No cost data recorded for this PR
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

const thStyle: React.CSSProperties = {
  textAlign: 'left',
  paddingBottom: 6,
  color: '#6b7280',
  fontWeight: 500,
}

const tdStyle: React.CSSProperties = {
  padding: '5px 0',
}
