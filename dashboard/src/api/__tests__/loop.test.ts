import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Mock client.ts so we control jsonRpc directly — avoids warm-up timing issues
vi.mock('../client', () => ({
  jsonRpc: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

import { jsonRpc } from '../client'
const mockJsonRpc = jsonRpc as ReturnType<typeof vi.fn>

describe('loop.ts — delegates to client.ts jsonRpc', () => {
  beforeEach(() => {
    mockJsonRpc.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('getLoopTimeline calls jsonRpc with loop.timeline and limit param', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { getLoopTimeline } = await import('../loop')
    await getLoopTimeline(42)
    expect(mockJsonRpc).toHaveBeenCalledWith('loop.timeline', { limit: 42 })
  })

  it('getLoopTimeline uses default limit of 100', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { getLoopTimeline } = await import('../loop')
    await getLoopTimeline()
    expect(mockJsonRpc).toHaveBeenCalledWith('loop.timeline', { limit: 100 })
  })

  it('getIterationDetail calls jsonRpc with loop.iteration_detail and timestamp', async () => {
    const detail = { timestamp: '2026-01-01T00:00:00Z', metrics: {}, log: null, log_path: null }
    mockJsonRpc.mockResolvedValue(detail)
    const { getIterationDetail } = await import('../loop')
    const result = await getIterationDetail('2026-01-01T00:00:00Z')
    expect(mockJsonRpc).toHaveBeenCalledWith('loop.iteration_detail', { timestamp: '2026-01-01T00:00:00Z' })
    expect(result).toEqual(detail)
  })

  it('getLoopTimeline returns data from jsonRpc', async () => {
    const data = [{ timestamp: '2026-01-01T00:00:00Z', duration_seconds: 30, agents_spawned: 1, prs_merged: 0, discussions_scanned: 5, prs_scanned: 3, idle: false, error: null }]
    mockJsonRpc.mockResolvedValue(data)
    const { getLoopTimeline } = await import('../loop')
    const result = await getLoopTimeline(10)
    expect(result).toEqual(data)
  })
})

// Integration-style test: verify that loop.ts does NOT define its own auth helpers
// (the grep-zero-hits acceptance criterion, verified statically here)
describe('loop.ts — no duplicate auth helpers', () => {
  it('module source does not contain getBaseUrl, getToken, or rpcCall definitions', async () => {
    // Read the module source and verify the duplicate pattern is gone
    const fs = await import('fs')
    const path = await import('path')
    const src = fs.readFileSync(path.resolve(__dirname, '../loop.ts'), 'utf-8')
    expect(src).not.toMatch(/function getBaseUrl/)
    expect(src).not.toMatch(/function getToken/)
    expect(src).not.toMatch(/function rpcCall/)
    expect(src).not.toMatch(/function getRpcUrl/)
  })

  it('module imports jsonRpc from client', async () => {
    const fs = await import('fs')
    const path = await import('path')
    const src = fs.readFileSync(path.resolve(__dirname, '../loop.ts'), 'utf-8')
    expect(src).toContain("from './client'")
  })
})
