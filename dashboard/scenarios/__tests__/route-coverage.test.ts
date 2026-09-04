/**
 * route-coverage.test.ts — coverage-gate vitest test for dashboard/scenarios/routes.manifest.json.
 *
 * Mirrors the schema-validation style of
 * dashboard/scenarios/loop-controller/__tests__/scenario_validate.test.ts, but asserts
 * ROUTE COVERAGE rather than scenario-file shape: every route in the manifest should
 * eventually resolve to >=1 *.scenario.json declaring that route via
 * `visual_verification.routes_touched` or a top-level `route` key.
 *
 * D#1527 scoped this Discussion to the bounded gate infrastructure only — authoring the
 * 14+ missing scenarios is explicitly deferred to a follow-on Discussion. To keep this
 * test file (and the rest of the dashboard vitest suite) green today while still making
 * the coverage gap mechanically observable, this file is split into:
 *   1. An always-passing test that computes and prints "covered N / M routes" plus the
 *      uncovered-route list — the backlog for the follow-on bug-hunt.
 *   2. A regression-pin test that hard-fails only if a route that IS currently covered
 *      loses its coverage (e.g. someone deletes a scenario file or its routes_touched key).
 * Once the follow-on lands scenarios for the remaining routes, KNOWN_COVERED_ROUTES should
 * grow to match, and a future PR can tighten test 1 into a hard per-route assertion.
 *
 * NOTE: the existing loop-controller/*.scenario.json files predate the routes_touched /
 * route declaration convention and do not declare it, so /loop-controller currently shows
 * as "uncovered" by this gate despite having three scenario files. That is intentional —
 * declaring coverage is now a mechanical, drift-proof contract (declared field, not url
 * heuristics), and updating those files is scoped to the follow-on Discussion, not this one.
 */
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SCENARIOS_ROOT = path.resolve(__dirname, '..')
const MANIFEST_PATH = path.join(SCENARIOS_ROOT, 'routes.manifest.json')

interface ManifestEntry {
  route: string
  component: string
  parameterized: boolean
  expected_done_state: string
  needs_seed_fixture: boolean
}

function loadManifest(): ManifestEntry[] {
  const raw = fs.readFileSync(MANIFEST_PATH, 'utf8')
  return JSON.parse(raw) as ManifestEntry[]
}

function findScenarioFiles(dir: string): string[] {
  const results: string[] = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      results.push(...findScenarioFiles(fullPath))
    } else if (entry.isFile() && entry.name.endsWith('.scenario.json')) {
      results.push(fullPath)
    }
  }
  return results
}

function loadCoveredRoutes(): Set<string> {
  const covered = new Set<string>()
  for (const filePath of findScenarioFiles(SCENARIOS_ROOT)) {
    let data: Record<string, unknown>
    try {
      data = JSON.parse(fs.readFileSync(filePath, 'utf8')) as Record<string, unknown>
    } catch {
      continue // malformed scenario files are caught by scenario_validate.test.ts, not here
    }

    const visualVerification = data.visual_verification as { routes_touched?: unknown } | undefined
    if (Array.isArray(visualVerification?.routes_touched)) {
      for (const route of visualVerification!.routes_touched as unknown[]) {
        if (typeof route === 'string') covered.add(route)
      }
    }

    if (typeof data.route === 'string') {
      covered.add(data.route)
    }
  }
  return covered
}

// Routes with a scenario file that declares coverage today (via routes_touched or a
// top-level `route` key). This is a regression pin, not the full coverage target — see
// file header. Only grow this list when a scenario file gains a genuine declaration.
const KNOWN_COVERED_ROUTES = ['/stats']

describe('route coverage gate', () => {
  let manifest: ManifestEntry[]
  let covered: Set<string>

  beforeAll(() => {
    manifest = loadManifest()
    covered = loadCoveredRoutes()
  })

  it('loads a non-empty manifest', () => {
    expect(manifest.length).toBeGreaterThan(0)
  })

  it('excludes the catch-all redirect (path="*") from the manifest', () => {
    expect(manifest.some(entry => entry.route === '*')).toBe(false)
  })

  it('reports current route coverage — covered N / M routes (informational)', () => {
    const uncoveredRoutes = manifest.filter(entry => !covered.has(entry.route)).map(entry => entry.route)
    const coveredCount = manifest.length - uncoveredRoutes.length
    const totalRoutes = manifest.length

    console.log(`covered ${coveredCount} / ${totalRoutes} routes`)
    if (uncoveredRoutes.length > 0) {
      console.log(`Uncovered routes (backlog for the follow-on bug-hunt Discussion): ${uncoveredRoutes.join(', ')}`)
    }

    // M must equal the full manifest length — coverage is computed over every
    // declared route, not a subset.
    expect(totalRoutes).toBe(manifest.length)
    // covered + uncovered must partition the manifest exactly.
    expect(coveredCount + uncoveredRoutes.length).toBe(totalRoutes)
  })

  it('keeps currently-covered routes covered (regression pin)', () => {
    const regressions = KNOWN_COVERED_ROUTES.filter(route => !covered.has(route))
    expect(regressions, `Routes that lost coverage: ${regressions.join(', ')}`).toEqual([])
  })

  it('every KNOWN_COVERED_ROUTES entry exists in the manifest (pin stays valid)', () => {
    const manifestRoutes = new Set(manifest.map(entry => entry.route))
    const stale = KNOWN_COVERED_ROUTES.filter(route => !manifestRoutes.has(route))
    expect(stale, `KNOWN_COVERED_ROUTES entries no longer in manifest: ${stale.join(', ')}`).toEqual([])
  })
})
