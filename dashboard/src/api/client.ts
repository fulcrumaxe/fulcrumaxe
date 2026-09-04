import { resolveRestBaseUrl, resolveRpcBaseUrl } from '../lib/backendTarget'
import type {
  Project,
  HealthStatus,
  BudgetStatus,
  KpiSummary,
  VelocityPoint,
  CycleTimeBreakdown,
  Agent,
  ControlSettings,
  ControlGate,
  AuditEntry,
  SpawnQueueStatus,
  Session,
  InnovateState,
  Idea,
  IdeasResponse,
  DiscussionListResult,
  DiscussionGetResult,
  DiscussionStatus,
  CircuitBreakerSummary,
  CircuitBreakerTransition,
  ClaudeSpawnSummary,
  SpawnBlockEvent,
  PerDiscussionCost,
} from './types'

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

// Thrown by jsonRpc() instead of issuing a /rpc request when no RPC token can
// be resolved (localStorage empty and /api/config hasn't produced one yet).
// Before this existed, an unresolved token fell through to a real fetch with
// `Authorization: Bearer` and nothing after it — which the server reads as an
// auth failure (401) rather than "the client has no credentials yet" (D#2316
// finding 4). Callers can catch this specifically to show a
// config-unavailable state instead of a generic transport/auth error.
export class ConfigUnavailableError extends Error {
  constructor(message = 'Dashboard config is unavailable — no RPC token resolved yet') {
    super(message)
    this.name = 'ConfigUnavailableError'
  }
}

// ApiClient wraps REST calls through _fetchWithAuthRetry (defined below after the
// config-cache helpers) so get/post/put/patch all benefit from the same ensureConfig()
// + 401-retry logic that jsonRpc() uses.  del() is a fire-and-forget mutation that
// doesn't carry auth today — kept simple intentionally.
//
// baseUrlGetter is called on every request so switching the backend target
// (via backendTarget.ts) is reflected immediately without reloading the page.
class ApiClient {
  private baseUrlGetter: () => string

  constructor(baseUrlGetter: string | (() => string)) {
    if (typeof baseUrlGetter === 'string') {
      const fixed = baseUrlGetter.replace(/\/$/, '')
      this.baseUrlGetter = () => fixed
    } else {
      this.baseUrlGetter = baseUrlGetter
    }
  }

  private get baseUrl(): string {
    return this.baseUrlGetter().replace(/\/$/, '')
  }

  async get<T>(path: string): Promise<T> {
    const res = await _fetchWithAuthRetry(`${this.baseUrl}${path}`, {})
    if (!res.ok) throw new ApiError(res.status, await res.text())
    return res.json() as Promise<T>
  }

  async post<T>(path: string, body: unknown): Promise<T> {
    const res = await _fetchWithAuthRetry(`${this.baseUrl}${path}`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new ApiError(res.status, await res.text())
    return res.json() as Promise<T>
  }

  async put<T>(path: string, body: unknown): Promise<T> {
    const res = await _fetchWithAuthRetry(`${this.baseUrl}${path}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new ApiError(res.status, await res.text())
    return res.json() as Promise<T>
  }

  async patch<T>(path: string, body: unknown): Promise<T> {
    const res = await _fetchWithAuthRetry(`${this.baseUrl}${path}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new ApiError(res.status, await res.text())
    return res.json() as Promise<T>
  }

  async del(path: string): Promise<void> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
    })
    if (!res.ok) throw new ApiError(res.status, await res.text())
  }
}

// Singleton client — base URL is resolved dynamically on each request so
// switching the backend target (backendTarget.ts) takes effect immediately.
const client = new ApiClient(resolveRestBaseUrl)

// ---------------------------------------------------------------------------
// Active-project accessor
// ---------------------------------------------------------------------------
// The context lives in React land (ActiveProjectContext.tsx) but the API
// client is a plain module.  We bridge the two with a simple module-level
// getter that the context sets on mount / whenever the active project changes.
//
// Why not use a React context directly here?  jsonRpc() is called outside
// React components (e.g. in poll loops, in kpi.ts) so we can't call hooks.
// A module-level getter is the lightest seam without coupling client.ts to
// React internals.

let _activeProjectGetter: (() => string | null) = () => {
  // Default: read localStorage directly so the getter works even before
  // the React context has mounted (e.g. during the very first jsonRpc call).
  try {
    return localStorage.getItem('af.activeProject')
  } catch {
    return null
  }
}

/**
 * Register a getter that returns the currently active project name.
 *
 * Called once by ActiveProjectProvider on mount.  The getter is a stable
 * function that reads from React state so it's always fresh without re-renders.
 */
export function setActiveProjectGetter(getter: () => string | null): void {
  _activeProjectGetter = getter
}

/** Return the currently active project name (may be null before context mounts). */
export function getActiveProject(): string | null {
  return _activeProjectGetter()
}

// Resource-specific API clients

export const projectsApi = {
  list: () => client.get<Project[]>('/api/projects'),
  get: (id: string) => client.get<Project>(`/api/projects/${id}`),
  create: (data: Partial<Project>) => client.post<Project>('/api/projects', data),
  update: (id: string, data: Partial<Project>) => client.patch<Project>(`/api/projects/${id}`, data),
  delete: (id: string) => client.del(`/api/projects/${id}`),
}

export const kpiApi = {
  summary: (projectId: string) => client.get<KpiSummary>(`/api/projects/${projectId}/kpi`),
  velocity: (projectId: string) => client.get<VelocityPoint[]>(`/api/projects/${projectId}/kpi/velocity`),
  cycleTime: (projectId: string) => client.get<CycleTimeBreakdown[]>(`/api/projects/${projectId}/kpi/cycle-time`),
}

export const agentsApi = {
  list: (projectId: string) => client.get<Agent[]>(`/api/projects/${projectId}/agents`),
  get: (projectId: string, agentId: string) =>
    client.get<Agent>(`/api/projects/${projectId}/agents/${agentId}`),
}

export const controlApi = {
  getSettings: (projectId: string) => client.get<ControlSettings>(`/api/projects/${projectId}/control`),
  updateSettings: (projectId: string, settings: Partial<ControlSettings>) =>
    client.patch<ControlSettings>(`/api/projects/${projectId}/control`, settings),
  getGates: (projectId: string) => client.get<ControlGate[]>(`/api/projects/${projectId}/control/gates`),
  getAudit: (projectId: string) => client.get<AuditEntry[]>(`/api/projects/${projectId}/control/audit`),
}

export const budgetApi = {
  status: (projectId: string) => client.get<BudgetStatus>(`/api/projects/${projectId}/budget/status`),
  cost: (projectId: string) => client.get<{ total: number; breakdown: Record<string, number> }>(
    `/api/projects/${projectId}/cost`
  ),
}

export const spawnQueueApi = {
  status: (projectId: string) => client.get<SpawnQueueStatus>(`/api/projects/${projectId}/spawn-queue`),
  pending: (projectId: string) =>
    client.get<SpawnQueueStatus['pending']>(`/api/projects/${projectId}/spawn-queue/pending`),
  active: (projectId: string) =>
    client.get<SpawnQueueStatus['active']>(`/api/projects/${projectId}/spawn-queue/active`),
}

export const sessionsApi = {
  current: () => client.get<Session>('/api/sessions/current'),
  list: () => client.get<Session[]>('/api/sessions'),
}

export const healthApi = {
  status: () => client.get<HealthStatus>('/health'),
  loop: () => client.get<HealthStatus['loop']>('/health/loop'),
  modules: () => client.get<Record<string, boolean>>('/health/modules'),
}

export const innovateApi = {
  getState: () => client.get<InnovateState>('/api/innovate'),
  toggle: (enabled: boolean) =>
    client.post<InnovateState>('/api/innovate/toggle', { enabled }),
  tick: () =>
    client.post<{ run_id: string; iteration_count: number }>('/api/innovate/tick', {}),
}

export const ideasApi = {
  list: () => client.get<IdeasResponse>('/api/ideas'),
  upvote: (id: string) => client.post<Idea>(`/api/ideas/${id}/upvote`, {}),
  dismiss: (id: string) => client.post<Idea>(`/api/ideas/${id}/dismiss`, {}),
  promote: (id: string) => client.post<Idea>(`/api/ideas/${id}/promote`, {}),
}

// ---- /api/config auto-discovery -------------------------------------------
// The dashboard auto-discovers the JSON-RPC URL and token from the backend's
// /api/config endpoint (served by backend/api.py, restricted to localhost).
// Result is cached in memory so we only fetch once per page load.
//
// TOKEN HYGIENE: The config response includes rpcToken in plaintext. This is
// acceptable because /api/config is localhost-only (enforced by backend/api.py).
// NEVER log the config object, data.rpcToken, or any derived token value to
// console — it would expose the bearer token in browser devtools. In production,
// the token is a real secret loaded from .autonomous-team/dashboard-token.

interface DashboardConfig {
  rpcBaseUrl: string
  rpcToken: string
  dashboardVersion: string
}

let _configCache: DashboardConfig | null = null
// Retry-tolerant promise: cleared on failure so subsequent calls can retry.
// Set to a resolved-success promise on cache hit so concurrent callers dedup.
let _configFetchPromise: Promise<DashboardConfig | null> | null = null
let _configRetryCount = 0
const CONFIG_MAX_RETRIES = 3

async function _fetchDashboardConfig(): Promise<DashboardConfig | null> {
  if (_configCache) return _configCache
  if (_configFetchPromise) return _configFetchPromise

  _configFetchPromise = (async () => {
    try {
      const res = await fetch('/api/config', { credentials: 'same-origin' })
      if (!res.ok) {
        // Don't permanently cache failures — clear so next call can retry
        _configFetchPromise = null
        return null
      }
      const data = await res.json() as DashboardConfig
      if (data.rpcBaseUrl && data.rpcToken) {
        _configCache = data
        // Reset the retry budget on success. Without this, a page that
        // exhausted CONFIG_MAX_RETRIES during a rough start (before this
        // fetch finally succeeded) would still be carrying a maxed-out
        // counter, and _invalidateConfigCache() is the only other place that
        // resets it — success wasn't resetting it at all (D#2316 finding 4).
        _configRetryCount = 0
        return data
      }
      _configFetchPromise = null
      return null
    } catch {
      // Network error — clear promise so a later call can retry
      _configFetchPromise = null
      return null
    }
  })()

  return _configFetchPromise
}

// ensureConfig fetches /api/config, retrying up to CONFIG_MAX_RETRIES times
// when the result is null (network failure, or 200 with an empty token during
// dashboard start-up).  Stops as soon as _configCache is populated.
async function ensureConfig(): Promise<void> {
  while (!_configCache && _configRetryCount < CONFIG_MAX_RETRIES) {
    _configRetryCount++
    await _fetchDashboardConfig()
  }
  // Exhausting the retry budget must only bound *this* burst of attempts, not
  // disable every future call for the rest of the page's life. Before this
  // reset, _configRetryCount stayed at CONFIG_MAX_RETRIES forever once hit,
  // so ensureConfig() became a permanent no-op — a poller kept calling it on
  // every tick, but the while loop's guard was already false, so it never
  // fetched /api/config again even after the backend came back (D#2316
  // finding 4: "401s for the life of the page"). Resetting here means the
  // *next* call gets a fresh set of retries.
  if (!_configCache) {
    _configRetryCount = 0
  }
}

// Discard the cached config so the next ensureConfig() call re-fetches it.
// Called by jsonRpc() and _fetchWithAuthRetry() on an unexpected 401 so the
// browser picks up a rotated token.
export function _invalidateConfigCache(): void {
  _configCache = null
  _configFetchPromise = null
  _configRetryCount = 0
}

// Invalidate the cache and re-fetch /api/config, returning the resolved token.
// Used by callers outside this module that build their own request (e.g.
// JsonRpcClient in lib/jsonrpcClient.ts, used by the Loop Controller's SSE tail)
// and so can't go through jsonRpc()/_fetchWithAuthRetry()'s built-in 401 retry.
export async function refreshRpcToken(): Promise<string> {
  _invalidateConfigCache()
  await ensureConfig()
  return getRpcToken()
}

// Resolve the backend base URL — precedence order:
//   1. VITE_API_URL build-time env (CI / production builds)
//   2. af_dashboard_base_url stored in localStorage (E2E tests, dev overrides)
//   3. /api/config endpoint (populated via ensureConfig() before first jsonRpc call)
//   4. window.location.origin fallback (same-origin dev server via Vite proxy)
export function getRpcBaseUrl(): string {
  // When the TS backend is selected, route all RPC traffic there too.
  const tsBase = resolveRpcBaseUrl()
  if (tsBase) return tsBase

  // Python backend: use the existing config-cache / localStorage logic.
  const viteUrl = ((import.meta as unknown as { env: { VITE_API_URL?: string } }).env.VITE_API_URL)
  if (viteUrl) return viteUrl.replace(/\/$/, '')
  const stored = typeof localStorage !== 'undefined'
    ? localStorage.getItem('af_dashboard_base_url')
    : null
  if (stored) return stored.replace(/\/$/, '')
  // Use cached config if already fetched; otherwise fall back to origin
  if (_configCache?.rpcBaseUrl) return _configCache.rpcBaseUrl.replace(/\/$/, '')
  return (typeof window !== 'undefined' ? window.location.origin : '').replace(/\/$/, '')
}

export function getRpcToken(): string {
  // 1. localStorage override (E2E tests, dev overrides)
  const stored = typeof localStorage !== 'undefined' ? localStorage.getItem('af_dashboard_token') : null
  if (stored) return stored
  // 2. Cached config from /api/config (populated by ensureConfig)
  if (_configCache?.rpcToken) return _configCache.rpcToken
  return ''
}

// Warm the config cache on module load (non-blocking — resolves before first RPC call)
_fetchDashboardConfig().catch(() => { /* ignored — ensureConfig retries on demand */ })

// Shared 401-retry helper used by both jsonRpc() and the REST helpers (ApiClient).
//
// Awaits ensureConfig() when no localStorage token is set so the cache is warm
// before the first request.  On a 401 when using a config-supplied token,
// invalidates the cache, re-fetches /api/config, and retries the request once.
// This mirrors the identical pattern in jsonRpc() so both paths stay in sync.
//
// ApiClient is defined earlier in this file, but its methods call this function
// at runtime — not at class definition time — so there is no hoisting issue.
async function _fetchWithAuthRetry(url: string, init: RequestInit): Promise<Response> {
  const lsToken = typeof localStorage !== 'undefined'
    ? localStorage.getItem('af_dashboard_token')
    : null
  if (!lsToken) {
    await ensureConfig()
  }

  const doFetch = (): Promise<Response> => {
    const token = getRpcToken()
    return fetch(url, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    })
  }

  let res = await doFetch()

  // On 401 with a config-supplied token: the token may have rotated or the
  // server restarted.  Invalidate, re-fetch config, retry once.
  if (res.status === 401 && !lsToken) {
    _invalidateConfigCache()
    await ensureConfig()
    res = await doFetch()
  }

  return res
}

// JSON-RPC transport for methods on backend/server.py
// Always awaits ensureConfig() when localStorage has no token, so fresh clients
// pick up the rpcToken from /api/config before the first POST /rpc.
//
// Project scoping: when an active project is set (via ActiveProjectContext),
// it is automatically injected as a "project" field into every RPC params
// object.  Methods that are scope-aware (discussions.list, kpi.*, stats.*)
// use this to read from the right project's data store.  Methods that ignore
// it (fleet.projects, circuit_breaker.*) simply discard the field.
export async function jsonRpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  // Skip /api/config fetch when localStorage already has a token (saves a round-trip)
  const lsToken = typeof localStorage !== 'undefined'
    ? localStorage.getItem('af_dashboard_token')
    : null
  if (!lsToken) {
    await ensureConfig()
  }

  // Inject active project into params unless the caller already set it
  const activeProject = getActiveProject()
  const scopedParams = activeProject && !('project' in params)
    ? { ...params, project: activeProject }
    : params

  const doFetch = async (): Promise<Response> => {
    const token = getRpcToken()
    // Never issue a request carrying `Authorization: Bearer` with nothing
    // after it — the server reads that as a failed auth attempt (401) when
    // the real problem is "the client has no token yet" (D#2316 finding 4).
    // Fail with a distinguishable error instead so callers can tell the two
    // apart.
    if (!token) {
      throw new ConfigUnavailableError()
    }
    return fetch(`${getRpcBaseUrl()}/rpc`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params: scopedParams }),
    })
  }

  let res = await doFetch()

  // On 401: the config-supplied token may have gone stale (server restart, token
  // rotation, or a start-up race where /api/config returned an empty token on the
  // first request).  Invalidate the cache, re-fetch config, and retry once.
  if (res.status === 401 && !lsToken) {
    // Fire-and-forget telemetry before retrying so we can observe how often
    // the cold-start auth race fires in production.  Best-effort — failures ignored.
    fetch(`${getRpcBaseUrl()}/rpc`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getRpcToken()}` },
      body: JSON.stringify({ jsonrpc: '2.0', id: 0, method: 'auth_retry.record', params: {} }),
    }).catch(() => { /* best-effort */ })
    _invalidateConfigCache()
    await ensureConfig()
    res = await doFetch()
  }

  if (!res.ok) throw new ApiError(res.status, await res.text())
  const body = await res.json() as { result?: T; error?: { message: string; code: number } }
  if (body.error) throw new ApiError(body.error.code, body.error.message)
  return body.result as T
}

export const circuitBreakerApi = {
  summary: () => jsonRpc<CircuitBreakerSummary>('circuit_breaker.summary'),
  history: (role: string, limit = 20) =>
    jsonRpc<CircuitBreakerTransition[]>('circuitBreaker.history', { role, limit }),
}

export const claudeSpawnTrackerApi = {
  summary: () => jsonRpc<ClaudeSpawnSummary>('claude_spawn_tracker.summary'),
}

export const discussionsApi = {
  list: (params: {
    status?: DiscussionStatus | '*'
    q?: string
    max_age_days?: number
    limit?: number
    cursor?: string
  } = {}) => jsonRpc<DiscussionListResult>('discussions.list', params as Record<string, unknown>),

  get: (number: number) =>
    jsonRpc<DiscussionGetResult>('discussions.get', { number }),
}

export const costApi = {
  perDiscussion: (discussion: number) =>
    jsonRpc<PerDiscussionCost | null>('cost.per_discussion', { discussion }),
}

export async function apiSpawnBlocks(limit = 10): Promise<SpawnBlockEvent[]> {
  const r = await fetch(`${getRpcBaseUrl()}/api/spawn-blocks?limit=${limit}`)
  if (!r.ok) throw new ApiError(r.status, `spawn-blocks ${r.status}`)
  return r.json() as Promise<SpawnBlockEvent[]>
}

export { client as default }
