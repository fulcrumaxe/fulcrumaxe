/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, type ReactNode } from 'react'
import type { Session } from '../api/types'

// Solo-dev mode: no authentication. A fixed local session is always active.
const DEV_SESSION: Session = {
  id: 'local-user',
  userId: 'local-user',
  username: 'dev',
  avatarUrl: '',
  createdAt: new Date().toISOString(),
  expiresAt: new Date(Date.now() + 86400_000).toISOString(),
}

interface AuthContextValue {
  session: Session | null
  token: string | null
  loading: boolean
  login: () => void
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const value: AuthContextValue = {
    session: DEV_SESSION,
    token: null,
    loading: false,
    login: () => {},
    logout: () => {},
  }
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
