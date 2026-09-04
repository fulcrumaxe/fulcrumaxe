/**
 * orchestrator/offload-policy.ts — SDK selective-offload routing policy.
 *
 * Mirrors backend/orchestrator/offload_policy.py 1:1.
 *
 * The SDK is an OFFLOAD LANE, not a replacement for Claude Code.
 *
 * A spawn is eligible for SDK routing ONLY when ALL of the following are true:
 *
 *   1. The spawn is EXPLICITLY flagged `sdkEligible=true` for that spawn.
 *      There is NO automatic spill, NO alternation, and NO capacity-based routing.
 *      An executor or reviewer that happens to be spawned at a busy moment does NOT
 *      silently migrate to the SDK.
 *
 *   2. The spawn's role is one of the low-stakes background roles defined in
 *      `SDK_ELIGIBLE_ROLES` below. Roles that touch code quality, security,
 *      user-visible output, or the control plane (executor, code-reviewer,
 *      security-reviewer, acceptance-tester, project-manager, team-lead) are
 *      explicitly excluded.
 *
 * Programmatic exports:
 *   import { isOffloadEligible, SDK_ELIGIBLE_ROLES } from "./offload-policy.js";
 */

// ---------------------------------------------------------------------------
// Eligible roles
// ---------------------------------------------------------------------------

/**
 * Roles that are permitted to run on the SDK offload lane.
 *
 * Criteria for inclusion:
 *   - Read-heavy, low-mutation (scan, summarise, report) work
 *   - No direct code changes committed to the repo
 *   - No verdict that gates a PR merge (code-review-passed, security-review-passed,
 *     acceptance-passed labels are NOT issued by these roles)
 *   - Safe to retry or discard if the SDK run produces a wrong answer
 *
 * Criteria for EXCLUSION (must stay on CC / main path):
 *   - executor — writes code, creates PRs
 *   - code-reviewer — issues code-review-passed label
 *   - security-reviewer — issues security-review-passed label
 *   - acceptance-tester — issues acceptance-passed label
 *   - project-manager — writes Specs, controls Discussion status
 *   - team-lead / orchestration roles — control-plane work
 */
export const SDK_ELIGIBLE_ROLES: ReadonlySet<string> = new Set([
  "docs-writer",
  "run-analyst",
  "quality-sweep",
  "feedback-scanner",
  "mission-analyst",
]);

// ---------------------------------------------------------------------------
// Policy function
// ---------------------------------------------------------------------------

/**
 * Return true when this spawn should be routed to the SDK offload lane.
 *
 * BOTH conditions must hold:
 *
 *   1. `sdkEligible` is true — the caller explicitly opted the spawn into
 *      the SDK lane. Default is false; no spawn migrates to SDK without an
 *      explicit flag.
 *
 *   2. `role` is in `SDK_ELIGIBLE_ROLES` — even if a caller mistakenly
 *      passes `sdkEligible=true` for an executor or reviewer, the policy
 *      function hard-blocks the upgrade. Executors and reviewers stay on the
 *      main path unconditionally.
 *
 * @param role - The agent role string (e.g. "docs-writer", "executor").
 * @param sdkEligible - Explicit opt-in flag from the spawn spec.
 * @returns true → route to SDK (when credit is also available). false → route to CC.
 *
 * @example
 * isOffloadEligible("docs-writer", true)   // true
 * isOffloadEligible("docs-writer", false)  // false
 * isOffloadEligible("executor", true)      // false
 * isOffloadEligible("code-reviewer", true) // false
 * isOffloadEligible("unknown-role", true)  // false
 */
export function isOffloadEligible(role: string, sdkEligible: boolean): boolean {
  return sdkEligible && SDK_ELIGIBLE_ROLES.has(role);
}
