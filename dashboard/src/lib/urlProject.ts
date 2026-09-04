/**
 * urlProject.ts — URL path helpers for project name extraction.
 *
 * Separates URL-parsing logic from the React context so it can be
 * imported from both component files and tests without triggering
 * the react-refresh/only-export-components lint rule.
 */

/**
 * Extracts the project name from a pathname like `/project/:name/...`.
 * Returns null when the pathname doesn't match the pattern.
 *
 * Examples:
 *   /project/projectb/kpi       → "projectb"
 *   /project/projectb/          → "projectb"
 *   /project/fulcrumaxe → "fulcrumaxe"
 *   /kpi                    → null
 *   /                       → null
 */
export function projectNameFromPathname(pathname: string): string | null {
  const match = /^\/project\/([^/]+)(?:\/|$)/.exec(pathname)
  return match ? decodeURIComponent(match[1]) : null
}
