import React from 'react'
import type { PrDetailPr } from '../../api/types'
import { formatDate } from '../../lib/safeDate'

interface Props {
  pr: PrDetailPr
}

export default function PrMetaCard({ pr }: Props) {
  const stateColor =
    pr.state === 'MERGED' ? '#7c3aed'
    : pr.state === 'CLOSED' ? '#dc2626'
    : '#16a34a'

  const statePill = (
    <span style={{
      background: stateColor,
      color: '#fff',
      borderRadius: 4,
      padding: '2px 8px',
      fontSize: 12,
      marginLeft: 8,
      verticalAlign: 'middle',
    }}>
      {pr.state}
    </span>
  )

  return (
    <div data-testid="pr-meta-card" style={cardStyle}>
      <h2 style={{ margin: 0, fontSize: 18, color: '#f9fafb', display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 4 }}>
        <a
          href={pr.html_url}
          target="_blank"
          rel="noreferrer"
          style={{ color: '#818cf8', textDecoration: 'none' }}
        >
          PR #{pr.number}
        </a>
        {statePill}
      </h2>
      <p style={{ margin: '6px 0 0', fontSize: 14, color: '#e5e7eb' }}>{pr.title}</p>
      <div style={{
        marginTop: 12,
        fontSize: 13,
        color: '#9ca3af',
        display: 'flex',
        gap: 16,
        flexWrap: 'wrap',
      }}>
        {pr.author && (
          <span>
            Author: <b style={{ color: '#d1d5db' }}>{pr.author}</b>
          </span>
        )}
        {pr.merged_at && (
          <span>
            Merged: <b style={{ color: '#d1d5db' }}>{formatDate(pr.merged_at)}</b>
          </span>
        )}
        <span style={{ color: '#4ade80' }}>+{pr.additions}</span>
        <span style={{ color: '#f87171' }}>-{pr.deletions}</span>
        <span>{pr.files_changed} file{pr.files_changed !== 1 ? 's' : ''} changed</span>
      </div>
    </div>
  )
}

const cardStyle: React.CSSProperties = {
  background: '#1e293b',
  borderRadius: 8,
  padding: 20,
  marginBottom: 16,
}
