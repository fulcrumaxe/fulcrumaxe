import type { WsEvent, WsEventType } from './types'

type Listener = (data: WsEvent) => void

export class WsClient {
  private ws: WebSocket | null = null
  private url: string = ''
  private retryDelay = 1000
  private readonly maxDelay = 30000
  private listeners = new Map<WsEventType | '*', Set<Listener>>()
  private stopped = false
  private pingInterval: ReturnType<typeof setInterval> | null = null

  connect(url: string): void {
    this.url = url
    this.stopped = false
    this._connect()
  }

  private _connect(): void {
    if (this.stopped) return

    try {
      this.ws = new WebSocket(this.url)
    } catch {
      this._scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      this.retryDelay = 1000
      this._startPing()
    }

    this.ws.onclose = () => {
      this._stopPing()
      this._scheduleReconnect()
    }

    this.ws.onerror = () => {
      // onclose fires after onerror — reconnect handled there
    }

    this.ws.onmessage = (e: MessageEvent) => {
      try {
        const msg = JSON.parse(e.data as string) as WsEvent
        this._dispatch(msg)
      } catch {
        // Malformed message — ignore
      }
    }
  }

  private _scheduleReconnect(): void {
    if (this.stopped) return
    const delay = this.retryDelay
    this.retryDelay = Math.min(this.retryDelay * 2, this.maxDelay)
    setTimeout(() => this._connect(), delay)
  }

  private _startPing(): void {
    this._stopPing()
    this.pingInterval = setInterval(() => {
      this.send({ command: 'ping' })
    }, 30000)
  }

  private _stopPing(): void {
    if (this.pingInterval !== null) {
      clearInterval(this.pingInterval)
      this.pingInterval = null
    }
  }

  private _dispatch(event: WsEvent): void {
    // Notify wildcard listeners
    this.listeners.get('*')?.forEach(fn => fn(event))
    // Notify specific event listeners
    this.listeners.get(event.event)?.forEach(fn => fn(event))
  }

  send(data: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data))
    }
  }

  subscribe(event: WsEventType | '*', fn: Listener): () => void {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set())
    this.listeners.get(event)!.add(fn)
    return () => this.unsubscribe(event, fn)
  }

  unsubscribe(event: WsEventType | '*', fn: Listener): void {
    this.listeners.get(event)?.delete(fn)
  }

  disconnect(): void {
    this.stopped = true
    this._stopPing()
    this.ws?.close()
    this.ws = null
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }
}

// Singleton WebSocket client
export const wsClient = new WsClient()
