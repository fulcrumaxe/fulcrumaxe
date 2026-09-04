/**
 * Shared style tokens for /runs tile components.
 */
import type React from 'react'

export const sharedStyles: Record<string, React.CSSProperties> = {
  state: {
    color: '#6b7280',
    fontSize: 14,
    padding: '40px 0',
    textAlign: 'center',
  },
  section: {
    marginTop: 32,
  },
  sectionHeading: {
    margin: '0 0 12px',
    fontSize: 18,
    fontWeight: 600,
    color: '#f9fafb',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse' as const,
    fontSize: 13,
    background: '#111827',
    border: '1px solid #1f2937',
    borderRadius: 6,
  },
  th: {
    padding: '8px 12px',
    textAlign: 'left' as const,
    color: '#9ca3af',
    fontWeight: 500,
    borderBottom: '1px solid #1f2937',
    background: '#0f172a',
  },
  tr: {
    borderBottom: '1px solid #1f2937',
  },
  td: {
    padding: '8px 12px',
    color: '#f9fafb',
  },
  badge: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: 4,
    fontSize: 12,
    fontWeight: 600,
  },
}
