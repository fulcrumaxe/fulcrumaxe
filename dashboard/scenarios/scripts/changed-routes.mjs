#!/usr/bin/env node
/**
 * changed-routes.mjs — maps changed dashboard/src/pages files to route paths
 * using routes.manifest.json's `component` field.
 *
 * Pure stdout helper, no side effects, does not invoke Chrome. This is the
 * scoped-sweep input for a future post-merge job (performance-expert's
 * cadence recommendation on D#1527: nightly full sweep + scoped post-merge,
 * never a synchronous per-PR gate) — wiring that job is deferred to the
 * follow-on Discussion; this script only produces the route list.
 *
 * Usage:
 *   node changed-routes.mjs <base_ref>
 *
 * Prints a JSON array of route paths (e.g. ["/stats","/runs"]) to stdout.
 * An empty diff prints "[]" and exits 0.
 */
import fs from 'fs'
import path from 'path'
import { execFileSync } from 'child_process'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '../../..')
const MANIFEST_PATH = path.join(REPO_ROOT, 'dashboard/scenarios/routes.manifest.json')
const PAGES_DIR = 'dashboard/src/pages/'

function loadManifest() {
  if (!fs.existsSync(MANIFEST_PATH)) return []
  try {
    const parsed = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'))
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function getChangedFiles(baseRef) {
  const output = execFileSync(
    'git',
    ['diff', '--name-only', `${baseRef}...HEAD`, '--', PAGES_DIR],
    { cwd: REPO_ROOT, encoding: 'utf8' }
  )
  return output
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)
}

function main() {
  const baseRef = process.argv[2]
  if (!baseRef) {
    console.error('Usage: node changed-routes.mjs <base_ref>')
    process.exit(1)
  }

  let changedFiles
  try {
    changedFiles = getChangedFiles(baseRef)
  } catch (err) {
    console.error(`git diff failed: ${err.message}`)
    process.exit(1)
  }

  const manifest = loadManifest()
  const componentToRoutes = new Map()
  for (const entry of manifest) {
    const list = componentToRoutes.get(entry.component) || []
    list.push(entry.route)
    componentToRoutes.set(entry.component, list)
  }

  const touchedRoutes = new Set()
  for (const file of changedFiles) {
    const componentName = path.basename(file).replace(/\.tsx?$/, '')
    const routes = componentToRoutes.get(componentName)
    if (routes) routes.forEach(r => touchedRoutes.add(r))
  }

  console.log(JSON.stringify([...touchedRoutes].sort()))
  process.exit(0)
}

main()
