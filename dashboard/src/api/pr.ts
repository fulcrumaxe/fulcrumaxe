/**
 * pr.ts — typed wrappers for PR-related JSON-RPC methods.
 *
 * All calls go through client.ts's jsonRpc(), which awaits ensureConfig()
 * when localStorage has no token — so fresh clients never hit 401.
 */

import type { PrDetail, PrListEntry } from './types'
import { jsonRpc } from './client'

export type PrDetailResult = PrDetail & { error?: string }

/**
 * Fetch all open PRs with gate-label state, fix-cycle count, age, and quality score.
 */
export async function fetchPrList(): Promise<PrListEntry[]> {
  return jsonRpc<PrListEntry[]>('dashboard.pr_list', {})
}

/**
 * Fetch detail for a single PR: quality score, cost breakdown, linked discussion.
 */
export async function fetchPrDetail(prNumber: number): Promise<PrDetailResult> {
  return jsonRpc<PrDetailResult>('dashboard.pr_detail', { pr_number: prNumber })
}
