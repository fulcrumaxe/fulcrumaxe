/**
 * ActiveProjectContext — tracks which project the dashboard is currently
 * scoped to and propagates that choice to every API call.
 *
 * Identity source: /api/projects (not the host-wide fleet registry — see
 * D#2239). That registry lists every project discovered on the machine;
 * reading it for "which project is this instance?" means a name-sort
 * decides identity, and it leaks other projects' repo/state_dir/ports into
 * this instance's page. /api/projects is scoped to this instance and
 * carries a server-derived `primary` flag (backend/api.py _enrich_project)
 * that is the actual signal for "this instance's own project".
 *
 * URL sync: when the current URL contains a /project/:name/ segment, that project
 * is activated (URL wins over localStorage). This means navigating directly to
 * http://localhost:5102/project/projectb/kpi shows projectb data even if localStorage
 * previously stored "fulcrumaxe".
 *
 * Usage:
 *   // Wrap your app:
 *   <ActiveProjectProvider><App /></ActiveProjectProvider>
 *
 *   // In any component:
 *   const { activeName, setActive } = useActiveProject()
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useLocation } from 'react-router-dom'
import { projectNameFromPathname } from '../lib/urlProject'
import { closeAllEventSources } from './sseRegistry'
import { projectsApi } from '../api/client'
import type { Project } from '../api/types'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ActiveProjectState {
  /** The active project name. Persisted in localStorage under "af.activeProject". */
  activeName: string | null
  /** Set the active project; tears down SSE and triggers a page-wide refetch. */
  setActive: (name: string) => void
  /** True while the project list is loading for the first time. */
  loading: boolean
}

// ---------------------------------------------------------------------------
// Storage key
// ---------------------------------------------------------------------------

// Port-scoped so each Vite dev instance (e.g. AF on 5173, projectb on 5102) keeps
// its own active-project memory. Both share the same localhost origin, so a
// shared key lets one app stomp the other's selection on page load.
const STORAGE_KEY = `af.activeProject.${window.location.port}`

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const ActiveProjectContext = createContext<ActiveProjectState>({
  activeName: null,
  setActive: () => undefined,
  loading: true,
})

// SSE teardown is delegated to sseRegistry.ts — see that file for
// registerEventSource / unregisterEventSource / closeAllEventSources.

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

interface Props {
  children: React.ReactNode
}

export function ActiveProjectProvider({ children }: Props) {
  const location = useLocation()
  const [activeName, setActiveNameState] = useState<string | null>(() => {
    // Rehydrate from localStorage on first render.
    // URL-sync effect below overrides this when a /project/:name/ segment is present.
    try {
      return localStorage.getItem(STORAGE_KEY) || null
    } catch {
      return null
    }
  })
  const [loading, setLoading] = useState(true)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // URL wins over localStorage: when the URL contains /project/:name/, activate
  // that project. Fires on mount and on every navigation so the picker badge
  // stays in sync with the address bar.
  useEffect(() => {
    const nameFromUrl = projectNameFromPathname(location.pathname)
    if (!nameFromUrl) return

    // Persist so navigating away from the project route preserves the selection
    try {
      localStorage.setItem(STORAGE_KEY, nameFromUrl)
    } catch {
      // ignore storage failures
    }

    if (mountedRef.current) {
      setActiveNameState(prev => (prev === nameFromUrl ? prev : nameFromUrl))
    }
  }, [location.pathname])

  // Load project list from the backend on mount
  useEffect(() => {
    let cancelled = false

    async function fetchProjects() {
      try {
        const list: Project[] = await projectsApi.list()
        if (cancelled || !mountedRef.current) return

        // Set default active project when:
        //   1. Nothing is stored in localStorage, OR
        //   2. The stored project no longer exists in the list
        // Note: URL-sync effect writes to localStorage before this runs, so
        // URL-derived names survive this fetch without being overwritten.
        const stored = (() => {
          try {
            return localStorage.getItem(STORAGE_KEY)
          } catch {
            return null
          }
        })()

        // Match on either id or name — useActiveRepo.ts does the same, so a
        // stored id-shaped value and a name-shaped value both resolve.
        const storedExists = stored && list.some(p => p.name === stored || p.id === stored)
        if (!storedExists && list.length > 0) {
          // Prefer the server-derived primary project; fall back to first entry.
          // This replaces "first alive project in a name-sorted list", which is
          // what let a name-sort decide the instance's displayed identity.
          const primary = list.find(p => p.primary) ?? list[0]
          const defaultName = primary.name
          try {
            localStorage.setItem(STORAGE_KEY, defaultName)
          } catch {
            // ignore storage failures
          }
          if (mountedRef.current) setActiveNameState(defaultName)
        }
      } catch {
        // Non-fatal — projects endpoint unavailable (e.g. backend just started)
      } finally {
        if (mountedRef.current) setLoading(false)
      }
    }

    fetchProjects()
    return () => {
      cancelled = true
    }
  }, [])

  const setActive = useCallback((name: string) => {
    // Persist first so the next jsonRpc call picks it up immediately
    try {
      localStorage.setItem(STORAGE_KEY, name)
    } catch {
      // ignore
    }
    // Close all open SSE connections — they'll re-open with the new project param
    closeAllEventSources()
    setActiveNameState(name)
  }, [])

  const value = useMemo(
    () => ({ activeName, setActive, loading }),
    [activeName, setActive, loading],
  )

  return (
    <ActiveProjectContext.Provider value={value}>
      {children}
    </ActiveProjectContext.Provider>
  )
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Returns the active project context.
 *
 * Must be called inside an <ActiveProjectProvider>.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useActiveProject(): ActiveProjectState {
  return useContext(ActiveProjectContext)
}

/**
 * Returns the name of the currently active project, or null when no project
 * is selected yet. Convenience hook for components that only need the name.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useActiveProjectName(): string | null {
  return useContext(ActiveProjectContext).activeName
}
