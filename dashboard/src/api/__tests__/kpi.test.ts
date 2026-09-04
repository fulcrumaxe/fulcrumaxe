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

describe('kpi.ts — delegates to client.ts jsonRpc', () => {
  beforeEach(() => {
    mockJsonRpc.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('getVelocity calls jsonRpc with kpi.history and days param', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { getVelocity } = await import('../kpi')
    await getVelocity(14)
    expect(mockJsonRpc).toHaveBeenCalledWith('kpi.history', { days: 14 })
  })

  it('getVelocity uses default of 30 days', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { getVelocity } = await import('../kpi')
    await getVelocity()
    expect(mockJsonRpc).toHaveBeenCalledWith('kpi.history', { days: 30 })
  })

  it('getCycleTime calls jsonRpc with kpi.cycle_time and days param', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { getCycleTime } = await import('../kpi')
    await getCycleTime(30)
    expect(mockJsonRpc).toHaveBeenCalledWith('kpi.cycle_time', { days: 30 })
  })

  it('getCycleTime uses default of 90 days', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { getCycleTime } = await import('../kpi')
    await getCycleTime()
    expect(mockJsonRpc).toHaveBeenCalledWith('kpi.cycle_time', { days: 90 })
  })

  it('getCostByDiscussion calls jsonRpc with cost.by_discussion, top and days params', async () => {
    const data = [{ discussion: 1, tokens: 1000, usd: 0.05 }]
    mockJsonRpc.mockResolvedValue(data)
    const { getCostByDiscussion } = await import('../kpi')
    const result = await getCostByDiscussion(5, 14)
    expect(mockJsonRpc).toHaveBeenCalledWith('cost.by_discussion', { top: 5, days: 14 })
    expect(result).toEqual(data)
  })

  it('getCostByDiscussion uses default top of 10 and default days of 90', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { getCostByDiscussion } = await import('../kpi')
    await getCostByDiscussion()
    expect(mockJsonRpc).toHaveBeenCalledWith('cost.by_discussion', { top: 10, days: 90 })
  })
})

// Verify kpi.ts does NOT define its own auth helpers
describe('kpi.ts — no duplicate auth helpers', () => {
  it('module source does not contain getBaseUrl, getToken, or rpcCall definitions', async () => {
    const fs = await import('fs')
    const path = await import('path')
    const src = fs.readFileSync(path.resolve(__dirname, '../kpi.ts'), 'utf-8')
    expect(src).not.toMatch(/function getBaseUrl/)
    expect(src).not.toMatch(/function getToken/)
    expect(src).not.toMatch(/function rpcCall/)
    expect(src).not.toMatch(/function getRpcUrl/)
  })

  it('module imports jsonRpc from client', async () => {
    const fs = await import('fs')
    const path = await import('path')
    const src = fs.readFileSync(path.resolve(__dirname, '../kpi.ts'), 'utf-8')
    expect(src).toContain("from './client'")
  })
})
