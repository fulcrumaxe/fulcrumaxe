/**
 * Regression test for "server-side failure rendered as no-data-yet" (D#2315).
 *
 * Before this fix, every *Tile.tsx that imports TileFetchError did
 * `isTransportError(err) ? err : null` in its catch block -- a non-transport
 * error (an ApiError, the shape `jsonRpc` throws for a JSON-RPC `error` body
 * -- e.g. the -32000 a crashed `stats.loop_idle_ratio` handler used to
 * return) was discarded into `fetchError = null`, and the tile fell through
 * to its own "no data yet" / empty-state copy. That is a statement about
 * data volume standing in for a server-side exception.
 *
 * Tiles are discovered from the filesystem (via import.meta.glob and a
 * source-content check for the TileFetchError import), not a hand-written
 * list -- a tile added later that follows the same import is covered
 * automatically, per D#2315 Spec item 14.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ComponentType } from 'react'
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

vi.mock('../../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../../api/client')>('../../../api/client')
  return {
    ...actual,
    jsonRpc: vi.fn(),
  }
})

import { jsonRpc, ApiError } from '../../../api/client'

const mockJsonRpc = vi.mocked(jsonRpc)

beforeEach(() => {
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// Discovery -- every *Tile.tsx module in this directory, eagerly loaded so
// each entry's default export is available as a real component.
// ---------------------------------------------------------------------------
type TileModule = { default: ComponentType<{ refreshSignal?: number }> }
const allTileModules = import.meta.glob<TileModule>('../*Tile.tsx', { eager: true })

const STATS_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const OLD_SWALLOW_PATTERN = /isTransportError\(err\)\s*\?\s*err\s*:\s*null/

// Filter to tiles that actually render <TileFetchError> -- read straight
// from source, not a maintained list, so a tile that never imports
// TileFetchError (and so has nothing to swallow) isn't force-fit into this
// suite.
const tileEntries = Object.entries(allTileModules)
  .map(([globPath, mod]) => {
    const fileName = globPath.replace(/^\.\.\//, '')
    const source = readFileSync(join(STATS_DIR, fileName), 'utf-8')
    return { fileName, mod, source }
  })
  .filter(({ source }) => /from ['"]\.\/TileFetchError['"]/.test(source))

describe('TileBackendError discovery', () => {
  it('found tiles that import TileFetchError (discovery is not broken)', () => {
    // A floor, not a hand-written list: if this glob or the source-content
    // filter above ever matches zero files, the rest of this suite would
    // vacuously pass having tested nothing -- fail loudly instead.
    expect(tileEntries.length).toBeGreaterThanOrEqual(5)
  })
})

describe.each(tileEntries.map(e => [e.fileName, e] as const))('%s', (fileName, { mod, source }) => {
  it('no longer contains the old isTransportError(err) ? err : null swallow', () => {
    expect(OLD_SWALLOW_PATTERN.test(source)).toBe(false)
  })

  it('renders a distinct backend-error state (not transport, not empty) on an ApiError', async () => {
    mockJsonRpc.mockRejectedValue(new ApiError(-32000, "'int' object has no attribute 'replace'"))
    const Component = mod.default
    render(<Component />)

    const backendError = await screen.findByTestId('tile-backend-error')
    expect(backendError).toBeInTheDocument()

    // Not the transport-failure state ...
    expect(screen.queryByTestId('tile-fetch-error')).not.toBeInTheDocument()
    // ... and not any tile's "no data yet" empty-state copy.
    expect(screen.queryByText(/no data yet/i)).not.toBeInTheDocument()
    expect(screen.queryByTestId('idle-ratio-na')).not.toBeInTheDocument()

    // The server's message is actionable and must be shown.
    expect(screen.getByText(/int.*object has no attribute 'replace'/i)).toBeInTheDocument()
  })

  it('still renders the transport-failure state (unchanged) on a raw TypeError', async () => {
    mockJsonRpc.mockRejectedValue(new TypeError('Failed to fetch'))
    const Component = mod.default
    render(<Component />)

    await screen.findByTestId('tile-fetch-error')
    expect(screen.queryByTestId('tile-backend-error')).not.toBeInTheDocument()
  })
})
