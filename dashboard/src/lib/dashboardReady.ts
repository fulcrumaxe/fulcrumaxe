/**
 * dashboardReady.ts — the readiness predicate a browser-tester must satisfy
 * before capturing anything (D#2549).
 *
 * A screenshot of a page mid-load is not evidence a feature works. Two
 * browser-tester passes were measured returning `pass` on screenshots
 * showing "Loading metrics…" and "0 of N populated" — the tester never
 * checked whether the data had actually arrived, it just took a screenshot
 * on a timer. This function is the specific condition to check instead: not
 * a fixed sleep, a direct read of the DOM.
 *
 * `.claude/agents/browser-tester.md` runs the same logic in-page via
 * evaluate_script, since a live Chrome tab cannot import this module — keep
 * the two copies in lockstep by hand. This file is the canonical, tested
 * source; the role doc's copy is a plain-JS transcription of it.
 */
export function isDashboardReady(doc: Document): boolean {
  const body = doc.body
  if (!body) return false

  // Still-loading state: a "Loading …" message is visible somewhere on the
  // page. This is the exact string StatsPage.tsx renders while its first
  // fetch is in flight ("Loading metrics…") — matched case-insensitively
  // and without the ellipsis so the check does not silently break if the
  // copy changes to "Loading..." or similar.
  const text = body.textContent || ''
  if (/loading/i.test(text)) return false

  // A populated grid/list container has at least one rendered child.
  // Container absent (still mounting) or present-but-empty (fetch resolved
  // with zero rows, or not resolved yet) both count as not ready.
  const containers = doc.querySelectorAll(
    '[data-testid$="-grid"], [data-testid$="-list"]',
  )
  if (containers.length === 0) return false
  return Array.from(containers).some(el => el.children.length > 0)
}

/**
 * The literal source browser-tester evaluates in-page via evaluate_script
 * (mcp__*__evaluate_script). Kept alongside isDashboardReady so the two
 * never drift silently out of sync — a reviewer can diff them side by side.
 * Must stay a plain, self-contained expression: no imports, no TypeScript
 * syntax, since it runs unbundled in a live Chrome tab.
 */
export const DASHBOARD_READY_SCRIPT = `(() => {
  const body = document.body;
  if (!body) return false;
  const text = body.textContent || '';
  if (/loading/i.test(text)) return false;
  const containers = document.querySelectorAll('[data-testid$="-grid"], [data-testid$="-list"]');
  if (containers.length === 0) return false;
  return Array.from(containers).some(el => el.children.length > 0);
})()`
