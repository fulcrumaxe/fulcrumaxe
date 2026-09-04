/**
 * Protocol event types from backend/server.py.
 * Each event arrives as a single JSON line on stdout.
 */

export interface BaseEvent {
  id?: string;
}

export interface ReadyEvent extends BaseEvent {
  type: 'ready';
  version: string;
  model: string;
}

export interface ThinkingEvent extends BaseEvent {
  type: 'thinking';
  content: string;
}

export interface ContentEvent extends BaseEvent {
  type: 'content';
  content: string;
}

export interface ToolUseEvent extends BaseEvent {
  type: 'tool_use';
  tool: string;
  call_id: string;
  input: Record<string, unknown>;
}

export interface ToolResultEvent extends BaseEvent {
  type: 'tool_result';
  call_id: string;
  result: string;
  is_error: boolean;
}

export interface UsageEvent extends BaseEvent {
  type: 'usage';
  usage: { input_tokens: number; output_tokens: number };
}

export interface DoneEvent extends BaseEvent {
  type: 'done';
  session_id: string;
}

export interface ErrorEvent extends BaseEvent {
  type: 'error';
  error: string;
}

export interface AgentSpawnEvent extends BaseEvent {
  type: 'agent_spawn';
  agent_id: string;
  agent_name: string;
  parent_id: string | null;
}

export interface AgentEventEnvelope extends BaseEvent {
  type: 'agent_event';
  agent_id: string;
  inner: BackendEvent;
}

export interface AgentExitEvent extends BaseEvent {
  type: 'agent_exit';
  agent_id: string;
  exit_code: number | null;
}

/** A lightweight event sourced from the file-based agent feed (.autonomous-team/agent-feed.jsonl). */
export interface AgentFeedFileEvent extends BaseEvent {
  type: 'agent_feed';
  /** Agent ID string from the feed file (e.g. "agent-3"). */
  agent: string;
  /** Role from the feed file (executor, code-reviewer, impl-coordinator, …). */
  role: string;
  /** Event kind from the feed file (spawn, tool_call, tool_result, message, done, error). */
  event: string;
  /** Human-readable detail string (max 200 chars). */
  detail: string;
  /** Discussion number, if available. */
  discussion?: number;
}

export type BackendEvent =
  | ReadyEvent
  | ThinkingEvent
  | ContentEvent
  | ToolUseEvent
  | ToolResultEvent
  | UsageEvent
  | DoneEvent
  | ErrorEvent
  | AgentSpawnEvent
  | AgentEventEnvelope
  | AgentExitEvent
  | AgentFeedFileEvent;
