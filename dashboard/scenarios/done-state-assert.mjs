#!/usr/bin/env node
/**
 * done-state-assert.mjs — pure route -> predicate helper for
 * dashboard/scenarios/routes.manifest.json's `expected_done_state` field.
 *
 * D#1536 Phase 0 declares an `expected_done_state` ('data' | 'empty' | 'error')
 * for every route in the manifest. This module is the mechanical bridge between
 * that declaration and the success-criteria shape a browser sweep (Phase 1,
 * follow-on Discussion) will assert against a real page. It has NO Chrome
 * DevTools MCP / network / DOM dependency — it is pure data-in, data-out, so it
 * can be unit tested and imported by both the sweep script and vitest, the same
 * way generate-route-manifest.mjs's exported functions are pure and testable.
 *
 * Testid convention assumed here (see dashboard/src/pages/**\/*.tsx):
 *   - empty state:  data-testid ending in "-empty" (e.g. "stats-empty")
 *   - error state:  data-testid ending in "-error" (e.g. "stats-error")
 *   - data state:   neither of the above is present — real content rendered
 *   - next-step affordance on an error state: a testid containing "retry",
 *     "next-step", or a plain <a>/<button> is out of scope for this pure
 *     helper (DOM inspection is Phase 1's job) — callers pass a boolean.
 */

export const DONE_STATES = ['data', 'empty', 'error']

const EMPTY_TESTID_RE = /-empty(-|$)/i
const ERROR_TESTID_RE = /-error(-|$)/i
const NEXT_STEP_TESTID_RE = /-(retry|next-step|next)(-|$)/i

/**
 * Returns the declarative success-criteria shape for a given
 * expected_done_state value. This is what the Phase 1 sweep script should
 * assert on a rendered page.
 */
export function successCriteriaFor(expectedDoneState) {
  switch (expectedDoneState) {
    case 'data':
      return {
        state: 'data',
        summary: 'A data-bearing testid is present; no empty-state or error-state testid is present.',
        requires: ['data_testid_present'],
        forbids: ['empty_testid_present', 'error_testid_present'],
      }
    case 'empty':
      return {
        state: 'empty',
        summary: 'A human-readable empty-state testid (matching /-empty$/) is present; no error-state testid is present.',
        requires: ['empty_testid_present'],
        forbids: ['error_testid_present'],
      }
    case 'error':
      return {
        state: 'error',
        summary: 'An error-state testid (matching /-error$/) is present AND a next-step affordance (retry control, link, or instructional text) is present.',
        requires: ['error_testid_present', 'next_step_present'],
        forbids: [],
      }
    default:
      throw new Error(
        `Unknown expected_done_state: "${expectedDoneState}". Must be one of: ${DONE_STATES.join(', ')}.`
      )
  }
}

/**
 * Given the set of data-testid values observed on a rendered route and
 * whether a next-step affordance was found, evaluate whether the page
 * matches its declared expected_done_state.
 *
 * @param {string} expectedDoneState - one of DONE_STATES
 * @param {string[]} observedTestIds - every data-testid value found on the page
 * @param {boolean} [hasNextStep] - true if a retry/next-step affordance is present
 *   (only consulted for the 'error' state; Phase 1 supplies this from DOM inspection)
 * @returns {{ pass: boolean, reasons: string[] }}
 */
export function evaluateDoneState(expectedDoneState, observedTestIds, hasNextStep = false) {
  const criteria = successCriteriaFor(expectedDoneState)
  const hasEmpty = observedTestIds.some(id => EMPTY_TESTID_RE.test(id))
  const hasError = observedTestIds.some(id => ERROR_TESTID_RE.test(id))
  const hasNextStepTestId = observedTestIds.some(id => NEXT_STEP_TESTID_RE.test(id)) || hasNextStep

  const reasons = []

  if (criteria.state === 'data') {
    if (hasEmpty) reasons.push('found an empty-state testid but expected data')
    if (hasError) reasons.push('found an error-state testid but expected data')
  } else if (criteria.state === 'empty') {
    if (!hasEmpty) reasons.push('no empty-state testid found')
    if (hasError) reasons.push('found an error-state testid but expected empty')
  } else if (criteria.state === 'error') {
    if (!hasError) reasons.push('no error-state testid found')
    if (hasError && !hasNextStepTestId) reasons.push('error-state testid found but no next-step affordance present')
  }

  return { pass: reasons.length === 0, reasons }
}
