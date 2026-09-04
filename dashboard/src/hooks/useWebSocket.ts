import { useState, useEffect, useRef } from 'react'
import type { WsEvent } from '../api/types'
import client from '../api/client'

// We poll /api/events instead of using a WebSocket. The Rust saas-service that
// was supposed to serve /ws never shipped — see wiki/Reality-Audit.md. The
// Python backend exposes the same audit feed via a polling endpoint. The hook
// keeps its old name (useWebSocket) so consumers don't need to change.

const POLL_INTERVAL_MS = 2000
const MAX_EVENTS = 500

interface UseWebSocketReturn {
  events: WsEvent[]
  connected: boolean
}

interface EventsResponse {
  events: WsEvent[]
  next_since: number
}

export function useWebSocket(): UseWebSocketReturn {
  const [events, setEvents] = useState<WsEvent[]>([])
  const [connected, setConnected] = useState(false)
  const sinceRef = useRef(0)

  useEffect(() => {
    let stopped = false
    let timer: number | null = null

    const poll = async () => {
      try {
        const data = await client.get<EventsResponse>(`/api/events?since=${sinceRef.current}&limit=200`)
        setConnected(true)
        if (typeof data.next_since === 'number') {
          sinceRef.current = data.next_since
        }
        if (data.events && data.events.length > 0) {
          setEvents(prev => {
            const next = [...prev, ...data.events]
            return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next
          })
        }
      } catch {
        setConnected(false)
      } finally {
        if (!stopped) {
          timer = window.setTimeout(poll, POLL_INTERVAL_MS)
        }
      }
    }

    poll()

    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
    }
  }, [])

  return { events, connected }
}
