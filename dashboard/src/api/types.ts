// API response types

export interface Project {
  id: string
  name: string
  repo: string
  health: 'healthy' | 'degraded' | 'critical'
  /**
   * Number of agents currently running for this project (from fleet concurrency table).
   * Absent — never 0 — when this project's fleet.db row could not be resolved or read
   * (see `liveness: 'unknown'`); a project that was not successfully queried must never
   * render an earned-looking zero (D#2314).
   */
  activeAgents?: number
  /**
   * ISO timestamp of the most-recently-started row in fleet.db for this
   * project's active agents. Present only when activeAgents is set and > 0.
   * Labelled "started", never "activity" — fleet.db's schema
   * (project_name, agent_id, role, started_at, pid) has no heartbeat
   * column, so a long-running agent's start time is all that's honest (D#2314).
   */
  newestStartedAt?: string
  /**
   * Role name per currently-active fleet.db row, freshest first. Present
   * only when activeAgents is set and > 0. Used for the card's "what
   * they're doing" line (role names only — no PR/Discussion reference,
   * since fleet.db has no column for one yet) (D#2314).
   */
  roles?: string[]
  /** Number of agent roles defined in the role catalog (.claude/agents/*.md). */
  availableRoles?: number
  /** Human-readable reason when health is not 'healthy', e.g. "no loop activity", "loop stale". */
  healthReason?: string
  momentum: 'accelerating' | 'steady' | 'slowing' | 'stalled'
  createdAt: string
  liveness?: 'active' | 'idle' | 'unknown'
  /** True for the dashboard's own host project — server-derived, not name-guessed. */
  primary?: boolean
}

export interface HealthStatus {
  status: 'ok' | 'degraded' | 'error'
  modules: Record<string, boolean>
  loop?: LoopHealth
}

export interface LoopHealth {
  lastRun: string
  status: 'ok' | 'error' | 'idle'
  duration: number
}

export interface BudgetStatus {
  dailySpend: number
  dailyLimit: number
  monthlySpend: number
  monthlyLimit: number
  currency: string
  alertThreshold: number
}

export interface KpiSummary {
  velocity: number
  momentum: number
  cycleTimeMean: number
  /** null when sample count is below the minimum threshold (need 5+ measured Discussions) */
  estimationAccuracy: number | null
  /** number of Discussions with both estimated_hours and actual_hours */
  estimationAccuracySampleCount?: number
  /** minimum sample count required before accuracy is reported (default 5) */
  estimationAccuracyMinSamples?: number
  period: string
}

export interface VelocityPoint {
  date: string
  points: number
  prs: number
}

export interface CycleTimeBreakdown {
  phase: string
  hours: number
}

export interface Agent {
  id: string
  role: string
  status: 'running' | 'idle' | 'error' | 'done'
  startedAt: string
  duration: number
  discussion?: number
  pr?: number
}

export interface ControlSettings {
  autoMerge: boolean
  requireSecurityReview: boolean
  maxConcurrentAgents: number
  loopIntervalMinutes: number
  budgetAlertEnabled: boolean
  qualityGateThreshold: number
}

export interface ControlGate {
  name: string
  enabled: boolean
  requiredLabels: string[]
}

export interface AuditEntry {
  id: string
  timestamp: string
  actor: string
  action: string
  target: string
  details: Record<string, unknown>
}

export interface SpawnQueueItem {
  id: string
  role: string
  discussion: number
  priority: number
  status: 'pending' | 'active' | 'done' | 'failed'
  createdAt: string
}

export interface SpawnQueueStatus {
  pending: SpawnQueueItem[]
  active: SpawnQueueItem[]
  totalToday: number
}

export interface Session {
  id: string
  userId: string
  username: string
  avatarUrl: string
  createdAt: string
  expiresAt: string
}

export interface InnovateState {
  enabled: boolean
  last_iteration_at: string | null
  iteration_count: number
}

export interface Idea {
  id: string
  title: string
  summary: string
  votes: number
  status: 'pending' | 'promoted' | 'dismissed'
  created_at: string
}

export interface IdeasResponse {
  ideas: Idea[]
  /** ISO-8601 timestamp of when the backend read the ideas from disk. */
  fetched_at: string
  /** True when the blackboard ideas directory is empty — no real ideas yet. */
  source_empty: boolean
}

// WebSocket event types
// Discussion Explorer types

export type DiscussionStatus =
  | 'DISCUSSING'
  | 'SPEC_READY'
  | 'IMPLEMENTING'
  | 'REVIEWING'
  | 'DONE'
  | 'CLOSED'
  | 'UNKNOWN'

export interface DiscussionSummary {
  number: number
  title: string
  status: DiscussionStatus
  linkedPr: number | null
  url: string | null
  createdAt: string | null
  updatedAt: string | null
  author: string | null
  costUsd?: number | null
}

export interface PerDiscussionCost {
  discussion: number
  cost_usd: number
  total_cost_usd: number
  total_input_tokens: number
  total_output_tokens: number
  agent_count: number
  agents: string[]
  agent_breakdown: Record<string, number>
  pr_breakdown: Record<string, number>
}

export interface DiscussionComment {
  body: string
  createdAt: string | null
  author: string | null
}

export interface LinkedPR {
  number: number
  url: string
  state: string
  labels: string[]
}

export interface DiscussionAgentRun {
  ts: string
  role: string
  verdict: string
  pr: number | null
}

export interface DiscussionDetail {
  number: number
  title: string
  body: string
  status: DiscussionStatus
  url: string | null
  createdAt: string | null
  updatedAt: string | null
  author: string | null
}

export interface DiscussionGetResult {
  discussion: DiscussionDetail
  comments: DiscussionComment[]
  linked_pr: LinkedPR | null
  agent_runs: DiscussionAgentRun[]
}

export interface DiscussionListResult {
  items: DiscussionSummary[]
  next_cursor?: string
}

export interface CircuitBreakerEntry {
  discussion: number
  count: number
  agent: string | null
  reason: string | null
  updated_at: string | null
}

export interface CircuitBreakerSummary {
  tripped: CircuitBreakerEntry[]
  warnings: CircuitBreakerEntry[]
  threshold: number
}

export interface CircuitBreakerTransition {
  role: string
  from_state: 'healthy' | 'tripped'
  to_state: 'healthy' | 'tripped'
  timestamp: string
  reason: string
  context: {
    recent_errors?: string[]
    trip_count_24h?: number
  }
  last_pr: number | null
}

export interface ClaudeSpawnThresholds {
  spawns_per_hour_max: number
  spend_per_hour_usd_max: number
  spawns_24h_max: number
}

export interface ClaudeSpawnTrippedMeta {
  tripped_at: string
  reason: string
  threshold_name: string
  value: number
  last_attempt_at: string
}

export interface ClaudeSpawnSummary {
  tripped: boolean
  spawns_1h: number
  spawns_24h: number
  spend_1h_usd: number
  spend_24h_usd: number
  per_source: Record<string, number>
  thresholds: ClaudeSpawnThresholds
  tripped_meta: ClaudeSpawnTrippedMeta | null
}

export type WsEventType =
  | 'agent.started'
  | 'agent.output'
  | 'agent.done'
  | 'agent.error'
  | 'loop.started'
  | 'loop.done'
  | 'pr.created'
  | 'pr.merged'
  | 'discussion.updated'

export interface WsEvent {
  event: WsEventType
  timestamp: string
  projectId?: string
  agentId?: string
  role?: string
  content?: string
  data?: Record<string, unknown>
}

// ---------------------------------------------------------------------------
// PR Detail page types
// ---------------------------------------------------------------------------

export interface PrDetailPr {
  number: number
  title: string
  author: string | null
  state: string
  merged_at: string | null
  additions: number
  deletions: number
  files_changed: number
  html_url: string
}

export interface PrDetailQualityDimension {
  score: number
  max: number
  notes: string
}

export interface PrDetailQuality {
  total: number
  grade: string
  complexity: PrDetailQualityDimension
  test_coverage: PrDetailQualityDimension
  review_rounds: PrDetailQualityDimension
  size: PrDetailQualityDimension
}

export interface PrDetailCostByRole {
  role: string
  input_tokens: number
  output_tokens: number
  usd: number
}

export interface PrDetailCost {
  input_tokens: number
  output_tokens: number
  total_tokens: number
  usd: number
  by_role: PrDetailCostByRole[]
}

export interface PrDetailDiscussion {
  number: number
  title: string
  url: string
}

export interface PrDetail {
  pr: PrDetailPr
  discussion: PrDetailDiscussion | null
  quality: PrDetailQuality | null
  cost: PrDetailCost | null
  review_rounds: number
}

// ---------------------------------------------------------------------------
// PR Inspector (list view) types — Discussion #393
// ---------------------------------------------------------------------------

export interface PrListEntry {
  number: number
  title: string
  author: string | null
  age_seconds: number
  labels: string[]
  fix_cycles: number
  quality_score: number | null
  discussion_number: number | null
  html_url: string
}

// ---------------------------------------------------------------------------
// Spawn block event types — Discussion #417
// ---------------------------------------------------------------------------

export type SpawnBlockReason =
  | 'budget_exceeded'
  | 'circuit_breaker_open'
  | 'subscription_throttled'
  | 'worktree_cap_reached'
  | 'concurrency_cap_reached'

export interface SpawnBlockEvent {
  ts: string
  role: string
  event_type: 'spawn_blocked'
  reason: SpawnBlockReason
  message: string
  discussion?: number
  details?: Record<string, unknown>
}
