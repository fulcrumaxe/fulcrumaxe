/**
 * DocumentTitleSync — keeps the browser tab title in sync with the project
 * this dashboard instance is actually serving.
 *
 * Before this existed, dashboard/index.html hardcoded <title>Autonomous
 * Forever</title> — the pre-rename product name — so every tab read the old
 * name regardless of which project was served (D#2316 finding 5). The
 * nav-bar ProjectBadge already reads the right source for this
 * (useActiveProjectName(), backed by /api/projects — see
 * ActiveProjectContext.tsx); this component reads the same source and
 * mirrors it onto document.title.
 *
 * Renders nothing. index.html keeps a static "Dashboard" fallback for the
 * brief pre-mount / pre-config render before the active project resolves.
 */
import { useEffect } from 'react'
import { useActiveProjectName } from '../context/ActiveProjectContext'

export const FALLBACK_TITLE = 'Dashboard'

export function DocumentTitleSync() {
  const activeName = useActiveProjectName()

  useEffect(() => {
    document.title = activeName ? `${activeName} — Dashboard` : FALLBACK_TITLE
  }, [activeName])

  return null
}
