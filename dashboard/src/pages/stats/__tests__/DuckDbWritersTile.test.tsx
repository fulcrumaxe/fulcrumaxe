/**
 * Component tests for DuckDbWritersTile (D#2326).
 *
 * The tile's job is to keep four answers apart. The regression that prompted
 * these tests is the third one: on a host with no lsof the backend could not
 * look at all, and the tile still headlined "no active writers" with the
 * reason demoted to a footnote underneath it — a confident zero sitting on
 * top of an admission that nothing was measured.
 *
 *   determined   — nothing holds the file, and everything was looked at
 *   partial      — some pids could not be inspected; not a confirmed zero
 *   undetermined — no source could answer; reason INSTEAD of an empty state
 *   populated    — the writer table
 *
 * All network calls are mocked — no real backend needed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { DuckDbWritersResponse } from '../DuckDbWritersTile'

vi.mock('../../../api/client', () => ({
  jsonRpc: vi.fn(),
}))

import { jsonRpc } from '../../../api/client'
import DuckDbWritersTile from '../DuckDbWritersTile'

const mockJsonRpc = vi.mocked(jsonRpc)

function makeResponse(
  overrides: Partial<DuckDbWritersResponse> = {},
): DuckDbWritersResponse {
  return {
    writers: [],
    checked_at: '2099-01-01T00:00:00Z',
    warning: null,
    source: 'proc',
    inspected_pids: 10,
    uninspected_pids: 0,
    capped: false,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DuckDbWritersTile — determined empty', () => {
  it('says "no active writers" only when nothing went unlooked-at', async () => {
    mockJsonRpc.mockResolvedValue(makeResponse())
    render(<DuckDbWritersTile />)

    await screen.findByTestId('duckdb-writers-empty')
    expect(screen.getByText('no active writers')).toBeInTheDocument()
    expect(screen.queryByTestId('duckdb-writers-partial')).not.toBeInTheDocument()
    expect(
      screen.queryByTestId('duckdb-writers-undetermined'),
    ).not.toBeInTheDocument()
  })
})

describe('DuckDbWritersTile — undetermined', () => {
  it('does NOT claim "no active writers" when no source could answer', async () => {
    // This is the exact shape the RPC returned on the operator host before
    // the /proc reader existed.
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        warning: 'lsof not found on PATH',
        source: null,
        inspected_pids: null,
        uninspected_pids: null,
      }),
    )
    render(<DuckDbWritersTile />)

    await screen.findByTestId('duckdb-writers-undetermined')
    expect(screen.queryByText('no active writers')).not.toBeInTheDocument()
    expect(screen.queryByTestId('duckdb-writers-empty')).not.toBeInTheDocument()
  })

  it('shows the reason text so the operator knows what to fix', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({ warning: 'lsof not found on PATH', source: null }),
    )
    render(<DuckDbWritersTile />)

    const el = await screen.findByTestId('duckdb-writers-undetermined')
    expect(el.textContent).toContain('lsof not found on PATH')
    expect(el.textContent).toContain('could not determine')
  })
})

describe('DuckDbWritersTile — partial', () => {
  it('does not report a bare zero when pids could not be inspected', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({ inspected_pids: 83, uninspected_pids: 342 }),
    )
    render(<DuckDbWritersTile />)

    await screen.findByTestId('duckdb-writers-partial-empty')
    expect(screen.queryByText('no active writers')).not.toBeInTheDocument()
  })

  it('states how many pids were skipped and out of how many', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({ inspected_pids: 83, uninspected_pids: 342 }),
    )
    render(<DuckDbWritersTile />)

    const note = await screen.findByTestId('duckdb-writers-partial')
    expect(note.textContent).toContain('342')
    expect(note.textContent).toContain('425')
    expect(note.textContent).toContain('not a confirmed complete list')
  })

  it('shows the rows it did find alongside the partial note', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        writers: [{ pid: 4242, cmd: 'python3', age_seconds: 12, fd_mode: 'rw' }],
        inspected_pids: 83,
        uninspected_pids: 342,
      }),
    )
    render(<DuckDbWritersTile />)

    await screen.findByTestId('duckdb-writers-tile')
    expect(screen.getByText('4242')).toBeInTheDocument()
    expect(screen.getByTestId('duckdb-writers-partial')).toBeInTheDocument()
  })

  it('names a capped scan as the reason when the cap was the cause', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({ inspected_pids: 100, uninspected_pids: 325, capped: true }),
    )
    render(<DuckDbWritersTile />)

    const note = await screen.findByTestId('duckdb-writers-partial')
    expect(note.textContent).toContain('scan capped')
  })
})

describe('DuckDbWritersTile — populated', () => {
  it('renders pid, cmd and mode for each writer', async () => {
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        writers: [{ pid: 77568, cmd: 'python3', age_seconds: 3600, fd_mode: 'w' }],
      }),
    )
    render(<DuckDbWritersTile />)

    await screen.findByTestId('duckdb-writers-tile')
    expect(screen.getByText('77568')).toBeInTheDocument()
    expect(screen.getByText('python3')).toBeInTheDocument()
    expect(screen.getByText('w')).toBeInTheDocument()
  })

  it('renders an unknown fd mode as not-reported rather than guessing "r"', async () => {
    // Guessing read here would understate a write lock, which is the one
    // thing this tile exists to make visible.
    mockJsonRpc.mockResolvedValue(
      makeResponse({
        writers: [{ pid: 5, cmd: 'python3', age_seconds: 30, fd_mode: null }],
      }),
    )
    render(<DuckDbWritersTile />)

    await screen.findByTestId('duckdb-writers-tile')
    expect(screen.getByText('not reported')).toBeInTheDocument()
    expect(screen.queryByText('r')).not.toBeInTheDocument()
    expect(screen.queryByText('rw')).not.toBeInTheDocument()
  })
})

describe('DuckDbWritersTile — a source that cannot count what it missed', () => {
  it('reads as it did before when uninspected_pids is absent', async () => {
    // The lsof path, and any older backend that predates these fields.
    // Absent means "not reported", which must not turn into a partial banner
    // — nor into a claim of completeness beyond what was already made.
    mockJsonRpc.mockResolvedValue({
      writers: [],
      checked_at: '2099-01-01T00:00:00Z',
      warning: null,
    } as DuckDbWritersResponse)
    render(<DuckDbWritersTile />)

    await screen.findByTestId('duckdb-writers-empty')
    expect(screen.queryByTestId('duckdb-writers-partial')).not.toBeInTheDocument()
  })
})
