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

describe('pr.ts — delegates to client.ts jsonRpc', () => {
  beforeEach(() => {
    mockJsonRpc.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('fetchPrList calls jsonRpc with dashboard.pr_list', async () => {
    mockJsonRpc.mockResolvedValue([])
    const { fetchPrList } = await import('../pr')
    await fetchPrList()
    expect(mockJsonRpc).toHaveBeenCalledWith('dashboard.pr_list', {})
  })

  it('fetchPrDetail calls jsonRpc with dashboard.pr_detail and pr_number', async () => {
    const detail = { number: 42, title: 'Test PR' }
    mockJsonRpc.mockResolvedValue(detail)
    const { fetchPrDetail } = await import('../pr')
    const result = await fetchPrDetail(42)
    expect(mockJsonRpc).toHaveBeenCalledWith('dashboard.pr_detail', { pr_number: 42 })
    expect(result).toEqual(detail)
  })

  it('fetchPrList returns array from jsonRpc', async () => {
    const data = [{ number: 1, title: 'PR #1' }, { number: 2, title: 'PR #2' }]
    mockJsonRpc.mockResolvedValue(data)
    const { fetchPrList } = await import('../pr')
    const result = await fetchPrList()
    expect(result).toEqual(data)
  })
})

// Verify loop.ts does NOT define its own auth helpers
describe('pr.ts — no duplicate auth helpers', () => {
  it('module source does not contain getBaseUrl, getToken, or rpcCall definitions', async () => {
    const fs = await import('fs')
    const path = await import('path')
    const src = fs.readFileSync(path.resolve(__dirname, '../pr.ts'), 'utf-8')
    expect(src).not.toMatch(/function getBaseUrl/)
    expect(src).not.toMatch(/function getToken/)
    expect(src).not.toMatch(/function rpcCall/)
    expect(src).not.toMatch(/function getRpcUrl/)
  })

  it('module imports jsonRpc from client', async () => {
    const fs = await import('fs')
    const path = await import('path')
    const src = fs.readFileSync(path.resolve(__dirname, '../pr.ts'), 'utf-8')
    expect(src).toContain("from './client'")
  })
})
