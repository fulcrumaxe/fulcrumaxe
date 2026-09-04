import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Sidebar } from '../components/Sidebar'
import { Header } from '../components/Header'
import { Card } from '../components/Card'
import { Table, type Column } from '../components/Table'
import { LoadingSpinner } from '../components/LoadingSpinner'
import { useApi } from '../hooks/useApi'
import { controlApi } from '../api/client'
import type { AuditEntry, ControlSettings } from '../api/types'
import { formatLocaleString } from '../lib/safeDate'

export default function SettingsPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  // The global /settings route (as opposed to /project/:id/settings) has no
  // project id — skip the project-scoped control-plane fetches entirely rather
  // than firing GET /api/projects//control (double slash, 404, console noise).
  const hasProject = id !== ''
  const { data: settings, loading: settingsLoading } = useApi(
    () => (hasProject ? controlApi.getSettings(id) : Promise.resolve(null)),
    [id, hasProject]
  )
  const { data: audit, loading: auditLoading } = useApi(
    () => (hasProject ? controlApi.getAudit(id) : Promise.resolve(null)),
    [id, hasProject]
  )
  const [localSettings, setLocalSettings] = useState<Partial<ControlSettings>>({})
  const [saveError, setSaveError] = useState<string | null>(null)
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Debounced save: whenever localSettings changes, wait 600ms then POST to backend.
  // No-op on the global /settings route — there's no project id to save against.
  useEffect(() => {
    if (!hasProject) return
    if (Object.keys(localSettings).length === 0) return
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    saveTimerRef.current = setTimeout(() => {
      controlApi.updateSettings(id, localSettings)
        .then(() => setSaveError(null))
        .catch((err: unknown) => setSaveError(err instanceof Error ? err.message : String(err)))
    }, 600)
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    }
  }, [id, hasProject, localSettings])

  const merged: Partial<ControlSettings> = { ...settings, ...localSettings }

  const auditColumns: Column<AuditEntry>[] = [
    { key: 'timestamp', header: 'Time', render: v => formatLocaleString(String(v)) },
    { key: 'actor', header: 'Actor' },
    { key: 'action', header: 'Action' },
    { key: 'target', header: 'Target' },
  ]

  if (settingsLoading || auditLoading) return <LoadingSpinner />

  return (
    <div className="layout">
      <Sidebar />
      <div className="layout-main">
        <Header projectName={id} />
        <main className="main-content">
          <h2 className="page-title">Settings</h2>

          {merged && (
            <Card title="Control Plane">
              {saveError && (
                <p style={{ margin: '0 0 8px', fontSize: 12, color: '#ef4444' }}>
                  Save failed: {saveError}
                </p>
              )}
              <div className="settings-grid">
                <label className="settings-toggle">
                  <input
                    type="checkbox"
                    checked={merged.autoMerge ?? false}
                    onChange={e => setLocalSettings(s => ({ ...s, autoMerge: e.target.checked }))}
                  />
                  Auto-merge on gate labels
                </label>
                <label className="settings-toggle">
                  <input
                    type="checkbox"
                    checked={merged.requireSecurityReview ?? false}
                    onChange={e =>
                      setLocalSettings(s => ({ ...s, requireSecurityReview: e.target.checked }))
                    }
                  />
                  Always require security review
                </label>
                <label className="settings-field">
                  Max concurrent agents
                  <input
                    type="range"
                    min={1}
                    max={10}
                    value={merged.maxConcurrentAgents ?? 3}
                    onChange={e =>
                      setLocalSettings(s => ({ ...s, maxConcurrentAgents: Number(e.target.value) }))
                    }
                  />
                  <span>{merged.maxConcurrentAgents ?? 3}</span>
                </label>
                <label className="settings-field">
                  Quality gate threshold
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={(merged.qualityGateThreshold ?? 0.8) * 100}
                    onChange={e =>
                      setLocalSettings(s => ({
                        ...s,
                        qualityGateThreshold: Number(e.target.value) / 100,
                      }))
                    }
                  />
                  <span>{Math.round((merged.qualityGateThreshold ?? 0.8) * 100)}%</span>
                </label>
              </div>
            </Card>
          )}

          <Card title="Onboarding">
            <p style={{ margin: '0 0 12px', fontSize: 13, color: '#9ca3af' }}>
              Takes you through a quick tour of the main dashboard pages.
            </p>
            <button
              type="button"
              onClick={() => {
                localStorage.removeItem('af_tour_seen')
                navigate('/')
              }}
              style={{
                background: '#1d4ed8',
                color: '#fff',
                border: 'none',
                borderRadius: 6,
                padding: '8px 16px',
                cursor: 'pointer',
                fontSize: 13,
              }}
            >
              Restart onboarding tour
            </button>
          </Card>

          {audit && (
            <Card title="Audit Trail">
              <Table columns={auditColumns} rows={audit} keyField="id" />
            </Card>
          )}
        </main>
      </div>
    </div>
  )
}
