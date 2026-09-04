import React from 'react'
import type { PrDetailDiscussion } from '../../api/types'

interface Props {
  discussion: PrDetailDiscussion | null
}

export default function DiscussionLinkCard({ discussion }: Props) {
  return (
    <div data-testid="discussion-link-card" style={cardStyle}>
      <h3 style={headingStyle}>Linked Discussion</h3>
      {discussion ? (
        <div>
          <a
            href={`/discussions?focus=${discussion.number}`}
            style={{ color: '#818cf8', textDecoration: 'none', fontSize: 15 }}
          >
            #{discussion.number}: {discussion.title}
          </a>
          <a
            href={discussion.url}
            target="_blank"
            rel="noreferrer"
            style={{ marginLeft: 8, fontSize: 12, color: '#6b7280' }}
          >
            (GitHub)
          </a>
        </div>
      ) : (
        <p style={{ margin: 0, color: '#6b7280', fontSize: 14 }}>No linked Discussion</p>
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
