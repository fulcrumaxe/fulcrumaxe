/**
 * kpi.ts — typed wrappers for the three KPI JSON-RPC methods.
 *
 * All calls go through client.ts's jsonRpc(), which awaits ensureConfig()
 * when localStorage has no token — so fresh clients never hit 401.
 */

import { jsonRpc } from './client'

// ---------------------------------------------------------------------------
// Exported types
// ---------------------------------------------------------------------------

export interface VelocityPoint {
  date: string   // "YYYY-MM-DD"
  count: number
}

export interface CycleTimeBucket {
  bucket: '0-2h' | '2-6h' | '6-24h' | '24h+'
  count: number
}

export interface CostEntry {
  discussion: number
  tokens: number
  usd: number
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

/** Merged-PRs-per-day for the last `days` days (default 30). */
export function getVelocity(days = 30): Promise<VelocityPoint[]> {
  return jsonRpc<VelocityPoint[]>('kpi.history', { days })
}

/** Cycle-time histogram for the given window (default 90 days). */
export function getCycleTime(days = 90): Promise<CycleTimeBucket[]> {
  return jsonRpc<CycleTimeBucket[]>('kpi.cycle_time', { days })
}

/** Top-N discussions by token spend for the given window (default 10, last 90 days). */
export function getCostByDiscussion(top = 10, days = 90): Promise<CostEntry[]> {
  return jsonRpc<CostEntry[]>('cost.by_discussion', { top, days })
}
