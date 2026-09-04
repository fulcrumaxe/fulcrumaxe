import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { WsClient } from '../ws'

// Mock WebSocket
class MockWebSocket {
  static OPEN = 1
  static CLOSED = 3
  readyState = MockWebSocket.OPEN
  url: string
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  sentMessages: string[] = []

  constructor(url: string) {
    this.url = url
  }

  send(data: string) {
    this.sentMessages.push(data)
  }

  close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  simulateOpen() { this.onopen?.() }
  simulateMessage(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }) }
  simulateClose() { this.close() }
}

vi.stubGlobal('WebSocket', MockWebSocket)

describe('WsClient', () => {
  let client: WsClient
  let mockWs: MockWebSocket

  beforeEach(() => {
    vi.useFakeTimers()
    client = new WsClient()
    client.connect('ws://localhost/ws')
    // Get the WebSocket instance that was created
    mockWs = (client as unknown as { ws: MockWebSocket }).ws!
  })

  afterEach(() => {
    client.disconnect()
    vi.useRealTimers()
  })

  it('connects to the given URL', () => {
    expect(mockWs.url).toBe('ws://localhost/ws')
  })

  it('dispatches messages to wildcard listeners', () => {
    const listener = vi.fn()
    client.subscribe('*', listener)
    mockWs.simulateOpen()
    mockWs.simulateMessage({ event: 'agent.started', timestamp: '2026-01-01T00:00:00Z', content: 'hello' })
    expect(listener).toHaveBeenCalledWith(
      expect.objectContaining({ event: 'agent.started', content: 'hello' })
    )
  })

  it('dispatches messages to specific event listeners', () => {
    const listener = vi.fn()
    client.subscribe('agent.started', listener)
    mockWs.simulateOpen()
    mockWs.simulateMessage({ event: 'agent.started', timestamp: '2026-01-01T00:00:00Z' })
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('does not dispatch to wrong event listener', () => {
    const listener = vi.fn()
    client.subscribe('agent.done', listener)
    mockWs.simulateOpen()
    mockWs.simulateMessage({ event: 'agent.started', timestamp: '2026-01-01T00:00:00Z' })
    expect(listener).not.toHaveBeenCalled()
  })

  it('unsubscribe stops receiving events', () => {
    const listener = vi.fn()
    const unsub = client.subscribe('*', listener)
    unsub()
    mockWs.simulateOpen()
    mockWs.simulateMessage({ event: 'agent.started', timestamp: '2026-01-01T00:00:00Z' })
    expect(listener).not.toHaveBeenCalled()
  })

  it('resets retryDelay to 1000ms on successful open', () => {
    // Force retry delay to be high
    const clientInternal = client as unknown as { retryDelay: number }
    clientInternal.retryDelay = 16000
    mockWs.simulateOpen()
    expect(clientInternal.retryDelay).toBe(1000)
  })

  it('schedules reconnect with exponential backoff on close', () => {
    const connectSpy = vi.spyOn(client as unknown as { _connect: () => void }, '_connect')
    const clientInternal = client as unknown as { retryDelay: number }
    clientInternal.retryDelay = 1000
    mockWs.simulateOpen()
    mockWs.simulateClose()
    // After close, retryDelay doubles (1000 → 2000)
    expect(clientInternal.retryDelay).toBe(2000)
    vi.advanceTimersByTime(1000)
    expect(connectSpy).toHaveBeenCalled()
  })

  it('caps retryDelay at maxDelay (30000)', () => {
    const clientInternal = client as unknown as { retryDelay: number; maxDelay: number }
    // Set retryDelay AFTER open (open resets it to 1000)
    mockWs.simulateOpen()
    clientInternal.retryDelay = 20000
    mockWs.simulateClose()
    expect(clientInternal.retryDelay).toBe(30000)
  })

  it('does not reconnect after disconnect()', () => {
    const connectSpy = vi.spyOn(client as unknown as { _connect: () => void }, '_connect')
    mockWs.simulateOpen()
    client.disconnect()
    mockWs.simulateClose()
    vi.advanceTimersByTime(5000)
    expect(connectSpy).not.toHaveBeenCalled()
  })

  it('isConnected reflects WebSocket state', () => {
    mockWs.readyState = MockWebSocket.OPEN
    expect(client.isConnected).toBe(true)
    mockWs.readyState = MockWebSocket.CLOSED
    expect(client.isConnected).toBe(false)
  })
})
