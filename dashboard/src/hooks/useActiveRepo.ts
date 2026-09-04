/**
 * useActiveRepo — resolves the selected project's repo slug ("owner/name").
 *
 * Reads /api/projects (not /api/fleet/projects — see D#2234). The fleet
 * endpoint's repo field can legitimately be an empty string
 * (backend/fleet/runtime.py:151) and the record only exists once
 * start-dashboard.sh has written a dashboard-runtime.json, so using it
 * would trade a wrong link for a missing one. /api/projects self-seeds
 * and types repo as a required string.
 *
 * Returns null when nothing resolves — callers must render plain text in
 * that case, never a link to a guessed repository.
 */
import { useMemo } from 'react'
import { useActiveProject } from '../context/ActiveProjectContext'
import { useApi } from './useApi'
import { projectsApi } from '../api/client'
import { normalizeRepoSlug } from '../lib/repoUrls'

export function useActiveRepo(): string | null {
  const { activeName } = useActiveProject()
  const { data } = useApi(projectsApi.list, [])

  return useMemo(() => {
    const projects = data ?? []
    const picked = projects.find(p => p.id === activeName || p.name === activeName) ?? projects[0]
    return normalizeRepoSlug(picked?.repo)
  }, [data, activeName])
}
