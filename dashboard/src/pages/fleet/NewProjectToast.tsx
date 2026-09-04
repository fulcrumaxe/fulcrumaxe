/**
 * NewProjectToast — banner shown when fleet_discovery finds a project
 * not yet in the operator's known list.
 *
 * On mount, compares the live project list against the backend's persisted
 * known list (falling back to a localStorage cache — see new-project-detector.ts).
 * If new projects exist, renders a dismissible header banner.
 * On dismiss, persists acknowledgment via fleet.discovery_ack RPC.
 *
 * role="status" (polite), not role="alert" (assertive) — a newly-discovered
 * project is informational, not an emergency, and doesn't need to interrupt
 * a screen reader mid-sentence (D#2317 PR-a item 11).
 */

import { useEffect, useState } from 'react'
import { detectNewProjects, ackProjects } from './lib/new-project-detector'

interface Props {
  projectNames: string[]
}

const styles: Record<string, React.CSSProperties> = {
  banner: {
    background: '#1e3a5f',
    borderBottom: '1px solid #2563eb',
    padding: '10px 20px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    fontSize: 13,
    color: '#dbeafe',
  },
  left: { display: 'flex', alignItems: 'center', gap: 10 },
  badge: {
    background: '#2563eb',
    color: '#fff',
    borderRadius: 4,
    padding: '2px 8px',
    fontSize: 11,
    fontWeight: 700,
  },
  dismiss: {
    background: 'none',
    border: '1px solid #3b82f6',
    color: '#93c5fd',
    borderRadius: 4,
    padding: '4px 12px',
    cursor: 'pointer',
    fontSize: 12,
  },
}

export default function NewProjectToast({ projectNames }: Props) {
  const [newProjects, setNewProjects] = useState<string[]>([])

  useEffect(() => {
    if (projectNames.length === 0) return
    let cancelled = false
    detectNewProjects(projectNames).then((found) => {
      if (!cancelled && found.length > 0) {
        setNewProjects(found)
      }
    })
    return () => {
      cancelled = true
    }
  }, [projectNames])

  if (newProjects.length === 0) return null

  const handleDismiss = async () => {
    await ackProjects(newProjects)
    setNewProjects([])
  }

  const names = newProjects.join(', ')
  const label = newProjects.length === 1
    ? `New project discovered: ${names}`
    : `New projects discovered: ${names}`

  return (
    <div style={styles.banner} role="status" data-testid="new-project-toast">
      <div style={styles.left}>
        <span style={styles.badge}>NEW</span>
        <span>{label}</span>
      </div>
      <button
        style={styles.dismiss}
        onClick={() => { void handleDismiss() }}
        type="button"
      >
        Dismiss
      </button>
    </div>
  )
}
