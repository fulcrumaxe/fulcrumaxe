/// <reference types="vite/client" />
/**
 * Auto-discovered tile registry.
 *
 * Uses Vite's import.meta.glob to find all *Tile.tsx files in this directory.
 * Adding a new tile = drop a new *Tile.tsx file here, no other wiring needed.
 *
 * Sorted alphabetically by filename for stable render order.
 */

import type { ComponentType } from 'react'

interface TileModule {
  default: ComponentType<{ refreshSignal?: number }>
}

// Eager glob — all *Tile.tsx modules loaded synchronously at build time.
// The glob pattern is relative to this file's directory.
const modules = import.meta.glob<TileModule>('./*Tile.tsx', { eager: true })

export interface TileEntry {
  name: string
  Component: ComponentType<{ refreshSignal?: number }>
}

export const tileRegistry: TileEntry[] = Object.entries(modules)
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([path, mod]) => ({
    // Strip "./" prefix and ".tsx" suffix to get e.g. "CostSpikesTile"
    name: path.replace(/^\.\//, '').replace(/\.tsx$/, ''),
    Component: mod.default,
  }))
