/**
 * Registry auto-discovery tests.
 *
 * Verifies that registry.ts picks up all *Tile.tsx files in the stats directory
 * and that adding a new tile requires no manual wiring.
 */
import { describe, it, expect } from 'vitest'
import { tileRegistry } from '../registry'

// Dynamically count how many *Tile.tsx files exist alongside registry.ts.
// This avoids hardcoding a number that goes stale when tiles are added.
const allTileModules = import.meta.glob('../*Tile.tsx', { eager: false })
const expectedCount = Object.keys(allTileModules).length

describe('tileRegistry', () => {
  it('discovers every *Tile.tsx file in the stats directory', () => {
    expect(tileRegistry.length).toBe(expectedCount)
  })

  it('includes VerdictOverturnTile (previously missing from StatsPage)', () => {
    const names = tileRegistry.map(t => t.name)
    expect(names).toContain('VerdictOverturnTile')
  })

  it('each entry has a name string and a Component function', () => {
    for (const entry of tileRegistry) {
      expect(typeof entry.name).toBe('string')
      expect(entry.name.length).toBeGreaterThan(0)
      expect(typeof entry.Component).toBe('function')
    }
  })

  it('is sorted alphabetically by name', () => {
    const names = tileRegistry.map(t => t.name)
    const sorted = [...names].sort((a, b) => a.localeCompare(b))
    expect(names).toEqual(sorted)
  })
})
