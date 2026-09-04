// agentFeedTail.ts — SSE-backed agent feed tailer with polling fallback.

import { AgentEvent, AgentsTailParams } from '../types/loopController'
import { JsonRpcClient } from './jsonrpcClient'
import { registerEventSource, unregisterEventSource } from '../context/sseRegistry'
import { getActiveProject } from '../api/client'

export interface TailOptions {
  filter?: AgentsTailParams['filter']
  onEvent: (e: AgentEvent) => void
  onError: (e: Error) => void
}

export interface TailHandle {
  close(): void
}

const POLL_INTERVAL_MS = 2000

export function tailAgentFeed(getClient: () => JsonRpcClient, opts: TailOptions): TailHandle {
  let closed = false
  let es: EventSource | null = null
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let lastSince: string | undefined = undefined

  function startSSE() {
    if (closed) return
    const client = getClient()
    const params: Record<string, string> = {}
    if (opts.filter?.role) params['filter[role]'] = opts.filter.role
    // Inject active project so the SSE endpoint tails the right feed file
    // (Gap 4: sseUrl() doesn't call getActiveProject() — inject here instead).
    const activeProject = getActiveProject()
    if (activeProject) params['project'] = activeProject
    const url = client.sseUrl('/feed', params)
    es = new EventSource(url)
    // Register so closeAllEventSources() can tear this down on project switch.
    registerEventSource(es)

    es.onmessage = (ev: MessageEvent<string>) => {
      try {
        const event = JSON.parse(ev.data) as AgentEvent
        if (event.type === 'connected') return
        lastSince = (event.timestamp as string | undefined) ?? lastSince
        opts.onEvent(event)
      } catch {
        // ignore parse errors
      }
    }

    es.onerror = () => {
      if (es) {
        unregisterEventSource(es)
        es.close()
        es = null
      }
      if (!closed) {
        // SSE disconnected — fall back to polling
        startPolling()
      }
    }
  }

  function startPolling() {
    if (closed) return
    pollTimer = setTimeout(poll, POLL_INTERVAL_MS)
  }

  async function poll() {
    if (closed) return
    try {
      const result = await getClient().call('agents.tail', {
        since: lastSince,
        filter: opts.filter,
        limit: 50,
      })
      for (const ev of result.events) {
        lastSince = (ev.timestamp as string | undefined) ?? lastSince
        opts.onEvent(ev)
      }

      // Try to reconnect SSE
      startSSE()
    } catch (err) {
      opts.onError(err instanceof Error ? err : new Error(String(err)))
      if (!closed) {
        pollTimer = setTimeout(poll, POLL_INTERVAL_MS)
      }
    }
  }

  startSSE()

  return {
    close() {
      closed = true
      if (es) {
        unregisterEventSource(es)
        es.close()
        es = null
      }
      if (pollTimer !== null) {
        clearTimeout(pollTimer)
        pollTimer = null
      }
    },
  }
}
