/**
 * ProjectBadge — nav-bar badge naming the project this dashboard instance
 * was started against.
 *
 * Reads useActiveProjectName(), which resolves from /api/projects (see
 * ActiveProjectContext.tsx). Renders nothing until that resolves, so nothing
 * flashes a stale or wrong name during load.
 *
 * This replaces the badge half of the nav-bar project switcher removed in
 * D#2239 — see archive/dashboard-project-picker-2026-09-02/README.md for
 * what was removed and why.
 */
import { useActiveProjectName } from '../context/ActiveProjectContext'

const badgeStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  padding: '2px 8px',
  borderRadius: 4,
  background: '#1d4ed8',
  color: '#eff6ff',
  letterSpacing: '0.02em',
}

export function ProjectBadge() {
  const activeName = useActiveProjectName()

  if (!activeName) return null

  return (
    <span style={badgeStyle} title={`Viewing: ${activeName}`}>
      {activeName}
    </span>
  )
}
