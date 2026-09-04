import { useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Sidebar } from '../components/Sidebar'
import { Header } from '../components/Header'
import { LoadingSpinner } from '../components/LoadingSpinner'
import { useApi } from '../hooks/useApi'
import { projectsApi, ApiError } from '../api/client'
import type { Project } from '../api/types'
import { formatRelative } from '../lib/safeDate'
import { useState } from 'react'

/**
 * D#2314 PR2 — one honest status line replaces the old liveness badge,
 * health badge, and velocity line. See wiki/design-notes/2314.md for the wireframe.
 *
 * Three states, each with a distinct icon *and* a distinct status word so
 * color is never the only signal:
 *   ● active  — N agents running · newest started <time>
 *   ○ idle    — No agents running · checked <time>
 *   ▲ unknown — No signal — can't read this project's state dir
 *
 * The active line says "newest started", never a phrase implying a
 * heartbeat, because fleet.db's schema (project_name, agent_id, role,
 * started_at, pid) has no such column — started_at is all the data can
 * honestly support.
 */
interface StatusLineInfo {
  icon: string
  iconColor: string
  /** Full visible line, e.g. "3 agents running · newest started 4m ago". */
  text: string
  /** Same content, screen-reader phrasing (comma instead of the glyph/dash). */
  ariaLabel: string
}

function statusLineInfo(p: Project, fetchedAt: string | null): StatusLineInfo {
  if (p.liveness === 'active') {
    const count = p.activeAgents ?? 0
    const word = `${count} agent${count === 1 ? '' : 's'} running`
    const detail = `newest started ${formatRelative(p.newestStartedAt)}`
    return {
      icon: '●',
      iconColor: 'var(--color-success)',
      text: `${word} · ${detail}`,
      ariaLabel: `${word}, ${detail}`,
    }
  }
  if (p.liveness === 'idle') {
    const detail = `checked ${formatRelative(fetchedAt)}`
    return {
      icon: '○',
      iconColor: 'var(--color-text-secondary)',
      text: `No agents running · ${detail}`,
      ariaLabel: `No agents running, ${detail}`,
    }
  }
  // 'unknown' (or missing) — the no-signal state. Never render a count here;
  // an unqueried project must never look like a quiet one (D#2314).
  return {
    icon: '▲',
    iconColor: 'var(--color-warning)',
    text: "No signal — can't read this project's state dir",
    ariaLabel: "No signal, can't read this project's state dir",
  }
}

/**
 * "What they're doing" — role names only (D#2314 PR2 item 22). fleet.db has
 * no discussion/pr column yet, so a linked PR/Discussion is deferred, named
 * data rather than invented. Up to two "<role> running" clauses, then
 * "+N more". Returns null when there's no role data to show — a legitimate
 * state (agents registered outside the instrumented path), not a bug.
 */
function rolesLine(roles?: string[]): string | null {
  if (!roles || roles.length === 0) return null
  const shown = roles.slice(0, 2).map(r => `${r} running`)
  const extra = roles.length - shown.length
  return extra > 0 ? `${shown.join(', ')}, +${extra} more` : shown.join(', ')
}

function ProjectStatusLine({ project, fetchedAt }: { project: Project; fetchedAt: string | null }) {
  const info = statusLineInfo(project, fetchedAt)
  const doing = project.liveness === 'active' ? rolesLine(project.roles) : null
  return (
    <div className="project-card-status">
      <div role="status" aria-label={info.ariaLabel} className="project-card-status-line">
        <span aria-hidden="true" style={{ color: info.iconColor, marginRight: 6 }}>{info.icon}</span>
        <span>{info.text}</span>
      </div>
      {doing && <div className="project-card-doing">{doing}</div>}
    </div>
  )
}

export default function ProjectListPage() {
  const { data: projects, loading, error, refetch } = useApi(() => projectsApi.list(), [])
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [repo, setRepo] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  // Client-side fetch time — the evidence stamp for the idle state, since
  // fleet.db has no rows (and so no timestamp of its own) when idle (D#2314 F3).
  // Set during render (React's "adjust state when a value changes" pattern)
  // rather than in an effect, so it lands in the same commit as the data
  // it's timestamping instead of a trailing, separately-observable update.
  const [fetchedAt, setFetchedAt] = useState<string | null>(null)
  const [stampedProjects, setStampedProjects] = useState<Project[] | null>(null)
  if (projects && projects !== stampedProjects) {
    setStampedProjects(projects)
    setFetchedAt(new Date().toISOString())
  }

  // Auto-route when exactly one active project is detected, unless ?picker=1 suppresses it.
  useEffect(() => {
    if (loading) return
    if (searchParams.get('picker') === '1') return
    if (!projects || projects.length !== 1) return
    if (projects[0].liveness === 'active') {
      navigate(`/project/${projects[0].id}`, { replace: true })
    }
  }, [loading, projects, navigate, searchParams])

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setSubmitError(null)
    try {
      await projectsApi.create({ name, repo } as Parameters<typeof projectsApi.create>[0])
      setName('')
      setRepo('')
      setShowForm(false)
      refetch?.()
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : (err as Error).message
      setSubmitError(msg.slice(0, 200))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleDelete(p: Project, e: React.MouseEvent) {
    e.stopPropagation()
    if (p.primary) return
    if (!confirm(`Delete project "${p.name}"?`)) return
    try {
      await projectsApi.delete(p.id)
      refetch?.()
    } catch {
      /* leave the card; refetch on next render will reflect server state */
    }
  }

  return (
    <div className="layout">
      <Sidebar />
      <div className="layout-main">
        <Header />
        <main className="main-content">
          <div className="page-header">
            <h2 className="page-title">Projects</h2>
          </div>

          {loading && <LoadingSpinner />}
          {error && (
            <div className="error-message" role="alert">
              Failed to load projects: {error.message}
            </div>
          )}
          {projects && (
            <div className="project-grid" data-tour="home-status">
              {projects.map(p => (
                <div
                  key={p.id}
                  className="project-card"
                  role="button"
                  tabIndex={0}
                  onClick={() => navigate(`/project/${p.id}`)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' || e.key === ' ') navigate(`/project/${p.id}`)
                  }}
                  style={{ cursor: 'pointer', position: 'relative' }}
                >
                  {!p.primary && (
                    <button
                      type="button"
                      onClick={e => handleDelete(p, e)}
                      title={`Delete ${p.name}`}
                      style={{
                        position: 'absolute',
                        top: 8,
                        right: 8,
                        background: 'transparent',
                        color: '#8b949e',
                        border: '1px solid #30363d',
                        borderRadius: 4,
                        padding: '2px 6px',
                        cursor: 'pointer',
                        fontSize: 11,
                      }}
                    >
                      ✕
                    </button>
                  )}
                  <div className="project-card-header">
                    <span className="project-card-name">{p.name}</span>
                  </div>
                  <div className="project-card-meta">
                    <span className="project-card-repo">{p.repo}</span>
                  </div>
                  <ProjectStatusLine project={p} fetchedAt={fetchedAt} />
                </div>
              ))}
            </div>
          )}

          <div style={{ marginTop: 24, borderTop: '1px solid #30363d', paddingTop: 16 }}>
            {projects && projects.length === 1 && (
              <p style={{ fontSize: 12, color: '#8b949e', marginBottom: 8 }}>
                We auto-detected your project. Add another only if you're operating multiple.
              </p>
            )}
            <button
              type="button"
              className="button button--primary"
              onClick={() => setShowForm(v => !v)}
            >
              {showForm ? 'Cancel' : projects && projects.length > 1 ? 'Add another project' : 'Connect another project'}
            </button>
          </div>

          {showForm && (
            <form
              onSubmit={handleCreate}
              style={{
                background: '#161b22',
                border: '1px solid #30363d',
                borderRadius: 8,
                padding: 16,
                margin: '12px 0',
                display: 'flex',
                gap: 8,
                alignItems: 'flex-end',
                flexWrap: 'wrap',
              }}
            >
              <label style={{ display: 'flex', flexDirection: 'column', flex: '1 1 200px', fontSize: 12, color: '#8b949e' }}>
                Name
                <input
                  type="text"
                  value={name}
                  onChange={e => setName(e.target.value)}
                  required
                  placeholder="my-project"
                  style={{ marginTop: 4, padding: 6, background: '#0d1117', color: '#c9d1d9', border: '1px solid #30363d', borderRadius: 4 }}
                />
              </label>
              <label style={{ display: 'flex', flexDirection: 'column', flex: '2 1 300px', fontSize: 12, color: '#8b949e' }}>
                Repo (owner/name)
                <input
                  type="text"
                  value={repo}
                  onChange={e => setRepo(e.target.value)}
                  required
                  placeholder="owner/repo"
                  style={{ marginTop: 4, padding: 6, background: '#0d1117', color: '#c9d1d9', border: '1px solid #30363d', borderRadius: 4 }}
                />
              </label>
              <button
                type="submit"
                className="button button--primary"
                disabled={submitting || !name || !repo}
              >
                {submitting ? 'Adding...' : 'Add'}
              </button>
              {submitError && (
                <div style={{ flexBasis: '100%', color: '#f85149', fontSize: 12 }}>{submitError}</div>
              )}
            </form>
          )}
        </main>
      </div>
    </div>
  )
}
