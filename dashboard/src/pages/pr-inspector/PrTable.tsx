/**
 * PrTable — sortable table of open PRs for the PR Inspector page.
 *
 * Columns: #, title, author, age, gate-label badges, fix-cycles,
 *          quality score, linked Discussion, GitHub link.
 * Clicking a row navigates to /pr/:number (PRDetailPage from #389).
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { PrListEntry } from '../../api/types'
import GateBadges from './GateBadges'

export type SortKey = 'age' | 'fix_cycles' | 'quality'
export type SortDir = 'asc' | 'desc'

interface PrTableProps {
  prs: PrListEntry[]
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

const thStyle: React.CSSProperties = {
  padding: '8px 12px',
  textAlign: 'left',
  color: '#9ca3af',
  fontWeight: 500,
  fontSize: 12,
  borderBottom: '1px solid #374151',
  whiteSpace: 'nowrap',
  cursor: 'pointer',
  userSelect: 'none',
}

const tdStyle: React.CSSProperties = {
  padding: '8px 12px',
  fontSize: 13,
  color: '#e5e7eb',
  borderBottom: '1px solid #1f2937',
  verticalAlign: 'middle',
}

export default function PrTable({ prs }: PrTableProps) {
  const navigate = useNavigate()
  const [sortKey, setSortKey] = useState<SortKey>('age')
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  function handleSortClick(key: SortKey) {
    if (sortKey === key) {
      setSortDir(d => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('asc')
    }
  }

  function sortIndicator(key: SortKey): string {
    if (sortKey !== key) return ''
    return sortDir === 'asc' ? ' ↑' : ' ↓'
  }

  const sorted = [...prs].sort((a, b) => {
    let av: number
    let bv: number
    if (sortKey === 'age') {
      av = a.age_seconds
      bv = b.age_seconds
    } else if (sortKey === 'fix_cycles') {
      av = a.fix_cycles
      bv = b.fix_cycles
    } else {
      av = a.quality_score ?? -1
      bv = b.quality_score ?? -1
    }
    return sortDir === 'asc' ? av - bv : bv - av
  })

  if (sorted.length === 0) {
    return (
      <div style={{ color: '#6b7280', padding: 32, textAlign: 'center' }}>
        No open PRs
      </div>
    )
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <th style={thStyle}>#</th>
            <th style={thStyle}>Title</th>
            <th style={thStyle}>Author</th>
            <th
              style={{ ...thStyle, color: sortKey === 'age' ? '#60a5fa' : '#9ca3af' }}
              onClick={() => handleSortClick('age')}
            >
              Age{sortIndicator('age')}
            </th>
            <th style={thStyle}>Gate labels</th>
            <th
              style={{ ...thStyle, color: sortKey === 'fix_cycles' ? '#60a5fa' : '#9ca3af' }}
              onClick={() => handleSortClick('fix_cycles')}
            >
              Fix cycles{sortIndicator('fix_cycles')}
            </th>
            <th
              style={{ ...thStyle, color: sortKey === 'quality' ? '#60a5fa' : '#9ca3af' }}
              onClick={() => handleSortClick('quality')}
            >
              Quality{sortIndicator('quality')}
            </th>
            <th style={thStyle}>Discussion</th>
            <th style={thStyle}>GitHub</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map(pr => (
            <tr
              key={pr.number}
              onClick={() => navigate(`/pr/${pr.number}`)}
              style={{ cursor: 'pointer' }}
              onMouseEnter={e => {
                ;(e.currentTarget as HTMLTableRowElement).style.background = '#1f2937'
              }}
              onMouseLeave={e => {
                ;(e.currentTarget as HTMLTableRowElement).style.background = 'transparent'
              }}
            >
              <td style={{ ...tdStyle, color: '#6b7280' }}>#{pr.number}</td>
              <td style={{ ...tdStyle, maxWidth: 280 }}>
                <span
                  title={pr.title}
                  style={{ overflow: 'hidden', textOverflow: 'ellipsis', display: 'block', whiteSpace: 'nowrap' }}
                >
                  {pr.title}
                </span>
              </td>
              <td style={{ ...tdStyle, color: '#9ca3af' }}>{pr.author ?? '—'}</td>
              <td style={{ ...tdStyle, color: '#9ca3af' }}>{formatAge(pr.age_seconds)}</td>
              <td style={tdStyle}>
                {pr.labels.length === 0 ? (
                  <span
                    style={{ color: '#4b5563', fontStyle: 'italic', fontSize: 12 }}
                    title="No gate labels yet — reviewer adds labels after reviewing"
                  >
                    (awaiting first label)
                  </span>
                ) : (
                  <GateBadges labels={pr.labels} />
                )}
              </td>
              <td style={{ ...tdStyle, textAlign: 'center' }}>
                {pr.fix_cycles > 0 ? (
                  <span style={{ color: '#f87171' }}>{pr.fix_cycles}</span>
                ) : (
                  <span style={{ color: '#4b5563' }}>0</span>
                )}
              </td>
              <td style={{ ...tdStyle, textAlign: 'center' }}>
                {pr.quality_score != null ? (
                  <span style={{ color: pr.quality_score >= 60 ? '#4ade80' : '#f87171' }}>
                    {Math.round(pr.quality_score)}
                  </span>
                ) : (
                  <span
                    style={{ color: '#4b5563' }}
                    title="Not yet computed — quality_scorer runs on review-passed PRs"
                  >
                    —
                  </span>
                )}
              </td>
              <td style={tdStyle}>
                {pr.discussion_number != null ? (
                  <span style={{ color: '#818cf8' }}>#{pr.discussion_number}</span>
                ) : (
                  <span style={{ color: '#4b5563' }}>—</span>
                )}
              </td>
              <td style={tdStyle}>
                <a
                  href={pr.html_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                  style={{ color: '#60a5fa', textDecoration: 'none', fontSize: 12 }}
                >
                  ↗
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
