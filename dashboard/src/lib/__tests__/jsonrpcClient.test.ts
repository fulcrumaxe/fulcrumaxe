import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { AuthError } from '../../types/loopController'
import { JsonRpcClient } from '../jsonrpcClient'
import { refreshRpcToken } from '../../api/client'

// jsonrpcClient.ts imports refreshRpcToken from api/client — mock it so we can
// simulate the startup-race scenario (first /rpc call 401s before /api/config
// has resolved a token, refreshRpcToken() returns the freshly-fetched one).
vi.mock('../../api/client', () => ({
  refreshRpcToken: vi.fn(),
}))

const mockFetch = vi.fn()
// eslint-disable-next-line @typescript-eslint/no-explicit-any
;(globalThis as any).fetch = mockFetch

const mockRefreshRpcToken = refreshRpcToken as ReturnType<typeof vi.fn>

describe('JsonRpcClient 401 retry', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    mockRefreshRpcToken.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('retries once with a fresh token on 401 and succeeds', async () => {
    mockRefreshRpcToken.mockResolvedValue('fresh-token')

    mockFetch.mockImplementation(async (_url: string, opts?: RequestInit) => {
      const auth = (opts?.headers as Record<string, string>)?.['Authorization'] ?? ''
      if (auth === 'Bearer stale-token') {
        return { status: 401, ok: false, json: async () => ({}) }
      }
      return {
        status: 200,
        ok: true,
        json: async () => ({ result: { loops: [] }, id: 1, jsonrpc: '2.0' }),
      }
    })

    const client = new JsonRpcClient('http://localhost:8765', 'stale-token')
    const result = await client.call('loop.list', {})

    expect(result).toEqual({ loops: [] })
    expect(mockFetch).toHaveBeenCalledTimes(2)
    expect(client.token).toBe('fresh-token')
  })

  it('throws AuthError when the retry also 401s', async () => {
    mockRefreshRpcToken.mockResolvedValue('still-stale-token')
    mockFetch.mockResolvedValue({ status: 401, ok: false, json: async () => ({}) })

    const client = new JsonRpcClient('http://localhost:8765', 'stale-token')

    await expect(client.call('loop.list', {})).rejects.toThrow(AuthError)
    expect(mockFetch).toHaveBeenCalledTimes(2)
  })

  it('does not retry when refreshRpcToken returns the same token', async () => {
    mockRefreshRpcToken.mockResolvedValue('stale-token')
    mockFetch.mockResolvedValue({ status: 401, ok: false, json: async () => ({}) })

    const client = new JsonRpcClient('http://localhost:8765', 'stale-token')

    await expect(client.call('loop.list', {})).rejects.toThrow(AuthError)
    // Only the initial request — no point retrying with an identical token.
    expect(mockFetch).toHaveBeenCalledTimes(1)
  })
})
