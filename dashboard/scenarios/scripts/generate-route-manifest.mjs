#!/usr/bin/env node
/**
 * generate-route-manifest.mjs — generates dashboard/scenarios/routes.manifest.json
 * from the <Route> table in dashboard/src/App.tsx.
 *
 * App.tsx is the single source of truth for the dashboard's route list (D#1527).
 * This script is a line/regex scan over the one contiguous <Routes> block — App.tsx
 * is a single file with a simple, flat route table, so a full AST parse (e.g.
 * @babel/parser) is not required. If the route table ever grows multi-file or the
 * regex proves fragile, switch to an AST parse and note the change here.
 *
 * Usage:
 *   node generate-route-manifest.mjs           regenerate routes.manifest.json
 *   node generate-route-manifest.mjs --check    drift guard: exit non-zero if
 *                                                App.tsx has drifted from the
 *                                                committed manifest
 *
 * Regeneration NEVER clobbers hand-set expected_done_state / needs_seed_fixture
 * values for routes that already exist in the committed manifest — only route
 * keys (added/removed routes) change. This is what makes --check meaningful
 * without erasing human-curated metadata.
 */
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
// dashboard/scenarios/scripts -> dashboard/scenarios -> dashboard -> repo root
const REPO_ROOT = path.resolve(__dirname, '../../..')
const APP_TSX_PATH = path.join(REPO_ROOT, 'dashboard/src/App.tsx')
const MANIFEST_PATH = path.join(REPO_ROOT, 'dashboard/scenarios/routes.manifest.json')

const DEFAULT_DONE_STATE = 'unspecified'
const DEFAULT_NEEDS_SEED_FIXTURE = false

/**
 * Extract { route, component, parameterized } for every <Route> in App.tsx,
 * excluding the catch-all path="*" redirect.
 */
export function extractRoutes(appTsxSource) {
  const routeBlockRe = /<Route\s+path="([^"]+)"\s+element=\{([\s\S]*?)\}\s*\/>/g
  const routes = []
  let match
  while ((match = routeBlockRe.exec(appTsxSource)) !== null) {
    const routePath = match[1]
    if (routePath === '*') continue // catch-all redirect — excluded from coverage requirements

    const elementSource = match[2]
    const tagNames = [...elementSource.matchAll(/<([A-Z][A-Za-z0-9]*)\b/g)].map(m => m[1])
    // Suspense-wrapped lazy routes list "Suspense" first, then the real page
    // component — skip Suspense/fallback wrapper names to find the page.
    const realComponent = tagNames.find(t => t !== 'Suspense') || tagNames[0] || 'Unknown'

    routes.push({
      route: routePath,
      component: realComponent,
      parameterized: routePath.includes(':'),
    })
  }
  return routes
}

/**
 * Merge freshly-extracted routes with the previously committed manifest,
 * preserving hand-set expected_done_state / needs_seed_fixture for routes
 * that already existed. New routes get the defaults; routes no longer in
 * App.tsx are dropped.
 */
export function mergeManifest(freshRoutes, existingManifest) {
  const existingByRoute = new Map((existingManifest || []).map(entry => [entry.route, entry]))
  return freshRoutes.map(fresh => {
    const existing = existingByRoute.get(fresh.route)
    return {
      route: fresh.route,
      component: fresh.component,
      parameterized: fresh.parameterized,
      expected_done_state: existing?.expected_done_state ?? DEFAULT_DONE_STATE,
      needs_seed_fixture: existing?.needs_seed_fixture ?? DEFAULT_NEEDS_SEED_FIXTURE,
      // Optional hand-set field (D#1536 Phase 0) — preserved like the other
      // hand-curated metadata above; only present when a route has one set.
      ...(existing?.console_warning_allowlist !== undefined
        ? { console_warning_allowlist: existing.console_warning_allowlist }
        : {}),
    }
  })
}

function loadExistingManifest() {
  if (!fs.existsSync(MANIFEST_PATH)) return []
  try {
    const parsed = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'))
    return Array.isArray(parsed) ? parsed : []
  } catch (err) {
    console.error(`Warning: could not parse existing manifest (${err.message}); treating as empty.`)
    return []
  }
}

/**
 * Compare freshly-extracted routes against the committed manifest's
 * route/component/parameterized shape (never the hand-set metadata fields —
 * those are allowed to diverge from generation intentionally).
 */
export function diffManifest(freshRoutes, committedManifest) {
  const committedByRoute = new Map(committedManifest.map(entry => [entry.route, entry]))
  const freshByRoute = new Map(freshRoutes.map(entry => [entry.route, entry]))

  const added = freshRoutes.filter(r => !committedByRoute.has(r.route)).map(r => r.route)
  const removed = committedManifest.filter(e => !freshByRoute.has(e.route)).map(e => e.route)
  const changed = freshRoutes
    .filter(r => committedByRoute.has(r.route))
    .filter(r => {
      const existing = committedByRoute.get(r.route)
      return existing.component !== r.component || existing.parameterized !== r.parameterized
    })
    .map(r => r.route)

  return { added, removed, changed }
}

function main() {
  const checkMode = process.argv.includes('--check')

  if (!fs.existsSync(APP_TSX_PATH)) {
    console.error(`App.tsx not found at ${APP_TSX_PATH}`)
    process.exit(1)
  }

  const appSource = fs.readFileSync(APP_TSX_PATH, 'utf8')
  const freshRoutes = extractRoutes(appSource)

  if (freshRoutes.length === 0) {
    console.error('No routes extracted from App.tsx — regex likely out of sync with route table shape.')
    process.exit(1)
  }

  const existingManifest = loadExistingManifest()

  if (checkMode) {
    const { added, removed, changed } = diffManifest(freshRoutes, existingManifest)
    if (added.length || removed.length || changed.length) {
      console.error('Route manifest drift detected (routes.manifest.json is stale vs App.tsx):')
      if (added.length) console.error(`  In App.tsx but missing from manifest: ${added.join(', ')}`)
      if (removed.length) console.error(`  In manifest but no longer in App.tsx: ${removed.join(', ')}`)
      if (changed.length) console.error(`  Component/parameterized mismatch: ${changed.join(', ')}`)
      console.error('Run: node dashboard/scenarios/scripts/generate-route-manifest.mjs to regenerate.')
      process.exit(1)
    }
    console.log(`Route manifest is in sync with App.tsx (${freshRoutes.length} routes).`)
    process.exit(0)
  }

  const merged = mergeManifest(freshRoutes, existingManifest)
  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(merged, null, 2) + '\n')
  console.log(`Wrote ${merged.length} routes to ${path.relative(REPO_ROOT, MANIFEST_PATH)}`)
  process.exit(0)
}

// Only run main() when executed directly (not when imported for tests).
if (import.meta.url === `file://${process.argv[1]}`) {
  main()
}
