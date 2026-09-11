import { describe, it, expect } from 'vitest'
import { isDashboardReady, DASHBOARD_READY_SCRIPT } from '../dashboardReady'

// D#2549: this predicate exists because two browser-tester passes were
// measured on screenshots of a still-loading page ("Loading metrics…",
// "0 of N populated"). These cases demonstrate both directions required by
// the Discussion's acceptance criteria — false on a still-loading page, true
// on a populated one — and the "stalled page" case is the mutation-evidence
// demonstration: pointed at a page that never finishes loading, the
// predicate must return false (go red), never true.

function docFromHtml(html: string): Document {
  const parser = new DOMParser()
  return parser.parseFromString(html, 'text/html')
}

describe('isDashboardReady — still-loading page (false)', () => {
  it('returns false while a "Loading metrics…" message is showing and the grid is absent', () => {
    const doc = docFromHtml(`
      <div>
        <p>Per-merge metrics — 0 of 17 populated</p>
        <div>Loading metrics…</div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(false)
  })

  it('returns false — deliberately stalled page — grid mounted but empty, still loading', () => {
    // Mirrors the real regression: the grid container exists (React has
    // mounted it) but the fetch has not resolved, so it has no children.
    const doc = docFromHtml(`
      <div>
        <div data-testid="stats-grid"></div>
        <div>Loading metrics…</div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(false)
  })

  it('returns false when no recognizable grid/list container has rendered at all', () => {
    const doc = docFromHtml(`<div><p>Team Stats</p></div>`)
    expect(isDashboardReady(doc)).toBe(false)
  })
})

describe('isDashboardReady — populated page (true)', () => {
  it('returns true once the loading message is gone and the grid has children', () => {
    const doc = docFromHtml(`
      <div>
        <p>Per-merge metrics — 12 of 12 populated</p>
        <div data-testid="stats-grid">
          <div data-testid="metric-tile">loop_iteration_duration_seconds</div>
          <div data-testid="metric-tile">time_to_merge_seconds</div>
        </div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(true)
  })

  it('also recognizes a "-list" container, not only "-grid"', () => {
    const doc = docFromHtml(`
      <div data-testid="active-agents-list">
        <div>agent-1</div>
      </div>
    `)
    expect(isDashboardReady(doc)).toBe(true)
  })
})

describe('isDashboardReady — edge cases', () => {
  it('returns false for a document with no body', () => {
    const doc = docFromHtml('')
    // jsdom always synthesizes a <body>, so force the case explicitly to
    // cover a caller that hands in a detached/partial document.
    Object.defineProperty(doc, 'body', { value: null, configurable: true })
    expect(isDashboardReady(doc)).toBe(false)
  })
})

describe('DASHBOARD_READY_SCRIPT — the in-page copy stays in lockstep', () => {
  it('is a self-contained expression with no imports, matching isDashboardReady on both fixtures', () => {
    expect(DASHBOARD_READY_SCRIPT).not.toContain('import ')
    expect(DASHBOARD_READY_SCRIPT).not.toContain('export ')

    const loading = docFromHtml(`<div data-testid="stats-grid"></div><div>Loading metrics…</div>`)
    const populated = docFromHtml(
      `<div data-testid="stats-grid"><div data-testid="metric-tile">x</div></div>`,
    )

    // Deliberately evaluating the exact literal the browser-tester passes
    // to evaluate_script, against `document` bound to each fixture, to
    // prove the two copies agree.
    const runAgainst = (doc: Document) =>
      new Function('document', `return ${DASHBOARD_READY_SCRIPT}`)(doc)

    expect(runAgainst(loading)).toBe(isDashboardReady(loading))
    expect(runAgainst(populated)).toBe(isDashboardReady(populated))
    expect(runAgainst(loading)).toBe(false)
    expect(runAgainst(populated)).toBe(true)
  })
})
