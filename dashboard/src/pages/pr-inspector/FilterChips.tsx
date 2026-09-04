/**
 * FilterChips — "all", "stuck", and "ready" filter tabs for the PR Inspector.
 */

export type PrFilter = 'all' | 'stuck' | 'ready'

interface FilterChipsProps {
  active: PrFilter
  onChange: (f: PrFilter) => void
  counts: { all: number; stuck: number; ready: number }
}

const FILTERS: { id: PrFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'stuck', label: 'Stuck' },
  { id: 'ready', label: 'Ready to merge' },
]

export default function FilterChips({ active, onChange, counts }: FilterChipsProps) {
  return (
    <div style={{ display: 'flex', gap: 8 }}>
      {FILTERS.map(f => {
        const isActive = f.id === active
        return (
          <button
            key={f.id}
            onClick={() => onChange(f.id)}
            style={{
              background: isActive ? '#3b82f6' : '#374151',
              color: isActive ? '#fff' : '#9ca3af',
              border: 'none',
              borderRadius: 6,
              padding: '4px 12px',
              cursor: 'pointer',
              fontSize: 13,
              fontWeight: isActive ? 600 : 400,
            }}
          >
            {f.label}{' '}
            <span style={{ opacity: 0.7 }}>({counts[f.id]})</span>
          </button>
        )
      })}
    </div>
  )
}
