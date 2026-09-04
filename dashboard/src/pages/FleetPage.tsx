/**
 * FleetPage — /fleet route.
 *
 * Top-level page for multi-project fleet observability.
 * Renders 3 tiles: ProjectListTile, FleetCostTile, FleetConcurrencyTile.
 * Also shows NewProjectToast when a previously-unseen project is discovered.
 */

import { useState, useEffect } from 'react'
import ProjectListTile from './fleet/ProjectListTile'
import FleetCostTile from './fleet/FleetCostTile'
import FleetConcurrencyTile from './fleet/FleetConcurrencyTile'
import NewProjectToast from './fleet/NewProjectToast'
import { jsonRpc } from '../api/client'

interface FleetProjectsResponse {
  projects: Array<{ name: string; ok: boolean }>
  not_modified?: boolean
  etag?: string
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    padding: '24px 32px',
    fontFamily: 'system-ui, sans-serif',
    minHeight: '100vh',
    background: '#0f172a',
    color: '#f9fafb',
  },
  header: {
    fontSize: 22,
    fontWeight: 700,
    color: '#f9fafb',
    marginBottom: 24,
  },
  tiles: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: 0,
  },
}

export default function FleetPage() {
  const [projectNames, setProjectNames] = useState<string[]>([])

  // Fetch project names once on mount for new-project toast detection
  useEffect(() => {
    jsonRpc<FleetProjectsResponse>('fleet.projects', {})
      .then((resp) => {
        if (resp.projects) {
          setProjectNames(resp.projects.map((p) => p.name))
        }
      })
      .catch(() => {
        // Non-fatal — toast simply won't appear
      })
  }, [])

  return (
    <>
      <NewProjectToast projectNames={projectNames} />
      <div style={styles.page}>
        <h1 style={styles.header}>Fleet Overview</h1>
        <div style={styles.tiles}>
          <ProjectListTile />
          <FleetCostTile />
          <FleetConcurrencyTile />
        </div>
      </div>
    </>
  )
}
