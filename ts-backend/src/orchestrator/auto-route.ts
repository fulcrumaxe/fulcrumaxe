/**
 * orchestrator/auto-route.ts — SDK_AUTO_ROUTE gate for eligible roles.
 *
 * Mirrors backend/orchestrator/auto_route.py 1:1.
 *
 * When the environment variable SDK_AUTO_ROUTE=1 is set, spawns of offload-eligible
 * low-stakes roles are automatically treated as sdkEligible=true — so they use the
 * proven SDK lane without each spawn needing the manual --sdk-lane flag.
 *
 * DEFAULT: OFF. Setting SDK_AUTO_ROUTE=1 is the only way to enable this behaviour.
 *
 * Safety invariants:
 *   - When SDK_AUTO_ROUTE is unset or not "1", this module has zero effect on routing.
 *   - Non-eligible roles (executor, code-reviewer, security-reviewer, acceptance-tester,
 *     project-manager, team-lead, etc.) NEVER auto-route — shouldAutoRoute() reuses
 *     SDK_ELIGIBLE_ROLES from offload-policy, so the role gate is enforced here before
 *     the caller even reads the return value.
 *   - The decision is pure (no side-effects); callers are responsible for audit logging.
 *
 * Programmatic exports:
 *   import { shouldAutoRoute } from "./auto-route.js";
 */

import { SDK_ELIGIBLE_ROLES } from "./offload-policy.js";

/**
 * Return true when SDK_AUTO_ROUTE=1 AND role is in SDK_ELIGIBLE_ROLES.
 *
 * @param role - The agent role string (e.g. "docs-writer", "executor").
 * @returns
 *   `true`  → auto-route this spawn to SDK (caller should treat sdkEligible=true).
 *   `false` → no auto-routing; preserve the original sdkEligible flag as-is.
 *
 * @example
 * // SDK_AUTO_ROUTE unset:
 * shouldAutoRoute("docs-writer")  // false
 *
 * // SDK_AUTO_ROUTE=1:
 * shouldAutoRoute("docs-writer")  // true
 * shouldAutoRoute("executor")     // false
 * shouldAutoRoute("code-reviewer") // false
 */
export function shouldAutoRoute(role: string): boolean {
  return process.env["SDK_AUTO_ROUTE"] === "1" && SDK_ELIGIBLE_ROLES.has(role);
}
