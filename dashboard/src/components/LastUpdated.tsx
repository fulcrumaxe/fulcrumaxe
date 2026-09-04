/**
 * LastUpdated — "Last updated Xm ago" indicator used across all data pages.
 *
 * Usage:
 *   <LastUpdated fetchedAt={isoTimestamp} />
 *
 * Updates every 5s without a prop change. Returns null when fetchedAt is null.
 */
import { useEffect, useState } from 'react'

interface Props {
  fetchedAt: string | null
  style?: React.CSSProperties
}

function useSecondsAgo(isoTimestamp: string | null): string {
  const [label, setLabel] = useState<string>('')

  useEffect(() => {
    if (!isoTimestamp) {
      setLabel('')
      return
    }
    function update() {
      const diff = Math.floor((Date.now() - new Date(isoTimestamp!).getTime()) / 1000)
      if (diff < 5) setLabel('just now')
      else if (diff < 60) setLabel(`${diff}s ago`)
      else if (diff < 3600) setLabel(`${Math.floor(diff / 60)}m ago`)
      else setLabel(`${Math.floor(diff / 3600)}h ago`)
    }
    update()
    const t = setInterval(update, 5_000)
    return () => clearInterval(t)
  }, [isoTimestamp])

  return label
}

export function LastUpdated({ fetchedAt, style }: Props) {
  const label = useSecondsAgo(fetchedAt)
  if (!label) return null

  return (
    <span
      style={{
        fontSize: 12,
        color: '#6b7280',
        fontVariantNumeric: 'tabular-nums',
        ...style,
      }}
    >
      Last updated {label}
    </span>
  )
}
