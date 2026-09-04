/**
 * Sidebar — project-context navigation only.
 *
 * IA decision (Discussion #495): top bar is primary global navigation.
 * Sidebar is scoped to project-level pages only (Dashboard, Agent Feed, KPI, Settings).
 * Global items (Projects, Ideas) were removed from sidebar to avoid
 * duplication with the top bar — they remain accessible via the top bar on every page.
 *
 * The sidebar renders nothing when there is no active project context (no :id param).
 */
import { NavLink, useParams } from 'react-router-dom'

interface NavItem {
  path: string
  label: string
  icon: string
}

export function Sidebar() {
  const { id } = useParams<{ id: string }>()

  // Only show sidebar when inside a project context.
  // Global navigation lives in the top bar (App.tsx).
  const projectItems: NavItem[] = id
    ? [
        { path: `/project/${id}`, label: 'Dashboard', icon: '◈' },
        { path: `/project/${id}/agents`, label: 'Agent Feed', icon: '◎' },
        { path: `/project/${id}/kpi`, label: 'KPI', icon: '◇' },
        { path: `/project/${id}/settings`, label: 'Settings', icon: '⚙' },
      ]
    : []

  if (projectItems.length === 0) return null

  return (
    <nav className="sidebar" aria-label="Project navigation">
      <div className="sidebar-logo">AF</div>
      <ul className="sidebar-nav">
        {projectItems.map(item => (
          <li key={item.path}>
            <NavLink
              to={item.path}
              end
              className={({ isActive }) => `sidebar-link${isActive ? ' sidebar-link--active' : ''}`}
            >
              <span className="sidebar-icon" aria-hidden="true">{item.icon}</span>
              <span className="sidebar-label">{item.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
