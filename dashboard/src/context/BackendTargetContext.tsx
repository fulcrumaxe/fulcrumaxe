/**
 * BackendTargetContext — React wrapper around the backendTarget module.
 *
 * Provides the currently selected backend type to any component, and
 * exposes a setter that persists to localStorage via backendTarget.ts.
 * Components that only need to read or write the setting should use the
 * useBackendTarget() hook.
 */

import { createContext, useContext, useEffect, useState, useMemo } from 'react'
import {
  type BackendType,
  getBackendTarget,
  setBackendTarget,
  subscribeBackendTarget,
} from '../lib/backendTarget'

interface BackendTargetState {
  backend: BackendType
  setBackend: (t: BackendType) => void
}

const BackendTargetContext = createContext<BackendTargetState>({
  backend: 'python',
  setBackend: () => undefined,
})

interface Props {
  children: React.ReactNode
}

export function BackendTargetProvider({ children }: Props) {
  const [backend, setBackendState] = useState<BackendType>(getBackendTarget)

  useEffect(() => {
    // Stay in sync if another tab or module calls setBackendTarget() directly.
    return subscribeBackendTarget(setBackendState)
  }, [])

  const setBackend = (t: BackendType) => {
    setBackendTarget(t)
    setBackendState(t)
  }

  const value = useMemo(() => ({ backend, setBackend }), [backend])

  return (
    <BackendTargetContext.Provider value={value}>
      {children}
    </BackendTargetContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components
export function useBackendTarget(): BackendTargetState {
  return useContext(BackendTargetContext)
}
