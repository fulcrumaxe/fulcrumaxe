/**
 * loop.ts — typed wrappers for the loop timeline JSON-RPC methods.
 *
 * All calls go through client.ts's jsonRpc(), which awaits ensureConfig()
 * when localStorage has no token — so fresh clients never hit 401.
 */

import { jsonRpc } from './client'

// ---------------------------------------------------------------------------
// Exported types
// ---------------------------------------------------------------------------

/** One loop iteration's metric data point, as returned by loop.timeline. */
export interface LoopIterationPoint {
  timestamp: string         // ISO8601
  duration_seconds: number
  agents_spawned: number
  prs_merged: number
  discussions_scanned: number
  prs_scanned: number
  idle: boolean
  error: string | null
}

/** Detail metrics row — includes all LoopIterationPoint fields plus optional action list. */
export interface LoopIterationMetrics extends Partial<LoopIterationPoint> {
  actions?: string[]
}

/** Cross-links extracted from a loop-run log (D#N and PR #N references). */
export interface LoopRunReferences {
  discussions: number[]
  prs: number[]
}

/** Full detail for a single loop iteration, as returned by loop.iteration_detail. */
export interface LoopIterationDetail {
  timestamp: string
  metrics: LoopIterationMetrics
  log: string | null        // null when the .log file pre-dates loop-runs dir
  log_path: string | null
  references?: LoopRunReferences  // added in D#412; absent on older API responses
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

/** Last N loop iterations ordered oldest → newest (default 100, max 500). */
export function getLoopTimeline(limit = 100): Promise<LoopIterationPoint[]> {
  return jsonRpc<LoopIterationPoint[]>('loop.timeline', { limit })
}

/** Full detail (metrics + log text) for a single iteration timestamp. */
export function getIterationDetail(timestamp: string): Promise<LoopIterationDetail> {
  return jsonRpc<LoopIterationDetail>('loop.iteration_detail', { timestamp })
}
