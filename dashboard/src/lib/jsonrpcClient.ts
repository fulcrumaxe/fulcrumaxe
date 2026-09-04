// jsonrpcClient.ts — typed fetch-based JSON-RPC 2.0 client for the Loop Controller.

import {
  AuthError,
  JsonRpcError,
  MethodName,
  ParamsOf,
  ResultOf,
} from '../types/loopController'
import { refreshRpcToken } from '../api/client'

let _nextId = 1

export class JsonRpcClient {
  readonly baseUrl: string
  private _token: string

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl.replace(/\/$/, '')
    this._token = token
  }

  get token(): string {
    return this._token
  }

  private async post(token: string, body: string): Promise<Response> {
    try {
      return await fetch(`${this.baseUrl}/rpc`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body,
      })
    } catch (err) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Error({ cause }) is ES2022; cast for ES2020 target
      throw new (Error as any)(`Network error: ${err instanceof Error ? err.message : String(err)}`, { cause: err })
    }
  }

  // On startup, the dashboard's /api/config auto-discovery hasn't necessarily
  // resolved a token yet when this client is first constructed (e.g. the Loop
  // Controller's SSE tail fires on mount). The shared jsonRpc()/ApiClient path
  // in api/client.ts already retries once on 401 with a freshly-fetched token —
  // mirror that here so this client doesn't surface a transient first-load 401
  // as a hard error.
  async call<M extends MethodName>(method: M, params: ParamsOf<M>): Promise<ResultOf<M>> {
    const id = _nextId++
    const body = JSON.stringify({ jsonrpc: '2.0', id, method, params })

    let resp = await this.post(this._token, body)

    if (resp.status === 401) {
      const freshToken = await refreshRpcToken()
      if (freshToken && freshToken !== this._token) {
        this._token = freshToken
        resp = await this.post(freshToken, body)
      }
    }

    if (resp.status === 401) {
      throw new AuthError()
    }

    let json: unknown
    try {
      json = await resp.json()
    } catch {
      throw new Error(`Invalid JSON response (HTTP ${resp.status})`)
    }

    const rpc = json as Record<string, unknown>
    if (rpc.error) {
      const errPayload = rpc.error as { code: number; message: string; data?: unknown }
      throw new JsonRpcError(errPayload)
    }

    return rpc.result as ResultOf<M>
  }

  /**
   * Build a SSE URL with the token as a query param.
   *
   * NOTE — token-in-URL is an unavoidable browser limitation: the EventSource
   * API does not support custom request headers, so the bearer token must be
   * passed as a query parameter for SSE connections.
   *
   * This is acceptable for localhost dev use. For production deployments where
   * the token is a real secret, the recommended mitigations are:
   *   (a) Use a short-lived, SSE-scoped one-time token exchanged via a separate
   *       authenticated POST before opening the EventSource; or
   *   (b) Replace EventSource with fetch-based streaming (ReadableStream) which
   *       allows Authorization headers.
   *
   * Never log or store the URL returned by this method — the token is embedded
   * in the query string and visible in browser history and network logs.
   */
  sseUrl(path: string, extraParams: Record<string, string> = {}): string {
    const params = new URLSearchParams({ token: this.token, ...extraParams })
    return `${this.baseUrl}${path}?${params.toString()}`
  }
}
