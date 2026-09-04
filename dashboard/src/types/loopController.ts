// loopController.ts — TypeScript types mirroring the JSON-RPC contract for the Loop Controller.

export interface LoopStartParams {
  prompt: string
  cadence_seconds: number | null
}

export interface LoopStartResult {
  loop_id: string
  started_at: string
}

export interface LoopStopParams {
  loop_id: string
}

export interface LoopStopResult {
  loop_id: string
  stopped_at: string
}

export interface LoopEntry {
  loop_id: string
  prompt: string
  cadence_seconds: number | null
  started_at: string
  last_event_at: string
  pid: number
  status: 'running' | 'stopping' | 'stopped'
}

export interface LoopListResult {
  loops: LoopEntry[]
}

export interface LoopEventsParams {
  loop_id: string
  since_event_id?: string
  limit?: number
}

export interface AgentEvent {
  id?: string
  event_id?: string
  timestamp?: string
  ts?: string
  role?: string
  discussion?: number
  pr?: number
  verdict?: string
  event_type?: string
  loop_id?: string
  message?: string
  [key: string]: unknown
}

export interface LoopEventsResult {
  events: AgentEvent[]
  next_since_id: string | null
}

export interface AgentsTailParams {
  since?: string
  filter?: {
    role?: string
    discussion?: number
    event_type?: string
  }
  limit?: number
}

export interface AgentsTailResult {
  events: AgentEvent[]
  next_since: string | null
}

export interface TeamStatusSnapshot {
  snapshot_age_seconds: number | null
  discussions: Record<string, unknown>
  prs: Record<string, unknown>
  agents: Record<string, unknown>
  queue: { depth: number; pending: unknown[] }
  budget: Record<string, unknown>
  kpi: Record<string, unknown>
  recent_merges: unknown[]
  errors: string[]
  error?: string
}

// JSON-RPC discriminated union for errors
export interface JsonRpcErrorPayload {
  code: number
  message: string
  data?: unknown
}

export class JsonRpcError extends Error {
  code: number
  data?: unknown

  constructor(payload: JsonRpcErrorPayload) {
    super(payload.message)
    this.name = 'JsonRpcError'
    this.code = payload.code
    this.data = payload.data
  }
}

export class AuthError extends Error {
  constructor(message = 'Unauthorized — check your dashboard token') {
    super(message)
    this.name = 'AuthError'
  }
}

// Method name → params/result mapping (for typed client)
export interface RpcMethodMap {
  'loop.start': { params: LoopStartParams; result: LoopStartResult }
  'loop.stop': { params: LoopStopParams; result: LoopStopResult }
  'loop.list': { params: Record<string, never>; result: LoopListResult }
  'loop.events': { params: LoopEventsParams; result: LoopEventsResult }
  'agents.tail': { params: AgentsTailParams; result: AgentsTailResult }
  'team_status.snapshot': { params: Record<string, never>; result: TeamStatusSnapshot }
}

export type MethodName = keyof RpcMethodMap
export type ParamsOf<M extends MethodName> = RpcMethodMap[M]['params']
export type ResultOf<M extends MethodName> = RpcMethodMap[M]['result']
