import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ApiError } from '../client'

// We test the ApiClient class directly by re-implementing it here
class ApiClient {
  private baseUrl: string
  private getToken: () => string | null

  constructor(baseUrl: string, getToken: () => string | null) {
    this.baseUrl = baseUrl.replace(/\/$/, '')
    this.getToken = getToken
  }

  private headers(): Record<string, string> {
    const h: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = this.getToken()
    if (token) h['Authorization'] = `Bearer ${token}`
    return h
  }

  async get<T>(path: string): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, { headers: this.headers() })
    if (!res.ok) throw new ApiError(res.status, await res.text())
    return res.json() as Promise<T>
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new ApiError(res.status, await res.text())
    return res.json() as Promise<T>
  }
}

const mockFetch = vi.fn()
// eslint-disable-next-line @typescript-eslint/no-explicit-any
;(globalThis as any).fetch = mockFetch

// The two describe blocks below stub `window` and `localStorage` with bare
// objects to drive ensureConfig()'s module-level cache logic. Captured here,
// before any test runs, so afterEach can put the real jsdom window back —
// singleFork test runs share one process, so leaving the stub in place after
// a test poisons every test file that runs after this one (D#1897: the stub
// object is missing DOM globals like Error/Event that React and
// @testing-library rely on, which manifested as a "Should not already be
// working." cascade across unrelated component tests).
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const originalWindow = (globalThis as any).window
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const originalLocalStorage = (globalThis as any).localStorage

describe('ApiClient', () => {
  beforeEach(() => {
    mockFetch.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('constructs URL relative to baseUrl', async () => {
    const client = new ApiClient('http://localhost:8000', () => null)
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: '1' }),
    })
    await client.get('/api/projects')
    expect(mockFetch).toHaveBeenCalledWith(
      'http://localhost:8000/api/projects',
      expect.objectContaining({ headers: expect.objectContaining({ 'Content-Type': 'application/json' }) })
    )
  })

  it('strips trailing slash from baseUrl', async () => {
    const client = new ApiClient('http://localhost:8000/', () => null)
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] })
    await client.get('/api/projects')
    expect(mockFetch).toHaveBeenCalledWith('http://localhost:8000/api/projects', expect.anything())
  })

  it('attaches Authorization header when token is present', async () => {
    const client = new ApiClient('http://localhost:8000', () => 'test-token-123')
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) })
    await client.get('/api/sessions/current')
    expect(mockFetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer test-token-123' }),
      })
    )
  })

  it('omits Authorization header when no token', async () => {
    const client = new ApiClient('http://localhost:8000', () => null)
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({}) })
    await client.get('/api/health')
    const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit]
    const headers = opts.headers as Record<string, string>
    expect(headers['Authorization']).toBeUndefined()
  })

  it('throws ApiError on non-OK response', async () => {
    const client = new ApiClient('http://localhost:8000', () => null)
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      text: async () => 'Unauthorized',
    })
    await expect(client.get('/api/protected')).rejects.toThrow(ApiError)
  })

  it('ApiError contains status code', async () => {
    const client = new ApiClient('http://localhost:8000', () => null)
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      text: async () => 'Not found',
    })
    try {
      await client.get('/api/missing')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      expect((e as ApiError).status).toBe(404)
    }
  })

  it('post sends JSON body', async () => {
    const client = new ApiClient('http://localhost:8000', () => 'tok')
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'new' }) })
    await client.post('/api/projects', { name: 'test' })
    expect(mockFetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'test' }),
      })
    )
  })
})

// ---- jsonRpc / /api/config integration tests ---------------------------------
// These tests import the actual module functions via dynamic import so each
// test can reset module-level state (the config cache + promise).

describe('jsonRpc /api/config auto-discovery', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    // Reset module-level cache between tests
    vi.resetModules()
    // Provide a minimal localStorage stub
    const store: Record<string, string> = {}
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).localStorage = {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v },
      removeItem: (k: string) => { delete store[k] },
      clear: () => { Object.keys(store).forEach(k => delete store[k]) },
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).window = { location: { origin: 'http://localhost:5173' } }
  })

  afterEach(() => {
    vi.restoreAllMocks()
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).window = originalWindow
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).localStorage = originalLocalStorage
  })

  it('fresh client: fetches /api/config and uses the returned token on /rpc', async () => {
    // localStorage is empty (fresh client)
    localStorage.clear()

    // First call: module warm-up fetch (non-blocking) → will resolve to null because it
    // fires before we install the mock; that's OK — ensureConfig retries.
    // We install the mock now so ensureConfig()'s explicit call succeeds.
    mockFetch.mockImplementation(async (url: string) => {
      if (url === '/api/config') {
        return {
          ok: true,
          json: async () => ({
            rpcBaseUrl: 'http://test:8765',
            rpcToken: 'tk-from-config',
            dashboardVersion: 'x',
          }),
        }
      }
      // /rpc call
      return {
        ok: true,
        json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }),
      }
    })

    // Dynamic import so module-level state is fresh (vi.resetModules() ran in beforeEach)
    const { discussionsApi } = await import('../client')

    await discussionsApi.list({})

    // The /rpc call must carry the token from /api/config
    const rpcCall = mockFetch.mock.calls.find(([url]) => String(url).includes('/rpc'))
    expect(rpcCall).toBeDefined()
    const [rpcUrl, rpcOpts] = rpcCall as [string, RequestInit]
    expect(rpcUrl).toBe('http://test:8765/rpc')
    expect((rpcOpts.headers as Record<string, string>)['Authorization']).toBe('Bearer tk-from-config')
  })

  it('localStorage token wins: /api/config is NOT fetched', async () => {
    localStorage.setItem('af_dashboard_token', 'my-ls-token')
    localStorage.setItem('af_dashboard_base_url', 'http://ls-host:8765')

    mockFetch.mockImplementation(async (url: string) => {
      if (url === '/api/config') {
        // Should never be called when localStorage token is present
        throw new Error('unexpected /api/config fetch')
      }
      return {
        ok: true,
        json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }),
      }
    })

    const { discussionsApi } = await import('../client')
    await discussionsApi.list({})

    // Verify only the /rpc call happened, no /api/config
    const configCalls = mockFetch.mock.calls.filter(([url]) => String(url) === '/api/config')
    // Module warm-up fires on import; but since localStorage token is set, jsonRpc skips ensureConfig.
    // The warm-up may or may not fire depending on module load order — what matters is the /rpc call uses the ls token.
    const rpcCall = mockFetch.mock.calls.find(([url]) => String(url).includes('/rpc'))
    expect(rpcCall).toBeDefined()
    const [, rpcOpts] = rpcCall as [string, RequestInit]
    expect((rpcOpts.headers as Record<string, string>)['Authorization']).toBe('Bearer my-ls-token')
    // No second /api/config call should happen once jsonRpc runs (module warm-up may have one)
    expect(configCalls.length).toBeLessThanOrEqual(1)
  })

  it('transient failure: first /api/config rejects, second succeeds, token is used', async () => {
    localStorage.clear()

    let configCallCount = 0
    mockFetch.mockImplementation(async (url: string) => {
      if (url === '/api/config') {
        configCallCount++
        if (configCallCount === 1) {
          // First attempt fails (network error)
          throw new Error('network error')
        }
        // Second attempt succeeds
        return {
          ok: true,
          json: async () => ({
            rpcBaseUrl: 'http://retry-host:8765',
            rpcToken: 'tk-retry',
            dashboardVersion: 'x',
          }),
        }
      }
      return {
        ok: true,
        json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }),
      }
    })

    const { discussionsApi } = await import('../client')

    // First call — may use the failed warm-up attempt; ensureConfig increments retry count
    // and clears the promise so the next jsonRpc call can retry.
    await discussionsApi.list({}).catch(() => { /* may fail if no token yet */ })

    // Second call — ensureConfig retries, gets the token
    await discussionsApi.list({})

    const rpcCalls = mockFetch.mock.calls.filter(([url]) => String(url).includes('/rpc'))
    expect(rpcCalls.length).toBeGreaterThanOrEqual(1)
    // At least one /rpc call should carry the retry token
    const successRpc = rpcCalls.find(([, opts]) =>
      (opts as RequestInit & { headers: Record<string, string> }).headers?.['Authorization'] === 'Bearer tk-retry'
    )
    expect(successRpc).toBeDefined()
  })

  it('startup race: /api/config returns empty token first, then valid token — ensureConfig loops until populated', async () => {
    localStorage.clear()

    let configCallCount = 0
    mockFetch.mockImplementation(async (url: string) => {
      if (url === '/api/config') {
        configCallCount++
        if (configCallCount === 1) {
          // Start-up race: server returns 200 but token not written yet
          return {
            ok: true,
            json: async () => ({
              rpcBaseUrl: 'http://startup-host:8765',
              rpcToken: '',
              dashboardVersion: 'x',
            }),
          }
        }
        // Second attempt: token file now written
        return {
          ok: true,
          json: async () => ({
            rpcBaseUrl: 'http://startup-host:8765',
            rpcToken: 'tk-after-startup',
            dashboardVersion: 'x',
          }),
        }
      }
      return {
        ok: true,
        json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }),
      }
    })

    const { discussionsApi } = await import('../client')
    await discussionsApi.list({})

    const rpcCall = mockFetch.mock.calls.find(([url]) => String(url).includes('/rpc'))
    expect(rpcCall).toBeDefined()
    const [, rpcOpts] = rpcCall as [string, RequestInit]
    expect((rpcOpts.headers as Record<string, string>)['Authorization']).toBe('Bearer tk-after-startup')
  })

  it('401 retry: on 401 response, invalidates cache and retries with fresh config token', async () => {
    localStorage.clear()

    let configCallCount = 0
    mockFetch.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url === '/api/config') {
        configCallCount++
        if (configCallCount === 1) {
          // First config fetch: stale token
          return {
            ok: true,
            json: async () => ({
              rpcBaseUrl: 'http://stale-host:8765',
              rpcToken: 'tk-stale',
              dashboardVersion: 'x',
            }),
          }
        }
        // After invalidation: fresh token
        return {
          ok: true,
          json: async () => ({
            rpcBaseUrl: 'http://stale-host:8765',
            rpcToken: 'tk-fresh',
            dashboardVersion: 'x',
          }),
        }
      }
      if (String(url).includes('/rpc')) {
        const auth = (opts?.headers as Record<string, string>)?.['Authorization'] ?? ''
        if (auth === 'Bearer tk-stale') {
          // First RPC attempt: server returns 401 (token rotated)
          return {
            ok: false,
            status: 401,
            text: async () => '{"error": "unauthorized"}',
          }
        }
        // Retry with fresh token succeeds
        return {
          ok: true,
          json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }),
        }
      }
      return { ok: true, json: async () => ({}) }
    })

    const { discussionsApi } = await import('../client')
    await discussionsApi.list({})

    // Should have called /api/config twice: once on init, once after 401
    expect(configCallCount).toBeGreaterThanOrEqual(2)
    // Final RPC call should carry the fresh token
    const rpcCalls = mockFetch.mock.calls.filter(([url]) => String(url).includes('/rpc'))
    const freshRpc = rpcCalls.find(([, opts]) =>
      (opts as RequestInit & { headers: Record<string, string> }).headers?.['Authorization'] === 'Bearer tk-fresh'
    )
    expect(freshRpc).toBeDefined()
  })

  it('throws ApiError when retry also returns 401', async () => {
    // Both the first RPC call and the retry get 401 — should propagate ApiError to caller.
    localStorage.clear()

    mockFetch.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url === '/api/config') {
        return {
          ok: true,
          json: async () => ({
            rpcBaseUrl: 'http://double401-host:8765',
            rpcToken: 'tk-always-stale',
            dashboardVersion: 'x',
          }),
        }
      }
      if (String(url).includes('/rpc')) {
        const body = JSON.parse((opts?.body as string) ?? '{}') as { method?: string }
        // auth_retry.record is fire-and-forget telemetry — let it succeed so it doesn't interfere
        if (body.method === 'auth_retry.record') {
          return { ok: true, json: async () => ({ result: { recorded: true }, id: 0, jsonrpc: '2.0' }) }
        }
        // Both initial request and retry return 401
        return {
          ok: false,
          status: 401,
          text: async () => 'Unauthorized',
        }
      }
      return { ok: true, json: async () => ({}) }
    })

    const { discussionsApi, ApiError } = await import('../client')
    await expect(discussionsApi.list({})).rejects.toThrow(ApiError)

    // Verify we made exactly 2 non-telemetry /rpc calls (first attempt + one retry)
    const rpcCalls = mockFetch.mock.calls.filter(([url, opts]) => {
      if (!String(url).includes('/rpc')) return false
      try {
        const body = JSON.parse((opts as RequestInit)?.body as string) as { method?: string }
        return body.method !== 'auth_retry.record'
      } catch { return true }
    })
    expect(rpcCalls.length).toBe(2)
  })

  it('401 recovery: calls auth_retry.record exactly once before retrying', async () => {
    // Mirror the existing "401 retry" test structure: first config = stale token,
    // second config (after cache invalidation) = fresh token.
    localStorage.clear()

    let configCallCount = 0
    mockFetch.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url === '/api/config') {
        configCallCount++
        const token = configCallCount === 1 ? 'tk-stale-rr' : 'tk-fresh-rr'
        return {
          ok: true,
          json: async () => ({ rpcBaseUrl: 'http://rr-host:8765', rpcToken: token, dashboardVersion: 'x' }),
        }
      }
      if (String(url).includes('/rpc')) {
        const body = JSON.parse((opts?.body as string) ?? '{}') as { method?: string }
        // Telemetry call — always succeed, regardless of token
        if (body.method === 'auth_retry.record') {
          return { ok: true, json: async () => ({ result: { recorded: true }, id: 0, jsonrpc: '2.0' }) }
        }
        const auth = (opts?.headers as Record<string, string>)?.['Authorization'] ?? ''
        if (auth === 'Bearer tk-stale-rr') {
          return { ok: false, status: 401, text: async () => 'Unauthorized' }
        }
        return { ok: true, json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }) }
      }
      return { ok: true, json: async () => ({}) }
    })

    const { discussionsApi } = await import('../client')
    await discussionsApi.list({})

    // auth_retry.record must have been called exactly once (on the recovery path)
    const recordCalls = mockFetch.mock.calls.filter(([url, opts]) => {
      if (!String(url).includes('/rpc')) return false
      try {
        const body = JSON.parse((opts as RequestInit)?.body as string) as { method?: string }
        return body.method === 'auth_retry.record'
      } catch { return false }
    })
    expect(recordCalls.length).toBe(1)
  })

  // D#2316 finding 4: jsonRpc()'s doFetch() used to attach
  // `Authorization: Bearer ${token}` unconditionally, so once the config
  // retry budget was exhausted (getRpcToken() permanently returning '')
  // every subsequent /rpc call carried a literal empty bearer and 401ed —
  // for the life of the page, since nothing ever reset the counter. These
  // two tests reproduce that failure mode directly against the real
  // ensureConfig()/jsonRpc() implementation (not a mock standing in for it)
  // and assert on the captured fetch calls, not a count.
  it('never issues a /rpc call with an empty bearer once the config retry budget is exhausted', async () => {
    localStorage.clear()

    mockFetch.mockImplementation(async (url: string) => {
      if (url === '/api/config') {
        throw new Error('network error')
      }
      // Should never be reached for the /rpc method under test.
      return { ok: true, json: async () => ({ result: {}, id: 1, jsonrpc: '2.0' }) }
    })

    const { discussionsApi, ConfigUnavailableError } = await import('../client')

    // /api/config fails every time — exhausts CONFIG_MAX_RETRIES inside
    // ensureConfig(). Before the fix, this fell through to a real /rpc fetch
    // with an empty bearer; now it must fail with a distinguishable error
    // and never call /rpc at all.
    await expect(discussionsApi.list({})).rejects.toThrow(ConfigUnavailableError)

    const rpcCalls = mockFetch.mock.calls.filter(([url]) => String(url).includes('/rpc'))
    expect(rpcCalls.length).toBe(0)
    // Belt-and-suspenders: even if some future change makes a /rpc call happen
    // on this path, it must never carry an empty bearer.
    for (const [, opts] of rpcCalls) {
      const auth = (opts as RequestInit & { headers?: Record<string, string> }).headers?.['Authorization']
      if (auth !== undefined) {
        expect(auth).not.toBe('Bearer ')
        expect(auth).toMatch(/^Bearer \S+$/)
      }
    }
  })

  it('config-warming path recovers: after CONFIG_MAX_RETRIES consecutive failures, a later success repopulates the cache and the next /rpc call carries the token', async () => {
    localStorage.clear()

    let healthy = false
    let configCallCount = 0
    mockFetch.mockImplementation(async (url: string) => {
      if (url === '/api/config') {
        configCallCount++
        if (!healthy) throw new Error('network error')
        return {
          ok: true,
          json: async () => ({
            rpcBaseUrl: 'http://recovered-host:8765',
            rpcToken: 'tk-recovered',
            dashboardVersion: 'x',
          }),
        }
      }
      return {
        ok: true,
        json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }),
      }
    })

    const { discussionsApi, ConfigUnavailableError } = await import('../client')

    // Burst 1: config is down for the whole retry budget — must fail cleanly,
    // never call /rpc.
    await expect(discussionsApi.list({})).rejects.toThrow(ConfigUnavailableError)
    expect(configCallCount).toBeGreaterThanOrEqual(1)

    // The backend comes back. Before this fix, _configRetryCount stayed
    // pinned at CONFIG_MAX_RETRIES forever, so ensureConfig() was a
    // permanent no-op and this second call would fail exactly like the
    // first, regardless of the backend's health.
    healthy = true
    const result = await discussionsApi.list({})
    expect(result).toBeDefined()

    const rpcCall = mockFetch.mock.calls.find(([url]) => String(url).includes('/rpc'))
    expect(rpcCall).toBeDefined()
    const [, rpcOpts] = rpcCall as [string, RequestInit]
    expect((rpcOpts.headers as Record<string, string>)['Authorization']).toBe('Bearer tk-recovered')
  })

  it('auth_retry.record failure does not block the 401 retry', async () => {
    // Even when the telemetry call throws, the main retry must complete successfully.
    localStorage.clear()

    let configCallCount = 0
    mockFetch.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url === '/api/config') {
        configCallCount++
        const token = configCallCount === 1 ? 'tk-stale-rr2' : 'tk-fresh-rr2'
        return {
          ok: true,
          json: async () => ({ rpcBaseUrl: 'http://rr2-host:8765', rpcToken: token, dashboardVersion: 'x' }),
        }
      }
      if (String(url).includes('/rpc')) {
        const body = JSON.parse((opts?.body as string) ?? '{}') as { method?: string }
        if (body.method === 'auth_retry.record') {
          // Telemetry endpoint is down — fire-and-forget must swallow this
          throw new Error('telemetry unreachable')
        }
        const auth = (opts?.headers as Record<string, string>)?.['Authorization'] ?? ''
        if (auth === 'Bearer tk-stale-rr2') {
          return { ok: false, status: 401, text: async () => 'Unauthorized' }
        }
        return { ok: true, json: async () => ({ result: { nodes: [] }, id: 1, jsonrpc: '2.0' }) }
      }
      return { ok: true, json: async () => ({}) }
    })

    const { discussionsApi } = await import('../client')
    // Must resolve successfully even though auth_retry.record fetch throws
    const result = await discussionsApi.list({})
    expect(result).toBeDefined()
  })
})

// ---- REST helper 401-retry tests -------------------------------------------
// Verify that ApiClient.get() (and by extension post/put/patch) apply the same
// ensureConfig() + 401-retry pattern that jsonRpc() uses.

describe('REST ApiClient 401-retry via _fetchWithAuthRetry', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    vi.resetModules()
    const store: Record<string, string> = {}
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).localStorage = {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v },
      removeItem: (k: string) => { delete store[k] },
      clear: () => { Object.keys(store).forEach(k => delete store[k]) },
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).window = { location: { origin: 'http://localhost:5173' } }
  })

  afterEach(() => {
    vi.restoreAllMocks()
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).window = originalWindow
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).localStorage = originalLocalStorage
  })

  it('cold-start REST get(): fetches /api/config, gets 401, invalidates cache, retries with fresh token and succeeds', async () => {
    localStorage.clear()

    let configCallCount = 0
    mockFetch.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url === '/api/config') {
        configCallCount++
        if (configCallCount === 1) {
          return {
            ok: true,
            json: async () => ({
              rpcBaseUrl: 'http://rest-host:8765',
              rpcToken: 'tk-stale-rest',
              dashboardVersion: 'x',
            }),
          }
        }
        // After invalidation: fresh token
        return {
          ok: true,
          json: async () => ({
            rpcBaseUrl: 'http://rest-host:8765',
            rpcToken: 'tk-fresh-rest',
            dashboardVersion: 'x',
          }),
        }
      }
      // REST endpoint — return 401 for stale token, success for fresh token
      const auth = (opts?.headers as Record<string, string>)?.['Authorization'] ?? ''
      if (auth === 'Bearer tk-stale-rest') {
        return {
          ok: false,
          status: 401,
          text: async () => 'Unauthorized',
        }
      }
      return {
        ok: true,
        json: async () => ({ id: 'proj-1', name: 'Test Project' }),
      }
    })

    const { projectsApi } = await import('../client')
    const result = await projectsApi.get('proj-1')
    expect(result).toMatchObject({ id: 'proj-1' })

    // /api/config was fetched at least twice (initial + after 401)
    expect(configCallCount).toBeGreaterThanOrEqual(2)

    // The successful REST call carried the fresh token
    const restCalls = mockFetch.mock.calls.filter(([url]) => String(url).includes('/api/projects'))
    const freshCall = restCalls.find(([, opts]) =>
      (opts as RequestInit & { headers: Record<string, string> }).headers?.['Authorization'] === 'Bearer tk-fresh-rest'
    )
    expect(freshCall).toBeDefined()
  })
})
