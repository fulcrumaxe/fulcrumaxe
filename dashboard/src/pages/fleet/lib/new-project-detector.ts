/**
 * new-project-detector.ts — compare discovered projects against the known list.
 *
 * "Known" projects are persisted in ~/.autonomous-fleet-state/known.json via the
 * fleet.discovery_ack RPC. The backend is the source of truth; localStorage is
 * a fast-path cache the component can render from before the network round-trip
 * resolves, and a fallback for when the backend is unreachable. It used to be
 * the other way around — detectNewProjects only ever looked at localStorage, so
 * a fresh browser profile or a cleared localStorage re-announced every already-
 * known project as new (D#2317 PR-a item 11).
 *
 * Algorithm:
 *   1. Ask the backend for the persisted known list (fleet.discovery_known).
 *      On success, refresh the localStorage cache from that answer.
 *      On failure (offline, backend down), fall back to the cached list.
 *   2. Compare against the live project names from fleet.projects.
 *   3. Return any names that appear in live but not in known.
 *   4. After the user acknowledges, call ackProjects() to persist the update.
 */

import { jsonRpc } from '../../../api/client'

const LS_KEY = 'fleet_known_projects'

function readKnownLocal(): string[] {
  try {
    const raw = localStorage.getItem(LS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.map(String) : []
  } catch {
    return []
  }
}

function writeKnownLocal(names: string[]): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify([...new Set(names)].sort()))
  } catch {
    // localStorage unavailable — ignore
  }
}

interface DiscoveryKnownResponse {
  known?: unknown
}

/**
 * Fetch the backend's persisted known list. Falls back to the localStorage
 * cache when the backend call fails or returns something unexpected — the
 * cache is a fallback for that case, never the primary source.
 */
async function fetchKnown(): Promise<string[]> {
  try {
    const resp = await jsonRpc<DiscoveryKnownResponse>('fleet.discovery_known', {})
    if (Array.isArray(resp.known)) {
      const known = resp.known.map(String)
      writeKnownLocal(known)
      return known
    }
    return readKnownLocal()
  } catch {
    return readKnownLocal()
  }
}

/** Return project names present in liveNames but absent from the known list. */
export async function detectNewProjects(liveNames: string[]): Promise<string[]> {
  const known = new Set(await fetchKnown())
  return liveNames.filter((n) => !known.has(n))
}

/**
 * Acknowledge (mark as seen) a set of project names.
 * Updates both localStorage and persists to the backend via fleet.discovery_ack.
 */
export async function ackProjects(names: string[]): Promise<void> {
  const known = readKnownLocal()
  const merged = [...new Set([...known, ...names])].sort()
  writeKnownLocal(merged)

  // Persist each name to the backend (idempotent)
  for (const name of names) {
    try {
      await jsonRpc('fleet.discovery_ack', { project_name: name })
    } catch {
      // Non-fatal: local persistence succeeded; backend may be unreachable
    }
  }
}
